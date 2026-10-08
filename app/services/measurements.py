"""CRS-aware geometry measurement.

Strategy
--------
Measuring directly in degrees is meaningless, and a projected CRS supplied by the
file is not guaranteed to preserve area/distance (e.g. Web Mercator inflates area
enormously at high latitudes). So every feature follows the same path:

    source CRS --> WGS84 lon/lat --> local projected CRS chosen per feature --> measure

* Area   -> Lambert Azimuthal Equal Area centred on the feature (area-preserving).
* Length -> UTM zone of the feature (conformal, <0.1 % scale error inside a zone);
            azimuthal equidistant centred on the feature outside UTM's range (polar).

A WGS84 ellipsoidal (geodesic) value is computed alongside as an independent
reference so consumers can see how close the projected result is.

Mixed geometries (``GeometryCollection``, i.e. KML ``MultiGeometry``) are decomposed:
polygon parts contribute area, line parts contribute length, point parts are ignored.
Features crossing the antimeridian (180° longitude) are unwrapped before projecting.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import shapely
from pyproj import CRS, Geod, Transformer
from shapely import make_valid
from shapely.geometry import MultiLineString, MultiPolygon
from shapely.geometry.base import BaseGeometry

from app.enums import MeasurementStatus

logger = logging.getLogger(__name__)

_WGS84 = CRS.from_epsg(4326)
_GEOD = Geod(ellps="WGS84")

POLYGONAL = frozenset({"Polygon", "MultiPolygon"})
LINEAR = frozenset({"LineString", "MultiLineString", "LinearRing"})
POINTLIKE = frozenset({"Point", "MultiPoint"})


@dataclass(frozen=True)
class Projection:
    proj: str  # anything accepted by CRS.from_user_input
    label: str
    method: str


@dataclass(frozen=True)
class Measurement:
    status: MeasurementStatus
    area_m2: float | None = None
    length_m: float | None = None
    projected_crs: str | None = None
    method: str | None = None
    geodesic_reference: float | None = None
    geometry_repaired: bool = False
    message: str | None = None


# ----------------------------------------------------------------- CRS helpers
@lru_cache(maxsize=1024)
def _transformer_from_wgs84(proj: str) -> Transformer:
    return Transformer.from_crs(_WGS84, CRS.from_user_input(proj), always_xy=True)


def crs_label(crs: CRS) -> str:
    """Short human label, e.g. ``EPSG:4326``; falls back to the CRS name."""
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else (crs.name or "UNKNOWN")


def normalise_lon(lon: float) -> float:
    """Wrap a longitude into [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def choose_area_projection(lon: float, lat: float) -> Projection:
    """Equal-area projection centred (snapped to 0.5 deg for transformer caching)."""
    lat0, lon0 = round(lat * 2) / 2, round(normalise_lon(lon) * 2) / 2
    return Projection(
        proj=f"+proj=laea +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
        label=f"LAEA(lat_0={lat0}, lon_0={lon0})",
        method="equal-area projection (Lambert Azimuthal Equal Area)",
    )


def choose_length_projection(lon: float, lat: float) -> Projection:
    lon = normalise_lon(lon)
    if -80.0 <= lat <= 84.0:  # UTM's valid latitude band
        zone = min(60, int((lon + 180.0) // 6) + 1)
        epsg = (32600 if lat >= 0 else 32700) + zone
        return Projection(f"EPSG:{epsg}", f"EPSG:{epsg}", f"UTM zone {zone}{'N' if lat >= 0 else 'S'}")
    lat0, lon0 = round(lat * 2) / 2, round(lon * 2) / 2
    return Projection(
        proj=f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs",
        label=f"AEQD(lat_0={lat0}, lon_0={lon0})",
        method="azimuthal equidistant (polar region, outside UTM range)",
    )


# ------------------------------------------------------------ vectorised ops
def _apply(geom: BaseGeometry, fn: Callable[[np.ndarray], np.ndarray]) -> BaseGeometry:
    """Apply ``fn`` to the (N, 2) coordinate array of ``geom`` (vectorised, ~2x faster
    than ``shapely.ops.transform``, which falls back to a per-vertex Python loop)."""
    return shapely.transform(geom, fn)


def _reproject(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    return _apply(geom, lambda c: np.column_stack(transformer.transform(c[:, 0], c[:, 1])))


def _unwrap_antimeridian(geom: BaseGeometry) -> BaseGeometry:
    """Shift western-hemisphere longitudes by +360 so a feature straddling 180° is contiguous."""

    def shift(coords: np.ndarray) -> np.ndarray:
        out = coords.copy()
        out[out[:, 0] < 0, 0] += 360.0
        return out

    return _apply(geom, shift)


# ------------------------------------------------------------ geometry helpers
def _atoms(geom: BaseGeometry) -> Iterator[BaseGeometry]:
    """Yield the non-collection building blocks (Polygon / LineString / Point / ...)."""
    if geom.geom_type in {"MultiPolygon", "MultiLineString", "MultiPoint", "GeometryCollection"}:
        for part in geom.geoms:
            yield from _atoms(part)
    else:
        yield geom


def _geodesic_polygon_area(poly: BaseGeometry) -> float:
    """Ellipsoidal area of a polygon, with interior rings (holes) subtracted."""

    def ring_area(ring) -> float:
        xs, ys = ring.xy
        return abs(_GEOD.polygon_area_perimeter(xs, ys)[0])

    return ring_area(poly.exterior) - sum(ring_area(r) for r in poly.interiors)


def _geodesic_area(polys: list[BaseGeometry]) -> float | None:
    try:
        return sum(_geodesic_polygon_area(p) for p in polys)
    except Exception:  # noqa: BLE001 - purely informational
        return None


def _geodesic_length(lines: list[BaseGeometry]) -> float | None:
    try:
        return sum(_GEOD.geometry_length(line) for line in lines)
    except Exception:  # noqa: BLE001
        return None


def _check_wgs84(bounds: tuple[float, float, float, float]) -> None:
    if not all(math.isfinite(v) for v in bounds):
        raise ValueError("coordinates could not be transformed to WGS84")
    minx, miny, maxx, maxy = bounds
    if not (minx >= -180.0 and maxx <= 180.0 and miny >= -90.0 and maxy <= 90.0):
        raise ValueError("coordinates fall outside valid longitude/latitude ranges")


class MeasurementCalculator:
    """Measures geometries that all share one source CRS."""

    def __init__(self, source_crs: CRS):
        self.source_crs = source_crs
        self._to_wgs84: Transformer | None = None
        if not source_crs.equals(_WGS84, ignore_axis_order=True):
            self._to_wgs84 = Transformer.from_crs(source_crs, _WGS84, always_xy=True)

    # -- public ---------------------------------------------------------
    def measure(self, geom: BaseGeometry | None) -> Measurement:
        """Never raises: any failure is reported through ``Measurement.status``."""
        try:
            return self._measure(geom)
        except Exception as exc:  # noqa: BLE001 - one bad feature must not sink the file
            logger.warning("Measurement failed: %s", exc)
            return Measurement(MeasurementStatus.FAILED, message=f"Measurement failed: {exc}")

    # -- internals ------------------------------------------------------
    def _measure(self, geom: BaseGeometry | None) -> Measurement:
        if geom is None or geom.is_empty:
            return Measurement(MeasurementStatus.UNSUPPORTED, message="Missing or empty geometry")

        polys: list[BaseGeometry] = []
        lines: list[BaseGeometry] = []
        has_points = False
        unknown: set[str] = set()
        for atom in _atoms(geom):
            if atom.is_empty:
                continue
            if atom.geom_type == "Polygon":
                polys.append(atom)
            elif atom.geom_type in LINEAR:
                lines.append(atom)
            elif atom.geom_type in POINTLIKE:
                has_points = True
            else:
                unknown.add(atom.geom_type)

        if not polys and not lines:
            if unknown:
                names = ", ".join(sorted(unknown))
                return Measurement(
                    MeasurementStatus.UNSUPPORTED,
                    message=f"Geometry type '{names}' is not supported for measurement",
                )
            if has_points:
                return Measurement(
                    MeasurementStatus.NOT_APPLICABLE, message="Points have no area or length"
                )
            return Measurement(MeasurementStatus.UNSUPPORTED, message="Missing or empty geometry")

        notes: list[str] = []
        repaired = False
        valid_polys: list[BaseGeometry] = []
        for poly in polys:
            if poly.is_valid:
                valid_polys.append(poly)
            else:  # e.g. self-intersecting "bow-tie" rings
                repaired = True
                valid_polys.extend(p for p in _atoms(make_valid(poly)) if p.geom_type == "Polygon")
        if repaired:
            notes.append("Invalid geometry was repaired before measuring")

        poly_geom = MultiPolygon(valid_polys) if valid_polys else None
        line_geom = (
            MultiLineString([shapely.LineString(ln.coords) if ln.geom_type == "LinearRing" else ln for ln in lines])
            if lines
            else None
        )

        # Source CRS -> WGS84 lon/lat
        if self._to_wgs84 is not None:
            poly_geom = _reproject(poly_geom, self._to_wgs84) if poly_geom is not None else None
            line_geom = _reproject(line_geom, self._to_wgs84) if line_geom is not None else None
        parts = [g for g in (poly_geom, line_geom) if g is not None]
        if not parts:  # every polygon degenerated to nothing during repair
            return Measurement(
                MeasurementStatus.UNSUPPORTED,
                geometry_repaired=repaired,
                message="Geometry has no valid polygon or line component after repair",
            )
        for g in parts:
            _check_wgs84(g.bounds)

        # Features straddling the antimeridian: make them contiguous before projecting.
        minx = min(g.bounds[0] for g in parts)
        maxx = max(g.bounds[2] for g in parts)
        if maxx - minx > 180.0:
            poly_geom = _unwrap_antimeridian(poly_geom) if poly_geom is not None else None
            line_geom = _unwrap_antimeridian(line_geom) if line_geom is not None else None
            parts = [g for g in (poly_geom, line_geom) if g is not None]
            if max(g.bounds[2] for g in parts) - min(g.bounds[0] for g in parts) > 180.0:
                raise ValueError("feature spans more than 180 degrees of longitude")
            notes.append("Feature crosses the antimeridian; longitudes were unwrapped")

        area = length = None
        labels: list[str] = []
        methods: list[str] = []
        geodesic: float | None = None

        if poly_geom is not None:
            minx, miny, maxx, maxy = poly_geom.bounds
            proj = choose_area_projection((minx + maxx) / 2.0, (miny + maxy) / 2.0)
            projected = _reproject(poly_geom, _transformer_from_wgs84(proj.proj))
            area = float(projected.area)
            labels.append(proj.label)
            methods.append(proj.method)
            geodesic = _geodesic_area(list(_atoms(poly_geom)))

        if line_geom is not None:
            minx, miny, maxx, maxy = line_geom.bounds
            proj = choose_length_projection((minx + maxx) / 2.0, (miny + maxy) / 2.0)
            projected = _reproject(line_geom, _transformer_from_wgs84(proj.proj))
            length = float(projected.length)
            labels.append(proj.label)
            methods.append(proj.method)
            geodesic = _geodesic_length(list(_atoms(line_geom)))

        if area is not None and length is not None:
            geodesic = None  # one reference value cannot describe both quantities
            notes.append("Mixed geometry: area from polygon parts, length from line parts")
        if has_points and (polys or lines) and geom.geom_type == "GeometryCollection":
            notes.append("Point parts have no measurement and were ignored")

        return Measurement(
            MeasurementStatus.OK,
            area_m2=area,
            length_m=length,
            projected_crs=" + ".join(labels),
            method="; ".join(methods),
            geodesic_reference=geodesic,
            geometry_repaired=repaired,
            message="; ".join(notes) or None,
        )
