"""
Step 3: Score every NJ ZIP area on need, supply, and the gap between them.

NEED  = weighted blend of child poverty, kids on Medicaid, kids uninsured,
        and share of the ZIP inside a federal mental health shortage area.

SUPPLY = access to nonprofit service capacity, measured with the
        two-step floating catchment area (2SFCA) method from health geography:
          step 1: for each nonprofit, divide its capacity by the number of
                  children in poverty living within RADIUS miles of it
          step 2: for each ZIP, add up those ratios for every nonprofit
                  within RADIUS miles
        Capacity is measured two ways (org count and revenue) and blended,
        so one very large organization can't dominate.

GAP   = need percentile minus supply percentile (-1 to +1; higher = bigger gap).

SENSITIVITY: the ranking is recomputed under 4 weighting schemes x 3 radii
(12 scenarios). ZIPs in the top 15 in at least 80% of scenarios are "robust".

Run from the project root (after 02_transform.py):
    python src/03_score.py
"""

from itertools import product
from pathlib import Path
import sys

import duckdb
import numpy as np
import pandas as pd

DB_PATH = Path("data/nj_gap.duckdb")
OUT_DIR = Path("data/processed")

BASE_RADIUS = 10  # miles
RADII = [5, 10, 15]
TOP_N = 15
ROBUST_THRESHOLD = 0.8

WEIGHT_SCHEMES = {
    "baseline":       {"poverty": 0.35, "medicaid": 0.25, "uninsured": 0.15, "shortage": 0.25},
    "poverty_heavy":  {"poverty": 0.50, "medicaid": 0.20, "uninsured": 0.10, "shortage": 0.20},
    "equal":          {"poverty": 0.25, "medicaid": 0.25, "uninsured": 0.25, "shortage": 0.25},
    "shortage_heavy": {"poverty": 0.25, "medicaid": 0.20, "uninsured": 0.15, "shortage": 0.40},
}


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = (np.sin((lat2 - lat1) / 2) ** 2
         + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * np.arcsin(np.sqrt(a))


def two_sfca(dist, demand, capacity, radius):
    """Access per unit of demand for each ZIP (rows) given orgs (columns)."""
    within = dist <= radius
    demand_near_org = (within * demand[:, None]).sum(axis=0)
    ratio = np.divide(capacity, demand_near_org,
                      out=np.zeros_like(capacity, dtype=float),
                      where=demand_near_org > 0)
    return (within * ratio[None, :]).sum(axis=1)


def pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True)


def score(df: pd.DataFrame, weights: dict, access_count, access_dollars) -> pd.Series:
    """Gap score for the scored ZIPs under one scenario."""
    need = (weights["poverty"]   * pct(df["pct_children_in_poverty"])
            + weights["medicaid"]  * pct(df["pct_kids_medicaid"])
            + weights["uninsured"] * pct(df["pct_kids_uninsured"])
            + weights["shortage"]  * df["shortage_share"])
    supply = (pct(pd.Series(access_count, index=df.index))
              + pct(pd.Series(access_dollars, index=df.index))) / 2
    return pct(need) - pct(supply)


def main() -> None:
    if not DB_PATH.exists():
        sys.exit("Database not found. Run 01_ingest.py and 02_transform.py first.")
    con = duckdb.connect(str(DB_PATH))

    z = con.execute("SELECT * FROM mart_zcta").df()
    orgs = con.execute(
        "SELECT ein, name, zip5, COALESCE(revenue, 0) AS revenue, lat, lon "
        "FROM stg_orgs WHERE lat IS NOT NULL"
    ).df()

    # Readable place names: the most common city among ALL nonprofits in each ZIP
    names = con.execute("""
        SELECT LEFT(ZIP, 5) AS zcta, MODE(CITY) AS place_name
        FROM raw_bmf GROUP BY 1
    """).df()
    names["place_name"] = names["place_name"].str.title()
    z = z.merge(names, on="zcta", how="left")
    z["place_name"] = z["place_name"].fillna("(unknown)")

    # Demand = children in poverty. All ZIPs count as demand, even tiny ones.
    z["kids_in_poverty"] = (z["children"].fillna(0)
                            * z["pct_children_in_poverty"].fillna(0) / 100).round()
    z = z.dropna(subset=["lat", "lon"]).reset_index(drop=True)

    dist = haversine_miles(z["lat"].values[:, None], z["lon"].values[:, None],
                           orgs["lat"].values[None, :], orgs["lon"].values[None, :])
    demand = z["kids_in_poverty"].values
    ones = np.ones(len(orgs))
    revenue = orgs["revenue"].values.astype(float)

    access = {r: (two_sfca(dist, demand, ones, r), two_sfca(dist, demand, revenue, r))
              for r in RADII}

    # Only score ZIPs with enough children and complete need data
    mask = ((~z["low_population_flag"])
            & z["pct_children_in_poverty"].notna()
            & z["pct_kids_medicaid"].notna()
            & z["pct_kids_uninsured"].notna())
    scored = z[mask].copy()
    idx = scored.index.values

    # ---------------- Baseline scores ----------------
    base_w = WEIGHT_SCHEMES["baseline"]
    a_count, a_dollars = access[BASE_RADIUS]
    scored["orgs_per_1k_poor_kids"] = (a_count[idx] * 1000).round(2)
    scored["dollars_per_poor_kid"] = a_dollars[idx].round(0)
    scored["need_pct"] = pct(
        base_w["poverty"] * pct(scored["pct_children_in_poverty"])
        + base_w["medicaid"] * pct(scored["pct_kids_medicaid"])
        + base_w["uninsured"] * pct(scored["pct_kids_uninsured"])
        + base_w["shortage"] * scored["shortage_share"]).round(3)
    scored["supply_pct"] = ((pct(scored["orgs_per_1k_poor_kids"])
                             + pct(scored["dollars_per_poor_kid"])) / 2)
    scored["supply_pct"] = pct(scored["supply_pct"]).round(3)
    scored["gap_score"] = (scored["need_pct"] - scored["supply_pct"]).round(3)
    scored["gap_rank"] = scored["gap_score"].rank(ascending=False, method="min").astype(int)

    # ---------------- Sensitivity analysis ----------------
    top_counts = pd.Series(0, index=scored.index)
    scenarios = list(product(WEIGHT_SCHEMES.items(), RADII))
    for (_, w), r in scenarios:
        g = score(scored, w, access[r][0][idx], access[r][1][idx])
        top_counts[g.rank(ascending=False, method="first") <= TOP_N] += 1
    scored["top15_frequency"] = (top_counts / len(scenarios)).round(2)
    scored["robust"] = scored["top15_frequency"] >= ROBUST_THRESHOLD

    # ---------------- Save ----------------
    cols = ["zcta", "place_name", "county_name", "lat", "lon", "children", "kids_in_poverty",
            "pct_children_in_poverty", "pct_kids_medicaid", "pct_kids_uninsured",
            "shortage_share", "org_count", "orgs_per_1k_poor_kids", "dollars_per_poor_kid",
            "need_pct", "supply_pct", "gap_score", "gap_rank", "top15_frequency", "robust"]
    out = scored[cols].sort_values("gap_rank")
    out["children"] = out["children"].astype(int)
    out["kids_in_poverty"] = out["kids_in_poverty"].astype(int)
    con.register("scores_df", out)
    con.execute("CREATE OR REPLACE TABLE scores_zcta AS SELECT * FROM scores_df")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT_DIR / "scores_zcta.csv", index=False)

    # ---------------- Report ----------------
    pd.set_option("display.width", 200)
    print(f"\nScored {len(scored)} of {len(z)} ZIP areas "
          f"({len(z) - len(scored)} excluded: too few children or missing data).")
    print(f"Supply measured within {BASE_RADIUS} miles using {len(orgs)} nonprofits.")

    print(f"\n=== TOP {TOP_N} SERVICE DESERTS (baseline) ===")
    print(out.head(TOP_N)[["gap_rank", "zcta", "place_name", "county_name", "children",
                          "pct_children_in_poverty", "shortage_share",
                          "orgs_per_1k_poor_kids", "dollars_per_poor_kid",
                          "gap_score", "top15_frequency"]].to_string(index=False))

    n_robust = int(out["robust"].sum())
    print(f"\n=== ROBUSTNESS: {n_robust} ZIPs stay in the top {TOP_N} in "
          f">= {ROBUST_THRESHOLD:.0%} of {len(scenarios)} scenarios ===")
    print(out[out["robust"]][["zcta", "place_name", "county_name",
                              "top15_frequency"]].to_string(index=False))

    top = out.head(TOP_N)
    fragile = top[~top["robust"]]
    if len(fragile):
        print("\nIn the baseline top 15 but sensitive to assumptions:")
        print(fragile[["zcta", "place_name", "top15_frequency"]].to_string(index=False))

    print("\n=== HEADLINE NUMBERS (baseline) ===")
    top_q = out["gap_score"] >= out["gap_score"].quantile(0.8)
    total_poor = out["kids_in_poverty"].sum()
    share_poor = out.loc[top_q, "kids_in_poverty"].sum() / total_poor
    med_top = out.loc[top_q, "dollars_per_poor_kid"].median()
    med_rest = out.loc[~top_q, "dollars_per_poor_kid"].median()
    print(f"  Highest-gap fifth of ZIPs: {top_q.sum()} areas, "
          f"{int(out.loc[top_q, 'kids_in_poverty'].sum()):,} children in poverty "
          f"({share_poor:.0%} of the state's total in scored areas)")
    print(f"  Median nonprofit $ reachable per poor child: "
          f"${med_top:,.0f} there vs ${med_rest:,.0f} everywhere else")
    if med_top > 0:
        print(f"  -> Other areas have {med_rest / med_top:.1f}x the reachable capacity per child")

    print("\n=== GAP BY COUNTY (children in poverty living in top-fifth gap ZIPs) ===")
    by_county = (out[top_q].groupby("county_name")["kids_in_poverty"].sum()
                 .sort_values(ascending=False).astype(int))
    print(by_county.to_string())

    con.close()
    print(f"\nDone. Saved scores_zcta table and {OUT_DIR / 'scores_zcta.csv'}")


if __name__ == "__main__":
    main()
