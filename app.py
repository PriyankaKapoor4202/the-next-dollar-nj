"""
The Next Dollar -- dashboard.

Reads data/processed/scores_zcta.csv (produced by src/03_score.py).
Run locally or in Codespaces:
    streamlit run app.py
"""

from pathlib import Path

import branca.colormap as cm
import folium
import numpy as np
import pandas as pd
import streamlit as st
import xyzservices.providers as xyz
from streamlit_folium import st_folium

DATA = Path("data/processed/scores_zcta.csv")
RADIUS_MI = 10

INK = "#1B2B3A"
PLUM = "#7A2E5C"     # large gap
MIST = "#E3E8EC"     # middle
HARBOR = "#2F6F73"   # well served

st.set_page_config(page_title="The Next Dollar", page_icon="🧭", layout="wide")

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;800&display=swap');
    html, body, [class*="css"], .stMarkdown, .stMetric, button, input {
        font-family: 'Public Sans', system-ui, sans-serif;
    }
    h1 { font-weight: 800; letter-spacing: -0.02em; margin-bottom: 0.1rem; }
    .question { font-size: 1.25rem; color: #3E5163; margin-top: 0; max-width: 46rem; }
    .finding {
        border-left: 6px solid #7A2E5C; padding: 0.9rem 1.2rem; margin: 1.2rem 0 1.6rem 0;
        font-size: 1.15rem; line-height: 1.55; max-width: 52rem; background: #F4EEF2;
    }
    .finding strong { color: #7A2E5C; }
    .note { color: #56687A; font-size: 0.9rem; max-width: 52rem; line-height: 1.55; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data
def load() -> pd.DataFrame:
    df = pd.read_csv(DATA, dtype={"zcta": str})
    df["zcta"] = df["zcta"].str.zfill(5)
    df["label"] = df["place_name"] + " (" + df["zcta"] + ")"
    return df


def child_weighted_median(values: pd.Series, weights: pd.Series) -> float:
    order = np.argsort(values.values)
    v, w = values.values[order], weights.values[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, c[-1] / 2)])


if not DATA.exists():
    st.error("No scores found. Run python src/03_score.py first, "
             "which writes data/processed/scores_zcta.csv.")
    st.stop()

df = load()

# ---------------------------------------------------------------- header
st.title("The Next Dollar")
st.markdown(
    '<p class="question">Where should New Jersey\'s next children\'s mental health '
    "dollar go? A comparison of need and nonprofit capacity across "
    f"{len(df)} ZIP areas.</p>",
    unsafe_allow_html=True,
)

# Headline, computed live so it always matches the data
q = pd.qcut(df["pct_children_in_poverty"].rank(method="first"), 5, labels=False)
poorest, richest = df[q == 4], df[q == 0]
share_poor = poorest["kids_in_poverty"].sum() / df["kids_in_poverty"].sum()
orgs_poor = poorest["orgs_per_1k_poor_kids"].median()
orgs_rich = richest["orgs_per_1k_poor_kids"].median()
fewer = 1 - orgs_poor / orgs_rich if orgs_rich else 0
st.markdown(
    f'<div class="finding"><strong>{share_poor:.0%}</strong> of the children in poverty '
    f"in these areas live in the poorest fifth of ZIP codes. Those communities have "
    f"<strong>{fewer:.0%} fewer</strong> mental health nonprofits within reach per child "
    f"in poverty than the wealthiest fifth ({orgs_poor:.1f} vs {orgs_rich:.1f} per 1,000).</div>",
    unsafe_allow_html=True,
)

tab_map, tab_where, tab_budget, tab_method = st.tabs(
    ["Map", "Where to invest", "Plan a budget", "How this works"]
)

# ---------------------------------------------------------------- map
with tab_map:
    counties = ["All counties"] + sorted(df["county_name"].unique())
    pick = st.selectbox("County", counties)
    view = df if pick == "All counties" else df[df["county_name"] == pick]

    cmap = cm.LinearColormap([HARBOR, MIST, PLUM], vmin=-1, vmax=1,
                             caption="Gap score (plum = need outpaces nonprofit capacity)")
    center = [view["lat"].mean(), view["lon"].mean()]
    m = folium.Map(location=center, zoom_start=8 if pick == "All counties" else 10,
                   tiles=xyz.Esri.WorldGrayCanvas)
    for r in view.itertuples():
        folium.CircleMarker(
            location=[r.lat, r.lon],
            radius=max(3, np.sqrt(r.kids_in_poverty) / 6),
            color=cmap(r.gap_score), fill=True, fill_color=cmap(r.gap_score),
            fill_opacity=0.75, weight=1,
            tooltip=(f"<b>{r.place_name}</b> ({r.zcta})<br>"
                     f"{r.kids_in_poverty:,} children in poverty ({r.pct_children_in_poverty:.0f}%)<br>"
                     f"${r.dollars_per_poor_kid:,.0f} nonprofit capacity within reach per child<br>"
                     f"Gap rank {r.gap_rank} of {len(df)}"),
        ).add_to(m)
    cmap.add_to(m)
    m.fit_bounds([[view["lat"].min(), view["lon"].min()],
                  [view["lat"].max(), view["lon"].max()]], padding=(20, 20))
    st_folium(m, height=560, use_container_width=True, returned_objects=[])
    st.markdown(
        '<p class="note">Circle size shows the number of children in poverty. '
        "Hover a circle for details.</p>",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------- lists
cols_show = {
    "label": "Place",
    "county_name": "County",
    "kids_in_poverty": "Children in poverty",
    "pct_children_in_poverty": "Child poverty %",
    "dollars_per_poor_kid": "$ within reach per child",
    "gap_score": "Gap score",
    "robust": "Holds across scenarios",
}

with tab_where:
    st.subheader("Where the most children are affected")
    st.caption("ZIP areas in the top 20% by gap score, ranked by children in poverty. "
               "This is the list to start from when deciding where money goes. "
               "Very large ZIP areas can rank high here on size alone, so check the "
               "gap score and whether the result holds across scenarios. For example, "
               "Lakewood (08701) has the most children in poverty of any area but a "
               "moderate gap, and community-based services there may be undercounted "
               "in IRS categories.")
    b = df[df["priority_score"] > 0].sort_values("kids_in_poverty", ascending=False).head(20)
    st.dataframe(b[list(cols_show)].rename(columns=cols_show), hide_index=True,
                 width="stretch")

    st.subheader("Where the gap is most severe")
    st.caption("Ranked by gap score alone. Often smaller or rural areas with no "
               "relevant nonprofit within reach.")
    a = df.sort_values("gap_rank").head(20)
    st.dataframe(a[list(cols_show)].rename(columns=cols_show), hide_index=True,
                 width="stretch")

# ---------------------------------------------------------------- budget
with tab_budget:
    st.subheader("What would a grant budget reach?")
    statewide = child_weighted_median(df["dollars_per_poor_kid"], df["kids_in_poverty"])
    c1, c2 = st.columns(2)
    with c1:
        budget = st.number_input("Budget ($)", min_value=10_000, max_value=50_000_000,
                                 value=500_000, step=50_000, format="%d")
        strategy = st.radio(
            "Fund areas in this order",
            ["Most children reached per dollar", "Most children in poverty first",
             "Deepest gap first"],
        )
    with c2:
        target = st.slider(
            "Bring each area up to this much nonprofit capacity per child in poverty ($)",
            min_value=250, max_value=5000, step=50, value=int(round(statewide, -1)),
            help=f"Default is the statewide median for a child in poverty: ${statewide:,.0f}.",
        )

    pool = df[(df["priority_score"] > 0) & (df["dollars_per_poor_kid"] < target)].copy()
    pool["shortfall"] = (target - pool["dollars_per_poor_kid"]) * pool["kids_in_poverty"]
    pool["cost_per_child"] = target - pool["dollars_per_poor_kid"]
    order = {
        "Most children reached per dollar": ("cost_per_child", True),
        "Most children in poverty first": ("kids_in_poverty", False),
        "Deepest gap first": ("gap_score", False),
    }[strategy]
    pool = pool.sort_values(order[0], ascending=order[1])

    remaining, rows = float(budget), []
    for r in pool.itertuples():
        if remaining <= 0:
            break
        grant = min(remaining, r.shortfall)
        remaining -= grant
        rows.append({
            "Place": r.label, "County": r.county_name,
            "Grant ($)": round(grant), "Children in poverty": r.kids_in_poverty,
            "Children brought to target": int(r.kids_in_poverty * grant / r.shortfall),
            "Fully funded": grant >= r.shortfall - 1,
        })
    plan = pd.DataFrame(rows)

    if plan.empty:
        st.info("Every high-gap area is already at or above this target. "
                "Raise the target to see where a budget would go.")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("Children brought to target", f"{plan['Children brought to target'].sum():,}")
        m2.metric("Areas funded", f"{len(plan)} ({int(plan['Fully funded'].sum())} fully)")
        m3.metric("Cost per child reached",
                  f"${(budget - remaining) / max(plan['Children brought to target'].sum(), 1):,.0f}")
        st.dataframe(plan, hide_index=True, width="stretch")
        total_need = pool["shortfall"].sum()
        st.markdown(
            f'<p class="note">Closing the gap in every high-gap area to ${target:,} per child '
            f"would take about ${total_need / 1e6:,.1f}M. Compare strategies: "
            "\"most children per dollar\" reaches the most kids, \"deepest gap first\" "
            "prioritizes the worst-off areas even when they are small.</p>",
            unsafe_allow_html=True,
        )
    st.markdown(
        '<p class="note">Simplification: nonprofit capacity is shared across nearby ZIP areas, '
        "so a grant in one place also helps its neighbors. This tool treats each area "
        "separately, which makes it a starting point for a conversation, not a final plan.</p>",
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------- method
with tab_method:
    st.subheader("How the scores are built")
    st.markdown(f"""
**Need** blends child poverty, children on Medicaid, uninsured children, and the share of
each ZIP area inside a federal mental health shortage area (HRSA).

**Nonprofit capacity** uses the two-step floating catchment area method from health
geography. Each nonprofit's capacity is divided by the number of children in poverty
within {RADIUS_MI} miles of it, then summed for every ZIP area across the nonprofits it
can reach. Capacity is counted two ways, by number of organizations and by revenue, and
blended so one very large organization can't dominate.

**Gap score** is the need percentile minus the capacity percentile, from -1 (well served)
to +1 (need far outpaces capacity).

**Robustness.** Rankings were recomputed under 4 weighting schemes and 5, 10, and 15-mile
radii. Areas marked "holds across scenarios" stayed in the top 10% in at least 80% of them.

**Limitations.** IRS data lists nonprofit headquarters, not every office, so regional
providers with satellite locations are undercounted where they don't have their main
address. Revenue is an imperfect proxy for service capacity. Census estimates for small
areas have wide margins of error, so ZIP areas with fewer than 500 children are excluded.

**Sources:** IRS Exempt Organizations Business Master File, U.S. Census Bureau American
Community Survey 5-year estimates, HRSA Health Professional Shortage Areas, Census
ZCTA relationship and Gazetteer files.
""")
