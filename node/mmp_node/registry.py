"""任务类型注册表：能力元数据（协议 capability schema）+ 引擎绑定。/capabilities 与 register 都从这里生成。"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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
    env: dict[str, str] = field(default_factory=dict)
    resident_vram_mb: int = 0
    run_vram_mb: int = 0
    keep_warm: bool = False
    warm_priority: int = 0
    internal_source: dict | None = None
    # (task_type, tier) → TierSpec；同一引擎可以提供多个类型的多个档
    tiers: dict[tuple[str, str], dict] = field(default_factory=dict)
    presenters: dict[str, Callable[[dict], dict]] = field(default_factory=dict)


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
        cmd = sec.cmd or [sec.python or sys.executable, "-m", sec.module]
        return EngineSpec(name=name, module=sec.module, cmd=cmd, cwd=engines_dir,
                          timeout_sec=sec.timeout_sec, capabilities=caps, env=dict(sec.env),
                          resident_vram_mb=sec.resident_vram_mb, run_vram_mb=sec.run_vram_mb,
                          keep_warm=sec.keep_warm, warm_priority=sec.warm_priority,
                          internal_source=getattr(mod, "INTERNAL_SOURCE", None),
                          presenters=dict(getattr(mod, "AGENT_CONTEXT_PRESENTERS", {})))

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

    @staticmethod
    def _matches_media_type(pattern: str, content_type: str) -> bool:
        actual = content_type.split(";", 1)[0].strip().lower()
        accepted = pattern.lower()
        return (accepted == "*/*" and "/" in actual) or accepted == actual or (
            accepted.endswith("/*") and actual.startswith(accepted[:-1])
        )

    @staticmethod
    def _digest_labels(digest: dict) -> set[str]:
        return {
            str(label["tag"]).casefold()
            for segment in digest.get("segments", [])
            for label in segment.get("labels", [])
            if isinstance(label, dict) and label.get("tag")
        }

    @staticmethod
    def _has_granularity(digest: dict, granularity: str | None) -> bool:
        if granularity in (None, "whole"):
            return True
        if granularity == "segment":
            return digest.get("kind") == "audio" and bool(digest.get("segments"))
        if granularity == "region":
            return digest.get("kind") == "image" and bool(digest.get("regions"))
        if granularity == "page":
            return digest.get("kind") == "image" and any(
                surface.get("kind") == "page" for surface in digest.get("surfaces", [])
            )
        return False

    def capabilities_for_digest(self, digest: dict, content_type: str | None = None) -> list[str]:
        """按原始 MIME 与预检事实筛选显式能力；不在此替 Harness 推断调用意图。"""
        if digest.get("kind") == "image":
            media_type = digest.get("format")
        elif digest.get("kind") == "audio":
            media_type = content_type or "audio/unknown"
        else:
            return []
        if not isinstance(media_type, str):
            return []

        labels = self._digest_labels(digest)
        available: list[str] = []
        for cap in self.types.values():
            if cap.get("purpose") in {"triage", "diagnostic"}:
                continue
            accepts = cap.get("input", {}).get("media", {}).get("accepts", [])
            if not any(self._matches_media_type(pattern, media_type) for pattern in accepts):
                continue
            consumes = cap.get("consumes") or {}
            wanted_labels = {str(label).casefold() for label in consumes.get("labels", [])}
            if wanted_labels and labels.isdisjoint(wanted_labels):
                continue
            if not self._has_granularity(digest, consumes.get("granularity")):
                continue
            available.append(cap["id"])
        return sorted(available)

    def populate_capabilities_available(self, result: object, content_type: str | None = None) -> bool:
        """把注册表派生信息覆盖进 digest，返回内容是否变化。"""
        if not isinstance(result, dict) or result.get("kind") not in {"audio", "image"}:
            return False
        available = self.capabilities_for_digest(result, content_type)
        changed = result.get("capabilities_available") != available
        result["capabilities_available"] = available
        return changed

    def result_schema(self, task_type: str):
        return self.capability(task_type)["output"]["schema"]

    def present_agent_context(self, task_type: str, result: object) -> dict | None:
        cap = self.capability(task_type)
        policy = cap["output"]["agent_context"]
        spec = self.engine_for(task_type, cap["tiers"][0]["tier"])
        presenter = spec.presenters.get(task_type)
        if presenter is None:
            if policy == "required":
                raise ApiError("engine_failed", f"{task_type} requires agent_context but has no presenter")
            return None
        if not isinstance(result, dict):
            raise ApiError("engine_failed", f"{task_type} cannot present non-object result")
        context = presenter(result)
        if not isinstance(context, dict):
            raise ApiError("engine_failed", f"{task_type} presenter returned invalid agent_context")
        return context
