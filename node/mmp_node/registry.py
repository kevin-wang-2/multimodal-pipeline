"""任务类型注册表：能力元数据（协议 capability schema）+ 引擎绑定。/capabilities 与 register 都从这里生成。"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, EngineSection
from .errors import ApiError

TIER_ORDER = ["remote", "gpu", "gpu-fast", "cpu"]  # 贵 → 便宜；回落沿着这个方向


@dataclass
class EngineSpec:
    name: str
    module: str
    cmd: list[str]
    cwd: Path
    timeout_sec: float
    capabilities: list[dict]          # 该引擎提供的任务类型
    # (task_type, tier) → TierSpec；同一引擎可以提供多个类型的多个档
    tiers: dict[tuple[str, str], dict] = field(default_factory=dict)


class Registry:
    def __init__(self, cfg: Config, engines_dir: Path):
        self.engines: dict[str, EngineSpec] = {}
        self.types: dict[str, dict] = {}                 # task_type → capability
        self.type_engine: dict[tuple[str, str], str] = {}  # (task_type, tier) → engine name
        for name, sec in cfg.engines.items():
            spec = self._load(name, sec, engines_dir)
            self.engines[name] = spec
            for cap in spec.capabilities:
                if cap["id"] in self.types:
                    raise ValueError(f"task type {cap['id']} registered by two engines")
                self.types[cap["id"]] = cap
                for t in cap["tiers"]:
                    spec.tiers[(cap["id"], t["tier"])] = t
                    self.type_engine[(cap["id"], t["tier"])] = name

    @staticmethod
    def _load(name: str, sec: EngineSection, engines_dir: Path) -> EngineSpec:
        # 引擎模块只被 import 来读 CAPABILITY，不在 A 进程里执行任何模型代码
        if str(engines_dir) not in sys.path:
            sys.path.insert(0, str(engines_dir))
        mod = importlib.import_module(sec.module)
        caps = [dict(c) for c in getattr(mod, "CAPABILITIES")]
        for cap in caps:
            for t in cap["tiers"]:
                if isinstance(sec.max_concurrency, int):
                    t["max_concurrency"] = sec.max_concurrency
                elif isinstance(sec.max_concurrency, dict) and t["tier"] in sec.max_concurrency:
                    t["max_concurrency"] = sec.max_concurrency[t["tier"]]
        cmd = sec.cmd or [sys.executable, "-m", sec.module]
        return EngineSpec(name=name, module=sec.module, cmd=cmd, cwd=engines_dir,
                          timeout_sec=sec.timeout_sec, capabilities=caps)

    # ---- 查询 ----
    def capability(self, task_type: str) -> dict:
        cap = self.types.get(task_type)
        if cap is None:
            raise ApiError("unsupported_type", f"unknown task type {task_type!r}")
        return cap

    def resolve_tier(self, task_type: str, requested: str | None) -> dict:
        cap = self.capability(task_type)
        if requested is None:
            return cap["tiers"][0]
        for t in cap["tiers"]:
            if t["tier"] == requested:
                return t
        raise ApiError("bad_request", f"{task_type} has no tier {requested!r}")

    def cheaper_tier(self, task_type: str, current: str) -> dict | None:
        """比 current 便宜且该类型提供的下一档；没有返回 None。"""
        cap = self.capability(task_type)
        idx = TIER_ORDER.index(current)
        for name in TIER_ORDER[idx + 1:]:
            for t in cap["tiers"]:
                if t["tier"] == name:
                    return t
        return None

    def engine_for(self, task_type: str, tier: str) -> EngineSpec:
        return self.engines[self.type_engine[(task_type, tier)]]

    def engine_versions(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for cap in self.types.values():
            for t in cap["tiers"]:
                out[t["engine"]] = t["engine_version"]
        return out

    def capabilities(self) -> list[dict]:
        return list(self.types.values())
