"""
Step 4: Build a lightweight map file of New Jersey ZIP area boundaries.

Downloads the Census cartographic boundary file for ZIP Code Tabulation Areas,
keeps only NJ areas in our analysis, simplifies the shapes so the file stays
small enough for a web dashboard, and writes data/processed/nj_zcta.geojson.

Run from the project root (after 02_transform.py):
    python src/04_geo.py
"""

from pathlib import Path
import sys

import duckdb
import geopandas as gpd
import requests
import shapely

URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip"
RAW = Path("data/raw/cb_2020_us_zcta520_500k.zip")
DB_PATH = Path("data/nj_gap.duckdb")
OUT = Path("data/processed/nj_zcta.geojson")

SIMPLIFY_DEGREES = 0.0005   # about 50 meters; invisible at state or county zoom
PRECISION = 0.00001         # round coordinates to about 1 meter


def main() -> None:
    if not RAW.exists():
        print("Downloading ZIP area boundaries (about 60 MB, one time)...")
        r = requests.get(URL, timeout=600,
                         headers={"User-Agent": "the-next-dollar-nj portfolio project"})
        r.raise_for_status()
        RAW.parent.mkdir(parents=True, exist_ok=True)
        RAW.write_bytes(r.content)

    if not DB_PATH.exists():
        sys.exit("Database not found. Run the earlier scripts first.")
    nj = duckdb.connect(str(DB_PATH), read_only=True).execute(
        "SELECT zcta FROM mart_zcta").df()

    gdf = gpd.read_file(f"zip://{RAW}")
    zcol = "ZCTA5CE20" if "ZCTA5CE20" in gdf.columns else "GEOID20"
    g = gdf[gdf[zcol].isin(nj["zcta"])][[zcol, "geometry"]].rename(columns={zcol: "zcta"})
    g = g.to_crs(4326)
    g["geometry"] = g.geometry.simplify(SIMPLIFY_DEGREES, preserve_topology=True)
    g["geometry"] = shapely.set_precision(g.geometry.values, PRECISION)
    g = g[~g.geometry.is_empty]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(g.to_json())
    size_mb = OUT.stat().st_size / 1e6
    print(f"Wrote {len(g)} of {len(nj)} NJ ZIP areas to {OUT} ({size_mb:.1f} MB)")
    missing = sorted(set(nj["zcta"]) - set(g["zcta"]))
    if missing:
        print(f"No boundary found for {len(missing)} ZIP areas (shown as missing): "
              f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")


if __name__ == "__main__":
    main()
