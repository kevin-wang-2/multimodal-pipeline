"""node.toml + 环境变量覆盖。只做加载与类型校验，不含业务默认值以外的逻辑。"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

ENV_PREFIX = "MMP_"


class NodeSection(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    key: str = Field(min_length=16)
    protocol_version: str = "1.1"
    data_dir: Path = Path("./cache")


class QueueSection(BaseModel):
    max_len: int = 200
    retry_after_sec: float = 5


class SchedulerSection(BaseModel):
    vram_total_mb: int = 0
    idle_unload_sec: float = 300
    engine_start_timeout_sec: float = 120


class MediaSection(BaseModel):
    max_inline_bytes: int = 8 * 1024 * 1024
    max_fetch_bytes: int = 256 * 1024 * 1024
    fetch_timeout_sec: float = 60
    max_inline_result_bytes: int = 1024 * 1024
    use_env_proxy: bool = False
    max_store_bytes: int = 2 * 1024 * 1024 * 1024   # 媒体目录上限，LRU 淘汰；在跑的任务 pin 住


class CacheSection(BaseModel):
    path: Path = Path("results.sqlite")


class JobsSection(BaseModel):
    retention_sec: float = 3600


class BrokerSection(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765
    api_key: str = ""
    inflight_grace_sec: float = 5


class BrokerEndpoint(BaseModel):
    """A 主动连的一个 B（B/C 绑定或独立 B）。可以有多个。"""
    url: str = Field(pattern=r"^wss?://")
    key: str | None = None             # register 用的密钥；默认 node.key
    heartbeat_interval_sec: float = 10
    backoff_min_sec: float = 1
    backoff_max_sec: float = 60
    backoff_jitter: float = 0.2
    use_env_proxy: bool = False       # 出站 ws 是否走 HTTP(S)_PROXY / ALL_PROXY


class EngineSection(BaseModel):
    module: str
    python: str | None = None          # 引擎自己的解释器（conda 环境）；默认与 A 相同
    cmd: list[str] | None = None       # 完整命令覆盖；默认 [python, -m, module]
    env: dict[str, str] = {}           # 追加给子进程的环境变量（如 MMP_MODELS_DIR）
    timeout_sec: float = 30
    max_concurrency: dict[str, int] | int | None = None


class Config(BaseModel):
    node: NodeSection
    queue: QueueSection = QueueSection()
    scheduler: SchedulerSection = SchedulerSection()
    media: MediaSection = MediaSection()
    cache: CacheSection = CacheSection()
    jobs: JobsSection = JobsSection()
    broker: BrokerSection = BrokerSection()
    brokers: list[BrokerEndpoint] = []
    engines: dict[str, EngineSection] = {}

    @property
    def cache_path(self) -> Path:
        return self.node.data_dir / self.cache.path


def _coerce(raw: str) -> Any:
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def apply_env(data: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    """MMP_SECTION__KEY=value → data[section][key]；三段式 MMP_ENGINES__ECHO__TIMEOUT_SEC 也支持。"""
    env = os.environ if environ is None else environ
    for k, v in env.items():
        if not k.startswith(ENV_PREFIX):
            continue
        parts = [p.lower() for p in k[len(ENV_PREFIX):].split("__")]
        if len(parts) < 2:
            continue
        cur = data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = _coerce(v)
    return data


def load_config(path: str | Path | None, environ: dict[str, str] | None = None) -> Config:
    data: dict[str, Any] = {}
    if path is not None:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    data = apply_env(data, environ)
    return Config.model_validate(data)
