"""FastAPI application factory."""

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from writer_service import __version__
from writer_service.api.admin import router as admin_router
from writer_service.api.ingest import router as ingest_router
from writer_service.auth.ratelimit import LoginLimiter
from writer_service.auth.routes import router as auth_router
from writer_service.config import Settings, get_settings
from writer_service.governance.db import create_engine_for

# `bun run build` output in a source checkout (src/writer_service/app.py -> repo root).
ADMIN_UI_DIST = Path(__file__).resolve().parents[2] / "admin-ui" / "dist"


def mount_admin_ui(app: FastAPI, dist: Path) -> bool:
    """Serve the built admin UI at ``/admin/`` if it has been built. The UI uses hash routing,
    so no server-side fallback is needed: every screen is ``/admin/#/...``."""
    if not (dist / "index.html").is_file():
        return False

    @app.get("/admin", include_in_schema=False)
    def admin_redirect() -> RedirectResponse:
        return RedirectResponse("/admin/")

    app.mount("/admin/", StaticFiles(directory=dist, html=True), name="admin-ui")
    return True


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Settings are validated here so bad config fails at startup."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        driver = getattr(app.state, "neo4j_driver", None)
        if driver is not None:
            driver.close()
        app.state.engine.dispose()

    app = FastAPI(title="Writer Service", version=__version__, lifespan=lifespan)
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
    app.include_router(ingest_router)
    app.include_router(admin_router)

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

    mount_admin_ui(app, Path(settings.admin_ui_dir) if settings.admin_ui_dir else ADMIN_UI_DIST)
    return app
