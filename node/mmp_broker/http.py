"""任务 API 的 HTTP 绑定（FastAPI）。只做：认证、参数解析、调操作、按 (status, body) 回。"""
from __future__ import annotations

import json

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from mmp_node import schemas
from mmp_node.errors import ApiError

from .core import Broker


def create_app(broker: Broker, api_key: str = "") -> FastAPI:
    app = FastAPI(title="mmp broker", version="1.0", docs_url=None, redoc_url=None)
    validate = schemas.validator_for_ref("urn:mmp:protocol:1:job-api#/$defs/JobRequest")

    def respond(status_body: tuple[int, dict]) -> JSONResponse:
        status, body = status_body
        return JSONResponse(body, status_code=status)

    def auth(authorization: str | None) -> None:
        if api_key and authorization != f"Bearer {api_key}":
            raise ApiError("unauthorized")

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(e.body(), status_code=e.http_status)

    @app.post("/jobs")
    async def submit(request: Request, authorization: str | None = Header(default=None)):
        auth(authorization)
        try:
            job = await request.json()
        except json.JSONDecodeError as e:
            raise ApiError("bad_request", f"body is not JSON: {e}")
        errs = sorted(validate.iter_errors(job), key=lambda e: list(e.path))
        if errs:
            raise ApiError("bad_request", "; ".join(f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in errs[:3]))
        return respond(await broker.submit(job))

    @app.get("/jobs/{job_id}")
    async def get(job_id: str, wait: int = Query(default=0, ge=0, le=120), authorization: str | None = Header(default=None)):
        auth(authorization)
        return respond(await broker.get(job_id, wait))

    @app.delete("/jobs/{job_id}")
    async def cancel(job_id: str, wait: int = Query(default=0, ge=0, le=120), authorization: str | None = Header(default=None)):
        auth(authorization)
        return respond(await broker.cancel(job_id, wait))

    @app.get("/capabilities")
    async def capabilities(authorization: str | None = Header(default=None)):
        auth(authorization)
        return respond(broker.capabilities())

    @app.get("/health")
    async def health():
        return respond(broker.health())

    return app
