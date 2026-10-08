"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import models  # noqa: F401  (register tables)
from app.api.routes import files
from app.config import Settings, get_settings
from app.database import Base, create_db
from app.exceptions import GeoAPIError

logger = logging.getLogger(__name__)

DESCRIPTION = """
Upload a **KML** or a **zipped Shapefile** and get back extracted features plus
**CRS-aware measurements**: polygon area (m²) and line length (m).

Geographic coordinates (e.g. EPSG:4326) are never measured in degrees — each feature is
reprojected to a suitable local projected CRS first. See the README for details.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    engine, session_factory = create_db(settings.resolved_database_url)
    Base.metadata.create_all(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.exception_handler(GeoAPIError)
    async def geo_error_handler(_: Request, exc: GeoAPIError) -> JSONResponse:
        body = {"detail": exc.message, "code": exc.code, "file_id": exc.file_id}
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error.", "code": "internal_error", "file_id": None},
        )

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(files.router)
    return app


app = create_app()
