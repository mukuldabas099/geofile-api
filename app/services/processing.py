"""File-processing pipeline: read -> normalise features -> measure -> persist."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import shapely
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import FileStatus, FileType
from app.exceptions import GeoAPIError, TooManyFeaturesError
from app.models import Feature, UploadedFile
from app.services.measurements import MeasurementCalculator, crs_label
from app.services.readers import parse_assume_crs, read_geodata

logger = logging.getLogger(__name__)

_INSERT_BATCH = 5_000  # rows per bulk INSERT (keeps memory flat for large files)


def json_safe(value: Any) -> Any:
    """Convert pandas/numpy scalars (NaN, NaT, Timestamp, int64...) into plain JSON types."""
    if value is None or value is pd.NaT or value is pd.NA:
        return None
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.datetime64):
        return None if np.isnat(value) else pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _geometry_to_geojson(geom: shapely.Geometry | None) -> dict | None:
    if geom is None or geom.is_empty:
        return None
    return json.loads(shapely.to_geojson(geom))


def process_upload(
    db: Session,
    record: UploadedFile,
    settings: Settings,
    assume_crs: str | None = None,
) -> UploadedFile:
    """Process a stored upload. On failure the record is marked FAILED, then the error re-raised."""
    record.status = FileStatus.PROCESSING.value
    db.commit()
    try:
        _run(db, record, settings, assume_crs)
    except GeoAPIError as exc:
        _mark_failed(db, record, exc.message)
        exc.file_id = record.id
        raise
    except Exception:
        logger.exception("Unexpected error processing file %s", record.id)
        _mark_failed(db, record, "Unexpected internal error while processing the file.")
        raise
    return record


def _run(db: Session, record: UploadedFile, settings: Settings, assume_crs: str | None) -> None:
    crs_override = parse_assume_crs(assume_crs)
    gdf = read_geodata(Path(record.stored_path), FileType(record.file_type), settings, crs_override)
    if len(gdf) > settings.max_features:
        raise TooManyFeaturesError(
            f"File has {len(gdf)} features; the limit is {settings.max_features}."
        )

    calculator = MeasurementCalculator(gdf.crs)
    geom_col = gdf.geometry.name
    prop_frame = gdf.drop(columns=[geom_col])
    columns = [str(c) for c in prop_frame.columns]

    batch: list[dict[str, Any]] = []
    count = 0
    for idx, (geom, values) in enumerate(zip(gdf.geometry, prop_frame.itertuples(index=False, name=None), strict=True)):
        m = calculator.measure(geom)
        batch.append(
            {
                "file_id": record.id,
                "idx": idx,
                "geometry_type": geom.geom_type if geom is not None else "None",
                "geometry": _geometry_to_geojson(geom),
                "properties": {c: json_safe(v) for c, v in zip(columns, values, strict=True)},
                "measurement_status": m.status.value,
                "area_m2": m.area_m2,
                "length_m": m.length_m,
                "projected_crs": m.projected_crs,
                "method": m.method,
                "geodesic_reference": m.geodesic_reference,
                "geometry_repaired": m.geometry_repaired,
                "message": m.message,
            }
        )
        count += 1
        if len(batch) >= _INSERT_BATCH:
            db.execute(insert(Feature), batch)
            batch = []
    if batch:
        db.execute(insert(Feature), batch)

    record.crs = crs_label(gdf.crs)
    record.feature_count = count
    record.status = FileStatus.COMPLETED.value
    record.error_message = None
    record.completed_at = datetime.now(UTC)
    db.commit()


def _mark_failed(db: Session, record: UploadedFile, message: str) -> None:
    db.rollback()
    record.status = FileStatus.FAILED.value
    record.error_message = message
    record.completed_at = datetime.now(UTC)
    db.commit()
