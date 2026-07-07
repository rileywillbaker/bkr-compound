"""FastAPI application entrypoint.

Serves the JSON API under /api, the WebSocket feed under /ws, health probes
under /health, and (in prod images) the built React SPA from frontend/dist.
"""

import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sentinel import DISCLAIMER, __version__
from sentinel.api.auth import require_auth
from sentinel.config import PROJECT_ROOT, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sentinel.data.bus import start_redis_relay

    stop = threading.Event()
    relay = start_redis_relay(stop)
    yield
    stop.set()
    if relay is not None:
        relay.join(timeout=3)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="B-Quant",
        version=__version__,
        description=DISCLAIMER,
        lifespan=lifespan,
    )

    if settings.is_dev:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/deep")
    def health_deep() -> dict[str, Any]:
        checks: dict[str, str] = {}
        try:
            from sqlalchemy import create_engine, text

            engine = create_engine(settings.database_url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:  # pragma: no cover - env-dependent
            checks["database"] = f"error: {exc.__class__.__name__}"
        try:
            import redis as redis_lib

            r = redis_lib.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
            r.ping()
            checks["redis"] = "ok"
        except Exception as exc:  # pragma: no cover - env-dependent
            checks["redis"] = f"error: {exc.__class__.__name__}"
        status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        return {"status": status, "checks": checks, "version": __version__}

    _register_routers(app)
    _mount_spa(app)
    return app


def _register_routers(app: FastAPI) -> None:
    """Attach feature routers. Each phase adds routers here. Everything except
    auth itself and /health sits behind the session-auth guard (a no-op in
    dev/local-network mode — see sentinel/api/auth.py)."""
    from sentinel.api import auth, ws
    from sentinel.api.routers import (
        alerts,
        analytics,
        app_settings,
        chat,
        context,
        pipeline,
        portfolio,
        providers,
        risk,
        signals,
        system,
    )

    app.include_router(auth.router)
    guard = [Depends(require_auth)]
    for module in (
        providers,
        system,
        context,
        portfolio,
        risk,
        pipeline,
        signals,
        alerts,
        chat,
        analytics,
        app_settings,
    ):
        app.include_router(module.router, dependencies=guard)
    app.include_router(ws.router)


def _mount_spa(app: FastAPI) -> None:
    dist = PROJECT_ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="spa")


app = create_app()
