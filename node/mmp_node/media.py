"""媒体句柄的取放 + 内容寻址的本地存储。

A 只做 HTTP GET / PUT；hash 在取到字节后算，与来源无关。存储按 media_id 寻址，同一媒体只落一份；
调用方带 `ref`（此前响应里的 media_id）时，本地已有就不再拉——这是避免反复拉同一资源的机制（协议 v1.1）。
目录有上限，按最近访问 LRU 淘汰；排队 / 运行中的任务 pin 住自己的媒体。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import MediaSection
from .errors import ApiError

log = logging.getLogger("mmp.media")


@dataclass
class Fetched:
    media_id: str
    path: Path
    size: int
    content_type: str | None
    fetched: bool          # False = ref 命中本地，没有发生下载 / 解码


def sha256_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class MediaStore:
    def __init__(self, cfg: MediaSection, data_dir: Path, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.dir = data_dir / "media"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._pins: dict[str, int] = {}
        self.db = sqlite3.connect(data_dir / "media.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS media (media_id TEXT PRIMARY KEY, size INTEGER NOT NULL, "
                        "content_type TEXT, created_at REAL NOT NULL, last_access REAL NOT NULL)")
        self._reconcile()

    # ---- 索引 ----
    def _path(self, media_id: str) -> Path:
        return self.dir / media_id.split(":", 1)[1]

    def derived_path(self, media_id: str, suffix: str) -> Path:
        """原媒体的派生物（如转码后的 16k WAV）：derived/<hex>.<suffix>，随原文件一起淘汰。"""
        return self.dir / "derived" / f"{media_id.split(':', 1)[1]}.{suffix}"

    def _unlink_with_derived(self, media_id: str) -> None:
        self._path(media_id).unlink(missing_ok=True)
        d = self.dir / "derived"
        if d.is_dir():
            for f in d.glob(media_id.split(":", 1)[1] + ".*"):
                f.unlink(missing_ok=True)

    def _reconcile(self) -> None:
        """启动时把索引与目录对齐：文件没了删索引，索引没了补一条（大小从文件取）。"""
        rows = {r[0] for r in self.db.execute("SELECT media_id FROM media")}
        for mid in list(rows):
            if not self._path(mid).exists():
                self.db.execute("DELETE FROM media WHERE media_id=?", (mid,))
        now = time.time()
        for f in self.dir.iterdir():
            if f.is_dir():
                continue          # derived/
            if f.suffix == ".part":
                f.unlink(missing_ok=True)
                continue
            mid = "sha256:" + f.name
            if mid not in rows and len(f.name) == 64:
                self.db.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?)", (mid, f.stat().st_size, None, now, now))

    def has(self, media_id: str) -> bool:
        row = self.db.execute("SELECT 1 FROM media WHERE media_id=?", (media_id,)).fetchone()
        if row is None:
            return False
        if not self._path(media_id).exists():
            self.db.execute("DELETE FROM media WHERE media_id=?", (media_id,))
            return False
        return True

    def touch(self, media_id: str) -> None:
        self.db.execute("UPDATE media SET last_access=? WHERE media_id=?", (time.time(), media_id))

    def total_bytes(self) -> int:
        return self.db.execute("SELECT COALESCE(SUM(size),0) FROM media").fetchone()[0]

    def pin(self, media_id: str) -> None:
        self._pins[media_id] = self._pins.get(media_id, 0) + 1

    def unpin(self, media_id: str) -> None:
        n = self._pins.get(media_id, 0) - 1
        if n <= 0:
            self._pins.pop(media_id, None)
        else:
            self._pins[media_id] = n

    def _store(self, data: bytes, content_type: str | None) -> tuple[str, Path]:
        media_id = sha256_id(data)
        path = self._path(media_id)
        now = time.time()
        if not path.exists():
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            self.db.execute("INSERT OR REPLACE INTO media VALUES (?,?,?,?,?)", (media_id, len(data), content_type, now, now))
            self._evict()
        else:
            self.db.execute("INSERT OR IGNORE INTO media VALUES (?,?,?,?,?)", (media_id, len(data), content_type, now, now))
            self.touch(media_id)
        return media_id, path

    def _evict(self) -> None:
        """超出上限就从最久未访问的开始删，跳过 pin 住的。"""
        total = self.total_bytes()
        if total <= self.cfg.max_store_bytes:
            return
        for mid, size in self.db.execute("SELECT media_id, size FROM media ORDER BY last_access ASC").fetchall():
            if total <= self.cfg.max_store_bytes:
                break
            if mid in self._pins:
                continue
            try:
                self._unlink_with_derived(mid)
            except OSError as e:  # Windows 上被引擎打开着 → 下次再说
                log.warning("evict %s failed: %s", mid, e)
                continue
            self.db.execute("DELETE FROM media WHERE media_id=?", (mid,))
            total -= size
            log.info("evicted %s (%d bytes)", mid, size)

    # ---- 取 ----
    async def fetch(self, media: dict) -> Fetched:
        ref = media.get("ref")
        ctype = media.get("content_type")
        if ref is not None and self.has(ref):
            self.touch(ref)
            p = self._path(ref)
            return Fetched(media_id=ref, path=p, size=p.stat().st_size, content_type=ctype, fetched=False)
        if "inline" in media:
            data = self._decode_inline(media["inline"])
        elif "get" in media:
            data = await self._get(media["get"])
        else:
            raise ApiError("media_not_found", f"{ref} is not in this node's store; resubmit with inline or get")
        media_id, path = self._store(data, ctype)
        if ref is not None and media_id != ref:
            raise ApiError("media_hash_mismatch", f"fetched content is {media_id}, ref says {ref}")
        return Fetched(media_id=media_id, path=path, size=len(data), content_type=ctype, fetched=True)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.fetch_timeout_sec, follow_redirects=True,
                                             trust_env=self.cfg.use_env_proxy)
        return self._client

    def _decode_inline(self, b64: str) -> bytes:
        # base64 长度 ≈ 4/3 字节数；先按长度粗筛，避免解码 8MB 以上的东西
        if len(b64) * 3 // 4 > self.cfg.max_inline_bytes + 2:
            raise ApiError("media_too_large", f"inline exceeds {self.cfg.max_inline_bytes} bytes; use get")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ApiError("bad_request", f"inline is not valid base64: {e}") from e
        if len(data) > self.cfg.max_inline_bytes:
            raise ApiError("media_too_large", f"inline exceeds {self.cfg.max_inline_bytes} bytes; use get")
        return data

    async def _get(self, ep: dict) -> bytes:
        try:
            async with self._http().stream("GET", ep["url"], headers=ep.get("headers") or {}) as r:
                if r.status_code // 100 != 2:
                    raise ApiError("media_fetch_failed", f"GET {r.status_code}")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > self.cfg.max_fetch_bytes:
                        raise ApiError("media_too_large", f"get exceeds {self.cfg.max_fetch_bytes} bytes")
                return bytes(buf)
        except httpx.HTTPError as e:
            raise ApiError("media_fetch_failed", f"{type(e).__name__}: {e}") from e

    async def put(self, ep: dict, data: bytes, content_type: str) -> None:
        headers = {"Content-Type": content_type, **(ep.get("headers") or {})}
        try:
            r = await self._http().put(ep["url"], content=data, headers=headers)
        except httpx.HTTPError as e:
            raise ApiError("engine_failed", f"PUT result failed: {type(e).__name__}: {e}") from e
        if r.status_code // 100 != 2:
            raise ApiError("engine_failed", f"PUT result failed: {r.status_code}")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self.db.close()
