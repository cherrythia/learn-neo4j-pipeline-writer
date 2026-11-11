"""FastAPI application factory."""

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from writer_service import __version__
from writer_service.auth.ratelimit import LoginLimiter
from writer_service.auth.routes import router as auth_router
from writer_service.config import Settings, get_settings
from writer_service.governance.db import create_engine_for


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings are validated here so bad config fails at startup."""
    settings = settings or get_settings()
    app = FastAPI(title="Writer Service", version=__version__)
    app.state.settings = settings
    app.state.engine = create_engine_for(settings.governance_db_url)
    # In dev an unset secret gets a random one: sessions simply end on restart.
    app.state.jwt_secret = (
        settings.admin_jwt_secret.get_secret_value()
        if settings.admin_jwt_secret
        else secrets.token_urlsafe(48)
    )
    app.state.login_limiter = LoginLimiter()
    app.include_router(auth_router)

    @app.middleware("http")
    async def version_header(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["x-service-version"] = __version__
        return response

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
