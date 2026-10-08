"""Generate sample_data/parcels_utm43n.zip (a Shapefile in a projected CRS, EPSG:32643)."""

import shutil
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon, box

out = Path(__file__).resolve().parent.parent / "sample_data"
x0, y0 = 500_000, 3_170_000  # inside UTM zone 43N (Delhi region)
gdf = gpd.GeoDataFrame(
    {"parcel": ["A", "B", "C"], "owner": ["Asha", "Ravi", "Meera"], "year": [2019, 2021, 2024]},
    geometry=[
        box(x0, y0, x0 + 100, y0 + 100),  # 100 m x 100 m
        box(x0 + 150, y0, x0 + 400, y0 + 200),  # 250 m x 200 m
        Polygon([(x0, y0 + 300), (x0 + 120, y0 + 300), (x0 + 60, y0 + 420)]),
    ],
    crs="EPSG:32643",
)
with tempfile.TemporaryDirectory() as tmp:
    gdf.to_file(Path(tmp) / "parcels.shp")
    shutil.make_archive(str(out / "parcels_utm43n"), "zip", tmp)
print("written", out / "parcels_utm43n.zip")
