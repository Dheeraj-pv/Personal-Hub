"""FastAPI app. Run: uv run uvicorn hub.main:create_app --factory --host 127.0.0.1 --port 8787"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from . import __version__
from .auth import is_authorized
from .config import Settings, load_settings
from .db import init_db, open_db
from .notes_export import APP as NOTES_APP
from .notes_export import TABLE as NOTES_TABLE
from .notes_export import export_notes
from .schemas import MAX_CHANGES, SyncIn
from .sync import health_apps, instance_id, run_sync

APPS = frozenset({"expenses", "notes"})
MAX_BODY_BYTES = 2 * 1024 * 1024

# Our own log lines never include request bodies, headers or tokens: only fixed messages and
# exception type names. The server (uvicorn) may still print a traceback for an unhandled error
# to the journal; Python tracebacks do not contain request bodies or headers.
log = logging.getLogger("hub")


class BodyTooLarge(Exception):
    pass


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _reject_constant(_: str) -> Any:
    raise ValueError("non-finite number")


def _parse_float(text: str) -> float:
    value = float(text)  # "1e999" becomes inf here; json's parse_constant never sees it
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > MAX_BODY_BYTES
        except ValueError:
            raise ValueError("bad content-length") from None
        if too_big:
            raise BodyTooLarge
    # The header can be missing (chunked) or wrong, so count the bytes as they arrive.
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    init_db(settings.db_path)
    app = FastAPI(title="Personal Hub", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, "not found" if exc.status_code == 404 else "request error")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", type(exc).__name__)
        return _error(500, "internal error")

    def _health(authed: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"status": "ok"}
        if authed:
            with open_db(settings.db_path) as conn:
                apps = health_apps(conn)
                ins = instance_id(conn)
            body.update(
                hub_version=f"personal-hub-{__version__}",
                server_time=int(time.time() * 1000),
                instance_id=ins,
                apps=apps,
            )
        return body

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        # A bad token still gets the bare status (like MockHub): details need a valid one.
        return await run_in_threadpool(_health, is_authorized(request, settings.token))

    def _sync(app_name: str, payload: SyncIn) -> dict[str, Any]:
        with open_db(settings.db_path) as conn:
            return run_sync(conn, app_name, payload.device_id, payload.cursor, payload.changes)

    @app.post("/sync/{app_name}")
    async def sync(app_name: str, request: Request) -> JSONResponse:
        if not is_authorized(request, settings.token):
            return _error(401, "unauthorized")
        if app_name not in APPS:
            return _error(404, "unknown app")
        try:
            raw = await _read_body(request)
        except BodyTooLarge:
            return _error(413, "body too large")
        except ValueError:
            return _error(400, "bad request")
        try:
            doc = json.loads(raw, parse_constant=_reject_constant, parse_float=_parse_float)
            if not isinstance(doc, dict):
                raise ValueError
            changes = doc.get("changes")
            if isinstance(changes, list) and len(changes) > MAX_CHANGES:
                return _error(400, f"too many changes (max {MAX_CHANGES})")
            payload = SyncIn.model_validate(doc)
        except (ValueError, RecursionError, ValidationError):  # ValidationError is a ValueError
            # Deliberately generic: validation messages would echo parts of the input.
            return _error(400, "invalid request")
        result = await run_in_threadpool(_sync, app_name, payload)
        # Markdown export runs after the response is sent and never raises.
        ids = [c.id for c in payload.changes if c.table == NOTES_TABLE]
        export = None
        if app_name == NOTES_APP and settings.notes_dir is not None and ids:
            export = BackgroundTask(export_notes, settings.db_path, settings.notes_dir, ids)
        return JSONResponse(result, background=export)

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    # Lets `uvicorn hub.main:app` work without importing (and requiring env) at import time.
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


if __name__ == "__main__":  # python -m hub.main  (uses HUB_HOST / HUB_PORT)
    import uvicorn

    s = load_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, workers=1)
