"""GameStakes application entry point.

FastAPI serves both the JSON API (under /api) and the built React SPA as
static files — one port, one URL, no separate frontend container.
"""

import time
import uuid
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger


class SPAStaticFiles(StaticFiles):
    """Static file server with SPA fallback: unknown client routes get index.html.

    The fallback deliberately excludes two classes of 404 that must stay 404:
    - /api paths, so unknown API routes return JSON errors, not HTML;
    - paths whose final segment has a file extension (stale asset hashes after
      an upgrade), so a missing bundle never masquerades as a 200 HTML page.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if (
                exc.status_code == 404
                and not scope["path"].startswith("/api")
                and "." not in path.rsplit("/", 1)[-1]
            ):
                return await super().get_response("index.html", scope)
            raise


def find_static_dir(settings: Settings) -> Path | None:
    """Locate the built SPA: explicit setting, container path, then local dev build."""
    candidates = [
        settings.static_dir,
        Path("/app/static"),
        Path(__file__).resolve().parent.parent.parent / "frontend" / "dist",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "index.html").is_file():
            return candidate
    return None


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    log = get_logger(__name__)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        root_path=settings.root_path,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()

        def log_request(status: int) -> None:
            if request.url.path.startswith("/api"):
                log.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                    request_id=request_id,
                )

        try:
            response: Response = await call_next(request)
        except Exception:
            log_request(500)
            raise
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-ID"] = request_id
        log_request(response.status_code)
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        # Keeps the X-Request-ID header on 500s so failed requests stay traceable.
        return JSONResponse(
            {"detail": "Internal Server Error"},
            status_code=500,
            headers={"X-Request-ID": getattr(request.state, "request_id", "")},
        )

    app.include_router(api_router, prefix="/api")

    static_dir = find_static_dir(settings)
    if static_dir is not None:
        app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="spa")
        log.info("spa.mounted", static_dir=str(static_dir))
    else:
        log.warning("spa.missing", detail="frontend build not found; serving API only")

        @app.get("/", include_in_schema=False)
        async def spa_placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "app": settings.app_name,
                    "detail": (
                        "Frontend build not found. Run `npm run build` in frontend/ "
                        "or use the Docker image. API is live at /api/health."
                    ),
                }
            )

    return app


app = create_app()
