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
(12 scenarios). ZIPs in the top 10% in at least 80% of scenarios are "robust",
and rank correlation (Spearman) with the baseline is reported per scenario.

PRIORITY: gap intensity alone favors small ZIPs, so a second list ranks
high-gap ZIPs (top 20% by gap) by how many children in poverty live there.

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
TOP_SHARE = 0.10          # robustness: is a ZIP in the top 10% under each scenario?
ROBUST_THRESHOLD = 0.8
MIN_CHILDREN_SCORED = 500 # Census estimates for smaller areas have very wide error margins

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
    mask = ((z["children"].fillna(0) >= MIN_CHILDREN_SCORED)
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
    cutoff = int(round(len(scored) * TOP_SHARE))
    correlations = []
    for (name, w), r in scenarios:
        g = score(scored, w, access[r][0][idx], access[r][1][idx])
        top_counts[g.rank(ascending=False, method="first") <= cutoff] += 1
        correlations.append((name, r, scored["gap_score"].corr(g, method="spearman")))
    scored["top10pct_frequency"] = (top_counts / len(scenarios)).round(2)
    scored["robust"] = scored["top10pct_frequency"] >= ROBUST_THRESHOLD
    scored["priority_score"] = np.where(
        scored["gap_score"] >= scored["gap_score"].quantile(0.8),
        scored["kids_in_poverty"], 0)

    # ---------------- Save ----------------
    cols = ["zcta", "place_name", "county_name", "lat", "lon", "children", "kids_in_poverty",
            "pct_children_in_poverty", "pct_kids_medicaid", "pct_kids_uninsured",
            "shortage_share", "org_count", "orgs_per_1k_poor_kids", "dollars_per_poor_kid",
            "need_pct", "supply_pct", "gap_score", "gap_rank", "top10pct_frequency", "robust",
            "priority_score"]
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

    show = ["zcta", "place_name", "county_name", "children", "kids_in_poverty",
            "pct_children_in_poverty", "shortage_share", "orgs_per_1k_poor_kids",
            "dollars_per_poor_kid", "gap_score", "top10pct_frequency"]

    print(f"\n=== LIST A: MOST SEVERE GAPS (intensity), top {TOP_N} ===")
    print(out.head(TOP_N)[["gap_rank"] + show].to_string(index=False))

    print(f"\n=== LIST B: MOST CHILDREN AFFECTED (top-20% gap ZIPs by kids in poverty), top {TOP_N} ===")
    print(out.sort_values("priority_score", ascending=False).head(TOP_N)[["gap_rank"] + show]
          .to_string(index=False))

    print(f"\n=== ROBUSTNESS: rank correlation with baseline across {len(scenarios)} scenarios ===")
    corr_df = pd.DataFrame(correlations, columns=["weights", "radius_mi", "spearman_vs_baseline"])
    print(corr_df.round(3).to_string(index=False))
    n_robust = int(out["robust"].sum())
    print(f"\n{n_robust} ZIPs are in the top 10% ({cutoff} ZIPs) in >= "
          f"{ROBUST_THRESHOLD:.0%} of scenarios.")

    print("\n=== EQUITY CHECK (non-circular): reachable capacity by child poverty level ===")
    print("ZIPs grouped ONLY by child poverty rate; compared ONLY on access.")
    out["poverty_quintile"] = pd.qcut(out["pct_children_in_poverty"].rank(method="first"), 5,
                                      labels=["1 lowest", "2", "3", "4", "5 highest"])
    eq = out.groupby("poverty_quintile", observed=True).agg(
        zips=("zcta", "count"),
        avg_child_poverty=("pct_children_in_poverty", "mean"),
        kids_in_poverty=("kids_in_poverty", "sum"),
        median_orgs_per_1k_poor_kids=("orgs_per_1k_poor_kids", "median"),
        median_dollars_per_poor_kid=("dollars_per_poor_kid", "median"),
    ).round(1)
    print(eq.to_string())

    # Children-weighted: what the typical poor child can reach, not the typical ZIP
    def weighted_median(g):
        g = g.sort_values("dollars_per_poor_kid")
        c = g["kids_in_poverty"].cumsum()
        return g.loc[c >= c.iloc[-1] / 2, "dollars_per_poor_kid"].iloc[0]
    wm = out.groupby("poverty_quintile", observed=True).apply(weighted_median, include_groups=False)
    print("\nReachable $ per poor child, for the typical child in poverty (child-weighted median):")
    print(wm.round(0).to_string())

    print("\n=== ZERO-ACCESS CHECK: ZIPs with no relevant nonprofit within "
          f"{BASE_RADIUS} miles ===")
    zero = out[out["orgs_per_1k_poor_kids"] == 0]
    print(f"{len(zero)} scored ZIPs; by county:")
    print(zero["county_name"].value_counts().to_string())

    # Possible missed providers near zero-access ZIPs: nonprofits whose NAME suggests
    # mental health/family services but weren't captured by our filters.
    print("\nPossible providers our filters missed, in counties with zero-access ZIPs")
    print("(review by hand -- if real providers show up, we broaden the filters):")
    if len(zero):
        con.register("zero_zips", z[z["county_name"].isin(zero["county_name"].unique())][["zcta"]])
        missed = con.execute("""
            SELECT b.NAME AS name, b.CITY AS city, b.NTEE_CD AS ntee,
                   ROUND(TRY_CAST(b.REVENUE_AMT AS DOUBLE) / 1e6, 2) AS revenue_m
            FROM raw_bmf b
            WHERE LEFT(b.ZIP, 5) IN (SELECT zcta FROM zero_zips)
              AND regexp_matches(UPPER(b.NAME),
                  '\\bCOUNSEL|\\bMENTAL|\\bGUIDANCE|\\bFAMILY SERVICE|\\bPSYCH|\\bTHERAP|\\bWELLNESS')
              AND CAST(b.EIN AS VARCHAR) NOT IN (SELECT CAST(ein AS VARCHAR) FROM stg_orgs)
            ORDER BY TRY_CAST(b.REVENUE_AMT AS DOUBLE) DESC NULLS LAST
            LIMIT 20
        """).df()
        print(missed.to_string(index=False) if len(missed) else "  (none found)")

    con.close()
    print(f"\nDone. Saved scores_zcta table and {OUT_DIR / 'scores_zcta.csv'}")


if __name__ == "__main__":
    main()
