"""不需要起栈的单元：配置、id、缓存键。"""
import asyncio
import re

from mmp_node.cache import params_hash, task_key
from mmp_node.config import apply_env, load_config
from mmp_node.engine_io import EnginePool, EngineResult
from mmp_node.ids import new_job_id, node_of, ulid
from mmp_node.registry import EngineSpec


def test_ulid_and_job_id():
    u = ulid()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", u)
    assert ulid(1) < ulid(2 ** 40)  # 时间戳在高位 → 可排序
    jid = new_job_id("gpu-box-1")
    assert re.fullmatch(r"^[a-z0-9][a-z0-9-]*-[0-9A-HJKMNP-TV-Z]{26}$", jid)
    assert node_of(jid) == "gpu-box-1"
    assert node_of("garbage") is None


def test_params_hash_is_canonical():
    assert params_hash({"a": 1, "b": [1, 2]}) == params_hash({"b": [1, 2], "a": 1})
    assert params_hash({"a": 1}) != params_hash({"a": 2})
    k = task_key("sha256:" + "0" * 64, "echo", "cpu", "1", {})
    assert k.startswith("sha256:") and k.count("|") == 4


def test_env_override(tmp_path):
    toml = tmp_path / "node.toml"
    toml.write_text('[node]\nid = "x"\nkey = "0123456789abcdef"\n[broker]\nport = 1\n[engines.echo]\nmodule = "engines.echo"\n')
    cfg = load_config(toml, {"MMP_BROKER__PORT": "9000", "MMP_BROKER__ENABLED": "false",
                             "MMP_QUEUE__MAX_LEN": "7", "MMP_ENGINES__ECHO__TIMEOUT_SEC": "2.5", "HOME": "/x"})
    assert cfg.broker.port == 9000 and cfg.broker.enabled is False
    assert cfg.queue.max_len == 7
    assert cfg.engines["echo"].timeout_sec == 2.5
    assert apply_env({}, {"MMP_NOSECTION": "1"}) == {}


async def test_keep_warm_gpu_is_evicted_for_heavy_then_restored(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeProcess:
        def __init__(self, spec, start_timeout, protocol_version):
            self.spec, self.alive, self.busy = spec, False, 0
            self.last_used = 0.0

        async def start(self):
            self.alive = True
            events.append(f"start:{self.spec.name}")

        async def run(self, job, timeout_sec):
            self.busy += 1
            events.append(f"run:{self.spec.name}")
            await asyncio.sleep(0)
            self.busy -= 1
            return EngineResult({}, {})

        async def shutdown(self):
            if self.alive:
                events.append(f"stop:{self.spec.name}")
            self.alive = False

    monkeypatch.setattr("mmp_node.engine_io.EngineProcess", FakeProcess)

    def spec(name: str, warm: bool, vram: int) -> EngineSpec:
        tier = {"tier": "gpu-fast", "engine": name, "engine_version": "1", "cost": "low", "vram_mb": vram}
        return EngineSpec(name=name, module=name, cmd=[name], cwd=tmp_path, timeout_sec=5,
                          capabilities=[], resident_vram_mb=vram, keep_warm=warm,
                          tiers={(name, "gpu-fast"): tier})

    base, heavy = spec("image-base", True, 1024), spec("paddle-vl", False, 12288)
    pool = EnginePool(5, 300, "1.2", [base, heavy], vram_total_mb=13000)
    pool.start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert pool.loaded() == ["image-base"]

    await pool.run(heavy, {"job_id": "x"}, 5, 12288)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert events[:5] == ["start:image-base", "stop:image-base", "start:paddle-vl",
                          "run:paddle-vl", "stop:paddle-vl"]
    assert pool.loaded() == ["image-base"]
    assert events[-1] == "start:image-base"
    await pool.close()


async def test_keep_warm_gpu_stays_loaded_when_capacity_is_enough(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeProcess:
        def __init__(self, spec, start_timeout, protocol_version):
            self.spec, self.alive, self.busy, self.last_used = spec, False, 0, 0.0
        async def start(self):
            self.alive = True; events.append(f"start:{self.spec.name}")
        async def run(self, job, timeout_sec):
            events.append(f"run:{self.spec.name}"); return EngineResult({}, {})
        async def shutdown(self):
            if self.alive: events.append(f"stop:{self.spec.name}")
            self.alive = False

    monkeypatch.setattr("mmp_node.engine_io.EngineProcess", FakeProcess)
    tier = lambda name, mb: {(name, "gpu-fast"): {"tier": "gpu-fast", "engine": name,
                                                   "engine_version": "1", "cost": "low", "vram_mb": mb}}
    base = EngineSpec("image-base", "x", ["x"], tmp_path, 5, [], resident_vram_mb=700,
                      keep_warm=True, warm_priority=100, tiers=tier("image-base", 900))
    heavy = EngineSpec("paddle-vl", "x", ["x"], tmp_path, 5, [], resident_vram_mb=14000,
                       tiers=tier("paddle-vl", 14000))
    pool = EnginePool(5, 300, "1.2", [base, heavy], vram_total_mb=16384, vram_headroom_mb=512)
    pool.start(); await asyncio.sleep(0); await asyncio.sleep(0)
    await pool.run(heavy, {"job_id": "x"}, 5, 14000)
    assert "stop:image-base" not in events
    assert pool.loaded() == ["image-base"]  # heavy 非常驻，任务结束即卸载
    await pool.close()
