"""Reading KML and zipped Shapefiles into a single GeoDataFrame (with a CRS)."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import geopandas as gpd
import pandas as pd
import pyogrio
from pyproj import CRS
from pyproj.exceptions import CRSError

from app.config import Settings
from app.enums import FileType
from app.exceptions import (
    EmptyFileError,
    InvalidArchiveError,
    InvalidUploadError,
    MissingCRSError,
    UnreadableFileError,
)

logger = logging.getLogger(__name__)
_CHUNK = 1024 * 1024


def parse_assume_crs(value: str | None) -> CRS | None:
    if not value:
        return None
    try:
        crs = CRS.from_user_input(value.strip())
    except CRSError as exc:
        raise InvalidUploadError(f"'assume_crs' is not a valid CRS: {value!r}") from exc
    return crs


def read_geodata(path: Path, file_type: FileType, settings: Settings, assume_crs: CRS | None) -> gpd.GeoDataFrame:
    gdf = _read_kml(path) if file_type is FileType.KML else _read_shapefile_zip(path, settings)

    if gdf.crs is None:
        if assume_crs is None:
            raise MissingCRSError(
                "The file has no CRS (missing .prj?). Re-upload with the 'assume_crs' form "
                "field, e.g. assume_crs=EPSG:4326."
            )
        gdf = gdf.set_crs(assume_crs)
    if len(gdf) == 0:
        raise EmptyFileError("The file was read successfully but contains no features.")
    return gdf


# --------------------------------------------------------------------- KML
def _read_kml(path: Path) -> gpd.GeoDataFrame:
    try:
        layers = [row[0] for row in pyogrio.list_layers(path)]
    except Exception as exc:  # noqa: BLE001 - GDAL raises assorted error types
        raise UnreadableFileError(f"Could not read KML: {_short(exc)}") from exc
    gdf = _combine([_read_layer(path, layer, "KML") for layer in layers], layers)
    if gdf.crs is None:  # KML is defined as WGS84 lon/lat
        gdf = gdf.set_crs(4326)
    return _drop_kml_boilerplate(gdf)


# GDAL's KML driver adds a fixed set of columns to every feature. When a file never
# fills them in (all null / driver sentinel) they are noise in the API output.
_KML_SENTINELS: dict[str, object] = {"tessellate": -1, "extrude": 0, "visibility": -1}


def _drop_kml_boilerplate(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    geom_col = gdf.geometry.name
    drop = []
    for col in gdf.columns:
        if col == geom_col or col == "_layer":
            continue
        series = gdf[col]
        if series.isna().all() or (col in _KML_SENTINELS and (series.dropna() == _KML_SENTINELS[col]).all()):
            drop.append(col)
    return gdf.drop(columns=drop)


# ---------------------------------------------------------------- Shapefile
def _read_shapefile_zip(path: Path, settings: Settings) -> gpd.GeoDataFrame:
    with tempfile.TemporaryDirectory(prefix="shp_") as tmp:
        root = Path(tmp)
        _safe_extract(path, root, settings)
        shp_files = sorted(
            p for p in root.rglob("*")
            if p.suffix.lower() == ".shp" and "__MACOSX" not in p.parts and not p.name.startswith(".")
        )
        if not shp_files:
            raise InvalidArchiveError("The ZIP archive does not contain a .shp file.")
        for shp in shp_files:
            siblings = {p.suffix.lower() for p in shp.parent.iterdir() if p.stem == shp.stem}
            missing = {".shx", ".dbf"} - siblings
            if missing:
                raise InvalidArchiveError(
                    f"Shapefile '{shp.name}' is incomplete; missing: {', '.join(sorted(missing))}."
                )
        frames = [_read_layer(p, None, "Shapefile") for p in shp_files]
        return _combine(frames, [p.stem for p in shp_files])


def _safe_extract(zip_path: Path, target: Path, settings: Settings) -> None:
    """Extract defensively: zip-slip, zip-bombs, entry-count and encryption checks."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            entries = [i for i in zf.infolist() if not i.is_dir()]
            if len(entries) > settings.max_zip_entries:
                raise InvalidArchiveError(f"Archive has too many files (limit {settings.max_zip_entries}).")
            if sum(i.file_size for i in entries) > settings.max_uncompressed_bytes:
                raise InvalidArchiveError(
                    f"Archive expands beyond {settings.max_uncompressed_mb} MB (limit exceeded)."
                )
            root = target.resolve()
            total = 0
            for info in entries:
                if info.flag_bits & 0x1:
                    raise InvalidArchiveError("Encrypted ZIP archives are not supported.")
                rel = PurePosixPath(info.filename.replace("\\", "/"))
                if rel.is_absolute() or ".." in rel.parts:
                    raise InvalidArchiveError(f"Unsafe path in archive: {info.filename!r}")
                dest = (root / rel).resolve()
                if not dest.is_relative_to(root):
                    raise InvalidArchiveError(f"Unsafe path in archive: {info.filename!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    while chunk := src.read(_CHUNK):
                        total += len(chunk)  # count real bytes: headers can lie
                        if total > settings.max_uncompressed_bytes:
                            raise InvalidArchiveError("Archive expands beyond the allowed size.")
                        out.write(chunk)
    except zipfile.BadZipFile as exc:
        raise InvalidArchiveError("The ZIP archive is corrupt.") from exc


# ------------------------------------------------------------------ shared
def _read_layer(path: Path, layer: str | None, kind: str) -> gpd.GeoDataFrame:
    try:
        # force_2d: Z/M values are irrelevant for planar area/length and break some ops
        return pyogrio.read_dataframe(path, layer=layer, force_2d=True)
    except Exception as exc:  # noqa: BLE001
        raise UnreadableFileError(f"Could not read {kind}: {_short(exc)}") from exc


def _combine(frames: list[gpd.GeoDataFrame], names: list[str]) -> gpd.GeoDataFrame:
    """Concatenate layers/shapefiles, reprojecting to the first CRS if they differ."""
    pairs = [(f, n) for f, n in zip(frames, names, strict=True) if len(f)]
    if not pairs:
        return frames[0] if frames else gpd.GeoDataFrame(geometry=[])
    if len(frames) > 1:
        for frame, name in pairs:
            frame["_layer"] = name
    base_crs = pairs[0][0].crs
    aligned = []
    for frame, _ in pairs:
        if base_crs is not None and frame.crs is not None and frame.crs != base_crs:
            frame = frame.to_crs(base_crs)
        aligned.append(frame)
    if len(aligned) == 1:
        return aligned[0].reset_index(drop=True)
    merged = pd.concat(aligned, ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=base_crs)


def _short(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:300] or exc.__class__.__name__
