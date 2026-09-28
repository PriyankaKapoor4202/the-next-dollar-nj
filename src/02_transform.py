"""
Step 2: Clean, standardize, and join the raw tables.

Builds (in DuckDB):
  dim_zcta          one row per NJ ZIP area, with main county and center point
  stg_acs           cleaned Census measures (sentinel codes -> NULL, shares computed)
  stg_orgs          relevant nonprofits (mental health / autism / developmental)
  stg_hpsa_zcta     share of each ZIP area inside a designated mental health shortage area
  mart_zcta         the analysis table: one row per ZIP area, need + supply side by side

Then prints a data quality report and exports mart_zcta to CSV.

Run from the project root (after 01_ingest.py):
    python src/02_transform.py
"""

from pathlib import Path
import sys

import duckdb
import pandas as pd

DB_PATH = Path("data/nj_gap.duckdb")
OUT_DIR = Path("data/processed")

MIN_CHILDREN = 100       # below this, rates are too noisy to trust
MIN_NJ_LAND_SHARE = 0.5  # drop ZCTAs that are mostly in NY/PA/DE

# \b = word boundary, so AUTIS matches "AUTISM" but not "BAUTISTA" (Spanish: Baptist)
KEYWORDS = r"\bAUTIS|\bDEVELOPMENTAL|\bBEHAVIORAL|\bCHILD GUIDANCE"
# Names that match a keyword but aren't service providers
EXCLUDE_NAMES = r"ECONOMICS"

# HRSA designates some shortage areas as whole towns ("county subdivisions").
# There's no Census town-to-ZCTA file, so these are mapped by hand to the
# ZIP codes serving each town. Any town missing here is reported below.
# Woodbridge spans several postal communities; 07067 (Colonia) and 08830 (Iselin)
# also include small parts of neighboring towns, so this is an approximation.
SUBDIVISION_ZCTAS = {
    "Carteret borough": ["07008"],
    "Rahway city": ["07065"],
    "Perth Amboy city": ["08861"],
    "Woodbridge township": ["07001", "07064", "07067", "07077", "07095",
                            "08830", "08832", "08863"],
}


def clean_num(col: str) -> str:
    """Census uses large negative codes (e.g. -666666666) for 'not available'."""
    return f"CASE WHEN TRY_CAST({col} AS DOUBLE) >= 0 THEN TRY_CAST({col} AS DOUBLE) END"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    if not DB_PATH.exists():
        sys.exit("Database not found. Run python src/01_ingest.py first.")
    con = duckdb.connect(str(DB_PATH))
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if not {"raw_zcta_tract", "raw_zcta_centroids"} <= tables:
        sys.exit("Missing new raw tables. Re-run python src/01_ingest.py first.")

    # ------------------------------------------------------------------
    # 1. ZIP areas: keep ZCTAs mostly inside NJ, assign each to the county
    #    holding the largest share of its land area, attach center point.
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE dim_zcta AS
        WITH parts AS (
            SELECT GEOID_ZCTA5_20                       AS zcta,
                   GEOID_COUNTY_20                      AS county_fips,
                   NAMELSAD_COUNTY_20                   AS county_name,
                   TRY_CAST(AREALAND_PART AS DOUBLE)     AS land_part,
                   TRY_CAST(AREALAND_ZCTA5_20 AS DOUBLE) AS land_total
            FROM raw_zcta_county
        ),
        ranked AS (
            SELECT *,
                   SUM(land_part) OVER (PARTITION BY zcta) / NULLIF(land_total, 0) AS nj_land_share,
                   ROW_NUMBER() OVER (PARTITION BY zcta ORDER BY land_part DESC)  AS rn
            FROM parts
        )
        SELECT r.zcta, r.county_fips, r.county_name,
               ROUND(r.nj_land_share, 3) AS nj_land_share,
               c.lat, c.lon
        FROM ranked r
        LEFT JOIN raw_zcta_centroids c ON c.zcta = r.zcta
        WHERE r.rn = 1 AND r.nj_land_share >= {MIN_NJ_LAND_SHARE}
    """)

    # ------------------------------------------------------------------
    # 2. Census measures
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE stg_acs AS
        WITH c AS (
            SELECT zcta,
                   {clean_num('children_under_18')}       AS children,
                   {clean_num('pct_children_in_poverty')} AS pct_children_in_poverty,
                   {clean_num('median_household_income')} AS median_household_income,
                   {clean_num('pop_under_19')}            AS pop_under_19,
                   {clean_num('under_19_medicaid_only')}  AS under_19_medicaid_only,
                   {clean_num('under_19_uninsured')}      AS under_19_uninsured
            FROM raw_acs
        )
        SELECT zcta, children, pct_children_in_poverty, median_household_income,
               ROUND(100.0 * under_19_medicaid_only / NULLIF(pop_under_19, 0), 1) AS pct_kids_medicaid,
               ROUND(100.0 * under_19_uninsured     / NULLIF(pop_under_19, 0), 1) AS pct_kids_uninsured
        FROM c
    """)

    # ------------------------------------------------------------------
    # 3. Relevant nonprofits. Excludes:
    #    - private non-operating foundations (FOUNDATION = '04'): they fund
    #      services rather than deliver them
    #    - substance abuse orgs (NTEE F20-F22): mostly adult addiction treatment,
    #      outside the children's mental health scope
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE stg_orgs AS
        SELECT b.EIN                             AS ein,
               b.NAME                            AS name,
               b.CITY                            AS city,
               LEFT(b.ZIP, 5)                    AS zip5,
               b.NTEE_CD                         AS ntee_cd,
               TRY_CAST(b.REVENUE_AMT AS DOUBLE) AS revenue,
               CASE WHEN b.NTEE_CD LIKE 'F%' THEN 'ntee_f' ELSE 'keyword' END AS match_reason,
               c.lat, c.lon
        FROM raw_bmf b
        LEFT JOIN raw_zcta_centroids c ON c.zcta = LEFT(b.ZIP, 5)
        WHERE (b.NTEE_CD LIKE 'F%' OR regexp_matches(UPPER(b.NAME), '{KEYWORDS}'))
          AND NOT regexp_matches(UPPER(b.NAME), '{EXCLUDE_NAMES}')
          AND COALESCE(b.FOUNDATION, '') <> '04'
          AND COALESCE(b.NTEE_CD, '') NOT LIKE 'F2%'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY b.EIN ORDER BY b.NAME) = 1
    """)

    # ------------------------------------------------------------------
    # 4. Mental health shortage areas -> share of each ZIP area covered.
    #    HRSA designates three kinds of pieces in NJ:
    #      Single County        -> ZIP land share inside that county
    #      Census Tract         -> ZIP land share inside that tract
    #      County Subdivision   -> hand-mapped ZIPs (SUBDIVISION_ZCTAS)
    #    Facility HPSAs (prisons, clinics) are sites, not areas: excluded.
    # ------------------------------------------------------------------
    con.execute("""
        CREATE OR REPLACE TABLE stg_hpsa_components AS
        SELECT "HPSA ID"                                  AS hpsa_id,
               TRY_CAST("HPSA Score" AS DOUBLE)           AS score,
               "HPSA Component Type Description"          AS component_type,
               "HPSA Component Name"                      AS component_name,
               LPAD("State and County Federal Information Processing Standard Code", 5, '0') AS county_fips
        FROM raw_hpsa_mh
        WHERE "Primary State Abbreviation" = 'NJ'
          AND "HPSA Status" = 'Designated'
          AND "Designation Type" ILIKE '%geographic%'
    """)

    sub_map = pd.DataFrame(
        [(town, z) for town, zs in SUBDIVISION_ZCTAS.items() for z in zs],
        columns=["town", "zcta"],
    )
    con.register("sub_map", sub_map)

    con.execute("""
        CREATE OR REPLACE TABLE stg_hpsa_zcta AS
        WITH tracts AS (
            -- "Census Tract 10.01; Essex County; New Jersey" -> 34013001001
            SELECT hpsa_id, score, county_fips,
                   regexp_extract(component_name, 'Census Tract ([0-9.]+)', 1) AS tract_num
            FROM stg_hpsa_components
            WHERE component_type = 'Census Tract'
        ),
        tract_geoids AS (
            SELECT hpsa_id, score,
                   county_fips
                   || LPAD(split_part(tract_num, '.', 1), 4, '0')
                   || CASE WHEN tract_num LIKE '%.%'
                           THEN RPAD(split_part(tract_num, '.', 2), 2, '0')
                           ELSE '00' END AS tract_geoid
            FROM tracts
        ),
        pieces AS (
            -- whole counties
            SELECT x.GEOID_ZCTA5_20 AS zcta, h.hpsa_id, h.score,
                   TRY_CAST(x.AREALAND_PART AS DOUBLE)
                     / NULLIF(TRY_CAST(x.AREALAND_ZCTA5_20 AS DOUBLE), 0) AS share
            FROM stg_hpsa_components h
            JOIN raw_zcta_county x ON x.GEOID_COUNTY_20 = h.county_fips
            WHERE h.component_type = 'Single County'
            UNION ALL
            -- census tracts
            SELECT x.GEOID_ZCTA5_20, t.hpsa_id, t.score,
                   TRY_CAST(x.AREALAND_PART AS DOUBLE)
                     / NULLIF(TRY_CAST(x.AREALAND_ZCTA5_20 AS DOUBLE), 0)
            FROM tract_geoids t
            JOIN raw_zcta_tract x ON x.GEOID_TRACT_20 = t.tract_geoid
            UNION ALL
            -- towns (hand-mapped)
            SELECT m.zcta, h.hpsa_id, h.score, 1.0
            FROM stg_hpsa_components h
            JOIN sub_map m ON split_part(h.component_name, ',', 1) = m.town
            WHERE h.component_type = 'County Subdivision'
        )
        SELECT zcta,
               ROUND(LEAST(SUM(share), 1.0), 3) AS shortage_share,
               MAX(score)                       AS mh_hpsa_score
        FROM pieces
        GROUP BY zcta
    """)

    # ------------------------------------------------------------------
    # 5. The analysis table
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE mart_zcta AS
        WITH org_agg AS (
            SELECT zip5 AS zcta,
                   COUNT(*)                  AS org_count,
                   SUM(COALESCE(revenue, 0)) AS org_revenue
            FROM stg_orgs
            GROUP BY 1
        )
        SELECT z.zcta,
               z.county_fips,
               z.county_name,
               z.lat,
               z.lon,
               a.children,
               a.pct_children_in_poverty,
               a.pct_kids_medicaid,
               a.pct_kids_uninsured,
               a.median_household_income,
               COALESCE(o.org_count, 0)          AS org_count,
               COALESCE(o.org_revenue, 0)        AS org_revenue,
               COALESCE(h.shortage_share, 0)     AS shortage_share,
               COALESCE(h.mh_hpsa_score, 0)      AS mh_hpsa_score,
               COALESCE(a.children, 0) < {MIN_CHILDREN} AS low_population_flag
        FROM dim_zcta z
        LEFT JOIN stg_acs       a ON a.zcta = z.zcta
        LEFT JOIN org_agg       o ON o.zcta = z.zcta
        LEFT JOIN stg_hpsa_zcta h ON h.zcta = z.zcta
    """)

    # ------------------------------------------------------------------
    # Data quality report
    # ------------------------------------------------------------------
    section("TABLE SIZES")
    for t in ["dim_zcta", "stg_acs", "stg_orgs", "stg_hpsa_components",
              "stg_hpsa_zcta", "mart_zcta"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<20} {n:>6,}")

    section("SHORTAGE COMPONENTS THAT DIDN'T MAP TO ANY ZIP AREA (should be empty)")
    unmapped = con.execute("""
        WITH tracts AS (
            SELECT component_name,
                   county_fips
                   || LPAD(split_part(regexp_extract(component_name, 'Census Tract ([0-9.]+)', 1), '.', 1), 4, '0')
                   || CASE WHEN regexp_extract(component_name, 'Census Tract ([0-9.]+)', 1) LIKE '%.%'
                           THEN RPAD(split_part(regexp_extract(component_name, 'Census Tract ([0-9.]+)', 1), '.', 2), 2, '0')
                           ELSE '00' END AS tract_geoid
            FROM stg_hpsa_components WHERE component_type = 'Census Tract'
        )
        SELECT 'Census Tract' AS type, component_name FROM tracts
        WHERE tract_geoid NOT IN (SELECT GEOID_TRACT_20 FROM raw_zcta_tract)
        UNION ALL
        SELECT 'County Subdivision', component_name FROM stg_hpsa_components
        WHERE component_type = 'County Subdivision'
          AND split_part(component_name, ',', 1) NOT IN (SELECT town FROM sub_map)
    """).df()
    print(unmapped.to_string(index=False) if len(unmapped) else "  (none -- all mapped)")

    section("SHORTAGE SANITY CHECK (Westfield should be 0, Rahway/Carteret/Bridgeton > 0)")
    print(con.execute("""
        SELECT zcta, county_name, shortage_share, mh_hpsa_score
        FROM mart_zcta
        WHERE zcta IN ('07090', '07065', '07008', '08302', '07206', '07201', '07111')
        ORDER BY zcta
    """).df().to_string(index=False))

    section("ZIP AREAS WITH ANY SHORTAGE COVERAGE, BY COUNTY")
    print(con.execute("""
        SELECT county_name,
               COUNT(*) FILTER (WHERE shortage_share > 0) AS zips_in_shortage,
               COUNT(*)                                   AS zips_total
        FROM mart_zcta
        GROUP BY 1 HAVING COUNT(*) FILTER (WHERE shortage_share > 0) > 0
        ORDER BY 2 DESC
    """).df().to_string(index=False))

    section("NONPROFITS THAT DIDN'T MATCH AN NJ ZIP AREA")
    print(con.execute("""
        SELECT COUNT(*) AS unmatched_orgs,
               (SELECT COUNT(*) FROM stg_orgs) AS total_orgs
        FROM stg_orgs WHERE zip5 NOT IN (SELECT zcta FROM dim_zcta)
    """).df().to_string(index=False))

    section("MISSING VALUES IN mart_zcta")
    print(con.execute("""
        SELECT COUNT(*) FILTER (WHERE children IS NULL)                AS children,
               COUNT(*) FILTER (WHERE pct_children_in_poverty IS NULL) AS poverty,
               COUNT(*) FILTER (WHERE pct_kids_medicaid IS NULL)       AS medicaid,
               COUNT(*) FILTER (WHERE lat IS NULL)                     AS no_location,
               COUNT(*) FILTER (WHERE low_population_flag)             AS low_pop_flagged
        FROM mart_zcta
    """).df().to_string(index=False))

    section("FIRST LOOK: most children, zero relevant nonprofits headquartered there")
    print(con.execute("""
        SELECT zcta, county_name, CAST(children AS INT) AS children,
               pct_children_in_poverty, shortage_share
        FROM mart_zcta
        WHERE org_count = 0 AND NOT low_population_flag
        ORDER BY children DESC LIMIT 10
    """).df().to_string(index=False))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY mart_zcta TO '{OUT_DIR / 'mart_zcta.csv'}' (HEADER)")
    con.close()
    print(f"\nDone. Exported {OUT_DIR / 'mart_zcta.csv'}")


if __name__ == "__main__":
    main()
