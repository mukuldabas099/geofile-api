"""Application service: ingestion + read queries used by the API layer."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.enums import FileStatus, MeasurementStatus
from app.exceptions import FileNotReadyError, ResourceNotFoundError
from app.models import Feature, UploadedFile
from app.services import storage
from app.services.processing import process_upload
from app.services.readers import parse_assume_crs


def ingest_upload(
    db: Session, settings: Settings, stream: BinaryIO, filename: str | None, assume_crs: str | None
) -> UploadedFile:
    display_name = storage.sanitize_filename(filename)
    file_type = storage.detect_file_type(display_name)
    parse_assume_crs(assume_crs)  # fail fast on a bad CRS, before touching disk

    file_id = uuid.uuid4().hex
    suffix = ".kml" if file_type.value == "kml" else ".zip"
    dest = settings.uploads_dir / file_id / f"source{suffix}"
    size = storage.save_upload(stream, dest, settings.max_upload_bytes)
    try:
        storage.verify_signature(dest, file_type)
    except Exception:
        storage.remove_stored(dest.parent)
        raise

    record = UploadedFile(
        id=file_id,
        filename=display_name,
        file_type=file_type.value,
        size_bytes=size,
        stored_path=str(dest),
        status=FileStatus.PENDING.value,
    )
    db.add(record)
    db.commit()
    return process_upload(db, record, settings, assume_crs)


def get_file(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise ResourceNotFoundError(f"File '{file_id}' not found.")
    return record


def get_completed_file(db: Session, file_id: str) -> UploadedFile:
    record = get_file(db, file_id)
    if record.status != FileStatus.COMPLETED.value:
        detail = f" Reason: {record.error_message}" if record.error_message else ""
        raise FileNotReadyError(f"File is {record.status}, results are unavailable.{detail}")
    return record


def list_files(db: Session, limit: int, offset: int) -> list[UploadedFile]:
    stmt = select(UploadedFile).order_by(UploadedFile.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))


def list_features(db: Session, file_id: str, limit: int, offset: int) -> list[Feature]:
    stmt = (
        select(Feature).where(Feature.file_id == file_id).order_by(Feature.idx).limit(limit).offset(offset)
    )
    return list(db.scalars(stmt))


def measurement_summary(db: Session, file_id: str) -> dict:
    def count(status: MeasurementStatus):
        return func.coalesce(func.sum(case((Feature.measurement_status == status.value, 1), else_=0)), 0)

    row = db.execute(
        select(
            func.count(Feature.pk),
            count(MeasurementStatus.OK),
            count(MeasurementStatus.NOT_APPLICABLE),
            count(MeasurementStatus.UNSUPPORTED),
            count(MeasurementStatus.FAILED),
            func.coalesce(func.sum(Feature.area_m2), 0.0),
            func.coalesce(func.sum(Feature.length_m), 0.0),
        ).where(Feature.file_id == file_id)
    ).one()
    return {
        "total_features": row[0],
        "measured": row[1],
        "not_applicable": row[2],
        "unsupported": row[3],
        "failed": row[4],
        "total_area_m2": row[5],
        "total_length_m": row[6],
    }


def delete_file(db: Session, file_id: str) -> None:
    record = get_file(db, file_id)
    directory = Path(record.stored_path).parent
    db.delete(record)
    db.commit()
    storage.remove_stored(directory)
