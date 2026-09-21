"""错误码 → HTTP 状态（协议.md §4）。整个 node/ 只用 ApiError 表达失败，HTTP 层只做映射。"""
from __future__ import annotations

HTTP_STATUS = {
    "bad_request": 400,
    "unsupported_type": 400,
    "unauthorized": 401,
    "not_found": 404,
    "media_too_large": 413,
    "media_fetch_failed": 422,
    "media_not_found": 422,
    "media_hash_mismatch": 422,
    "backpressure": 429,
    "engine_failed": 500,
    "no_node": 503,
    "node_offline": 503,
    "timeout": 504,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str | None = None, retry_after_sec: float | None = None):
        assert code in HTTP_STATUS, code
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.retry_after_sec = retry_after_sec

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    def body(self) -> dict:
        b: dict = {"error": self.code}
        if self.message:
            b["message"] = self.message
        if self.retry_after_sec is not None:
            b["retry_after_sec"] = self.retry_after_sec
        return b
