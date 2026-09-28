"""
Step 2: Clean, standardize, and join the raw tables.

Builds (in DuckDB):
  dim_zcta         one row per NJ ZIP area, assigned to its main county
  stg_acs          cleaned Census measures (sentinel codes -> NULL, shares computed)
  stg_orgs         relevant nonprofits (mental health / autism / developmental)
  stg_hpsa_county  mental health shortage designations rolled up to county
  mart_zcta        the analysis table: one row per ZIP area, need + supply side by side

Then prints a data quality report and exports mart_zcta to CSV.

Run from the project root (after 01_ingest.py):
    python src/02_transform.py
"""

from pathlib import Path
import sys

import duckdb

DB_PATH = Path("data/nj_gap.duckdb")
OUT_DIR = Path("data/processed")

MIN_CHILDREN = 100       # below this, rates are too noisy to trust
MIN_NJ_LAND_SHARE = 0.5  # drop ZCTAs that are mostly in NY/PA/DE
KEYWORDS = "AUTIS|DEVELOPMENTAL|BEHAVIORAL|CHILD GUIDANCE"


def clean_num(col: str) -> str:
    """Census uses large negative codes (e.g. -666666666) for 'not available'."""
    return f"CASE WHEN TRY_CAST({col} AS DOUBLE) >= 0 THEN TRY_CAST({col} AS DOUBLE) END"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    if not DB_PATH.exists():
        sys.exit("Database not found. Run python src/01_ingest.py first.")
    con = duckdb.connect(str(DB_PATH))

    # ------------------------------------------------------------------
    # 1. ZIP areas: keep ZCTAs mostly inside NJ, assign each to the county
    #    holding the largest share of its land area.
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
        SELECT zcta, county_fips, county_name, ROUND(nj_land_share, 3) AS nj_land_share
        FROM ranked
        WHERE rn = 1 AND nj_land_share >= {MIN_NJ_LAND_SHARE}
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
    # 3. Relevant nonprofits. Excludes private non-operating foundations
    #    (FOUNDATION = '04'): they fund services rather than deliver them.
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE stg_orgs AS
        SELECT EIN                               AS ein,
               NAME                              AS name,
               CITY                              AS city,
               LEFT(ZIP, 5)                      AS zip5,
               NTEE_CD                           AS ntee_cd,
               TRY_CAST(REVENUE_AMT AS DOUBLE)   AS revenue,
               CASE WHEN NTEE_CD LIKE 'F%' THEN 'ntee_f' ELSE 'keyword' END AS match_reason
        FROM raw_bmf
        WHERE (NTEE_CD LIKE 'F%' OR regexp_matches(UPPER(NAME), '{KEYWORDS}'))
          AND COALESCE(FOUNDATION, '') <> '04'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY EIN ORDER BY NAME) = 1
    """)

    # ------------------------------------------------------------------
    # 4. Mental health shortage areas, rolled up to county.
    #    Facility HPSAs (prisons, clinics) are sites, not areas, so they're excluded.
    # ------------------------------------------------------------------
    section("HPSA VALUES IN NJ (check the filters below make sense)")
    print(con.execute("""
        SELECT "Designation Type" AS designation_type, "HPSA Status" AS status, COUNT(*) AS rows
        FROM raw_hpsa_mh
        WHERE "Primary State Abbreviation" = 'NJ'
        GROUP BY 1, 2 ORDER BY 3 DESC
    """).df().to_string(index=False))

    con.execute("""
        CREATE OR REPLACE TABLE stg_hpsa_county AS
        SELECT LPAD("State and County Federal Information Processing Standard Code", 5, '0') AS county_fips,
               MAX(TRY_CAST("HPSA Score" AS DOUBLE)) AS mh_hpsa_score,
               COUNT(DISTINCT "HPSA ID")             AS mh_hpsa_designations
        FROM raw_hpsa_mh
        WHERE "Primary State Abbreviation" = 'NJ'
          AND "HPSA Status" = 'Designated'
          AND ("Designation Type" ILIKE '%geographic%' OR "Designation Type" ILIKE '%population%')
          AND "State and County Federal Information Processing Standard Code" IS NOT NULL
        GROUP BY 1
    """)

    # ------------------------------------------------------------------
    # 5. The analysis table
    # ------------------------------------------------------------------
    con.execute(f"""
        CREATE OR REPLACE TABLE mart_zcta AS
        WITH org_agg AS (
            SELECT zip5 AS zcta,
                   COUNT(*)                     AS org_count,
                   SUM(COALESCE(revenue, 0))    AS org_revenue
            FROM stg_orgs
            GROUP BY 1
        )
        SELECT z.zcta,
               z.county_fips,
               z.county_name,
               a.children,
               a.pct_children_in_poverty,
               a.pct_kids_medicaid,
               a.pct_kids_uninsured,
               a.median_household_income,
               COALESCE(o.org_count, 0)          AS org_count,
               COALESCE(o.org_revenue, 0)        AS org_revenue,
               COALESCE(h.mh_hpsa_score, 0)      AS mh_hpsa_score,
               h.county_fips IS NOT NULL         AS in_mh_shortage_area,
               COALESCE(a.children, 0) < {MIN_CHILDREN} AS low_population_flag
        FROM dim_zcta z
        LEFT JOIN stg_acs         a ON a.zcta = z.zcta
        LEFT JOIN org_agg         o ON o.zcta = z.zcta
        LEFT JOIN stg_hpsa_county h ON h.county_fips = z.county_fips
    """)

    # ------------------------------------------------------------------
    # Data quality report
    # ------------------------------------------------------------------
    section("TABLE SIZES")
    for t in ["dim_zcta", "stg_acs", "stg_orgs", "stg_hpsa_county", "mart_zcta"]:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<18} {n:>6,}")

    section("NONPROFITS THAT DIDN'T MATCH AN NJ ZIP AREA")
    print(con.execute("""
        SELECT COUNT(*) AS unmatched_orgs,
               (SELECT COUNT(*) FROM stg_orgs) AS total_orgs
        FROM stg_orgs WHERE zip5 NOT IN (SELECT zcta FROM dim_zcta)
    """).df().to_string(index=False))
    print(con.execute("""
        SELECT zip5, city, name FROM stg_orgs
        WHERE zip5 NOT IN (SELECT zcta FROM dim_zcta) LIMIT 5
    """).df().to_string(index=False))

    section("MISSING VALUES IN mart_zcta")
    print(con.execute("""
        SELECT COUNT(*) FILTER (WHERE children IS NULL)                AS children,
               COUNT(*) FILTER (WHERE pct_children_in_poverty IS NULL) AS poverty,
               COUNT(*) FILTER (WHERE pct_kids_medicaid IS NULL)       AS medicaid,
               COUNT(*) FILTER (WHERE median_household_income IS NULL) AS income,
               COUNT(*) FILTER (WHERE low_population_flag)             AS low_pop_flagged
        FROM mart_zcta
    """).df().to_string(index=False))

    section("MENTAL HEALTH SHORTAGE AREAS BY COUNTY")
    print(con.execute("""
        SELECT d.county_name, h.mh_hpsa_score, h.mh_hpsa_designations
        FROM stg_hpsa_county h
        LEFT JOIN (SELECT DISTINCT county_fips, county_name FROM dim_zcta) d USING (county_fips)
        ORDER BY h.mh_hpsa_score DESC
    """).df().to_string(index=False))

    section("LARGEST ORGS BY REVENUE (possible outliers)")
    print(con.execute("""
        SELECT name, city, ntee_cd, ROUND(revenue / 1e6, 1) AS revenue_millions
        FROM stg_orgs ORDER BY revenue DESC NULLS LAST LIMIT 8
    """).df().to_string(index=False))

    section("FIRST LOOK: most children, zero relevant nonprofits headquartered there")
    print(con.execute("""
        SELECT zcta, county_name, CAST(children AS INT) AS children,
               pct_children_in_poverty, in_mh_shortage_area
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
    
