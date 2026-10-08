import io
import os
import tempfile
import zipfile
from pathlib import Path

# Must be set before `app.main` is imported (it builds a default app at import time).
os.environ.setdefault("GEOAPI_DATA_DIR", tempfile.mkdtemp(prefix="geoapi_import_"))

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, box

from app.config import Settings
from app.main import create_app

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", max_upload_mb=1, max_zip_entries=20)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def make_shapefile_zip(gdf: gpd.GeoDataFrame, tmp_path: Path, drop_prj: bool = False) -> bytes:
    folder = tmp_path / "shp"
    folder.mkdir(exist_ok=True)
    gdf.to_file(folder / "layer.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in folder.iterdir():
            if drop_prj and f.suffix == ".prj":
                continue
            zf.write(f, f.name)
    return buf.getvalue()


def kml_bytes(*placemarks: str) -> bytes:
    body = "".join(f"<Placemark><name>p{i}</name>{p}</Placemark>" for i, p in enumerate(placemarks))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{body}</Document></kml>'
    ).encode()


def upload(client, name: str, content: bytes, **form):
    return client.post("/api/files/", files={"file": (name, content)}, data=form)


@pytest.fixture
def sample_kml() -> bytes:
    return (SAMPLE / "sample.kml").read_bytes()


@pytest.fixture
def helpers():
    return {"make_zip": make_shapefile_zip, "kml": kml_bytes, "upload": upload,
            "box": box, "Point": Point, "LineString": LineString}
