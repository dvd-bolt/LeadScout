"""FastAPI application assembly."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from leadscout.core.access import AccessError
from leadscout.runtime import AppContext, get_default_context

from .routes import ROUTERS


def create_app(context: AppContext | None = None) -> FastAPI:
    context = context if context is not None else get_default_context()
    app = FastAPI(
        title="LeadScout Mini App API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.context = context

    @app.exception_handler(AccessError)
    async def access_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message}})

    @app.get("/healthz")
    async def healthcheck() -> dict:
        return {"status": "ok"}

    for router in ROUTERS:
        app.include_router(router, prefix="/api/v1")

    if context.settings.web_dist_dir.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=context.settings.web_dist_dir, html=True),
            name="mini-app",
        )
    return app


__all__ = ["create_app"]
