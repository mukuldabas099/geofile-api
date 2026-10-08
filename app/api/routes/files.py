from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_app_settings, get_db
from app.config import Settings
from app.schemas import (
    ErrorResponse,
    FeatureOut,
    FeaturesResponse,
    FileInfo,
    MeasurementItem,
    MeasurementsResponse,
    MeasurementSummary,
)
from app.services import files as svc

router = APIRouter(prefix="/api/files", tags=["files"])

DB = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
Limit = Annotated[int, Query(ge=1, le=1000, description="Page size")]
Offset = Annotated[int, Query(ge=0, description="Items to skip")]

_upload_errors = {
    413: {"model": ErrorResponse, "description": "File exceeds the size limit"},
    415: {"model": ErrorResponse, "description": "Not a .kml or .zip upload"},
    422: {"model": ErrorResponse, "description": "File is invalid / unreadable / has no CRS"},
}
_not_found = {404: {"model": ErrorResponse, "description": "File not found"}}
_not_ready = {409: {"model": ErrorResponse, "description": "File not processed successfully"}}


@router.post("", response_model=FileInfo, status_code=201, include_in_schema=False)
@router.post(
    "/",
    response_model=FileInfo,
    status_code=201,
    summary="Upload and process a geospatial file",
    description=(
        "Accepts a `.kml` file or a `.zip` containing a Shapefile (`.shp`, `.shx`, `.dbf`, "
        "ideally `.prj`). The file is validated, stored, parsed and measured before the "
        "response is returned. If a Shapefile has no `.prj`, pass `assume_crs` (e.g. `EPSG:4326`)."
    ),
    responses=_upload_errors,
)
def upload_file(
    db: DB,
    settings: AppSettings,
    file: Annotated[UploadFile, File(description=".kml, or .zip containing a Shapefile")],
    assume_crs: Annotated[
        str | None,
        Form(description="CRS to assume when the file declares none, e.g. EPSG:4326"),
    ] = None,
):
    return svc.ingest_upload(db, settings, file.file, file.filename, assume_crs)


@router.get("", response_model=list[FileInfo], include_in_schema=False)
@router.get("/", response_model=list[FileInfo], summary="List uploaded files")
def list_files(db: DB, limit: Limit = 50, offset: Offset = 0):
    return svc.list_files(db, limit, offset)


@router.get("/{file_id}", response_model=FileInfo, include_in_schema=False)
@router.get("/{file_id}/", response_model=FileInfo, summary="File information and status", responses=_not_found)
def get_file(file_id: str, db: DB):
    return svc.get_file(db, file_id)


@router.get("/{file_id}/features", response_model=FeaturesResponse, include_in_schema=False)
@router.get(
    "/{file_id}/features/",
    response_model=FeaturesResponse,
    summary="Extracted features (geometry, CRS, properties)",
    responses={**_not_found, **_not_ready},
)
def get_features(file_id: str, db: DB, limit: Limit = 100, offset: Offset = 0):
    record = svc.get_completed_file(db, file_id)
    items = [
        FeatureOut(
            index=f.idx,
            geometry_type=f.geometry_type,
            geometry=f.geometry,
            crs=record.crs,
            properties=f.properties,
        )
        for f in svc.list_features(db, file_id, limit, offset)
    ]
    return FeaturesResponse(
        file_id=record.id, crs=record.crs, feature_count=record.feature_count,
        limit=limit, offset=offset, items=items,
    )


@router.get("/{file_id}/measurements", response_model=MeasurementsResponse, include_in_schema=False)
@router.get(
    "/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Per-feature measurements",
    description=(
        "Polygons → `area_m2`, lines → `length_m`, points → no measurement. Computed in a "
        "projected CRS chosen per feature (never in degrees). Unsupported geometries are "
        "reported with a status and message instead of failing the request."
    ),
    responses={**_not_found, **_not_ready},
)
def get_measurements(file_id: str, db: DB, limit: Limit = 100, offset: Offset = 0):
    record = svc.get_completed_file(db, file_id)
    items = [
        MeasurementItem(
            index=f.idx,
            geometry_type=f.geometry_type,
            status=f.measurement_status,
            area_m2=f.area_m2,
            length_m=f.length_m,
            projected_crs=f.projected_crs,
            method=f.method,
            geodesic_reference=f.geodesic_reference,
            geometry_repaired=f.geometry_repaired,
            message=f.message,
        )
        for f in svc.list_features(db, file_id, limit, offset)
    ]
    return MeasurementsResponse(
        file_id=record.id,
        crs=record.crs,
        summary=MeasurementSummary(**svc.measurement_summary(db, file_id)),
        limit=limit,
        offset=offset,
        items=items,
    )


@router.delete("/{file_id}", status_code=204, include_in_schema=False)
@router.delete("/{file_id}/", status_code=204, summary="Delete a file and its results", responses=_not_found)
def delete_file(file_id: str, db: DB):
    svc.delete_file(db, file_id)
    return Response(status_code=204)
