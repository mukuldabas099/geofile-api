"""Application settings, loaded from environment variables / .env (prefix ``GEOAPI_``)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="GEOAPI_", extra="ignore")

    app_name: str = "Geospatial File Measurement API"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    data_dir: Path = Path("data")
    database_url: str | None = None  # defaults to SQLite inside data_dir

    # Upload / safety limits
    max_upload_mb: int = 50
    max_uncompressed_mb: int = 250  # zip-bomb guard
    max_zip_entries: int = 100
    max_features: int = 200_000

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'geofiles.db').as_posix()}"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_uncompressed_bytes(self) -> int:
        return self.max_uncompressed_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
