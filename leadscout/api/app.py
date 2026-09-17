"""FastAPI application assembly."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from leadscout.core.access import AccessError
from leadscout.runtime import AppContext, get_default_context
from leadscout.storage import SCHEMA_VERSION

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

    @app.get("/livez")
    async def liveness() -> dict:
        return {"status": "ok"}

    @app.get("/healthz")
    async def healthcheck():
        checks = {"database": False, "scheduler": False}
        try:
            async with context.db.get_db_connection() as connection:
                version = await (await connection.execute("PRAGMA user_version")).fetchone()
                rows = await (await connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )).fetchall()
                tables = {str(row[0]) for row in rows}
                required_tables = {"users", "hh_accounts", "operations", "application_attempts"}
                checks["database"] = bool(
                    version and version[0] == SCHEMA_VERSION and required_tables.issubset(tables)
                )
        except Exception:
            checks["database"] = False
        scheduler = context.scheduler
        required_jobs = {"hh_auto_search", "daily_reset", "product_retention"}
        if getattr(getattr(context, "task_registry", None), "store", None) is not None:
            required_jobs.add("admin_retention")
        browser_pool = getattr(getattr(context.coordinator, "_dependencies", None), "browser_pool", None)
        if browser_pool is not None and hasattr(browser_pool, "close_idle"):
            required_jobs.add("browser_idle_cleanup")
        scheduled_jobs = {
            getattr(job, "id", "") for job in scheduler.get_jobs()
            if getattr(job, "next_run_time", True) is not None
        } if scheduler and getattr(scheduler, "running", False) else set()
        checks["scheduler"] = required_jobs.issubset(scheduled_jobs)
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ok" if ready else "not_ready", "checks": checks},
        )

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
