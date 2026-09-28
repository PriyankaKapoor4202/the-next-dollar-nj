# The Next Dollar
### Where should New Jersey's next children's mental health dollar go?

**The problem:** Funding for children's mental health and autism services tends to cluster where donors and infrastructure already are, not where need is highest. Foundations and agencies deciding where to invest lack a clear, local view of high-need, low-service areas.

**What this project does:** Scores ~580 New Jersey ZIP code areas on need (child population, poverty, public insurance, provider shortages) versus supply (relevant nonprofits and their revenue), identifies "service deserts," and recommends how a given budget could be allocated to reach the most underserved children.

## Key findings
_(Coming Day 6)_

## Recommendation
_(Coming Day 6)_

## Data sources
- IRS Exempt Organizations Business Master File (New Jersey)
- U.S. Census Bureau, American Community Survey 5-year estimates (ZCTA level)
- HRSA Mental Health Professional Shortage Areas
- Census 2020 ZCTA-to-county relationship file

## Method
Need is a weighted blend of child poverty, children on Medicaid, uninsured children, and the share of each ZIP area inside a federal mental health shortage area. Supply is measured with the two-step floating catchment area (2SFCA) method: each nonprofit's capacity is divided by the number of children in poverty within 10 miles, then summed for each ZIP area across all reachable nonprofits. Capacity is measured both as organization count and revenue, then blended so no single large organization dominates. The gap score is need percentile minus supply percentile. Rankings were tested across 12 scenarios (4 weighting schemes × 5/10/15-mile radii); areas in the top 15 in at least 80% of scenarios are marked robust.

## Limitations
- Nonprofit location reflects headquarters address, not necessarily where services are delivered.
- Keyword matching initially caught Spanish-language church names ("Bautista" contains "autis"); fixed with word-boundary matching.
- Substance abuse organizations (NTEE F20–F22) excluded as primarily adult-focused; this is a scope choice.
- Mental health shortage designations cover specific parts of counties, not whole counties; mapped at a finer level to avoid mislabeling areas like Westfield.
- Nonprofit headquarters don't reflect full service areas (e.g., one organization with $112.7M revenue serves multiple counties), so supply is measured by distance, not just ZIP.
- HRSA's shortage area IDs were blank in the source file, so census tracts were decoded from component names (e.g., "Census Tract 10.01" → tract code 001001), and designated towns were mapped to ZIP codes by hand.


## How to run
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python src/01_ingest.py
```
