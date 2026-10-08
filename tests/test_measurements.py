import pytest
from pyproj import CRS, Geod
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Point,
    Polygon,
    box,
)

from app.enums import MeasurementStatus
from app.services.measurements import MeasurementCalculator, choose_length_projection

WGS84 = CRS.from_epsg(4326)
GEOD = Geod(ellps="WGS84")


@pytest.fixture
def calc():
    return MeasurementCalculator(WGS84)


def geodesic_area(poly):
    return abs(GEOD.geometry_area_perimeter(poly)[0])


@pytest.mark.parametrize("lat", [0.0, 28.6, 60.0, 75.0])
def test_polygon_area_is_not_computed_in_degrees(calc, lat):
    poly = box(10.0, lat, 10.01, lat + 0.01)
    m = calc.measure(poly)
    assert m.status is MeasurementStatus.OK
    assert m.area_m2 == pytest.approx(geodesic_area(poly), rel=1e-3)
    assert m.area_m2 > 1_000  # 0.0001 "degree squared" would be absurdly small


def test_area_shrinks_with_latitude_for_same_degree_box(calc):
    equator = calc.measure(box(0, 0, 0.1, 0.1)).area_m2
    far_north = calc.measure(box(0, 60, 0.1, 60.1)).area_m2
    assert far_north < equator * 0.55  # cos(60deg) ~ 0.5


def test_line_length_matches_geodesic(calc):
    line = LineString([(77.0, 28.0), (77.1, 28.0), (77.1, 28.1)])
    m = calc.measure(line)
    assert m.status is MeasurementStatus.OK
    assert m.length_m == pytest.approx(GEOD.geometry_length(line), rel=1e-3)
    assert m.projected_crs == "EPSG:32643"


def test_polygon_with_hole_subtracts_hole(calc):
    donut = Polygon(box(0, 0, 0.02, 0.02).exterior.coords, [box(0.005, 0.005, 0.015, 0.015).exterior.coords])
    full = calc.measure(box(0, 0, 0.02, 0.02)).area_m2
    m = calc.measure(donut)
    assert m.area_m2 == pytest.approx(full * 0.75, rel=1e-3)
    assert m.geodesic_reference == pytest.approx(m.area_m2, rel=1e-3)


def test_multipolygon_sums_parts(calc):
    a, b = box(0, 0, 0.01, 0.01), box(1, 1, 1.01, 1.01)
    m = calc.measure(MultiPolygon([a, b]))
    assert m.area_m2 == pytest.approx(calc.measure(a).area_m2 + calc.measure(b).area_m2, rel=1e-3)


def test_projected_source_crs_measures_true_ground_area():
    calc = MeasurementCalculator(CRS.from_epsg(32643))
    m = calc.measure(box(500_000, 3_170_000, 500_100, 3_170_100))
    assert m.area_m2 == pytest.approx(10_000, rel=5e-3)


def test_web_mercator_does_not_inflate_area():
    # 1 km x 1 km ground square at 60N is ~4x bigger in Mercator grid units.
    calc = MeasurementCalculator(CRS.from_epsg(3857))
    from pyproj import Transformer

    t = Transformer.from_crs(4326, 3857, always_xy=True)
    x, y = t.transform(10.0, 60.0)
    m = calc.measure(box(x, y, x + 2000, y + 2000))  # 2 km grid ~ 1 km ground
    assert m.area_m2 == pytest.approx(1_000_000, rel=0.05)


def test_points_have_no_measurement(calc):
    m = calc.measure(Point(1, 1))
    assert m.status is MeasurementStatus.NOT_APPLICABLE
    assert m.area_m2 is None and m.length_m is None


@pytest.mark.parametrize("geom", [None, Point(), GeometryCollection(), Polygon()])
def test_unsupported_geometries_do_not_raise(calc, geom):
    m = calc.measure(geom)
    assert m.status is MeasurementStatus.UNSUPPORTED
    assert m.message


def test_invalid_bowtie_is_repaired(calc):
    bowtie = Polygon([(0, 0), (0.01, 0.01), (0.01, 0), (0, 0.01)])
    assert not bowtie.is_valid
    m = calc.measure(bowtie)
    assert m.status is MeasurementStatus.OK
    assert m.geometry_repaired and m.area_m2 > 0


def test_out_of_range_coordinates_fail_gracefully(calc):
    m = calc.measure(LineString([(0, 0), (500, 500)]))
    assert m.status is MeasurementStatus.FAILED


def test_polar_length_uses_fallback_projection():
    assert choose_length_projection(10, 89).label.startswith("AEQD")
    assert choose_length_projection(-179.99, -45).label == "EPSG:32701"
    assert choose_length_projection(180.0, 10).label == "EPSG:32601"  # 180 == -180, zone 1


def test_geometry_collection_is_decomposed(calc):
    poly, line = box(77, 28, 77.01, 28.01), LineString([(77, 28), (77.01, 28)])
    m = calc.measure(GeometryCollection([poly, Point(77, 28), line]))
    assert m.status is MeasurementStatus.OK
    assert m.area_m2 == pytest.approx(calc.measure(poly).area_m2)
    assert m.length_m == pytest.approx(calc.measure(line).length_m)
    assert m.geodesic_reference is None  # one number cannot describe both quantities
    assert "Point parts" in m.message


def test_collection_of_only_points_is_not_applicable(calc):
    m = calc.measure(GeometryCollection([Point(0, 0), Point(1, 1)]))
    assert m.status is MeasurementStatus.NOT_APPLICABLE


def test_multilinestring_sums_parts(calc):
    a, b = LineString([(77, 28), (77.01, 28)]), LineString([(77, 28.1), (77.01, 28.1)])
    m = calc.measure(MultiLineString([a, b]))
    assert m.length_m == pytest.approx(calc.measure(a).length_m + calc.measure(b).length_m, rel=1e-6)


def test_antimeridian_polygon_matches_geodesic(calc):
    fiji = Polygon([(179.9, -17), (-179.9, -17), (-179.9, -16.9), (179.9, -16.9)])
    m = calc.measure(fiji)
    assert m.status is MeasurementStatus.OK
    true_area = abs(GEOD.polygon_area_perimeter([179.9, 180.1, 180.1, 179.9], [-17, -17, -16.9, -16.9])[0])
    assert m.area_m2 == pytest.approx(true_area, rel=1e-3)
    assert "antimeridian" in m.message


def test_antimeridian_line(calc):
    line = LineString([(179.95, -17), (-179.95, -17)])  # 0.1 deg of longitude, not 359.9
    m = calc.measure(line)
    assert m.status is MeasurementStatus.OK
    assert m.length_m == pytest.approx(10_650, rel=5e-3)


def test_genuinely_global_feature_still_fails_gracefully(calc):
    m = calc.measure(LineString([(-170, 0), (0, 0), (170, 0)]))  # 340 deg even after unwrapping
    assert m.status is MeasurementStatus.FAILED


def test_geographic_and_projected_sources_agree():
    """Same ground polygon expressed in EPSG:4326 and EPSG:32643 must measure the same."""
    from pyproj import Transformer
    from shapely.ops import transform

    wgs = box(77.0, 28.0, 77.01, 28.01)
    utm = transform(Transformer.from_crs(4326, 32643, always_xy=True).transform, wgs)
    a = MeasurementCalculator(WGS84).measure(wgs).area_m2
    b = MeasurementCalculator(CRS.from_epsg(32643)).measure(utm).area_m2
    assert a == pytest.approx(b, rel=1e-6)
