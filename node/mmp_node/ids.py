"""job_id = <node_id>-<ULID>。ULID：48 位毫秒时间戳 + 80 位随机，Crockford base32。"""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(now_ms: int | None = None) -> str:
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    n = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[n & 31])
        n >>= 5
    return "".join(reversed(out))


def new_job_id(node_id: str) -> str:
    return f"{node_id}-{ulid()}"


def node_of(job_id: str) -> str | None:
    """从 job_id 取 node_id 前缀；格式不对返回 None。B 靠它路由。"""
    if len(job_id) < 28 or job_id[-27] != "-":
        return None
    return job_id[:-27]
