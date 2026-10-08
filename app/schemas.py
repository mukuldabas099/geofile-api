"""Pydantic response models (these drive the OpenAPI docs)."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import FileStatus, FileType, MeasurementStatus


class FileInfo(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "9f2c1d0e4b7a4c1e8a6b2d3f5e7a9b01",
                "filename": "survey.kml",
                "file_type": "kml",
                "size_bytes": 2048,
                "feature_count": 3,
                "crs": "EPSG:4326",
                "status": "COMPLETED",
                "error_message": None,
                "created_at": "2026-10-07T10:15:30Z",
                "completed_at": "2026-10-07T10:15:31Z",
            }
        },
    )

    id: str
    filename: str
    file_type: FileType
    size_bytes: int
    feature_count: int
    crs: str | None = Field(None, description="CRS of the source data, e.g. EPSG:4326")
    status: FileStatus
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator("created_at", "completed_at")
    @classmethod
    def _assume_utc(cls, value: datetime | None) -> datetime | None:
        """SQLite drops tzinfo on read; every timestamp we store is UTC, so say so."""
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class FeatureOut(BaseModel):
    index: int = Field(description="0-based position of the feature in the file")
    geometry_type: str
    geometry: dict[str, Any] | None = Field(None, description="GeoJSON geometry in the source CRS")
    crs: str | None
    properties: dict[str, Any]


class FeaturesResponse(BaseModel):
    file_id: str
    crs: str | None
    feature_count: int
    limit: int
    offset: int
    items: list[FeatureOut]


class MeasurementItem(BaseModel):
    index: int
    geometry_type: str
    status: MeasurementStatus
    area_m2: float | None = Field(None, description="Area in square metres (polygons)")
    length_m: float | None = Field(None, description="Length in metres (lines)")
    projected_crs: str | None = Field(None, description="Projected CRS used for the calculation")
    method: str | None = None
    geodesic_reference: float | None = Field(
        None, description="Independent WGS84 ellipsoidal value (m or m2) for sanity-checking"
    )
    geometry_repaired: bool = False
    message: str | None = None


class MeasurementSummary(BaseModel):
    total_features: int
    measured: int
    not_applicable: int
    unsupported: int
    failed: int
    total_area_m2: float
    total_length_m: float


class MeasurementsResponse(BaseModel):
    file_id: str
    crs: str | None
    summary: MeasurementSummary
    limit: int
    offset: int
    items: list[MeasurementItem]


class ErrorResponse(BaseModel):
    detail: str
    code: str
    file_id: str | None = None
