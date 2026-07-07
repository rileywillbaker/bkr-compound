"""API hardening middleware (spec §10 Phase 7).

- RateLimitMiddleware: fixed-window per-client cap on /api requests. This is
  a single-user app; the limit exists to stop runaway scripts/browser loops
  from hammering providers through the API, not to shape multi-tenant load.
- SecurityHeadersMiddleware: standard defensive headers on every response.
"""

import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

RATE_LIMIT_PER_MINUTE = 240
_EXEMPT_PREFIXES = ("/health", "/ws", "/assets")


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limit_per_minute: int = RATE_LIMIT_PER_MINUTE):
        super().__init__(app)
        self.limit = limit_per_minute
        self._lock = threading.Lock()
        self._window = 0
        self._counts: dict[str, int] = {}

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if not path.startswith("/api") or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)
        client = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        with self._lock:
            if window != self._window:
                self._window = window
                self._counts = {}
            self._counts[client] = self._counts.get(client, 0) + 1
            count = self._counts[client]
        if count > self.limit:
            return JSONResponse(
                {"detail": "rate limit exceeded; retry in under a minute"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Cache-Control", "no-store" if request.url.path.startswith("/api") else "public, max-age=3600"
        )
        return response
