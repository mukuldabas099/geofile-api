import io
import zipfile

import geopandas as gpd
import pytest
from pyproj import Geod

GEOD = Geod(ellps="WGS84")


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_kml_upload_and_full_flow(client, helpers, sample_kml):
    r = helpers["upload"](client, "survey.kml", sample_kml)
    assert r.status_code == 201, r.text
    info = r.json()
    assert info["filename"] == "survey.kml"
    assert info["status"] == "COMPLETED"
    assert info["crs"] == "EPSG:4326"
    assert info["feature_count"] == 5

    fid = info["id"]
    assert client.get(f"/api/files/{fid}/").json()["id"] == fid

    feats = client.get(f"/api/files/{fid}/features/").json()
    assert [f["geometry_type"] for f in feats["items"]] == [
        "Polygon", "LineString", "Point", "GeometryCollection", "None",
    ]
    # GDAL's all-null / sentinel KML boilerplate columns are not leaked into properties
    assert set(feats["items"][0]["properties"]) == {"Name", "description"}
    assert feats["items"][0]["properties"]["Name"] == "Park boundary"
    assert feats["items"][0]["crs"] == "EPSG:4326"
    assert feats["items"][0]["geometry"]["type"] == "Polygon"

    meas = client.get(f"/api/files/{fid}/measurements/").json()
    polygon, line, point, collection, nogeom = meas["items"]
    assert polygon["status"] == "OK" and polygon["area_m2"] == pytest.approx(
        polygon["geodesic_reference"], rel=1e-3
    )
    assert polygon["projected_crs"].startswith("LAEA")
    assert line["status"] == "OK" and line["length_m"] > 1000
    assert line["projected_crs"] == "EPSG:32643"
    assert point["status"] == "NOT_APPLICABLE"
    assert collection["status"] == "OK" and collection["length_m"] > 0 and collection["area_m2"] is None
    assert nogeom["status"] == "UNSUPPORTED" and "empty" in nogeom["message"]
    s = meas["summary"]
    assert (s["total_features"], s["measured"], s["not_applicable"], s["unsupported"]) == (5, 3, 1, 1)
    assert s["total_area_m2"] == pytest.approx(polygon["area_m2"])


def test_pagination(client, helpers, sample_kml):
    fid = helpers["upload"](client, "s.kml", sample_kml).json()["id"]
    page = client.get(f"/api/files/{fid}/measurements/?limit=2&offset=1").json()
    assert [i["index"] for i in page["items"]] == [1, 2]
    assert page["summary"]["total_features"] == 5
    assert client.get(f"/api/files/{fid}/measurements/?limit=0").status_code == 422


def test_shapefile_wgs84(client, helpers, tmp_path):
    gdf = gpd.GeoDataFrame({"name": ["a"], "n": [3]}, geometry=[helpers["box"](77, 28, 77.01, 28.01)], crs=4326)
    r = helpers["upload"](client, "x.zip", helpers["make_zip"](gdf, tmp_path))
    assert r.status_code == 201, r.text
    m = client.get(f"/api/files/{r.json()['id']}/measurements/").json()
    assert m["crs"] == "EPSG:4326"
    assert m["items"][0]["area_m2"] == pytest.approx(m["items"][0]["geodesic_reference"], rel=1e-3)


def test_shapefile_projected_sample(client):
    from pathlib import Path

    data = (Path(__file__).parent.parent / "sample_data" / "parcels_utm43n.zip").read_bytes()
    r = client.post("/api/files/", files={"file": ("parcels.zip", data)})
    assert r.status_code == 201
    assert r.json()["crs"] == "EPSG:32643"
    items = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["items"]
    assert items[0]["area_m2"] == pytest.approx(10_000, rel=5e-3)
    assert items[1]["area_m2"] == pytest.approx(50_000, rel=5e-3)


def test_shapefile_lines_and_points(client, helpers, tmp_path):
    lines = gpd.GeoDataFrame({"id": [1]}, geometry=[helpers["LineString"]([(77, 28), (77.1, 28)])], crs=4326)
    r = helpers["upload"](client, "l.zip", helpers["make_zip"](lines, tmp_path))
    item = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["items"][0]
    assert item["length_m"] == pytest.approx(GEOD.geometry_length(lines.geometry[0]), rel=1e-3)

    pts = gpd.GeoDataFrame({"id": [1]}, geometry=[helpers["Point"](77, 28)], crs=4326)
    r = helpers["upload"](client, "p.zip", helpers["make_zip"](pts, tmp_path / "..") )
    item = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["items"][0]
    assert item["status"] == "NOT_APPLICABLE"


def test_shapefile_missing_prj_requires_assume_crs(client, helpers, tmp_path):
    gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[helpers["box"](77, 28, 77.01, 28.01)], crs=4326)
    data = helpers["make_zip"](gdf, tmp_path, drop_prj=True)
    r = helpers["upload"](client, "noprj.zip", data)
    assert r.status_code == 422 and r.json()["code"] == "missing_crs"
    # the failure is recorded and queryable
    fid = r.json()["file_id"]
    info = client.get(f"/api/files/{fid}/").json()
    assert info["status"] == "FAILED" and "CRS" in info["error_message"]
    assert client.get(f"/api/files/{fid}/measurements/").status_code == 409

    ok = helpers["upload"](client, "noprj.zip", data, assume_crs="EPSG:4326")
    assert ok.status_code == 201 and ok.json()["crs"] == "EPSG:4326"
    assert helpers["upload"](client, "noprj.zip", data, assume_crs="nonsense").status_code == 422


def test_unsupported_extension(client):
    r = client.post("/api/files/", files={"file": ("notes.txt", b"hello")})
    assert r.status_code == 415 and r.json()["code"] == "unsupported_file_type"


def test_empty_file(client):
    assert client.post("/api/files/", files={"file": ("a.kml", b"")}).status_code == 422


def test_fake_kml_and_fake_zip(client):
    assert client.post("/api/files/", files={"file": ("a.kml", b"\x00\x01binary")}).status_code == 422
    assert client.post("/api/files/", files={"file": ("a.zip", b"not a zip")}).status_code == 422


def test_malformed_kml_marks_file_failed(client):
    bad = b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>'
    r = client.post("/api/files/", files={"file": ("bad.kml", bad)})
    assert r.status_code == 422
    assert client.get(f"/api/files/{r.json()['file_id']}/").json()["status"] == "FAILED"


def test_kml_without_features(client, helpers):
    r = helpers["upload"](client, "empty.kml", helpers["kml"]())
    assert r.status_code == 422


def test_zip_without_shapefile(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hi")
    r = client.post("/api/files/", files={"file": ("a.zip", buf.getvalue())})
    assert r.status_code == 422 and r.json()["code"] == "invalid_archive"


def test_zip_missing_sidecars(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.shp", b"\x00" * 100)
    r = client.post("/api/files/", files={"file": ("a.zip", buf.getvalue())})
    assert r.status_code == 422 and ".dbf" in r.json()["detail"]


def test_zip_slip_rejected(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.shp", b"x")
    r = client.post("/api/files/", files={"file": ("a.zip", buf.getvalue())})
    assert r.status_code == 422 and "Unsafe path" in r.json()["detail"]


def test_zip_too_many_entries(client):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(30):
            zf.writestr(f"f{i}.txt", "x")
    r = client.post("/api/files/", files={"file": ("a.zip", buf.getvalue())})
    assert r.status_code == 422 and "too many" in r.json()["detail"]


def test_oversize_upload(client):
    r = client.post("/api/files/", files={"file": ("big.kml", b"<kml>" + b"x" * (2 * 1024 * 1024))})
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


def test_not_found_and_delete(client, helpers, sample_kml):
    assert client.get("/api/files/nope/").status_code == 404
    assert client.get("/api/files/nope/measurements/").status_code == 404
    fid = helpers["upload"](client, "s.kml", sample_kml).json()["id"]
    assert client.delete(f"/api/files/{fid}/").status_code == 204
    assert client.get(f"/api/files/{fid}/").status_code == 404


def test_list_files(client, helpers, sample_kml):
    helpers["upload"](client, "one.kml", sample_kml)
    helpers["upload"](client, "two.kml", sample_kml)
    names = [f["filename"] for f in client.get("/api/files/").json()]
    assert set(names) == {"one.kml", "two.kml"}


def test_filename_is_sanitised_not_used_as_path(client, helpers, sample_kml, settings):
    r = helpers["upload"](client, "../../etc/passwd.kml", sample_kml)
    assert r.status_code == 201 and r.json()["filename"] == "passwd.kml"
    assert not (settings.data_dir.parent / "etc").exists()


def test_openapi_documents_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/files/", "/api/files/{file_id}/", "/api/files/{file_id}/measurements/"} <= set(paths)


def test_kml_nested_folders_and_extended_data(client, helpers):
    kml = (
        b'<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        b'<Folder><name>A</name><Folder><name>inner</name>'
        b'<Placemark><name>one</name><ExtendedData><Data name="owner"><value>Asha</value></Data></ExtendedData>'
        b'<Point><coordinates>77,28,5</coordinates></Point></Placemark></Folder></Folder>'
        b'<Folder><name>B</name><Placemark><name>two</name><Point><coordinates>77.1,28.1</coordinates></Point>'
        b'</Placemark></Folder></Document></kml>'
    )
    r = helpers["upload"](client, "nested.kml", kml)
    assert r.status_code == 201 and r.json()["feature_count"] == 2
    items = client.get(f"/api/files/{r.json()['id']}/features/").json()["items"]
    assert items[0]["properties"]["owner"] == "Asha"
    assert {i["properties"]["_layer"] for i in items} == {"inner", "B"}
    assert items[0]["geometry"]["coordinates"] == [77.0, 28.0]  # Z dropped


def test_antimeridian_polygon_via_api(client, helpers):
    kml = helpers["kml"](
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        "179.9,-17 -179.9,-17 -179.9,-16.9 179.9,-16.9 179.9,-17"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )
    fid = helpers["upload"](client, "fiji.kml", kml).json()["id"]
    item = client.get(f"/api/files/{fid}/measurements/").json()["items"][0]
    assert item["status"] == "OK"
    assert item["area_m2"] == pytest.approx(item["geodesic_reference"], rel=1e-3)
    assert "antimeridian" in item["message"]


def test_bulk_insert_crosses_batch_boundary(client, helpers, tmp_path, monkeypatch):
    from app.services import processing

    monkeypatch.setattr(processing, "_INSERT_BATCH", 7)
    boxes = [helpers["box"](77 + i * 0.01, 28, 77 + i * 0.01 + 0.005, 28.005) for i in range(20)]
    gdf = gpd.GeoDataFrame({"n": range(20)}, geometry=boxes, crs=4326)
    r = helpers["upload"](client, "many.zip", helpers["make_zip"](gdf, tmp_path))
    assert r.json()["feature_count"] == 20
    page = client.get(f"/api/files/{r.json()['id']}/measurements/?limit=100").json()
    assert [i["index"] for i in page["items"]] == list(range(20))
    assert page["summary"]["measured"] == 20


def test_slashless_urls_work_without_redirect(client, helpers, sample_kml):
    r = client.post("/api/files", files={"file": ("s.kml", sample_kml)}, follow_redirects=False)
    assert r.status_code == 201
    fid = r.json()["id"]
    for path in (f"/api/files/{fid}", f"/api/files/{fid}/measurements", f"/api/files/{fid}/features", "/api/files"):
        assert client.get(path, follow_redirects=False).status_code == 200, path
    assert client.delete(f"/api/files/{fid}", follow_redirects=False).status_code == 204


def test_timestamps_are_timezone_aware_on_every_endpoint(client, helpers, sample_kml):
    created = helpers["upload"](client, "s.kml", sample_kml).json()
    fetched = client.get(f"/api/files/{created['id']}/").json()
    assert created["created_at"].endswith("Z") and fetched["created_at"].endswith("Z")
    assert fetched["completed_at"].endswith("Z")
