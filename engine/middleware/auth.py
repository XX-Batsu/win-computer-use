"""
middleware/auth.py — Bearer token authentication middleware.

Every request must carry:
    Authorization: Bearer <ENGINE_SECRET>

Missing or invalid token → HTTP 401 JSON {"detail": "Unauthorized"}.
"""

import os

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on every incoming request."""

    def __init__(self, app, **kwargs):
        super().__init__(app, **kwargs)
        secret = os.environ.get("ENGINE_SECRET", "")
        assert len(secret) > 0, "ENGINE_SECRET must not be empty"
        self._secret = secret

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization", "")

        if not auth_header.startswith("Bearer ") or auth_header[len("Bearer "):] != self._secret:
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )

        return await call_next(request)
