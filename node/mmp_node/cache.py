"""结果缓存：SQLite（标准库）。键 = (media_id, type, tier, engine_version, 规范化 params 的 hash)。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path


def params_hash(params: dict) -> str:
    canon = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def task_key(media_id: str, task_type: str, tier: str, engine_version: str, params: dict) -> str:
    return f"{media_id}|{task_type}|{tier}|{engine_version}|{params_hash(params)}"


class ResultCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS results (
                 key TEXT PRIMARY KEY, media_id TEXT NOT NULL, task_type TEXT NOT NULL,
                 tier TEXT NOT NULL, engine_version TEXT NOT NULL,
                 source TEXT NOT NULL, result TEXT, result_ref TEXT, timings TEXT,
                 created_at REAL NOT NULL)"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS results_media ON results(media_id, task_type)")

    def get(self, key: str) -> dict | None:
        row = self.db.execute("SELECT source, result, result_ref, timings FROM results WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        source, result, result_ref, timings = row
        out = {"source": json.loads(source)}
        if result is not None:
            out["result"] = json.loads(result)
        if result_ref is not None:
            out["result_ref"] = json.loads(result_ref)
        if timings is not None:
            out["timings_ms"] = json.loads(timings)
        return out

    def put(self, key: str, media_id: str, task_type: str, tier: str, engine_version: str,
            source: dict, result=None, result_ref: dict | None = None, timings_ms: dict | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)",
            (key, media_id, task_type, tier, engine_version, json.dumps(source, ensure_ascii=False),
             None if result is None else json.dumps(result, ensure_ascii=False),
             None if result_ref is None else json.dumps(result_ref),
             None if timings_ms is None else json.dumps(timings_ms), time.time()),
        )

    def close(self) -> None:
        self.db.close()
