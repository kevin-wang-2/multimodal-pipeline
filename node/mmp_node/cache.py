"""v3 结果缓存：键由任务类型、引擎版本与规范化命名输入共同决定。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def arguments_hash(arguments: dict) -> str:
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def task_key(task_type: str, tier: str, engine_version: str, arguments: dict, *, bound: bool = False) -> str:
    base = f"{task_type}|{tier}|{engine_version}|{arguments_hash(arguments)}"
    if bound:
        base += "|bound"
    return base


class ResultCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS results_v3 (
                 key TEXT PRIMARY KEY, media_ids TEXT NOT NULL, task_type TEXT NOT NULL,
                 tier TEXT NOT NULL, engine_version TEXT NOT NULL,
                 source TEXT NOT NULL, outputs TEXT NOT NULL, timings TEXT,
                 created_at REAL NOT NULL)"""
        )

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT source, outputs, timings FROM results_v3 WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        source, outputs, timings = row
        out = {"source": json.loads(source), "outputs": json.loads(outputs)}
        if timings is not None:
            out["timings_ms"] = json.loads(timings)
        return out

    def put(self, key: str, media_ids: dict, task_type: str, tier: str, engine_version: str,
            source: dict, outputs: dict, timings_ms: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO results_v3 VALUES (?,?,?,?,?,?,?,?,?)",
            (key, json.dumps(media_ids, sort_keys=True), task_type, tier, engine_version, json.dumps(source, ensure_ascii=False),
             json.dumps(outputs, ensure_ascii=False),
             None if timings_ms is None else json.dumps(timings_ms), time.time()),
        )

    def close(self) -> None:
        self.db.close()
