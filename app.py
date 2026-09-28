"""
The Next Dollar -- dashboard.

Reads data/processed/scores_zcta.csv (src/03_score.py) and
data/processed/nj_zcta.geojson (src/04_geo.py).
    streamlit run app.py
"""

import copy
import json
from pathlib import Path

import altair as alt
import folium
import numpy as np
import pandas as pd
import streamlit as st
import xyzservices.providers as xyz
from streamlit_folium import st_folium

DATA = Path("data/processed/scores_zcta.csv")
GEO = Path("data/processed/nj_zcta.geojson")
REPO = "https://github.com/PriyankaKapoor4202/the-next-dollar-nj"
RADIUS_MI = 10
DEFAULT_BUDGET = 500_000

INK = "#1B2B3A"
PLUM = "#7A2E5C"
NOT_SCORED = "#F1F2F4"
# (lower, upper, label, color) on the gap score, -1 to +1
BINS = [
    (-1.01, 0.00, "Well served", "#D3E4E4"),
    (0.00, 0.25, "Small gap", "#F0DCE5"),
    (0.25, 0.50, "Moderate gap", "#DBA6BF"),
    (0.50, 0.75, "Large gap", "#AE5F88"),
    (0.75, 1.01, "Largest gap", "#6A2150"),
]
STRATEGIES = {
    "Most children per dollar": ("cost_per_child", True),
    "Largest community first": ("kids_in_poverty", False),
    "Deepest gap first": ("gap_score", False),
}

st.set_page_config(page_title="The Next Dollar", page_icon="🧭", layout="centered")
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;800&display=swap');
    html, body, [class*="css"], .stMarkdown, button, input, textarea {
        font-family: 'Public Sans', system-ui, sans-serif;
    }
    .block-container { padding-top: 2.5rem; max-width: 780px; }
    h1 { font-size: 3.1rem !important; font-weight: 800 !important;
         letter-spacing: -0.03em; line-height: 1.05 !important; margin-bottom: 0.4rem; }
    h2 { font-size: 1.65rem !important; font-weight: 800 !important;
         margin-top: 2.8rem !important; letter-spacing: -0.01em; }
    .lede { font-size: 1.25rem; line-height: 1.5; color: #3E5163; margin: 0 0 1.4rem 0; }
    .finding { border-left: 6px solid #7A2E5C; background: #F5EEF2;
               padding: 1rem 1.3rem; font-size: 1.12rem; line-height: 1.6; }
    .finding b { color: #7A2E5C; }
    .stat { padding-top: 0.6rem; }
    .stat .n { font-size: 2.3rem; font-weight: 800; color: #7A2E5C; line-height: 1.1; }
    .stat .l { font-size: 0.98rem; line-height: 1.45; color: #3E5163; margin-top: 0.3rem; }
    .body { font-size: 1.05rem; line-height: 1.6; color: #2A3B4C; }
    .legend { display: flex; flex-wrap: wrap; gap: 0.4rem 1.1rem; margin: 0.5rem 0 0.2rem 0;
              font-size: 0.92rem; color: #3E5163; }
    .legend span { display: inline-flex; align-items: center; gap: 0.4rem; }
    .legend i { width: 14px; height: 14px; display: inline-block; border: 1px solid #C9D1D8; }
    .spot { background: #FFFFFF; border: 1px solid #DCE2E7; padding: 1.1rem 1.3rem;
            font-size: 1.12rem; line-height: 1.6; margin-top: 0.4rem; }
    .spot b { color: #7A2E5C; }
    .facts { font-size: 0.95rem; color: #56687A; margin-top: 0.5rem; }
    .strategy { font-size: 1.02rem; line-height: 1.55; margin: 0.35rem 0; }
    .note { color: #56687A; font-size: 0.92rem; line-height: 1.55; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------- data helpers
@st.cache_data
def load_scores() -> pd.DataFrame:
    df = pd.read_csv(DATA, dtype={"zcta": str})
    df["zcta"] = df["zcta"].str.zfill(5)
    df["label"] = df["place_name"] + " (" + df["zcta"] + ")"
    return df


@st.cache_data
def load_geo() -> dict | None:
    return json.loads(GEO.read_text()) if GEO.exists() else None


def gap_bin(score: float) -> tuple[str, str]:
    for lo, hi, label, color in BINS:
        if lo <= score < hi:
            return label, color
    return BINS[-1][2], BINS[-1][3]


def child_weighted_median(values: pd.Series, weights: pd.Series) -> float:
    order = np.argsort(values.values)
    v, w = values.values[order], weights.values[order]
    c = np.cumsum(w)
    return float(v[np.searchsorted(c, c[-1] / 2)])


def allocate(df: pd.DataFrame, budget: float, target: float, strategy: str) -> pd.DataFrame:
    """Fill each eligible area's shortfall, in strategy order, until the budget runs out.
    Eligible: high-gap areas at least 20% below target (near-misses would inflate results)."""
    pool = df[(df["priority_score"] > 0) & (df["dollars_per_poor_kid"] < 0.8 * target)].copy()
    pool["cost_per_child"] = target - pool["dollars_per_poor_kid"]
    pool["shortfall"] = pool["cost_per_child"] * pool["kids_in_poverty"]
    col, asc = STRATEGIES[strategy]
    remaining, rows = float(budget), []
    for r in pool.sort_values(col, ascending=asc).itertuples():
        if remaining <= 0:
            break
        grant = min(remaining, r.shortfall)
        remaining -= grant
        rows.append({"Place": r.label, "County": r.county_name, "Grant": round(grant),
                     "Children in poverty": int(r.kids_in_poverty),
                     "Children brought to typical level": int(r.kids_in_poverty * grant / r.shortfall),
                     "Fully funded": grant >= r.shortfall - 1})
    plan = pd.DataFrame(rows)
    plan.attrs["total_need"] = pool["shortfall"].sum()
    return plan


if not DATA.exists():
    st.error("No scores found. Run python src/03_score.py first.")
    st.stop()

df = load_scores()
statewide = child_weighted_median(df["dollars_per_poor_kid"], df["kids_in_poverty"])
typical_orgs = child_weighted_median(df["orgs_per_1k_poor_kids"], df["kids_in_poverty"])
df["reach_pct"] = (df["orgs_per_1k_poor_kids"] / typical_orgs * 100).clip(upper=100) if typical_orgs else 0
df["consistent"] = np.where(df["robust"], "Yes", "Varies")
by_zcta = df.set_index("zcta")

# ----------------------------------------------------------------- headline
st.title("The Next Dollar")
st.markdown(
    '<p class="lede">Where should New Jersey\'s next children\'s mental health dollar go? '
    f"This compares need with nonprofit capacity in {len(df)} communities.</p>",
    unsafe_allow_html=True,
)

q = pd.qcut(df["pct_children_in_poverty"].rank(method="first"), 5, labels=False)
poorest, richest = df[q == 4], df[q == 0]
share_poor = poorest["kids_in_poverty"].sum() / df["kids_in_poverty"].sum()
o_poor = poorest["orgs_per_1k_poor_kids"].median()
o_rich = richest["orgs_per_1k_poor_kids"].median()
fewer = 1 - o_poor / o_rich if o_rich else 0

st.markdown(
    f'<div class="finding"><b>{share_poor:.0%}</b> of the children in poverty in these '
    f"communities live in the poorest fifth of them, and those communities have "
    f"<b>{fewer:.0%} fewer</b> mental health nonprofits within reach per child.</div>",
    unsafe_allow_html=True,
)

top15 = df[df["priority_score"] > 0].nlargest(15, "kids_in_poverty")
undesignated = int((top15["shortage_share"] == 0).sum())
reach = []
for s in STRATEGIES:
    plan0 = allocate(df, DEFAULT_BUDGET, statewide, s)
    reach.append(int(plan0["Children brought to typical level"].sum()) if len(plan0) else 0)

c1, c2, c3 = st.columns(3)
for col, n, label in [
    (c1, f"{fewer:.0%} fewer", "nonprofits within reach per child in the poorest communities"),
    (c2, f"{undesignated} of 15", "highest-need communities have no federal shortage designation"),
    (c3, f"{min(reach):,}–{max(reach):,}", "children the same $500,000 can reach, depending on where it goes"),
]:
    col.markdown(f'<div class="stat"><div class="n">{n}</div><div class="l">{label}</div></div>',
                 unsafe_allow_html=True)

# ----------------------------------------------------------------- map
st.header("Where the gaps are")
st.markdown(
    '<p class="body">Darker areas have more need relative to the nonprofit capacity '
    f"within {RADIUS_MI} miles. Hover over any area for details.</p>",
    unsafe_allow_html=True,
)

geo = load_geo()
if geo is None:
    st.info("Map boundaries not found. Run python src/04_geo.py to build them.")
else:
    counties = ["All of New Jersey"] + sorted(df["county_name"].unique())
    pick = st.selectbox("Zoom to a county", counties, label_visibility="collapsed")

    g = copy.deepcopy(geo)
    feats = []
    for f in g["features"]:
        z = f["properties"]["zcta"]
        if z in by_zcta.index:
            r = by_zcta.loc[z]
            if pick != "All of New Jersey" and r["county_name"] != pick:
                continue
            label, color = gap_bin(r["gap_score"])
            share = r["orgs_per_1k_poor_kids"] / typical_orgs if typical_orgs else 0
            f["properties"].update({
                "place": f"{r['place_name']} ({z})", "fill": color, "status": label,
                "kids": f"{int(r['kids_in_poverty']):,} children in poverty "
                        f"({r['pct_children_in_poverty']:.0f}%)",
                "access": f"Nonprofits within reach: {share:.0%} of the NJ typical level",
            })
        else:
            if pick != "All of New Jersey":
                continue
            f["properties"].update({"place": f"ZIP {z}", "fill": NOT_SCORED,
                                    "status": "Not scored (too few children)",
                                    "kids": "", "access": ""})
        feats.append(f)
    g["features"] = feats

    # Center and zoom are computed explicitly: the map component doesn't reliably
    # honor fit_bounds, which left the view zoomed into one corner of the state.
    def walk(coords):
        if isinstance(coords[0], (int, float)):
            yield coords
        else:
            for c in coords:
                yield from walk(c)
    pts = [pt for f in g["features"] for pt in walk(f["geometry"]["coordinates"])]
    lons, lats = [p_[0] for p_ in pts], [p_[1] for p_ in pts]
    center = [(min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2]
    span = max(max(lats) - min(lats), (max(lons) - min(lons)) * 0.75)
    zoom = 8 if span > 1.4 else 9 if span > 0.7 else 10 if span > 0.35 else 11

    m = folium.Map(location=center, zoom_start=zoom, tiles=xyz.Esri.WorldGrayCanvas,
                   zoom_control=True, scrollWheelZoom=False)
    layer = folium.GeoJson(
        g,
        style_function=lambda f: {"fillColor": f["properties"]["fill"], "fillOpacity": 0.9,
                                  "color": "#FFFFFF", "weight": 0.6},
        highlight_function=lambda f: {"weight": 2.5, "color": INK},
        tooltip=folium.GeoJsonTooltip(fields=["place", "status", "kids", "access"],
                                      labels=False, sticky=True,
                                      style="font-family: 'Public Sans', sans-serif; font-size: 13px;"),
    ).add_to(m)
    st_folium(m, center=center, zoom=zoom, height=640, use_container_width=True,
              returned_objects=[], key=f"map-{pick}")

    legend = "".join(f'<span><i style="background:{c}"></i>{l}</span>' for *_, l, c in BINS)
    legend += f'<span><i style="background:{NOT_SCORED}"></i>Not scored</span>'
    st.markdown(f'<div class="legend">{legend}</div>', unsafe_allow_html=True)

# ----------------------------------------------------------------- lookup
st.header("Look up a community")
labels = df.sort_values("place_name")["label"].tolist()
default = next((i for i, l in enumerate(labels) if "08861" in l), 0)
choice = st.selectbox("Community", labels, index=default, label_visibility="collapsed")
r = df[df["label"] == choice].iloc[0]
org_share = r["orgs_per_1k_poor_kids"] / typical_orgs if typical_orgs else 0
status, _ = gap_bin(r["gap_score"])
if r["orgs_per_1k_poor_kids"] == 0:
    access_line = f"no relevant nonprofit is headquartered within {RADIUS_MI} miles"
else:
    access_line = (
        f"there are about <b>{r['orgs_per_1k_poor_kids']:.1f}</b> relevant nonprofits within "
        f"{RADIUS_MI} miles per 1,000 children in poverty, <b>{org_share:.0%}</b> of the "
        f"New Jersey typical level ({typical_orgs:.1f}). Together they have about "
        f"${r['dollars_per_poor_kid']:,.0f} in revenue per child in poverty "
        f"(typical: ${statewide:,.0f})"
    )
designation = ("fully inside" if r["shortage_share"] >= 0.99 else
               "partly inside" if r["shortage_share"] > 0 else "not in")
st.markdown(
    f'<div class="spot">In <b>{r["place_name"]}</b> ({r["zcta"]}, {r["county_name"]}), '
    f'{int(r["kids_in_poverty"]):,} children live in poverty ({r["pct_children_in_poverty"]:.0f}%). '
    f"{access_line[0].upper() + access_line[1:]}.</div>"
    f'<p class="facts">{status}, ranked {int(r["gap_rank"])} of {len(df)}. '
    f"{designation[0].upper() + designation[1:]} a federal mental health shortage area."
    f'{" This result holds across the tested assumptions." if r["robust"] else ""}</p>',
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------- where to invest
st.header("Where the most children are affected")
st.markdown(
    '<p class="body">Communities with a large gap, ranked by how many children in poverty '
    "live there. This is where a funder would start.</p>",
    unsafe_allow_html=True,
)
top = df[df["priority_score"] > 0].nlargest(10, "kids_in_poverty")
st.dataframe(
    top[["label", "kids_in_poverty", "pct_children_in_poverty", "reach_pct", "consistent"]],
    hide_index=True, width="stretch",
    column_config={
        "label": st.column_config.TextColumn("Community"),
        "kids_in_poverty": st.column_config.NumberColumn("Children in poverty", format="localized"),
        "pct_children_in_poverty": st.column_config.NumberColumn("Child poverty", format="%.0f%%"),
        "reach_pct": st.column_config.ProgressColumn(
            "Nonprofits within reach vs NJ typical", format="%.0f%%", min_value=0, max_value=100),
        "consistent": st.column_config.TextColumn("Holds across tests"),
    },
)
st.markdown(
    '<p class="note">Lakewood (08701) tops this list because of its size; its gap is moderate, '
    "and community-based services there may not appear in IRS categories.</p>",
    unsafe_allow_html=True,
)
with st.expander("Smaller and rural communities with the most severe gaps"):
    sev = df.nsmallest(10, "gap_rank")
    st.dataframe(
        sev[["label", "kids_in_poverty", "reach_pct", "consistent"]], hide_index=True,
        width="stretch",
        column_config={
            "label": st.column_config.TextColumn("Community"),
            "kids_in_poverty": st.column_config.NumberColumn("Children in poverty", format="localized"),
            "reach_pct": st.column_config.ProgressColumn(
                "Nonprofits within reach vs NJ typical", format="%.0f%%",
                min_value=0, max_value=100),
            "consistent": st.column_config.TextColumn("Holds across tests"),
        },
    )

# ----------------------------------------------------------------- budget
st.header("What would a grant reach?")
BUDGETS = [100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000, 10_000_000, 25_000_000]
budget = st.select_slider(
    "Grant budget", options=BUDGETS, value=DEFAULT_BUDGET,
    format_func=lambda v: f"${v / 1e6:g}M" if v >= 1e6 else f"${v / 1e3:g}K")
with st.expander("Change the target level"):
    target = st.slider("Bring each community up to this much nonprofit capacity "
                       "per child in poverty ($)", 500, 5000, int(round(statewide, -1)), 50,
                       help=f"Default is the New Jersey typical level: ${statewide:,.0f}.")

plans = {s: allocate(df, budget, target, s) for s in STRATEGIES}
res = pd.DataFrame([
    {"Strategy": s,
     "Children": int(p["Children brought to typical level"].sum()) if len(p) else 0,
     "Where": ", ".join(p["Place"].head(2)) + ("…" if len(p) > 2 else "") if len(p) else "None eligible"}
    for s, p in plans.items()
])

st.markdown(
    f'<p class="body">Children brought up to the typical level with <b>${budget:,.0f}</b>, '
    "under three ways of deciding where the money goes:</p>",
    unsafe_allow_html=True,
)
bars = alt.Chart(res).mark_bar(color=PLUM, height=30).encode(
    x=alt.X("Children:Q", title=None, axis=alt.Axis(grid=False, labels=False, ticks=False, domain=False)),
    y=alt.Y("Strategy:N", sort=list(STRATEGIES), title=None,
            axis=alt.Axis(labelFontSize=14, labelLimit=260, labelColor=INK, domain=False, ticks=False)),
)
labels_ = bars.mark_text(align="left", dx=6, fontSize=15, fontWeight="bold", color=INK).encode(
    text=alt.Text("Children:Q", format=","))
st.altair_chart((bars + labels_).properties(height=150).configure_view(stroke=None),
                width="stretch")
for row in res.itertuples():
    st.markdown(f'<p class="strategy"><b>{row.Strategy}:</b> {row.Where}</p>',
                unsafe_allow_html=True)

total_need = next(iter(plans.values())).attrs.get("total_need", 0)
st.markdown(
    f'<p class="note">Bringing every eligible community to ${target:,} per child would take '
    f"about ${total_need / 1e6:,.0f}M, so where a grant goes matters as much as its size. "
    "Eligible communities have a large gap and sit at least 20% below the target.</p>",
    unsafe_allow_html=True,
)
with st.expander("See the full allocation for each strategy"):
    pick_s = st.radio("Strategy", list(STRATEGIES), horizontal=True, label_visibility="collapsed")
    p = plans[pick_s]
    if p.empty:
        st.write("No eligible communities at this target.")
    else:
        st.dataframe(p, hide_index=True, width="stretch",
                     column_config={"Grant": st.column_config.NumberColumn(format="$%d")})
    st.markdown(
        '<p class="note">Nonprofit capacity is shared across neighboring areas, so a grant in one '
        "place also helps nearby communities. This planner treats each area separately, which "
        "makes it a starting point for a conversation, not a final plan.</p>",
        unsafe_allow_html=True,
    )

# ----------------------------------------------------------------- method
st.header("How this works")
with st.expander("Method, robustness, and limitations"):
    st.markdown(f"""
**Need** blends child poverty, children on Medicaid, uninsured children, and the share of each
area inside a federal mental health shortage area (HRSA).

**Nonprofit capacity** uses the two-step floating catchment area method from health geography.
Each nonprofit's capacity is divided by the number of children in poverty within {RADIUS_MI}
miles of it, then summed for each community across every nonprofit it can reach. Capacity is
counted by number of organizations and by revenue, blended so one large organization can't
dominate.

**Gap score** is the need percentile minus the capacity percentile.

**Robustness.** Rankings were recomputed under 4 weighting schemes and 5, 10, and 15-mile
radii. "Consistent result" means a community stayed in the top 10% in at least 80% of them.

**Limitations.** IRS data lists headquarters, not every office, so regional providers with
satellite locations are undercounted. Revenue is an imperfect proxy for capacity. Communities
with fewer than 500 children are not scored because Census estimates there are unreliable.

**Sources:** IRS Exempt Organizations Business Master File; U.S. Census Bureau American
Community Survey 5-year estimates; HRSA Health Professional Shortage Areas; Census ZCTA
boundary, relationship, and Gazetteer files.
""")
st.markdown(f'<p class="note">Built by Priyanka Kapoor. <a href="{REPO}">Code and full '
            "write-up on GitHub</a>.</p>", unsafe_allow_html=True)
