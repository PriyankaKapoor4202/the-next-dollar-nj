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
_(Coming Day 3)_

## Limitations
- Nonprofit location reflects headquarters address, not necessarily where services are delivered.
_(More to come)_

## How to run
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python src/01_ingest.py
```
