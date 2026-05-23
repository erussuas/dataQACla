# EnergyCAP Pre-Export QA Tool — v2.1

Streamlit app for quality-assuring EnergyCAP data and reconciling with GEM before emissions export.

## What's new in v2.1

### Target-Year Selector
A dropdown in the sidebar lets you set the year being QA'd (e.g. 2025). All checks and GEM reconciliation focus exclusively on that year:
- R-11 bills outside the target year are filtered out before any gap/zero/outlier checks run
- R-19 prior years (e.g. 2024) are used only as seasonal baselines — they will never generate false gap or zero-use flags
- GEM monthly columns outside the target year are zeroed before reconciliation

### Refined Seasonal Estimate Classification
GEM estimates on seasonal/event-driven commodities (diesel, propane, fuel oil, LPG, etc.) are now classified into five sub-types rather than a binary suspect/not-suspect:

| Classification | Trigger | Severity |
|---------------|---------|---------|
| Confirmed Bad — Delivery Tracked | Meter has <N non-zero months (configurable) | Issue |
| Confirmed Bad — Before Start Date | GEM estimates before account existed | Issue |
| Confirmed Bad — No Start Date | No Acct-Meter Begin Date in R-03 | Issue |
| Structurally Unreliable | Non-monthly billing or ≥3 consecutive identical GEM values | Risk |
| Suspect — Magnitude Implausible | GEM estimate < N% of meter's avg non-zero use | Risk |
| Suspect — Standard | Seasonal commodity, none of the above triggers | Risk |
| Defensible | Zero confirmed by R-11 for this month, or historically zero | Info |
| Normal | GEM matches EnergyCAP actual | — |

### New Sidebar Controls
- **QA Target Year** — year being QA'd (top of sidebar)
- **Delivery-tracking threshold** — non-zero months below which a meter is delivery-tracked (default: 4)
- **Magnitude implausibility threshold** — GEM estimates below this % of average non-zero use are flagged (default: 10%)

## Supported Reports

| Report | Required | Description |
|--------|----------|-------------|
| R-03 | ✅ | Setup (Accounts / Meters / Sites) |
| R-11 | ✅ | Bill Transfer Format (bill-level detail + UOM) |
| R-19 | Optional | Monthly Use & Cost — prior years used as baselines only |
| R-13 | Optional | Bill Analysis (EnergyCAP outlier flags) |
| R-21 | Optional | Monthly Comparison |
| R-26 | Optional | Use and Cost Summary |
| GEM  | Optional | GEM Emissions Data Export (enables reconciliation) |

## QA Checks (33 total)

### Bill-Level (R-11)
Missing Native Use, Missing kBTU Conversion, Negative Use, Duplicate Bills,
Billing Period Gaps, Overlapping Bills, Unusual Period Length, Zero Use/Non-Zero Cost,
Non-Zero Use/Zero Cost, Consecutive Zero-Use Months, Use Outliers, Cost Outliers,
MoM % Change, UOM Inconsistency

### Setup (R-03)
Inactive Meters with Bills, Missing Serial Numbers, Excluded from Audits,
Deregulated Market, Missing Acct-Meter Begin Dates, Suspicious Account Start Date,
Non-Monthly Billing Frequency, Active Meters with No Bills

### Cross-Report
Orphan Bills (meter in R-11 not in R-03)

### GEM Reconciliation (8 checks)
Bills Missing from GEM, GEM Over-reports (>20%), GEM Under-reports (>20%),
Confirmed Bad Estimate — Before Start Date, Confirmed Bad Estimate — No Start Date,
Confirmed Bad Estimate — Delivery Tracked, Structurally Unreliable Estimates,
Suspect Estimates — Magnitude Implausible, Suspect Estimates — Standard,
Unmatched SANs

## Setup

```bash
git clone https://github.com/your-org/energycap-qa-tool.git
cd energycap-qa-tool
pip install -r requirements.txt
streamlit run app.py
```

## File Naming Tips
Name exports to include the report code for auto-detection:
`Report-03-Setup.xlsx`, `Report-11-Bills.xlsx`, `GEM_emissions_2025.xlsx`

## Important Note on R-19
R-19 is often exported with 2+ years for year-over-year context. Always set the
**QA Target Year** in the sidebar to match the year you're QA-ing. The tool will:
- Use only the target year's R-19 data for period detection
- Use prior years in R-19 as seasonal baselines (historically-zero-month detection)
- Never flag prior-year records as gaps or anomalies
