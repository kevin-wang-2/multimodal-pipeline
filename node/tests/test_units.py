"""不需要起栈的单元：配置、id、缓存键。"""
import re

from mmp_node.cache import params_hash, task_key
from mmp_node.config import apply_env, load_config
from mmp_node.ids import new_job_id, node_of, ulid


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
