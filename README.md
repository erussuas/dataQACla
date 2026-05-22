# EnergyCAP Pre-Export QA Tool — v2.0

A Streamlit app for quality-assuring EnergyCAP data and reconciling it with GEM (emissions calculation tool) before export.

## What's new in v2.0
- **Period detection** — auto-detects the date range in each uploaded file
- **Overlap computation** — finds the overlapping window across all files and focuses all QA and reconciliation on that period
- **Account start date checks** — flags accounts with missing or suspiciously old start dates, with a year-slider in the sidebar
- **Non-monthly billing frequency flags** — flags quarterly/bi-monthly meters where GEM distributes bills equally across months
- **GEM reconciliation tab** — full EnergyCAP ↔ GEM comparison with MWh conversion
- **GEM estimate quality classification** — four-tier rating: Confirmed Bad / Structurally Unreliable / Suspect / Defensible
- **GEM detail view** — row-level drill-down with filters by site, resource, estimate quality, and match tier
- **Configurable unit conversion** — edit MWh conversion factors per commodity in the sidebar

## Supported Reports

| Report | Required | Description |
|--------|----------|-------------|
| R-03 | ✅ | Setup (Accounts / Meters / Sites) |
| R-11 | ✅ | Bill Transfer Format (bill-level detail + UOM) |
| R-13 | Optional | Bill Analysis (EnergyCAP outlier flags) |
| R-19 | Optional | Monthly Utility Use and Cost |
| R-21 | Optional | Monthly Comparison |
| R-26 | Optional | Use and Cost Summary |
| GEM  | Optional | GEM Emissions Data Export (enables reconciliation) |

## Tabs

| Tab | Contents |
|-----|---------|
| Upload & Run | File upload, period/overlap detection, run button |
| EnergyCAP QA | EnergyCAP-only check results and summary |
| GEM Reconciliation | EnergyCAP ↔ GEM summary, estimate quality, match coverage |
| Risk Summary | Categorized risk explanations with emissions impact |
| Issue Register | Filterable table of confirmed issues (downloadable) |
| Risk Register | Filterable table of risks needing review (downloadable) |
| GEM Detail | Row-level GEM ↔ EnergyCAP comparison (downloadable) |
| Data Explorer | Raw data browser for each uploaded report |

## QA Checks (30 total)

### Bill-Level (R-11)
Missing Native Use, Missing kBTU Conversion, Negative Use, Duplicate Bills, Billing Period Gaps, Overlapping Bills, Unusual Period Length, Zero Use/Non-Zero Cost, Non-Zero Use/Zero Cost, Consecutive Zero-Use Months, Use Outliers, Cost Outliers, MoM % Change, UOM Inconsistency

### Setup (R-03)
Inactive Meters with Bills, Missing Serial Numbers, Excluded from Audits, Deregulated Market, Missing Acct-Meter Begin Dates, Suspicious Account Start Date, Non-Monthly Billing Frequency, Active Meters with No Bills

### Cross-Report
Orphan Bills (meter in R-11 not in R-03)

### GEM Reconciliation
Bills Missing from GEM, GEM Over-reports (>20%), GEM Under-reports (>20%), Confirmed Bad Estimates, Structurally Unreliable Estimates, Suspect Estimates (Seasonal), Unmatched SANs

## Setup

```bash
git clone https://github.com/your-org/energycap-qa-tool.git
cd energycap-qa-tool
pip install -r requirements.txt
streamlit run app.py
```

## GEM Estimate Quality Classification

| Classification | Trigger | Severity |
|---------------|---------|---------|
| Confirmed Bad | Before account start date, or no start date | Issue |
| Structurally Unreliable | Non-monthly meter, equal-split pattern | Risk |
| Suspect | Seasonal/event-driven commodity (diesel, propane, etc.) | Risk |
| Defensible | Steady-state commodity filling a bill gap | Info |

## File Naming Tips
The tool auto-detects report types from filenames. Name exports to include:
`Report-03`, `Report-11`, `GEM`, `emissions_matrix`, `energy_quantities`
