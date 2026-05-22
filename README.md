# EnergyCAP Pre-Export QA Tool

A Streamlit application for quality-assuring EnergyCAP data before it is exported to feed an emissions calculation tool.

## What it does

Ingests up to six EnergyCAP report exports, runs a comprehensive suite of QA checks, and produces:

- **QA Summary Dashboard** — metrics and check-by-check results
- **Risk Summary** — categorized risks with emissions impact descriptions
- **Issue Register** — confirmed data problems requiring correction in EnergyCAP (filterable, downloadable)
- **Risk Register** — records requiring human review before emissions calculation (filterable, downloadable)
- **Data Explorer** — raw data browser for each uploaded report

## Supported Reports

| Report | Name | Required |
|--------|------|----------|
| R-03 | Setup Report (Accounts / Vendors / Cost Centers / Meters / Sites) | ✅ Required |
| R-11 | Bill Transfer Format (bill-level detail with native use + UOM) | ✅ Required |
| R-13 | Bill Analysis (EnergyCAP outlier flags) | Optional |
| R-19 | Monthly Utility Use and Cost | Optional |
| R-21 | Monthly Comparison | Optional |
| R-26 | Use and Cost Summary | Optional |

## QA Checks Performed

### Bill-Level Checks (R-11)
| Check | Type | Emissions Impact |
|-------|------|-----------------|
| Missing Native Use | Issue | CRITICAL |
| Missing Common Use / kBTU conversion | Risk | HIGH |
| Negative Use Values | Issue | HIGH |
| Duplicate Bill IDs | Issue | CRITICAL |
| Missing Billing Periods (Gaps) | Issue | HIGH |
| Overlapping Billing Periods | Issue | HIGH |
| Unusual Billing Period Length (<5 or >95 days) | Risk | MEDIUM |
| Zero Use with Non-Zero Cost | Risk | MEDIUM |
| Non-Zero Use with Zero Cost | Risk | LOW |
| Consecutive Zero-Use Months | Risk | MEDIUM |
| Use Statistical Outliers (Z-score) | Risk | MEDIUM |
| Cost Statistical Outliers (Z-score) | Risk | LOW |
| Month-over-Month Use Change Spike | Risk | MEDIUM |
| UOM / Rate Schedule Inconsistency | Risk | HIGH |

### Setup Checks (R-03)
| Check | Type | Emissions Impact |
|-------|------|-----------------|
| Inactive Meters with Bills | Risk | HIGH |
| Missing Serial Numbers | Risk | LOW |
| Accounts Excluded from Audits | Risk | MEDIUM |
| Deregulated Market Meters | Risk | HIGH |
| Missing Acct-Meter Begin Dates | Issue | LOW |
| Active Meters with No Bills | Issue | HIGH |

### Cross-Report Checks (R-03 × R-11)
| Check | Type | Emissions Impact |
|-------|------|-----------------|
| Orphan Bills (meter in R-11 not in R-03) | Issue | HIGH |

## Setup

### 1. Clone the repository
```bash
git clone https://github.com/your-org/energycap-qa-tool.git
cd energycap-qa-tool
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the app
```bash
streamlit run app.py
```

The app will open at `http://localhost:8501`

## Usage

1. **Upload** your EnergyCAP Excel exports in the **Upload & Run** tab
   - The tool auto-detects report types from filename and column headers
   - Upload R-03 and R-11 at minimum; add R-13, R-19, R-21, R-26 for deeper analysis

2. **Configure** QA thresholds in the left sidebar:
   - Outlier Z-score threshold (default: 2.5)
   - Month-over-month % change alert (default: 50%)
   - Consecutive zero-use months to flag (default: 2)

3. **Click "Run QA Reconciliation"** — processing happens in seconds

4. **Review results** across the tabs:
   - QA Summary for the overall picture
   - Risk Summary for detailed risk explanations
   - Issue Register for records needing correction
   - Risk Register for records needing review

5. **Download** the Issue Register and Risk Register as CSV for tracking and remediation

## File Structure

```
energycap-qa-tool/
├── app.py              # Streamlit UI
├── qa_engine.py        # All QA check logic
├── utils.py            # Report loading, type detection, helpers
├── requirements.txt    # Python dependencies
└── README.md
```

## Report Naming Tips

The tool detects report types from filename hints. Name your exports to include the report number:
- `Report-03-Setup.xlsx` → detected as R-03
- `Report-11-Bills.xlsx` → detected as R-11
- `energycap_r19_monthly.xlsx` → detected as R-19

If detection fails, the tool will warn you and you can rename the file.

## Notes

- Date columns in EnergyCAP exports are Excel serial numbers — the tool converts these automatically
- R-11's `Native Use` column has no UOM label; commodity type is used as a proxy
- The tool does not modify any source data — it is read-only
- All processing happens locally; no data is sent externally
