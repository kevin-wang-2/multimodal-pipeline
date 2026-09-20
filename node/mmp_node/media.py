"""媒体句柄的取放。A 只做 HTTP GET / PUT；hash 在取到字节后算，与来源无关。"""
from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import MediaSection
from .errors import ApiError


@dataclass
class Fetched:
    media_id: str
    path: Path
    size: int
    content_type: str | None


def sha256_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class MediaStore:
    """内容寻址的临时目录：data_dir/media/<hex>。同一媒体多次提交只落一份。"""

    def __init__(self, cfg: MediaSection, data_dir: Path, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.dir = data_dir / "media"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._client = client

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.fetch_timeout_sec, follow_redirects=True,
                                             trust_env=self.cfg.use_env_proxy)
        return self._client

    async def fetch(self, media: dict) -> Fetched:
        if "inline" in media:
            data = self._decode_inline(media["inline"])
        else:
            data = await self._get(media["get"])
        media_id = sha256_id(data)
        path = self.dir / media_id.split(":", 1)[1]
        if not path.exists():
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
        return Fetched(media_id=media_id, path=path, size=len(data), content_type=media.get("content_type"))

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
