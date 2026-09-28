"""
Step 1: Ingest raw data into DuckDB.

Downloads (and caches) four public sources, loads them untouched into
DuckDB as raw_* tables, then prints a validation report.
No cleaning happens here -- that's Step 2.

Run from the project root:
    python src/01_ingest.py
"""

from pathlib import Path
import os
import sys

import duckdb
import pandas as pd
import requests

RAW_DIR = Path("data/raw")
DB_PATH = Path("data/nj_gap.duckdb")
HEADERS = {"User-Agent": "nj-service-deserts portfolio project (research use)"}

ACS_YEAR = 2023  # bump to the newest ACS 5-year release once you confirm it exists

SOURCES = {
    # IRS Exempt Organizations Business Master File, New Jersey
    "bmf_nj.csv": "https://www.irs.gov/pub/irs-soi/eo_nj.csv",
    # HRSA Mental Health Professional Shortage Areas (national)
    "hpsa_mh.csv": "https://data.hrsa.gov/DataDownload/DD_Files/BCD_HPSA_FCT_DET_MH.csv",
    # Census 2020 ZCTA-to-county relationship file (national, pipe-delimited)
    "zcta_county.txt": (
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/"
        "zcta520/tab20_zcta520_county20_natl.txt"
    ),
}

# ACS variables. Labels are checked against the Census API at runtime,
# so if a code is wrong you'll see it in the report instead of silently
# analyzing the wrong column.
ACS_DETAILED = {
    "B09001_001E": "children_under_18",
    "B19013_001E": "median_household_income",
    "B27010_002E": "pop_under_19",
    "B27010_007E": "under_19_medicaid_only",
    "B27010_017E": "under_19_uninsured",
}
ACS_SUBJECT = {
    "S1701_C03_002E": "pct_children_in_poverty",
}


def download(name: str, url: str) -> Path:
    """Download a file once; reuse the cached copy on later runs."""
    path = RAW_DIR / name
    if path.exists() and path.stat().st_size > 0:
        print(f"  cached   {name}")
        return path
    print(f"  fetching {name} ...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=180)
        r.raise_for_status()
    except requests.RequestException as e:
        sys.exit(
            f"\nCould not download {name}: {e}\n"
            f"Download it manually from {url}\n"
            f"and save it as {path}, then re-run."
        )
    path.write_bytes(r.content)
    return path


CENSUS_KEY = os.environ.get("CENSUS_API_KEY")


def census_get(url: str, params: dict | None = None):
    """Call the Census API and show its actual message if it doesn't return JSON."""
    params = dict(params or {})
    if CENSUS_KEY:
        params["key"] = CENSUS_KEY
    r = requests.get(url, params=params, headers=HEADERS, timeout=180)
    try:
        return r.json()
    except ValueError:
        sys.exit(
            f"\nCensus API did not return data (HTTP {r.status_code}).\n"
            f"It said:\n{r.text[:800]}\n\n"
            "If this mentions a key or limit, get a free key at "
            "https://api.census.gov/data/key_signup.html and run:\n"
            "  export CENSUS_API_KEY=your_key_here\n"
            "then re-run the script."
        )


def fetch_acs(dataset: str, variables: dict) -> pd.DataFrame:
    """Pull ACS variables for every ZCTA nationally (ZCTAs no longer nest in states)."""
    base = f"https://api.census.gov/data/{ACS_YEAR}/{dataset}"
    params = {
        "get": ",".join(["NAME", *variables.keys()]),
        "for": "zip code tabulation area:*",
    }
    rows = census_get(base, params)
    df = pd.DataFrame(rows[1:], columns=rows[0])
    return df.rename(columns={"zip code tabulation area": "zcta", **variables})


def check_acs_labels(dataset: str, variables: dict) -> None:
    """Print the official label for each variable so you can confirm it's right."""
    url = f"https://api.census.gov/data/{ACS_YEAR}/{dataset}/variables.json"
    meta = census_get(url)["variables"]
    for code, alias in variables.items():
        label = meta.get(code, {}).get("label", "!! NOT FOUND -- fix this code !!")
        print(f"  {code:<16} -> {alias:<26} | {label}")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading files:")
    paths = {name: download(name, url) for name, url in SOURCES.items()}

    print("\nPulling ACS from the Census API:")
    acs_d = fetch_acs("acs/acs5", ACS_DETAILED)
    acs_s = fetch_acs("acs/acs5/subject", ACS_SUBJECT)
    acs = acs_d.merge(acs_s.drop(columns="NAME"), on="zcta", how="left")
    acs.to_csv(RAW_DIR / "acs_zcta.csv", index=False)

    # NJ ZCTAs = any ZCTA that overlaps an NJ county (state FIPS 34)
    xwalk = pd.read_csv(paths["zcta_county.txt"], sep="|", dtype=str)

    con = duckdb.connect(str(DB_PATH))
    con.register("bmf_df", pd.read_csv(paths["bmf_nj.csv"], dtype=str))
    con.register("hpsa_df", pd.read_csv(paths["hpsa_mh.csv"], dtype=str, low_memory=False))
    con.register("xwalk_df", xwalk)
    con.register("acs_df", acs)

    con.execute("CREATE OR REPLACE TABLE raw_bmf AS SELECT * FROM bmf_df")
    con.execute("CREATE OR REPLACE TABLE raw_hpsa_mh AS SELECT * FROM hpsa_df")
    con.execute(
        "CREATE OR REPLACE TABLE raw_zcta_county AS "
        "SELECT * FROM xwalk_df WHERE GEOID_COUNTY_20 LIKE '34%' "
        "AND GEOID_ZCTA5_20 IS NOT NULL"
    )
    con.execute(
        "CREATE OR REPLACE TABLE raw_acs AS SELECT * FROM acs_df "
        "WHERE zcta IN (SELECT DISTINCT GEOID_ZCTA5_20 FROM raw_zcta_county)"
    )

    # ---------------- Validation report ----------------
    print("\n=== ROW COUNTS ===")
    for t in ["raw_bmf", "raw_hpsa_mh", "raw_zcta_county", "raw_acs"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<18} {n:>8,}")

    print("\n=== ACS VARIABLE LABELS (confirm these match the alias) ===")
    check_acs_labels("acs/acs5", ACS_DETAILED)
    check_acs_labels("acs/acs5/subject", ACS_SUBJECT)

    print("\n=== HPSA COLUMNS (paste these back for Step 2) ===")
    print("  " + ", ".join(c[0] for c in con.execute("DESCRIBE raw_hpsa_mh").fetchall()))

    print("\n=== NJ NONPROFITS BY MAJOR NTEE CATEGORY (top 10) ===")
    print(con.execute("""
        SELECT LEFT(NTEE_CD, 1) AS ntee_major, COUNT(*) AS orgs
        FROM raw_bmf GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """).df().to_string(index=False))

    print("\n=== CANDIDATE ORGS: mental health (F) or autism/developmental keywords ===")
    print(con.execute("""
        SELECT COUNT(*) AS candidates,
               SUM(CASE WHEN NTEE_CD LIKE 'F%' THEN 1 ELSE 0 END) AS ntee_f,
               SUM(CASE WHEN regexp_matches(UPPER(NAME),
                   'AUTIS|DEVELOPMENTAL|BEHAVIORAL|CHILD GUIDANCE') THEN 1 ELSE 0 END) AS keyword_hits
        FROM raw_bmf
        WHERE NTEE_CD LIKE 'F%'
           OR regexp_matches(UPPER(NAME), 'AUTIS|DEVELOPMENTAL|BEHAVIORAL|CHILD GUIDANCE')
    """).df().to_string(index=False))

    con.close()
    print(f"\nDone. Database saved to {DB_PATH}")


if __name__ == "__main__":
    main()
