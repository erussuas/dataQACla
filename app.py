"""
EnergyCAP Pre-Export QA Tool — v2.1
Single-file Streamlit app (utils + qa_engine + UI merged for Streamlit Cloud compatibility).
"""
"""
utils.py — Report loading, type detection, period detection, and helper functions
v2.1 — adds target-year awareness, R-19 multi-year handling
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
import warnings
warnings.filterwarnings("ignore")

# ── Report metadata ────────────────────────────────────────────────────────────
REPORT_LABELS = {
    "R03": "Setup Report (Accounts / Meters / Sites)",
    "R11": "Bill Transfer Format (Bill Detail + UOM)",
    "R13": "Bill Analysis (Outliers)",
    "R19": "Monthly Utility Use and Cost",
    "R21": "Monthly Comparison",
    "R26": "Use and Cost Summary",
    "GEM": "GEM Emissions Data Export",
}

REPORT_SIGNATURES = {
    "R03": ["account number", "meter status", "acct-meter begin date", "vendor code", "meter code"],
    "R11": ["bill id", "billing period", "native use", "common use", "commodity code", "place code"],
    "R13": ["bill id", "cost var dec", "use std dev", "est bill"],
    "R19": ["use kwh", "use therm", "demand"],
    "R21": ["var %", "ytd var %"],
    "R26": ["cost/day", "cost/unit", "#days"],
    "GEM": ["service aggregator number", "quantity for emissions", "country name"],
}

# ── Commodity mappings ─────────────────────────────────────────────────────────
GEM_TO_ECAP_COMMODITY = {
    "Electricity":                     ["ELECTRIC", "ELE", "ELECTRICITY"],
    "Natural Gas":                     ["NATURALGAS", "GAS", "NATGAS", "NG"],
    "Diesel":                          ["DIESEL", "DIE"],
    "Propane":                         ["PROPANE", "PRO", "LPG"],
    "LPG - Liquefied Petroleum Gases": ["LPG", "PROPANE", "PRO"],
    "Biomass":                         ["BIOMASS", "BIO"],
    "Fuel Oil":                        ["FUELOIL", "OIL"],
    "Steam":                           ["STEAM", "STM"],
    "Heat":                            ["HEAT", "HTG"],
    "Methane":                         ["METHANE", "GAS", "NATURALGAS"],
    "BioGas":                          ["BIOGAS", "BIO"],
    "Gasoline":                        ["GASOLINE"],
    "Coal":                            ["COAL"],
    "Butane":                          ["BUTANE", "LPG"],
    "Aviation Fuel":                   ["AVFUEL", "KEROSENE", "JET"],
}

# Commodity classification for GEM estimate quality
COMMODITY_ESTIMATE_CLASS = {
    "Electricity":                     "steady",
    "Natural Gas":                     "steady",
    "Steam":                           "steady",
    "Heat":                            "steady",
    "Fuel Oil":                        "seasonal",
    "Diesel":                          "seasonal",
    "Propane":                         "seasonal",
    "LPG - Liquefied Petroleum Gases": "seasonal",
    "Biomass":                         "seasonal",
    "Coal":                            "seasonal",
    "Methane":                         "seasonal",
    "BioGas":                          "seasonal",
    "Gasoline":                        "event",
    "Butane":                          "event",
    "Aviation Fuel":                   "event",
}

# Native unit → MWh conversion factors
NATIVE_TO_MWH = {
    "ELECTRIC":    1/1000,      # kWh → MWh
    "ELE":         1/1000,
    "ELECTRICITY": 1/1000,
    "NATURALGAS":  0.02931,     # CCF → MWh
    "GAS":         0.02931,
    "NATGAS":      0.02931,
    "NG":          0.02931,
    "THERM":       0.02931,
    "MCF":         0.2931,      # MCF → MWh
    "DIESEL":      0.03596,     # gallon → MWh
    "DIE":         0.03596,
    "PROPANE":     0.02558,     # gallon → MWh
    "PRO":         0.02558,
    "LPG":         0.02558,
    "FUELOIL":     0.04026,     # gallon → MWh (No. 2)
    "OIL":         0.04026,
    "STEAM":       0.000293,    # lb → MWh
    "STM":         0.000293,
    "COAL":        7.0,         # short ton → MWh
    "BIOMASS":     4.5,         # short ton → MWh (approx)
    "GASOLINE":    0.03374,     # gallon → MWh
    "BUTANE":      0.03247,     # gallon → MWh
    "AVFUEL":      0.03553,     # gallon → MWh (Jet-A)
    "KEROSENE":    0.03553,
    "METHANE":     0.02931,
    "BIOGAS":      0.02931,
}

NON_MONTHLY_FREQUENCIES = {
    "quarterly", "bimonthly", "bi-monthly", "bi monthly",
    "annual", "annually", "semi-annual", "semi annual",
    "biannual", "bi-annual",
}


# ── Excel serial date conversion ───────────────────────────────────────────────
def excel_serial_to_date(serial):
    try:
        serial = float(serial)
        if serial < 1:
            return None
        delta = timedelta(days=serial - 2)
        return (datetime(1899, 12, 31) + delta).date()
    except (TypeError, ValueError):
        return None


def parse_dates_column(series):
    def _parse(v):
        if pd.isna(v):
            return pd.NaT
        if isinstance(v, (int, float)):
            d = excel_serial_to_date(v)
            return pd.Timestamp(d) if d else pd.NaT
        try:
            return pd.to_datetime(v)
        except Exception:
            return pd.NaT
    return series.apply(_parse)


def billing_period_to_date(bp_str):
    try:
        return pd.to_datetime(str(bp_str), format="%Y%m")
    except Exception:
        return pd.NaT


def safe_zscore(series):
    mean = series.mean()
    std  = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series([0.0] * len(series), index=series.index)
    return (series - mean) / std


def format_issue_row(site, account, meter, bill_id, category,
                     severity, description, commodity=None, period=None):
    return {
        "Site":        site,
        "Account":     account,
        "Meter":       meter,
        "Bill ID":     bill_id,
        "Commodity":   commodity,
        "Period":      period,
        "Category":    category,
        "Severity":    severity,
        "Description": description,
    }


# ── Report loading ─────────────────────────────────────────────────────────────
def load_report(file_obj):
    """Load an uploaded file, detect its report type, and post-process."""
    name = file_obj.name.lower()

    # Filename hint
    rtype_hint = None
    gem_hints  = ["gem","emissions_matrix","energy_quantities","emissions matrix"]
    for h in gem_hints:
        if h.replace(" ","_").replace(" ","-") in name.replace(" ","_"):
            rtype_hint = "GEM"
            break
    if not rtype_hint:
        for code in ["r03","r11","r13","r19","r21","r26",
                     "report-03","report-11","report-13",
                     "report-19","report-21","report-26"]:
            clean = code.replace("-","").replace("_","")
            if clean in name.replace("-","").replace("_",""):
                num = ''.join(filter(str.isdigit, code))
                if num:
                    rtype_hint = f"R{num.zfill(2)}"
                break

    # Load file
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(file_obj)
        else:
            raw = pd.read_excel(file_obj, header=None)
            header_row = 0
            for i, row in raw.iterrows():
                non_null = row.dropna()
                str_vals = [v for v in non_null if isinstance(v, str) and len(v) > 1]
                if len(str_vals) >= 3:
                    header_row = i
                    break
            df = pd.read_excel(file_obj, header=header_row)
            df = df.dropna(how="all").reset_index(drop=True)
    except Exception as e:
        raise ValueError(f"Could not read file: {e}")

    df.columns = [str(c).strip() for c in df.columns]
    cols_lower  = [c.lower() for c in df.columns]

    # Detect type by column signatures
    rtype = rtype_hint
    if not rtype:
        best_match, best_score = None, 0
        for code, sigs in REPORT_SIGNATURES.items():
            score = sum(1 for s in sigs if any(s in c for c in cols_lower))
            if score > best_score:
                best_score = score
                best_match = code
        if best_score >= 2:
            rtype = best_match

    # GEM fallback — check raw header rows
    if not rtype and not name.endswith(".csv"):
        try:
            raw2 = pd.read_excel(file_obj, header=None, nrows=4)
            flat = " ".join(str(v).lower() for v in raw2.values.flatten() if pd.notna(v))
            if "quantity for emissions" in flat and "service aggregator" in flat:
                rtype = "GEM"
        except Exception:
            pass

    # Post-process
    if rtype == "GEM":
        df = _load_gem(file_obj)
    elif rtype == "R11":
        df = _process_r11(df)
    elif rtype == "R03":
        df = _process_r03(df)
    elif rtype == "R19":
        df = _process_r19(df)

    return df, rtype


def _load_gem(file_obj):
    """Parse GEM pivot export (3-row header) into a tidy DataFrame."""
    try:
        raw = pd.read_excel(file_obj, header=None)
    except Exception:
        raise ValueError("Could not read GEM file as Excel.")

    years  = raw.iloc[0, 8:20].tolist()
    months = raw.iloc[1, 8:20].tolist()
    month_cols_raw = []
    for y, m in zip(years, months):
        if pd.notna(y) and pd.notna(m):
            month_cols_raw.append(f"{int(y)}_{m}")

    n_cols    = raw.shape[1]
    base_cols = ['Customer','Country','Site','Resource','SAN',
                 'Deregulated_Type','Service_Vendor','Service_Account_Number']
    extra     = n_cols - 8 - len(month_cols_raw)
    all_cols  = base_cols + month_cols_raw + [f"Extra_{i}" for i in range(max(extra, 0))]
    all_cols  = all_cols[:n_cols]

    df = raw.iloc[3:, :n_cols].copy()
    df.columns = all_cols[:len(df.columns)]

    df = df[df['Customer'] == 'Sonoco ENC'].copy()
    df = df[df['SAN'].notna()].copy()
    df = df[df['SAN'].astype(str).str.strip() != 'Total'].copy()
    df = df[~df['SAN'].astype(str).str.startswith('Applied filters')].copy()
    df = df.reset_index(drop=True)

    for mc in month_cols_raw:
        if mc in df.columns:
            df[mc] = pd.to_numeric(df[mc], errors='coerce')

    df.attrs['month_cols'] = month_cols_raw
    df.attrs['year']       = int(years[0]) if not pd.isna(years[0]) else None
    df['Estimate_Class']   = df['Resource'].map(COMMODITY_ESTIMATE_CLASS).fillna('steady')

    return df


def _process_r11(df):
    for col in ["Start Date", "End Date"]:
        if col in df.columns:
            df[col] = parse_dates_column(df[col])
    for col in ["Native Use","Cost","Demand","Common Use","Total Cost","Days"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    if "Billing Period" in df.columns:
        df["Billing Period"] = df["Billing Period"].astype(str).str.strip()
    rename_map = {
        "Place Code":     "Site",
        "C Ctr Code":     "Cost Center",
        "Commodity Code": "Commodity",
        "Account Code":   "Account",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    return df


def _process_r03(df):
    for col in ["Acct-Meter Begin Date","Acct-Meter End Date",
                "Account Created Date","Meter Created Date","Account date close"]:
        if col in df.columns:
            df[col] = parse_dates_column(df[col])
    rename_map = {"Cost Center Code": "Cost Center", "Cost Center Name": "Cost Center Name"}
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    return df


def _process_r19(df):
    """
    R-19 may contain multiple years for YoY context.
    We tag each row with the year extracted from column headers or row data.
    The actual multi-year structure varies by export format — store as-is
    and let the engine extract what it needs by target year.
    """
    # Try to parse any date/period columns
    for col in df.columns:
        if any(x in col.lower() for x in ["date","period"]):
            try:
                df[col] = parse_dates_column(df[col])
            except Exception:
                pass
    df.attrs['report_type'] = 'R19'
    return df


# ── Period detection ───────────────────────────────────────────────────────────
def detect_period(report_type, df, target_year=None):
    """
    Return (begin_date, end_date) as pd.Timestamp.

    KEY RULE for R-19:
      R-19 may contain multiple years (e.g. 2024+2025) for YoY context.
      If target_year is given, R-19's period is clamped to that year only.
      Prior years in R-19 are reference data, not QA scope.
    """
    try:
        if report_type == "R11":
            if "Billing Period" in df.columns:
                bps   = df["Billing Period"].dropna().astype(str)
                bps   = bps[bps.str.match(r'^\d{6}$')]
                if bps.empty:
                    return None, None
                dates = pd.to_datetime(bps, format="%Y%m", errors='coerce').dropna()
                # If target_year given, restrict R-11 period to that year
                if target_year:
                    dates = dates[dates.dt.year == target_year]
                if dates.empty:
                    return None, None
                return dates.min(), dates.max() + pd.offsets.MonthEnd(0)

        elif report_type == "R19":
            # R-19: always clamp to target_year if provided, else return most recent year
            # Try to find year columns in the header
            year_cols = [c for c in df.columns
                         if str(c).strip().isdigit() and 1990 < int(str(c).strip()) < 2100]
            if year_cols:
                years = [int(str(c).strip()) for c in year_cols]
                ref_year = target_year if target_year and target_year in years else max(years)
                beg = pd.Timestamp(year=ref_year, month=1, day=1)
                end = pd.Timestamp(year=ref_year, month=12, day=31)
                return beg, end
            # Fallback: look for date columns
            for col in df.columns:
                if "date" in str(col).lower() or "period" in str(col).lower():
                    try:
                        dates = pd.to_datetime(df[col], errors='coerce').dropna()
                        if target_year:
                            dates = dates[dates.dt.year == target_year]
                        if not dates.empty:
                            return dates.min(), dates.max()
                    except Exception:
                        pass
            # Last resort: if target_year given, return that year
            if target_year:
                return (pd.Timestamp(year=target_year, month=1, day=1),
                        pd.Timestamp(year=target_year, month=12, day=31))
            return None, None

        elif report_type == "R03":
            col = "Acct-Meter Begin Date"
            if col in df.columns:
                d = df[col].dropna()
                if not d.empty:
                    if target_year:
                        return (pd.Timestamp(year=target_year, month=1, day=1),
                                pd.Timestamp(year=target_year, month=12, day=31))
                    return d.min(), pd.Timestamp.now()
            if target_year:
                return (pd.Timestamp(year=target_year, month=1, day=1),
                        pd.Timestamp(year=target_year, month=12, day=31))

        elif report_type == "GEM":
            mc   = df.attrs.get('month_cols', [])
            year = df.attrs.get('year', target_year or 2025)
            if target_year:
                year = target_year
            if mc:
                month_map = _month_label_map()
                valid = [c for c in mc if c in df.columns and
                         df[c].notna().any() and (df[c] > 0).any()]
                # Filter to target_year only
                if target_year:
                    valid = [c for c in valid if c.startswith(f"{target_year}_")]
                if valid:
                    first_mo = month_map.get(valid[0].split("_", 1)[1], 1)
                    last_mo  = month_map.get(valid[-1].split("_", 1)[1], 12)
                    beg = pd.Timestamp(year=year, month=first_mo, day=1)
                    end = pd.Timestamp(year=year, month=last_mo,  day=1) + pd.offsets.MonthEnd(0)
                    return beg, end
            if target_year:
                return (pd.Timestamp(year=target_year, month=1, day=1),
                        pd.Timestamp(year=target_year, month=12, day=31))

    except Exception:
        pass
    return None, None


def _month_label_map():
    return {"01-Jan":1,"02-Feb":2,"03-Mar":3,"04-Apr":4,
            "05-May":5,"06-Jun":6,"07-Jul":7,"08-Aug":8,
            "09-Sep":9,"10-Oct":10,"11-Nov":11,"12-Dec":12}


def compute_overlap(periods: dict):
    """Compute the overlapping window across all report periods."""
    valids = [(b, e) for b, e in periods.values()
              if b is not None and e is not None]
    if not valids:
        return None, None
    latest_begin  = max(b for b, e in valids)
    earliest_end  = min(e for b, e in valids)
    if latest_begin <= earliest_end:
        return latest_begin, earliest_end
    return None, None


def filter_r11_to_year(r11, target_year):
    """Filter R-11 to bills in the target year only."""
    if target_year is None or "Billing Period" not in r11.columns:
        return r11
    mask = r11["Billing Period"].astype(str).str[:4] == str(target_year)
    return r11[mask].copy()


def filter_gem_to_year(gem, target_year):
    """Zero out GEM monthly columns outside the target year."""
    if target_year is None or not hasattr(gem, 'attrs'):
        return gem
    mc   = gem.attrs.get('month_cols', [])
    year = gem.attrs.get('year', target_year)
    gem  = gem.copy()
    gem.attrs = {"month_cols": mc, "year": year}
    for col in mc:
        if col not in gem.columns:
            continue
        col_year = col.split("_")[0]
        try:
            if int(col_year) != target_year:
                gem[col] = np.nan
        except Exception:
            pass
    return gem


def extract_r19_reference_years(r19, target_year):
    """
    Extract prior-year monthly totals from R-19 for use in YoY and
    seasonal baseline checks. Returns a dict: {year: {meter_or_site: {month: value}}}.
    This is best-effort — R-19 format varies by EnergyCAP export settings.
    """
    # R-19 column parsing is complex due to variable export formats.
    # We return an empty dict for now; the engine handles absence gracefully.
    return {}


# ══ QA ENGINE ══

"""
qa_engine.py — All QA and GEM reconciliation logic.
v2.1 — target-year awareness, refined seasonal estimate classification
"""

import pandas as pd
import numpy as np
# (utils merged into single file)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_all_checks(reports, target_year=None,
                   overlap_begin=None, overlap_end=None,
                   outlier_zscore=2.5, pct_change_thresh=50,
                   zero_use_months=2, acct_start_year_threshold=2000,
                   delivery_tracking_threshold=4,
                   gem_magnitude_pct=10,
                   conversion_factors=None):
    """
    Run all QA + GEM reconciliation checks.

    target_year         — the year being QA'd; R-11/GEM filtered to this year;
                          R-19 prior years used only as reference baselines.
    delivery_tracking_threshold — meters with fewer non-zero months than this
                          are classified as delivery-tracked (seasonal estimate → Confirmed Bad).
    gem_magnitude_pct   — GEM estimates below this % of a meter's average non-zero
                          month are flagged as quantitatively implausible.
    """
    if conversion_factors is None:
        conversion_factors = NATIVE_TO_MWH

    r03 = reports.get("R03")
    r11 = reports.get("R11")
    r19 = reports.get("R19")
    gem = reports.get("GEM")

    # ── Build R-19 baseline (prior-year monthly actuals) ──────────────────────
    r19_baseline = _build_r19_baseline(r19, target_year) if r19 is not None else {}

    check_results = []
    all_issues    = []
    all_risks     = []

    # ── R-11 checks ───────────────────────────────────────────────────────────
    if r11 is not None:
        r11_checks = [
            check_missing_native_use(r11),
            check_missing_common_use(r11),
            check_negative_use(r11),
            check_duplicate_bills(r11),
            check_billing_period_gaps(r11),
            check_overlapping_bills(r11),
            check_days_anomaly(r11),
            check_zero_use_nonzero_cost(r11),
            check_zero_cost_nonzero_use(r11),
            check_consecutive_zero_use(r11, zero_use_months),
            check_use_outliers(r11, outlier_zscore),
            check_cost_outliers(r11, outlier_zscore),
            check_mom_change(r11, pct_change_thresh),
            check_uom_consistency(r11),
        ]
        check_results.extend(r11_checks)
        for c in r11_checks:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks",  []))

    # ── R-03 checks ───────────────────────────────────────────────────────────
    if r03 is not None:
        r03_checks = [
            check_inactive_meters(r03, r11),
            check_missing_serial(r03),
            check_excluded_from_audits(r03),
            check_deregulated_market(r03),
            check_missing_acct_meter_dates(r03),
            check_account_start_date(r03, acct_start_year_threshold),
            check_non_monthly_billing(r03),
            check_meter_no_bills(r03, r11),
        ]
        check_results.extend(r03_checks)
        for c in r03_checks:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks",  []))

    # ── Cross-report R03 × R11 ────────────────────────────────────────────────
    if r03 is not None and r11 is not None:
        cross = [check_orphan_bills(r03, r11)]
        check_results.extend(cross)
        for c in cross:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks",  []))

    # ── GEM reconciliation ────────────────────────────────────────────────────
    gem_results = {}
    if gem is not None and r11 is not None:
        gem_results = run_gem_reconciliation(
            gem, r11, r03,
            target_year=target_year,
            overlap_begin=overlap_begin,
            overlap_end=overlap_end,
            acct_start_year_threshold=acct_start_year_threshold,
            delivery_tracking_threshold=delivery_tracking_threshold,
            gem_magnitude_pct=gem_magnitude_pct,
            r19_baseline=r19_baseline,
            conversion_factors=conversion_factors,
        )
        check_results.extend(gem_results.get("check_results", []))
        all_issues.extend(gem_results.get("issues", []))
        all_risks.extend(gem_results.get("risks",  []))

    # ── Build registers ────────────────────────────────────────────────────────
    cols = ["Site","Account","Meter","Bill ID","Commodity",
            "Period","Category","Severity","Description"]
    issues_df = pd.DataFrame(all_issues)[cols] if all_issues else pd.DataFrame(columns=cols)
    risks_df  = pd.DataFrame(all_risks)[cols]  if all_risks  else pd.DataFrame(columns=cols)

    # ── Meta ──────────────────────────────────────────────────────────────────
    meta = {}
    if r11 is not None:
        meta["total_bills"]       = len(r11)
        meta["total_sites"]       = r11["Site"].nunique()       if "Site"       in r11.columns else 0
        meta["total_meters"]      = r11["Meter Code"].nunique() if "Meter Code" in r11.columns else 0
        meta["total_commodities"] = r11["Commodity"].nunique()  if "Commodity"  in r11.columns else 0
    else:
        meta = {"total_bills":0,"total_sites":0,"total_meters":0,"total_commodities":0}

    if gem is not None:
        meta["gem_rows"]  = len(gem)
        meta["gem_sites"] = gem["Site"].nunique() if "Site" in gem.columns else 0

    return {
        "check_results": check_results,
        "issues_df":     issues_df,
        "risks_df":      risks_df,
        "meta":          meta,
        "gem_detail":    gem_results.get("detail_df", pd.DataFrame()),
    }


# ══════════════════════════════════════════════════════════════════════════════
# R-19 BASELINE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_r19_baseline(r19, target_year):
    """
    Extract prior-year same-month actuals from R-19 for use in seasonal
    estimate refinement. Returns:
      {meter_or_site_key: {month_int: [list of prior-year values]}}
    """
    if r19 is None or target_year is None:
        return {}
    # R-19 export format varies widely; we do best-effort extraction.
    # Look for columns that contain year numbers != target_year
    baseline = {}
    try:
        prior_year = target_year - 1
        # Try to find columns named like the prior year or containing prior-year dates
        for col in r19.columns:
            col_str = str(col)
            if str(prior_year) in col_str:
                # This column likely contains prior-year data
                # Use the column index to infer month (common R-19 layout: 12 cols per year)
                pass  # Real parsing depends on actual export layout
    except Exception:
        pass
    return baseline  # Returns empty if parsing fails — handled gracefully downstream


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _result(name, description, emissions_impact, flagged_df,
            issues=None, risks=None, is_risk=False):
    count  = len(flagged_df) if flagged_df is not None and not flagged_df.empty else 0
    status = "ok"
    if count > 0:
        status = "warning" if is_risk else "error"
    return {
        "name":             name,
        "description":      description,
        "emissions_impact": emissions_impact,
        "status":           status,
        "count":            count,
        "sample":           flagged_df.head(10) if flagged_df is not None and count > 0
                            else pd.DataFrame(),
        "issues":           issues or [],
        "risks":            risks  or [],
    }


def _to_issues(df, category, severity, desc_col=None, default_desc=""):
    rows = []
    for _, r in df.iterrows():
        rows.append(format_issue_row(
            site=r.get("Site",""), account=r.get("Account", r.get("Account Code","")),
            meter=r.get("Meter Code",""), bill_id=r.get("Bill ID",""),
            category=category, severity=severity,
            description=r[desc_col] if desc_col and desc_col in r.index else default_desc,
            commodity=r.get("Commodity",""), period=r.get("Billing Period",""),
        ))
    return rows


def _to_risks(df, category, desc_col=None, default_desc=""):
    return _to_issues(df, category, "Risk", desc_col, default_desc)


# ══════════════════════════════════════════════════════════════════════════════
# R-11 CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_missing_native_use(r11):
    name    = "Missing Native Use"
    flagged = r11[r11["Native Use"].isna()].copy()
    flagged["_desc"] = "Native Use is null"
    return _result(name,
        "Bills where Native Use is null — cannot contribute to emissions.",
        "CRITICAL — null use means zero contribution to Scope 1/2 totals.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


def check_missing_common_use(r11):
    name = "Missing Common Use (kBTU conversion)"
    if "Common Use" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r11[
        r11["Native Use"].notna() & (r11["Native Use"] != 0) &
        (r11["Common Use"].isna() | (r11["Common Use"] == 0))
    ].copy()
    flagged["_desc"] = "Native Use present but Common Use (kBTU) is blank or zero"
    return _result(name,
        "Native Use present but kBTU conversion is blank.",
        "HIGH — emissions tools using Common Use as input will drop these bills.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_negative_use(r11):
    name    = "Negative Use Values"
    flagged = r11[r11["Native Use"].notna() & (r11["Native Use"] < 0)].copy()
    flagged["_desc"] = flagged["Native Use"].apply(lambda v: f"Native Use = {v:.2f}")
    return _result(name,
        "Bills with negative Native Use — usually a correction bill.",
        "HIGH — negative values will subtract from totals.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


def check_duplicate_bills(r11):
    name = "Duplicate Bill IDs"
    if "Bill ID" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    dupes = r11[r11["Bill ID"].notna() &
                r11.duplicated(subset=["Bill ID"], keep=False)].copy()
    dupes["_desc"] = dupes["Bill ID"].apply(
        lambda v: f"Bill ID {v} appears multiple times")
    return _result(name,
        "Same Bill ID appears more than once.",
        "CRITICAL — duplicates will double-count consumption.",
        dupes, issues=_to_issues(dupes, name, "Critical", "_desc"))


def check_billing_period_gaps(r11):
    name = "Missing Billing Periods (Gaps)"
    if "Meter Code" not in r11.columns or "Billing Period" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    gap_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        bps = grp["Billing Period"].dropna().unique()
        try:
            dates = sorted([billing_period_to_date(bp) for bp in bps
                            if pd.notna(billing_period_to_date(bp))])
        except Exception:
            continue
        if len(dates) < 2:
            continue
        expected = pd.date_range(dates[0], dates[-1], freq="MS")
        actual   = set(str(d)[:7] for d in dates)
        for exp in expected:
            if str(exp)[:7] not in actual:
                gap_rows.append({
                    "Site":       grp["Site"].iloc[0] if "Site" in grp.columns else "",
                    "Account":    grp["Account"].iloc[0] if "Account" in grp.columns else "",
                    "Meter Code": meter,
                    "Commodity":  grp["Commodity"].iloc[0] if "Commodity" in grp.columns else "",
                    "Bill ID":    "", "Billing Period": str(exp)[:7],
                    "_desc":      f"No bill found for {str(exp)[:7]}",
                })
    flagged = pd.DataFrame(gap_rows)
    return _result(name,
        "Meters with missing months in billing history.",
        "HIGH — missing months understate annual emissions.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


def check_overlapping_bills(r11):
    name = "Overlapping Billing Periods"
    if not all(c in r11.columns for c in ["Meter Code","Start Date","End Date"]):
        return _result(name, "", "", pd.DataFrame())
    overlap_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.dropna(subset=["Start Date","End Date"])\
                 .sort_values("Start Date").reset_index(drop=True)
        for i in range(len(grp) - 1):
            end_i   = grp.loc[i,   "End Date"]
            start_j = grp.loc[i+1, "Start Date"]
            if pd.notna(end_i) and pd.notna(start_j) and start_j < end_i:
                overlap_rows.append({
                    "Site":       grp.loc[i,"Site"] if "Site" in grp.columns else "",
                    "Account":    grp.loc[i,"Account"] if "Account" in grp.columns else "",
                    "Meter Code": meter,
                    "Commodity":  grp.loc[i,"Commodity"] if "Commodity" in grp.columns else "",
                    "Bill ID":    grp.loc[i,"Bill ID"], "Billing Period": "",
                    "_desc":      f"Bill {grp.loc[i,'Bill ID']} ends {end_i.date()} "
                                  f"overlaps next bill starting {start_j.date()}",
                })
    flagged = pd.DataFrame(overlap_rows)
    return _result(name,
        "Two bills for the same meter have overlapping date ranges.",
        "HIGH — overlaps inflate consumption and emissions.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


def check_days_anomaly(r11):
    name = "Unusual Billing Period Length"
    if "Days" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r11[(r11["Days"].notna()) &
                  ((r11["Days"] < 5) | (r11["Days"] > 95))].copy()
    flagged["_desc"] = flagged["Days"].apply(
        lambda d: f"Billing period = {int(d)} days")
    return _result(name,
        "Bills with <5 or >95 days — may be catch-up bills or errors.",
        "MEDIUM — affects calendarization.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_zero_use_nonzero_cost(r11):
    name = "Zero Use with Non-Zero Cost"
    if "Total Cost" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r11[
        (r11["Native Use"].isna() | (r11["Native Use"] == 0)) &
        (r11["Total Cost"].notna()) & (r11["Total Cost"] > 0)
    ].copy()
    flagged["_desc"] = "Native Use = 0 but Total Cost > 0"
    return _result(name,
        "Bill shows zero use but has a cost (demand/standby charges).",
        "MEDIUM — confirms no consumption; cost may need separate treatment.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_zero_cost_nonzero_use(r11):
    name = "Non-Zero Use with Zero Cost"
    if "Total Cost" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r11[
        (r11["Native Use"].notna()) & (r11["Native Use"] > 0) &
        ((r11["Total Cost"].isna()) | (r11["Total Cost"] == 0))
    ].copy()
    flagged["_desc"] = "Native Use > 0 but Total Cost = 0"
    return _result(name,
        "Bill has use but $0 cost — common for manually tracked commodities.",
        "LOW — verify this is intentional.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_consecutive_zero_use(r11, threshold=2):
    name = f"Consecutive Zero-Use Months (≥{threshold})"
    if "Meter Code" not in r11.columns or "Billing Period" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.sort_values("Billing Period")
        consecutive = 0; streak_start = None
        for _, row in grp.iterrows():
            use = row.get("Native Use", 0)
            if pd.isna(use) or use == 0:
                if consecutive == 0:
                    streak_start = row.get("Billing Period","")
                consecutive += 1
            else:
                if consecutive >= threshold:
                    flagged_rows.append({
                        "Site":       row.get("Site",""),
                        "Account":    row.get("Account",""),
                        "Meter Code": meter,
                        "Commodity":  row.get("Commodity",""),
                        "Bill ID":"", "Billing Period": streak_start,
                        "_desc":      f"{consecutive} consecutive zero-use months from {streak_start}",
                    })
                consecutive = 0; streak_start = None
        if consecutive >= threshold:
            flagged_rows.append({
                "Site":       grp["Site"].iloc[-1] if "Site" in grp.columns else "",
                "Account":    grp["Account"].iloc[-1] if "Account" in grp.columns else "",
                "Meter Code": meter,
                "Commodity":  grp["Commodity"].iloc[-1] if "Commodity" in grp.columns else "",
                "Bill ID":"", "Billing Period": streak_start,
                "_desc":      f"{consecutive} consecutive zero-use months from {streak_start}",
            })
    flagged = pd.DataFrame(flagged_rows)
    return _result(name,
        f"Meters with {threshold}+ consecutive months of zero use.",
        "MEDIUM — may indicate data gaps.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_use_outliers(r11, z_thresh=2.5):
    name = f"Use Outliers (Z-score > {z_thresh})"
    if "Meter Code" not in r11.columns or "Native Use" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_list = []
    for meter, grp in r11.groupby("Meter Code"):
        use = grp["Native Use"].dropna()
        if len(use) < 4:
            continue
        z = safe_zscore(use)
        outliers = grp.loc[z[z.abs() > z_thresh].index].copy()
        if not outliers.empty:
            outliers["Z-score"] = z[z.abs() > z_thresh].round(2)
            outliers["_desc"]   = outliers.apply(
                lambda r: f"Use={r['Native Use']:.1f}, Z={r['Z-score']:.2f}", axis=1)
            flagged_list.append(outliers)
    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    return _result(name,
        "Bills with statistically unusual use.",
        "MEDIUM — outliers can skew annual totals.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_cost_outliers(r11, z_thresh=2.5):
    name = f"Cost Outliers (Z-score > {z_thresh})"
    if "Meter Code" not in r11.columns or "Total Cost" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_list = []
    for meter, grp in r11.groupby("Meter Code"):
        cost = grp["Total Cost"].dropna()
        if len(cost) < 4:
            continue
        z = safe_zscore(cost)
        outliers = grp.loc[z[z.abs() > z_thresh].index].copy()
        if not outliers.empty:
            outliers["Z-score"] = z[z.abs() > z_thresh].round(2)
            outliers["_desc"]   = outliers.apply(
                lambda r: f"Cost={r['Total Cost']:.2f}, Z={r['Z-score']:.2f}", axis=1)
            flagged_list.append(outliers)
    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    return _result(name,
        "Bills with statistically unusual cost.",
        "LOW — may indicate billing errors.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_mom_change(r11, pct_thresh=50):
    name = f"Month-over-Month Use Change > {pct_thresh}%"
    if "Meter Code" not in r11.columns or "Native Use" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_list = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.sort_values("Billing Period").copy()
        grp["prior_use"]  = grp["Native Use"].shift(1)
        grp["pct_change"] = np.where(
            grp["prior_use"].notna() & (grp["prior_use"] != 0),
            ((grp["Native Use"] - grp["prior_use"]) / grp["prior_use"].abs()) * 100,
            np.nan)
        spikes = grp[grp["pct_change"].abs() > pct_thresh].copy()
        if not spikes.empty:
            spikes["_desc"] = spikes.apply(
                lambda r: f"Use changed {r['pct_change']:.1f}% vs prior month "
                          f"({r['prior_use']:.1f} → {r['Native Use']:.1f})", axis=1)
            flagged_list.append(spikes)
    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    return _result(name,
        f"Use changes more than {pct_thresh}% vs prior month.",
        "MEDIUM — large swings may indicate missing bills.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_uom_consistency(r11):
    name = "UOM / Rate Schedule Inconsistency"
    if "Meter Code" not in r11.columns or "Rate Schedule" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        unique_rates = grp["Rate Schedule"].dropna().unique()
        if len(unique_rates) > 1:
            flagged_rows.append({
                "Site":       grp["Site"].iloc[0] if "Site" in grp.columns else "",
                "Account":    grp["Account"].iloc[0] if "Account" in grp.columns else "",
                "Meter Code": meter,
                "Commodity":  grp["Commodity"].iloc[0] if "Commodity" in grp.columns else "",
                "Bill ID":"", "Billing Period":"",
                "_desc":      f"Rate schedule changes: {' | '.join(str(r) for r in unique_rates)}",
            })
    flagged = pd.DataFrame(flagged_rows)
    return _result(name,
        "A meter's rate schedule changes between billing periods — possible UOM shift.",
        "HIGH — applying wrong emission factor to wrong UOM silently corrupts Scope 1/2.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


# ══════════════════════════════════════════════════════════════════════════════
# R-03 CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_inactive_meters(r03, r11):
    name = "Inactive Meters with Bills"
    if r11 is None or "Meter Status" not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    inactive = r03[r03["Meter Status"].str.lower().str.strip() == "inactive"
                   ]["Meter Code"].unique()
    flagged  = r11[r11["Meter Code"].isin(inactive)].copy() if len(inactive) else pd.DataFrame()
    if not flagged.empty:
        flagged["_desc"] = "Meter is Inactive in R-03 but has bills"
    return _result(name,
        "Inactive meters that still have bills in R-11.",
        "HIGH — emissions tools may exclude inactive meters.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_missing_serial(r03):
    name = "Missing Meter Serial Numbers"
    if "Serial Number" not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    no_serial = r03[
        r03["Serial Number"].isna() |
        r03["Serial Number"].astype(str).str.upper().str.strip().isin(
            ["NO METER NUMBER","NOT METERED",""])
    ].copy()
    risk_rows = []
    for _, r in no_serial.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description="No serial number assigned", commodity="", period=""))
    return _result(name, "Meters with no serial number.", "LOW",
                   no_serial, risks=risk_rows, is_risk=True)


def check_excluded_from_audits(r03):
    name = "Accounts Excluded from Audits"
    col  = "Excluded From Audits (Y/N)"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r03[r03[col].astype(str).str.upper().str.strip() == "YES"].copy()
    risk_rows = []
    for _, r in flagged.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description="Excluded from EnergyCAP audit checks", commodity="", period=""))
    return _result(name,
        "Accounts bypassing EnergyCAP's internal outlier checks.",
        "MEDIUM — data quality issues won't be caught internally.",
        flagged, risks=risk_rows, is_risk=True)


def check_deregulated_market(r03):
    name = "Deregulated Market Meters"
    col  = "GEM Deregulated Mkt"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    dereg = r03[r03[col].notna() & (r03[col] != 0) & (r03[col] != "")].copy()
    risk_rows = []
    for _, r in dereg.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description="Deregulated market — verify both distribution and supply bills present",
            commodity="", period=""))
    return _result(name,
        "Meters in deregulated markets — distribution and supply may be separate.",
        "HIGH — missing supply bills means incomplete Scope 2 market-based calculation.",
        dereg, risks=risk_rows, is_risk=True)


def check_missing_acct_meter_dates(r03):
    name = "Missing Account-Meter Begin Dates"
    col  = "Acct-Meter Begin Date"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r03[r03[col].isna()].copy()
    issue_rows = []
    for _, r in flagged.iterrows():
        issue_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Critical",
            description="Acct-Meter Begin Date not set — GEM may backfill estimates before meter existed",
            commodity="", period=""))
    return _result(name,
        "Meters with no begin date — GEM may estimate for periods before meter existed.",
        "HIGH — causes fabricated GEM estimates prior to meter activation.",
        flagged, issues=issue_rows)


def check_account_start_date(r03, threshold_year=2000):
    name      = "Suspicious Account Start Date"
    col       = "Acct-Meter Begin Date"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    threshold = pd.Timestamp(year=threshold_year, month=1, day=1)
    flagged_rows = []
    for _, r in r03.iterrows():
        date_val = r.get(col)
        desc = None
        if pd.isna(date_val):
            desc = "Acct-Meter Begin Date is empty"
        elif pd.notna(date_val) and pd.Timestamp(date_val) < threshold:
            desc = (f"Acct-Meter Begin Date ({pd.Timestamp(date_val).date()}) "
                    f"is before {threshold_year} — verify")
        if desc:
            flagged_rows.append({
                "Site": r.get("Cost Center",""),
                "Account Number": r.get("Account Number",""),
                "Meter Code": r.get("Meter Code",""),
                "Acct-Meter Begin Date": date_val,
                "_desc": desc,
            })
    flagged   = pd.DataFrame(flagged_rows)
    risk_rows = []
    for _, r in flagged.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Site",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description=r["_desc"], commodity="", period=""))
    return _result(name,
        f"Accounts with null or pre-{threshold_year} start dates. "
        "GEM may generate estimates for periods before the account existed.",
        "HIGH — fabricated GEM estimates corrupt historical emissions baselines.",
        flagged, risks=risk_rows, is_risk=True)


def check_non_monthly_billing(r03):
    name         = "Non-Monthly Billing Frequency"
    flagged_rows = []
    for col in ["Bill Frequency","Billing Frequency"]:
        if col not in r03.columns:
            continue
        mask = r03[col].astype(str).str.lower().str.strip().isin(NON_MONTHLY_FREQUENCIES)
        for _, r in r03[mask].iterrows():
            flagged_rows.append({
                "Site": r.get("Cost Center",""),
                "Account Number": r.get("Account Number",""),
                "Meter Code": r.get("Meter Code",""),
                "Frequency": r.get(col,""),
                "_desc": (f"Billing frequency is '{r.get(col,'')}' — "
                          "GEM monthly distribution is an approximation"),
            })
    flagged = pd.DataFrame(flagged_rows).drop_duplicates(subset=["Meter Code"]) \
              if flagged_rows else pd.DataFrame()
    risk_rows = []
    for _, r in flagged.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Site",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description=r.get("_desc","Non-monthly billing frequency"),
            commodity="", period=""))
    return _result(name,
        "Meters billed quarterly, bi-monthly, or annually. "
        "GEM spreads the bill equally across months.",
        "MEDIUM — monthly GEM values are approximations, not metered monthly data.",
        flagged, risks=risk_rows, is_risk=True)


def check_meter_no_bills(r03, r11):
    name = "Active Meters with No Bills"
    if r11 is None or "Meter Code" not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    active  = r03[r03["Meter Status"].str.lower().str.strip() == "active"
                  ]["Meter Code"].unique() \
              if "Meter Status" in r03.columns else r03["Meter Code"].unique()
    billed   = r11["Meter Code"].unique()
    no_bills = set(active) - set(billed)
    flagged  = r03[r03["Meter Code"].isin(no_bills)].copy()
    issue_rows = []
    for _, r in flagged.iterrows():
        issue_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Critical",
            description="Active meter has no bills in R-11 export",
            commodity="", period=""))
    return _result(name,
        "Active meters in R-03 with no bills in R-11.",
        "HIGH — active meters with no bills likely represent missing data.",
        flagged, issues=issue_rows)


def check_orphan_bills(r03, r11):
    name = "Bills for Unknown Meters (Orphan Bills)"
    if "Meter Code" not in r03.columns or "Meter Code" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    known   = set(r03["Meter Code"].dropna().unique())
    flagged = r11[~r11["Meter Code"].isin(known)].copy()
    if not flagged.empty:
        flagged["_desc"] = "Meter Code not found in R-03 setup report"
    return _result(name,
        "Bills in R-11 whose Meter Code doesn't appear in R-03.",
        "HIGH — orphan bills may be excluded from site rollups.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


# ══════════════════════════════════════════════════════════════════════════════
# GEM RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════════

def run_gem_reconciliation(gem, r11, r03,
                           target_year=None,
                           overlap_begin=None, overlap_end=None,
                           acct_start_year_threshold=2000,
                           delivery_tracking_threshold=4,
                           gem_magnitude_pct=10,
                           r19_baseline=None,
                           conversion_factors=None):
    if conversion_factors is None:
        conversion_factors = NATIVE_TO_MWH
    if r19_baseline is None:
        r19_baseline = {}

    month_cols = gem.attrs.get('month_cols', [])
    gem_year   = gem.attrs.get('year', target_year or 2025)
    month_map  = _month_label_map()

    # ── Precompute R-11 meter profiles for seasonal refinement ────────────────
    r11_meter_profiles = _build_meter_profiles(r11, gem_year)

    # ── Build R-11 monthly pivot (MWh) ────────────────────────────────────────
    r11_pivot = _build_r11_pivot(r11, gem_year, conversion_factors)

    # ── Build GEM long format ──────────────────────────────────────────────────
    gem_long = _gem_to_long(gem, month_cols, month_map, gem_year)

    # ── Match GEM SANs to EnergyCAP Meter Codes ───────────────────────────────
    gem_long = _match_san_to_meter(gem_long, r11, r03)

    # ── Merge ─────────────────────────────────────────────────────────────────
    merged = pd.merge(gem_long, r11_pivot,
                      on=["Meter Code","Year","Month"],
                      how="outer", suffixes=("_gem","_ecap"))

    # Apply overlap period filter
    if overlap_begin and overlap_end:
        def in_overlap(row):
            try:
                d = pd.Timestamp(year=int(row["Year"]), month=int(row["Month"]), day=1)
                return overlap_begin <= d <= overlap_end
            except Exception:
                return True
        merged = merged[merged.apply(in_overlap, axis=1)].copy()

    # ── Compute variance ───────────────────────────────────────────────────────
    merged["GEM_MWh"]  = pd.to_numeric(
        merged.get("Qty_MWh_gem",  merged.get("Qty_MWh", np.nan)), errors='coerce')
    merged["ECAP_MWh"] = pd.to_numeric(
        merged.get("Qty_MWh_ecap", merged.get("Qty_MWh", np.nan)), errors='coerce')
    merged["Delta_MWh"] = merged["GEM_MWh"] - merged["ECAP_MWh"]
    merged["Delta_Pct"] = np.where(
        merged["ECAP_MWh"].notna() & (merged["ECAP_MWh"] != 0),
        (merged["Delta_MWh"] / merged["ECAP_MWh"].abs()) * 100,
        np.nan)

    # ── Classify GEM estimates ─────────────────────────────────────────────────
    merged = _classify_gem_rows(
        merged, r03, r11_meter_profiles, r19_baseline,
        acct_start_year_threshold, delivery_tracking_threshold,
        gem_magnitude_pct,
    )

    # ── Generate checks, issues, risks ────────────────────────────────────────
    checks, issues, risks = _gem_checks_from_merged(merged)

    return {
        "check_results": checks,
        "issues":        issues,
        "risks":         risks,
        "detail_df":     merged,
    }


# ── Meter profile builder ──────────────────────────────────────────────────────

def _build_meter_profiles(r11, target_year):
    """
    For each meter, compute:
      - nonzero_months:  count of months with use > 0 in target_year
      - avg_nonzero_use: average of non-zero monthly use values
      - zero_months_set: set of month integers that are zero/missing
      - commodity:       commodity code
      - billing_pattern: "delivery" if sparse, "continuous" otherwise
    """
    profiles = {}
    if r11 is None or "Meter Code" not in r11.columns:
        return profiles

    for meter, grp in r11.groupby("Meter Code"):
        use_by_month = {}
        for _, row in grp.iterrows():
            bp = str(row.get("Billing Period",""))
            if len(bp) == 6:
                try:
                    yr = int(bp[:4])
                    mo = int(bp[4:6])
                    if yr == target_year:
                        use = pd.to_numeric(row.get("Native Use", np.nan), errors='coerce')
                        use_by_month[mo] = float(use) if pd.notna(use) else 0.0
                except Exception:
                    pass

        nonzero_vals  = [v for v in use_by_month.values() if v > 0]
        zero_months   = {mo for mo, v in use_by_month.items() if v == 0}
        commodity     = grp["Commodity"].iloc[0] if "Commodity" in grp.columns else ""

        profiles[meter] = {
            "nonzero_months":  len(nonzero_vals),
            "avg_nonzero_use": np.mean(nonzero_vals) if nonzero_vals else 0.0,
            "zero_months_set": zero_months,
            "commodity":       str(commodity).upper(),
            "billing_pattern": "delivery" if len(nonzero_vals) < 4 else "continuous",
        }
    return profiles


# ── GEM row classifier (the heart of seasonal refinement) ─────────────────────

def _classify_gem_rows(merged, r03, meter_profiles, r19_baseline,
                       threshold_year, delivery_threshold, magnitude_pct):
    """
    Classify each merged row with one of:
      Normal | Defensible Estimate | Structurally Unreliable Estimate |
      Suspect Estimate - Standard | Suspect Estimate - Magnitude |
      Confirmed Bad Estimate - Before Start Date |
      Confirmed Bad Estimate - Delivery Tracked |
      Confirmed Bad Estimate - No Start Date
    """
    # Build lookup tables from R-03
    begin_dates  = {}
    billing_freq = {}
    if r03 is not None:
        bd_col = "Acct-Meter Begin Date"
        if bd_col in r03.columns and "Meter Code" in r03.columns:
            for _, r in r03.iterrows():
                mc = str(r.get("Meter Code",""))
                bd = r.get(bd_col)
                if mc and pd.notna(bd):
                    begin_dates[mc] = pd.Timestamp(bd)
        for col in ["Bill Frequency","Billing Frequency"]:
            if col in r03.columns:
                for _, r in r03.iterrows():
                    mc   = str(r.get("Meter Code",""))
                    freq = str(r.get(col,"")).lower().strip()
                    if mc:
                        billing_freq[mc] = freq

    # Detect consecutive-identical values per GEM SAN (estimation fingerprint)
    identical_sans = _detect_identical_consecutive(merged)

    def classify(row):
        mc    = str(row.get("Meter Code","") or "")
        yr    = int(row.get("Year", 0)   or 0)
        mo    = int(row.get("Month", 0)  or 0)
        gem   = row.get("GEM_MWh",  np.nan)
        ecap  = row.get("ECAP_MWh", np.nan)
        ecls  = str(row.get("Estimate_Class","steady"))
        san   = str(row.get("GEM_SAN","") or "")

        has_gem  = pd.notna(gem)  and float(gem)  != 0
        has_ecap = pd.notna(ecap) and float(ecap) != 0

        # Not an estimate situation
        if not has_gem or has_ecap:
            return "Normal"

        # ── CONFIRMED BAD checks (highest priority) ───────────────────────────

        # 1. No start date at all in R-03
        if mc not in begin_dates and r03 is not None and "Acct-Meter Begin Date" in r03.columns:
            return "Confirmed Bad Estimate — No Start Date"

        # 2. Before account start date
        if mc in begin_dates:
            try:
                row_date = pd.Timestamp(year=yr, month=mo, day=1)
                if row_date < begin_dates[mc]:
                    return "Confirmed Bad Estimate — Before Start Date"
            except Exception:
                pass

        # 3. Delivery-tracked meter (very sparse non-zero months)
        profile = meter_profiles.get(mc, {})
        if (ecls in ("seasonal","event") and
                profile.get("billing_pattern","") == "delivery" and
                profile.get("nonzero_months", 12) < delivery_threshold):
            return "Confirmed Bad Estimate — Delivery Tracked"

        # ── STRUCTURALLY UNRELIABLE checks ────────────────────────────────────

        # 4. Non-monthly billing frequency + consecutive identical GEM values
        freq = billing_freq.get(mc,"")
        is_non_monthly = any(nm in freq for nm in NON_MONTHLY_FREQUENCIES)
        is_identical   = san in identical_sans
        if is_non_monthly or is_identical:
            return "Structurally Unreliable Estimate"

        # ── SUSPECT checks ────────────────────────────────────────────────────

        if ecls in ("seasonal","event"):

            # 5. Quantitatively implausible: GEM estimate << meter's average non-zero use
            avg_nonzero = profile.get("avg_nonzero_use", 0.0)
            if avg_nonzero > 0:
                # Convert avg_nonzero to MWh for comparison
                commodity = profile.get("commodity","")
                factor    = NATIVE_TO_MWH.get(commodity, 1.0)
                avg_mwh   = avg_nonzero * factor
                if avg_mwh > 0 and float(gem) < (magnitude_pct / 100) * avg_mwh:
                    return "Suspect Estimate — Magnitude Implausible"

            # 6. This month is historically zero in prior years (from R-19 baseline)
            baseline_key = (mc, mo)
            if baseline_key in r19_baseline:
                prior_vals   = r19_baseline[baseline_key]
                pct_zero     = sum(1 for v in prior_vals if v == 0) / len(prior_vals)
                if pct_zero >= 0.5:
                    return "Defensible Estimate"  # Historically zero → downgrade from suspect

            # 7. This month is zero in the target year's own pattern
            # (if the meter shows zero for this month in R-11, it's probably genuine)
            zero_months = profile.get("zero_months_set", set())
            if mo in zero_months:
                return "Defensible Estimate"  # Zero is EnergyCAP's own value for this month

            return "Suspect Estimate — Standard"

        # ── DEFENSIBLE ────────────────────────────────────────────────────────
        return "Defensible Estimate"

    merged["Estimate_Quality"] = merged.apply(classify, axis=1)

    # Add human-readable sub-descriptions
    eq_label_map = {
        "Normal":                                    "Actual bill — GEM matches EnergyCAP",
        "Defensible Estimate":                       "Steady-state commodity gap fill — acceptable",
        "Structurally Unreliable Estimate":          "Non-monthly or equal-split estimate — monthly values are approximations",
        "Suspect Estimate — Standard":               "Seasonal commodity estimated — confirm zero is a gap not genuine",
        "Suspect Estimate — Magnitude Implausible":  "Estimate is far below meter's typical non-zero use — likely an average-of-zeros artifact",
        "Confirmed Bad Estimate — Before Start Date":"GEM estimates before account existed — fabricated data",
        "Confirmed Bad Estimate — No Start Date":    "No account start date — GEM has no lower bound for estimates",
        "Confirmed Bad Estimate — Delivery Tracked": "Delivery-tracked meter: sparse non-zero months are genuine — GEM should not estimate zeros",
    }
    merged["Estimate_Quality_Label"] = merged["Estimate_Quality"].map(eq_label_map).fillna("")

    return merged


def _detect_identical_consecutive(merged, min_run=3):
    """
    Find GEM_SANs that have >= min_run consecutive identical non-zero monthly values.
    Returns a set of SANs exhibiting this pattern (classic equal-split estimation fingerprint).
    """
    identical_sans = set()
    if "GEM_SAN" not in merged.columns or "GEM_MWh" not in merged.columns:
        return identical_sans

    for san, grp in merged.groupby("GEM_SAN"):
        grp = grp.sort_values(["Year","Month"])
        vals = grp["GEM_MWh"].dropna().tolist()
        if len(vals) < min_run:
            continue
        run = 1
        for i in range(1, len(vals)):
            if vals[i] != 0 and abs(vals[i] - vals[i-1]) < 1e-6:
                run += 1
                if run >= min_run:
                    identical_sans.add(san)
                    break
            else:
                run = 1
    return identical_sans


# ── R-11 pivot builder ─────────────────────────────────────────────────────────

def _build_r11_pivot(r11, gem_year, conversion_factors):
    """Aggregate R-11 native use to MWh by Meter Code + Year + Month."""
    rows = []
    if "Billing Period" not in r11.columns or "Native Use" not in r11.columns:
        return pd.DataFrame(columns=["Meter Code","Year","Month","Qty_MWh","Native_Use"])

    for _, row in r11.iterrows():
        bp = str(row.get("Billing Period",""))
        if not bp or len(bp) < 6:
            continue
        try:
            yr = int(bp[:4]); mo = int(bp[4:6])
        except Exception:
            continue
        if yr != gem_year:
            continue
        use = pd.to_numeric(row.get("Native Use", np.nan), errors='coerce')
        if pd.isna(use):
            continue
        commodity = str(row.get("Commodity","")).upper().strip()
        factor    = conversion_factors.get(commodity, None)
        mwh       = use * factor if factor else np.nan
        rows.append({
            "Meter Code": row.get("Meter Code",""),
            "Year": yr, "Month": mo,
            "Native_Use": use,
            "Commodity":  row.get("Commodity",""),
            "Qty_MWh":    mwh,
        })

    if not rows:
        return pd.DataFrame(columns=["Meter Code","Year","Month","Qty_MWh","Native_Use"])

    df    = pd.DataFrame(rows)
    pivot = df.groupby(["Meter Code","Year","Month","Commodity"]).agg(
        Qty_MWh=("Qty_MWh","sum"),
        Native_Use=("Native_Use","sum")
    ).reset_index()
    return pivot


# ── GEM long-format builder ────────────────────────────────────────────────────

def _gem_to_long(gem, month_cols, month_map, gem_year):
    """Convert GEM wide format to long format."""
    rows = []
    for _, row in gem.iterrows():
        san      = str(row.get("SAN","")).strip()
        resource = str(row.get("Resource","")).strip()
        site     = str(row.get("Site","")).strip()
        country  = str(row.get("Country","")).strip()
        est_cls  = str(row.get("Estimate_Class","steady"))
        svc_acct = str(row.get("Service_Account_Number","")).strip()
        vendor   = str(row.get("Service_Vendor","")).strip()

        for mc in month_cols:
            if mc not in gem.columns:
                continue
            parts = mc.split("_", 1)
            if len(parts) < 2:
                continue
            try:
                yr = int(parts[0])
                mo = month_map.get(parts[1], 0)
            except Exception:
                continue
            val = pd.to_numeric(row.get(mc, np.nan), errors='coerce')
            rows.append({
                "GEM_SAN": san, "Resource": resource,
                "Site": site, "Country": country,
                "Estimate_Class": est_cls,
                "Service_Account_Number": svc_acct,
                "Service_Vendor": vendor,
                "Year": yr, "Month": mo, "Qty_MWh": val,
            })
    return pd.DataFrame(rows)


# ── SAN → Meter Code matcher ───────────────────────────────────────────────────

def _match_san_to_meter(gem_long, r11, r03):
    """Add Meter Code column to GEM long by matching SAN via three tiers."""
    r11_meters = set(r11["Meter Code"].dropna().astype(str).unique()) \
                 if r11 is not None else set()

    serial_map = {}
    if r03 is not None and "Serial Number" in r03.columns and "Meter Code" in r03.columns:
        for _, r in r03.iterrows():
            sn = str(r.get("Serial Number","")).strip()
            if sn and sn not in ("NO METER NUMBER","NOT METERED","nan",""):
                serial_map[sn] = str(r.get("Meter Code",""))

    acct_map = {}
    if r11 is not None and "Account" in r11.columns and "Meter Code" in r11.columns:
        for _, r in r11.iterrows():
            acct = str(r.get("Account","")).strip()
            if acct and acct not in ("nan",""):
                acct_map[acct] = str(r.get("Meter Code",""))

    def match(san):
        s = str(san).strip()
        if s in r11_meters:      return s,   "Tier1-Direct"
        if s in serial_map:      return serial_map[s], "Tier2-Serial"
        if s in acct_map:        return acct_map[s],   "Tier2-Account"
        return None, "Unmatched"

    gem_long[["Meter Code","Match_Tier"]] = gem_long["GEM_SAN"].apply(
        lambda s: pd.Series(match(s)))
    return gem_long


# ── GEM checks from merged dataframe ──────────────────────────────────────────

def _gem_checks_from_merged(merged):
    """Generate check_results, issues, risks from the merged GEM↔ECAP dataframe."""
    checks = []
    issues = []
    risks  = []

    def period_str(row):
        try:
            return f"{int(row['Year'])}-{int(row['Month']):02d}"
        except Exception:
            return ""

    # ── Bills in EnergyCAP missing from GEM ───────────────────────────────────
    ecap_only = merged[
        merged["ECAP_MWh"].notna() & (merged["ECAP_MWh"] != 0) &
        (merged["GEM_MWh"].isna()  | (merged["GEM_MWh"]  == 0))
    ].copy()
    chk_issues = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="Bills in EnergyCAP Missing from GEM",
        severity="Critical",
        description=f"EnergyCAP={float(r.get('ECAP_MWh',0) or 0):.2f} MWh — bill not flowing to GEM",
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in ecap_only.iterrows()]
    checks.append(_result("Bills in EnergyCAP Missing from GEM",
        "EnergyCAP has consumption but GEM shows null/zero for same meter+month.",
        "CRITICAL — these bills are excluded from emissions calculations entirely.",
        ecap_only, issues=chk_issues))

    # ── GEM over-reports (>20%) ────────────────────────────────────────────────
    over = merged[merged["Delta_Pct"].notna() & (merged["Delta_Pct"] > 20)].copy()
    if not over.empty:
        over["_desc"] = [
            f"GEM={float(r.get('GEM_MWh',0) or 0):.2f} vs "
            f"EnergyCAP={float(r.get('ECAP_MWh',0) or 0):.2f} MWh "
            f"(+{float(r.get('Delta_Pct',0) or 0):.1f}%)"
            for _, r in over.iterrows()
        ]
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="GEM Over-reports vs EnergyCAP",
        severity="Risk",
        description=r.get("_desc","GEM > EnergyCAP by >20%"),
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in over.iterrows()]
    checks.append(_result("GEM Over-reports vs EnergyCAP (>20%)",
        "GEM quantity is more than 20% above EnergyCAP for same meter+month.",
        "HIGH — may indicate double-counting or GEM using estimated values when actuals exist.",
        over, risks=chk_risks))

    # ── GEM under-reports (>20% gap) ──────────────────────────────────────────
    under = merged[merged["Delta_Pct"].notna() & (merged["Delta_Pct"] < -20)].copy()
    if not under.empty:
        under["_desc"] = [
            f"GEM={float(r.get('GEM_MWh',0) or 0):.2f} vs "
            f"EnergyCAP={float(r.get('ECAP_MWh',0) or 0):.2f} MWh "
            f"({float(r.get('Delta_Pct',0) or 0):.1f}%)"
            for _, r in under.iterrows()
        ]
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="GEM Under-reports vs EnergyCAP",
        severity="Risk",
        description=r.get("_desc","GEM < EnergyCAP by >20%"),
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in under.iterrows()]
    checks.append(_result("GEM Under-reports vs EnergyCAP (>20% gap)",
        "GEM quantity is more than 20% below EnergyCAP.",
        "HIGH — losses in transit from EnergyCAP to GEM; emissions understated.",
        under, risks=chk_risks))

    # ── Confirmed bad estimates (all sub-types) ────────────────────────────────
    for eq_val, severity, impact in [
        ("Confirmed Bad Estimate — Before Start Date",
         "Critical",
         "CRITICAL — fabricated emissions data before meter activation."),
        ("Confirmed Bad Estimate — No Start Date",
         "Critical",
         "CRITICAL — no lower bound; GEM may estimate indefinitely into the past."),
        ("Confirmed Bad Estimate — Delivery Tracked",
         "Critical",
         "CRITICAL — delivery-tracked meter: zero months are genuine, not gaps."),
    ]:
        sub = merged[merged["Estimate_Quality"] == eq_val].copy()
        chk_issues_sub = [format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category=eq_val, severity=severity,
            description=f"GEM={float(r.get('GEM_MWh',0) or 0):.2f} MWh estimated for "
                        f"{period_str(r)} — {r.get('Estimate_Quality_Label','')}",
            commodity=r.get("Resource",""), period=period_str(r))
            for _, r in sub.iterrows()]
        checks.append(_result(eq_val,
            merged[merged["Estimate_Quality"]==eq_val].get(
                "Estimate_Quality_Label", pd.Series([eq_val])).iloc[0]
            if not sub.empty else eq_val,
            impact, sub, issues=chk_issues_sub))

    # ── Structurally unreliable estimates ─────────────────────────────────────
    struct = merged[merged["Estimate_Quality"] == "Structurally Unreliable Estimate"].copy()
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="GEM Estimate — Structurally Unreliable",
        severity="Risk",
        description=f"Non-monthly or equal-split: GEM monthly value is an approximation "
                    f"({float(r.get('GEM_MWh',0) or 0):.2f} MWh for {period_str(r)})",
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in struct.iterrows()]
    checks.append(_result("GEM Estimates — Structurally Unreliable",
        "Non-monthly billing or consecutive identical values detected. "
        "GEM distributes bills equally across months.",
        "MEDIUM — monthly values are approximations, not metered data.",
        struct, risks=chk_risks))

    # ── Suspect estimates — standard ──────────────────────────────────────────
    suspect_std = merged[merged["Estimate_Quality"] == "Suspect Estimate — Standard"].copy()
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="GEM Estimate — Seasonal Commodity",
        severity="Risk",
        description=f"Seasonal commodity: GEM estimated {float(r.get('GEM_MWh',0) or 0):.2f} MWh "
                    f"for {period_str(r)} but EnergyCAP shows zero — confirm this is a gap not genuine zero",
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in suspect_std.iterrows()]
    checks.append(_result("GEM Estimates — Seasonal Commodity (Standard)",
        "Seasonal commodity with GEM estimate where EnergyCAP shows zero. "
        "Historical pattern does not confirm this month as typically zero.",
        "MEDIUM — zero may be genuine; GEM should not estimate.",
        suspect_std, risks=chk_risks))

    # ── Suspect estimates — magnitude implausible ──────────────────────────────
    suspect_mag = merged[
        merged["Estimate_Quality"] == "Suspect Estimate — Magnitude Implausible"].copy()
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
        bill_id="", category="GEM Estimate — Magnitude Implausible",
        severity="Risk",
        description=f"Estimate of {float(r.get('GEM_MWh',0) or 0):.2f} MWh for {period_str(r)} "
                    f"is far below meter's typical non-zero use — likely average-of-zeros artifact",
        commodity=r.get("Resource",""), period=period_str(r))
        for _, r in suspect_mag.iterrows()]
    checks.append(_result("GEM Estimates — Magnitude Implausible",
        "GEM estimate is far below the meter's average non-zero monthly use. "
        "Likely an annualized-average artifact rather than a meaningful estimate.",
        "MEDIUM — these small estimates may be noise, not real consumption.",
        suspect_mag, risks=chk_risks))

    # ── Unmatched SANs ─────────────────────────────────────────────────────────
    unmatched = (merged[merged["Match_Tier"] == "Unmatched"]
                 .drop_duplicates("GEM_SAN").copy()
                 if "Match_Tier" in merged.columns else pd.DataFrame())
    chk_risks = [format_issue_row(
        site=r.get("Site",""), account="", meter=r.get("GEM_SAN",""),
        bill_id="", category="Unmatched GEM SAN", severity="Risk",
        description=f"SAN '{r.get('GEM_SAN','')}' cannot be matched to any EnergyCAP meter",
        commodity=r.get("Resource",""), period="")
        for _, r in unmatched.iterrows()]
    checks.append(_result("GEM SANs with No EnergyCAP Match",
        "GEM Service Aggregator Numbers that cannot be matched to any EnergyCAP meter, "
        "account, or serial number.",
        "HIGH — orphan GEM entries may be manual estimates or legacy meters.",
        unmatched, risks=chk_risks))

    # Flatten
    for c in checks:
        issues.extend(c.get("issues",[]))
        risks.extend(c.get("risks",[]))

    return checks, issues, risks


# ══ APP UI ══

"""
EnergyCAP Pre-Export QA Tool — v2.1
Streamlit app for QA-ing EnergyCAP data and reconciling with GEM.

Changes in v2.1:
  - Target-year selector: QA is always scoped to one explicit year
  - R-19 prior years treated as reference-only (no false gap/zero flags)
  - Refined GEM seasonal estimate classification (5 sub-types)
  - Delivery-tracking threshold and magnitude implausibility sliders
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import date
import warnings
warnings.filterwarnings("ignore")

# (qa_engine merged into single file)
# (utils merged into single file)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="EnergyCAP QA Tool",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.main-header{font-size:2rem;font-weight:700;color:#1a3a5c;
  border-bottom:3px solid #2196F3;padding-bottom:.5rem;margin-bottom:1.5rem;}
.section-header{font-size:1.15rem;font-weight:600;color:#1a3a5c;
  margin-top:1rem;margin-bottom:.4rem;}
.period-box{background:#f0f4f8;border-radius:8px;padding:10px 14px;
  border-left:4px solid #2196F3;margin:4px 0;font-size:.9rem;}
.overlap-box{background:#e8f5e9;border-radius:8px;padding:10px 14px;
  border-left:4px solid #4caf50;margin:4px 0;}
.warn-box{background:#fff8e1;border-radius:8px;padding:10px 14px;
  border-left:4px solid #ff9800;margin:4px 0;}
.target-box{background:#e3f2fd;border-radius:8px;padding:10px 14px;
  border-left:4px solid #1565c0;margin:4px 0;font-weight:600;}
.eq-confirmed{background:#fde8e8;border-radius:6px;padding:8px 12px;margin:3px 0;
  border-left:4px solid #c53030;}
.eq-struct{background:#fef3cd;border-radius:6px;padding:8px 12px;margin:3px 0;
  border-left:4px solid #d69e2e;}
.eq-suspect{background:#ffedd5;border-radius:6px;padding:8px 12px;margin:3px 0;
  border-left:4px solid #c05621;}
.eq-defensible{background:#dbeafe;border-radius:6px;padding:8px 12px;margin:3px 0;
  border-left:4px solid #1d4ed8;}
.eq-normal{background:#d1fae5;border-radius:6px;padding:8px 12px;margin:3px 0;
  border-left:4px solid #065f46;}
div[data-testid="stMetricValue"]{font-size:1.8rem!important;}
</style>
""", unsafe_allow_html=True)

# ── Session state ──────────────────────────────────────────────────────────────
for k, v in [("reports",{}), ("periods",{}), ("overlap",(None,None)),
             ("qa_results",{}), ("reconciled",False), ("target_year", date.today().year)]:
    if k not in st.session_state:
        st.session_state[k] = v

# ══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ══════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    try:
        st.image("https://www.energycap.com/wp-content/uploads/2021/03/energycap-logo.png",
                 width=160, use_container_width=False)
    except Exception:
        st.markdown("### ⚡ EnergyCAP QA Tool")
    st.markdown("---")

    # ── TARGET YEAR — most important control ──────────────────────────────────
    st.markdown("### 🎯 QA Target Year")
    target_year = st.selectbox(
        "Year being quality-assured",
        options=list(range(date.today().year, 2018, -1)),
        index=0,
        help="All QA checks and GEM reconciliation focus on this year. "
             "Prior years in R-19 are used only as reference baselines — "
             "they will NOT generate gap or zero-use flags."
    )
    st.session_state.target_year = target_year
    st.markdown(
        f'<div class="target-box">🎯 QA Target: <b>{target_year}</b></div>',
        unsafe_allow_html=True)
    st.markdown("""
<div style="font-size:.8rem;color:#555;margin-top:4px;">
R-19 may contain {yr-1}+{yr} for YoY context.<br>
Only {yr} data will be QA'd for gaps, zeros, and outliers.<br>
{yr-1} data is used solely for baseline comparisons.
</div>
""".replace("{yr}", str(target_year)).replace("{yr-1}", str(target_year-1)),
        unsafe_allow_html=True)

    st.markdown("---")
    st.markdown("### 📋 Reports")
    st.markdown("""
| Report | Description |
|--------|-------------|
| **R-03** | Setup / Config |
| **R-11** | Bill Detail + UOM |
| **R-19** | Monthly Use & Cost |
| **GEM**  | Emissions Export |
""")

    st.markdown("---")
    st.markdown("### ⚙️ QA Settings")
    outlier_z   = st.slider("Outlier Z-score threshold",  1.5, 4.0, 2.5, 0.1)
    pct_change  = st.slider("MoM % change alert",         20,  200,  50,  5)
    zero_months = st.slider("Consecutive zero-use months", 1,    6,   2,   1)

    st.markdown("---")
    st.markdown("### 📅 Account Start Date")
    acct_thresh = st.slider("Flag start dates before year",
                            1970, date.today().year, 2000, 1)

    st.markdown("---")
    st.markdown("### 🌡 Seasonal Estimate Settings")
    delivery_thresh = st.slider(
        "Delivery-tracking threshold (non-zero months/yr)",
        1, 6, 4, 1,
        help="Meters with fewer non-zero months than this are treated as "
             "delivery-tracked — GEM estimates on zero months flagged as Confirmed Bad.")
    magnitude_pct = st.slider(
        "Magnitude implausibility threshold (%)",
        1, 30, 10, 1,
        help="GEM estimates below this % of the meter's average non-zero monthly "
             "use are flagged as 'Magnitude Implausible'.")

    st.markdown("---")
    st.markdown("### 🔄 Unit Conversion (MWh)")
    with st.expander("Edit conversion factors"):
        st.caption("Native unit → MWh. Used to compare EnergyCAP vs GEM.")
        custom_factors = {}
        defaults = {
            "ELECTRIC (kWh)":    ("ELECTRIC",    1/1000),
            "Nat Gas CCF":       ("NATURALGAS",  0.02931),
            "Nat Gas MCF":       ("MCF",         0.2931),
            "Diesel (gal)":      ("DIESEL",      0.03596),
            "Propane (gal)":     ("PROPANE",     0.02558),
            "Fuel Oil (gal)":    ("FUELOIL",     0.04026),
            "LPG (gal)":         ("LPG",         0.02558),
        }
        for label, (key, default) in defaults.items():
            val = st.number_input(label, value=float(default),
                                  format="%.6f", key=f"cf_{key}")
            custom_factors[key] = val
        merged_factors = {**NATIVE_TO_MWH, **custom_factors}

    st.markdown("---")
    st.caption("EnergyCAP QA Tool v2.1 | Emissions Readiness")

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown('<div class="main-header">⚡ EnergyCAP Pre-Export QA Tool</div>',
            unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════
(tab_upload, tab_summary, tab_gem_summary,
 tab_risks, tab_issues, tab_risk_reg,
 tab_gem_detail, tab_explorer) = st.tabs([
    "📁 Upload & Run",
    "📊 EnergyCAP QA",
    "🔗 GEM Reconciliation",
    "⚠️ Risk Summary",
    "🔴 Issue Register",
    "🟡 Risk Register",
    "🔍 GEM Detail",
    "📂 Data Explorer",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — UPLOAD & RUN
# ══════════════════════════════════════════════════════════════════════════════
with tab_upload:
    st.markdown('<div class="section-header">Step 1 — Upload EnergyCAP & GEM Exports</div>',
                unsafe_allow_html=True)
    st.info(
        f"**QA target year: {target_year}.** "
        "R-03 and R-11 are required. R-19 (if uploaded) uses prior-year data as baseline only — "
        f"only {target_year} records will be checked for gaps and anomalies. "
        "GEM upload enables EnergyCAP↔GEM reconciliation."
    )

    uploaded = st.file_uploader(
        "Drop files here or click Browse",
        type=["xlsx","xls","csv"],
        accept_multiple_files=True,
    )

    if uploaded:
        for f in uploaded:
            try:
                df, rtype = load_report(f)
                if rtype:
                    st.session_state.reports[rtype] = df
                    period = detect_period(rtype, df, target_year=target_year)
                    st.session_state.periods[rtype] = period
                    label = REPORT_LABELS.get(rtype, rtype)

                    # Special note for R-19
                    extra_note = ""
                    if rtype == "R19":
                        extra_note = (f" | ⚠️ Prior years will be used as baseline only — "
                                      f"QA checks run on {target_year} data only")

                    p_str = ""
                    if period[0] and period[1]:
                        p_str = (f" | Period: **{period[0].strftime('%b %Y')}** → "
                                 f"**{period[1].strftime('%b %Y')}**")
                    st.success(f"✅ `{f.name}` → **{label}** ({len(df):,} rows)"
                               f"{p_str}{extra_note}")
                else:
                    st.warning(f"⚠️ `{f.name}` — could not detect report type.")
            except Exception as e:
                st.error(f"❌ `{f.name}` — {e}")

        # ── Period overview ────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown('<div class="section-header">📅 Period Coverage</div>',
                    unsafe_allow_html=True)

        period_data = {k: v for k, v in st.session_state.periods.items()
                       if v[0] is not None}

        if period_data:
            pcols = st.columns(min(len(period_data), 5))
            for i, (rtype, (beg, end)) in enumerate(period_data.items()):
                with pcols[i % 5]:
                    is_r19 = rtype == "R19"
                    note   = " (target year only)" if is_r19 else ""
                    st.markdown(
                        f'<div class="period-box"><b>{rtype}</b>{note}<br>'
                        f'{REPORT_LABELS.get(rtype,rtype)}<br>'
                        f'<b>{beg.strftime("%b %Y")}</b> → '
                        f'<b>{end.strftime("%b %Y")}</b></div>',
                        unsafe_allow_html=True)

            # Only non-R19 reports contribute to overlap
            non_r19 = {k: v for k, v in period_data.items() if k != "R19"}
            overlap_b, overlap_e = compute_overlap(non_r19)
            st.session_state.overlap = (overlap_b, overlap_e)

            if overlap_b and overlap_e:
                st.markdown(
                    f'<div class="overlap-box">✅ <b>QA window: '
                    f'{overlap_b.strftime("%B %Y")} → {overlap_e.strftime("%B %Y")}</b><br>'
                    f'All checks and GEM reconciliation focus on this window. '
                    f'R-19 prior-year data is used as baseline only.</div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div class="warn-box">⚠️ No overlapping period found across uploaded files. '
                    'Check that R-11 and GEM cover the same year.</div>',
                    unsafe_allow_html=True)

        # ── Coverage badges ────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("**Report coverage:**")
        badge_cols = st.columns(7)
        for i, (r, req) in enumerate([("R03",True),("R11",True),("R13",False),
                                       ("R19",False),("R21",False),("R26",False),("GEM",False)]):
            with badge_cols[i]:
                loaded = r in st.session_state.reports
                icon   = "🟢" if loaded else ("🔴" if req else "⚪")
                reqtxt = " *(req)*" if req else ""
                r19note = "\n*(ref only)*" if r == "R19" and loaded else ""
                st.markdown(f"{icon} **{r}**{reqtxt}{r19note}")

    # ── Run button ─────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-header">Step 2 — Run QA & Reconciliation</div>',
                unsafe_allow_html=True)

    has_required = all(r in st.session_state.reports for r in ["R03","R11"])
    if not has_required:
        st.warning("⚠️ Please upload at least **R-03** and **R-11** to run QA.")

    c1, c2 = st.columns([3,1])
    with c1:
        run_btn = st.button("▶ Run QA & Reconciliation", type="primary",
                            disabled=not has_required, use_container_width=True)
    with c2:
        if st.button("🗑 Clear All", use_container_width=True):
            for k in ["reports","periods","qa_results"]:
                st.session_state[k] = {}
            st.session_state.overlap = (None, None)
            st.session_state.reconciled = False
            st.rerun()

    if run_btn:
        ob, oe = st.session_state.overlap
        with st.spinner(f"Running QA for {target_year}…"):
            try:
                rpts = st.session_state.reports.copy()

                # Filter R-11 and GEM to target year only
                if "R11" in rpts:
                    rpts["R11"] = filter_r11_to_year(rpts["R11"], target_year)
                if "GEM" in rpts:
                    rpts["GEM"] = filter_gem_to_year(rpts["GEM"], target_year)
                # R-19 stays as-is — engine uses it for baseline only

                results = run_all_checks(
                    rpts,
                    target_year=target_year,
                    overlap_begin=ob, overlap_end=oe,
                    outlier_zscore=outlier_z,
                    pct_change_thresh=pct_change,
                    zero_use_months=zero_months,
                    acct_start_year_threshold=acct_thresh,
                    delivery_tracking_threshold=delivery_thresh,
                    gem_magnitude_pct=magnitude_pct,
                    conversion_factors=merged_factors,
                )
                st.session_state.qa_results = results
                st.session_state.reconciled = True
                n_iss = len(results.get("issues_df",[]))
                n_rsk = len(results.get("risks_df",[]))
                st.success(f"✅ {target_year} QA complete — "
                           f"**{n_iss}** issues, **{n_rsk}** risks. Review tabs above.")
                st.balloons()
            except Exception as e:
                st.error(f"Error: {e}")
                raise e

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — ECAP QA SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with tab_summary:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        res       = st.session_state.qa_results
        issues_df = res.get("issues_df", pd.DataFrame())
        risks_df  = res.get("risks_df",  pd.DataFrame())
        meta      = res.get("meta", {})
        ty        = st.session_state.target_year
        ob, oe    = st.session_state.overlap

        st.markdown(f'<div class="section-header">EnergyCAP Data Quality — {ty}</div>',
                    unsafe_allow_html=True)

        if ob and oe:
            st.info(f"📅 Focused on: **{ob.strftime('%B %Y')}** → **{oe.strftime('%B %Y')}** "
                    f"(target year {ty}). R-19 prior years excluded from gap/zero checks.")

        c1,c2,c3,c4,c5,c6 = st.columns(6)
        c1.metric("Total Bills",   f"{meta.get('total_bills',0):,}")
        c2.metric("Sites",         f"{meta.get('total_sites',0):,}")
        c3.metric("Meters",        f"{meta.get('total_meters',0):,}")
        c4.metric("Commodities",   f"{meta.get('total_commodities',0):,}")
        c5.metric("🔴 Issues",     f"{len(issues_df):,}")
        c6.metric("🟡 Risks",      f"{len(risks_df):,}")

        st.markdown("---")
        ecap_checks = [c for c in res.get("check_results",[])
                       if "GEM" not in c["name"] and "Unmatched" not in c["name"]]

        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("#### 🔴 Issues by Category")
            ecap_issues = issues_df[~issues_df["Category"].str.contains("GEM|Unmatched",na=False)] \
                          if not issues_df.empty else pd.DataFrame()
            if ecap_issues.empty:
                st.success("No EnergyCAP-specific issues.")
            else:
                for _, r in (ecap_issues.groupby("Category").size()
                             .reset_index(name="Count")
                             .sort_values("Count",ascending=False)).iterrows():
                    st.markdown(f'🔴 **{r["Category"]}** — {r["Count"]} records')

        with col_r:
            st.markdown("#### 🟡 Risks by Category")
            ecap_risks = risks_df[~risks_df["Category"].str.contains("GEM|Unmatched",na=False)] \
                         if not risks_df.empty else pd.DataFrame()
            if ecap_risks.empty:
                st.success("No EnergyCAP-specific risks.")
            else:
                for _, r in (ecap_risks.groupby("Category").size()
                             .reset_index(name="Count")
                             .sort_values("Count",ascending=False)).iterrows():
                    st.markdown(f'🟡 **{r["Category"]}** — {r["Count"]} records')

        st.markdown("---")
        st.markdown("#### 🔬 Check-by-Check Results")
        for chk in ecap_checks:
            icon = {"ok":"✅","warning":"⚠️","error":"❌"}.get(chk["status"],"ℹ️")
            with st.expander(f"{icon} {chk['name']} — {chk['count']} flagged",
                             expanded=(chk["status"]=="error" and chk["count"]>0)):
                st.markdown(f"**Description:** {chk.get('description','')}")
                st.markdown(f"**Emissions impact:** {chk.get('emissions_impact','')}")
                if chk["count"] > 0:
                    st.dataframe(chk["sample"], use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — GEM RECONCILIATION SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with tab_gem_summary:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    elif "GEM" not in st.session_state.reports:
        st.warning("No GEM file uploaded. Upload a GEM export to enable reconciliation.")
    else:
        res    = st.session_state.qa_results
        detail = res.get("gem_detail", pd.DataFrame())
        meta   = res.get("meta", {})
        ty     = st.session_state.target_year
        ob, oe = st.session_state.overlap

        st.markdown(f'<div class="section-header">EnergyCAP ↔ GEM Reconciliation — {ty}</div>',
                    unsafe_allow_html=True)
        if ob and oe:
            st.info(f"📅 Reconciliation period: **{ob.strftime('%B %Y')}** → "
                    f"**{oe.strftime('%B %Y')}**")

        if detail.empty:
            st.warning("No reconciliation data — check that Meter Codes overlap between R-11 and GEM.")
        else:
            total_gem  = detail["GEM_MWh"].sum()  if "GEM_MWh"  in detail.columns else 0
            total_ecap = detail["ECAP_MWh"].sum() if "ECAP_MWh" in detail.columns else 0
            delta      = total_gem - total_ecap
            pct        = (delta / total_ecap * 100) if total_ecap else 0
            unmatched  = detail[detail.get("Match_Tier","") == "Unmatched"] \
                         if "Match_Tier" in detail.columns else pd.DataFrame()

            c1,c2,c3,c4,c5 = st.columns(5)
            c1.metric("GEM Total (MWh)",      f"{total_gem:,.1f}")
            c2.metric("EnergyCAP Total (MWh)",f"{total_ecap:,.1f}")
            c3.metric("Delta (MWh)",          f"{delta:+,.1f}", delta=f"{pct:+.1f}%")
            c4.metric("GEM Rows",             f"{meta.get('gem_rows',0):,}")
            c5.metric("Unmatched SANs",
                      f"{unmatched['GEM_SAN'].nunique() if not unmatched.empty and 'GEM_SAN' in unmatched.columns else 0:,}")

            st.markdown("---")

            # ── Estimate quality breakdown ─────────────────────────────────────
            if "Estimate_Quality" in detail.columns:
                st.markdown("#### 🧠 GEM Estimate Quality Breakdown")
                st.caption(
                    "Each GEM meter-month where EnergyCAP shows zero but GEM shows a value "
                    "is classified by estimate quality. This reflects the refinements for "
                    "seasonal commodities, delivery-tracked meters, and account start dates."
                )

                eq_styles = {
                    "Normal":
                        ("eq-normal",  "✅", "Actual bill — GEM matches EnergyCAP"),
                    "Defensible Estimate":
                        ("eq-defensible","🟦","Steady-state commodity gap-fill — acceptable"),
                    "Structurally Unreliable Estimate":
                        ("eq-struct",  "🟡","Non-monthly meter or equal-split pattern — monthly values are approximations"),
                    "Suspect Estimate — Standard":
                        ("eq-suspect", "🟠","Seasonal commodity estimated — confirm zero is a gap not genuine"),
                    "Suspect Estimate — Magnitude Implausible":
                        ("eq-suspect", "🟠","Estimate far below meter's typical use — likely average-of-zeros artifact"),
                    "Confirmed Bad Estimate — Before Start Date":
                        ("eq-confirmed","🔴","GEM estimates before meter existed — fabricated data"),
                    "Confirmed Bad Estimate — No Start Date":
                        ("eq-confirmed","🔴","No account start date — GEM has no lower bound"),
                    "Confirmed Bad Estimate — Delivery Tracked":
                        ("eq-confirmed","🔴","Delivery-tracked meter: zeros are genuine — GEM should not estimate"),
                }

                eq_counts = detail.groupby("Estimate_Quality").size().reset_index(name="Count")
                for _, row in eq_counts.sort_values("Count", ascending=False).iterrows():
                    eq    = row["Estimate_Quality"]
                    cnt   = row["Count"]
                    cls, icon, desc = eq_styles.get(eq, ("","ℹ️", eq))
                    sub_mwh = detail[detail["Estimate_Quality"]==eq]["GEM_MWh"].sum()
                    st.markdown(
                        f'<div class="{cls}">'
                        f'{icon} <b>{eq}</b> — {cnt} meter-months '
                        f'({sub_mwh:,.1f} MWh)<br>'
                        f'<span style="font-size:.83rem;color:#444;">{desc}</span>'
                        f'</div>',
                        unsafe_allow_html=True)

            st.markdown("---")

            # ── Match tier breakdown ───────────────────────────────────────────
            if "Match_Tier" in detail.columns:
                st.markdown("#### 🔗 SAN → Meter Code Match Coverage")
                tier_counts = (detail.drop_duplicates(["GEM_SAN","Match_Tier"])
                               .groupby("Match_Tier").size())
                tc = st.columns(4)
                for i, (tier, icon, label) in enumerate([
                    ("Tier1-Direct",  "🟢","Direct SAN = Meter Code"),
                    ("Tier2-Serial",  "🟡","Matched via Serial Number"),
                    ("Tier2-Account", "🟡","Matched via Account Code"),
                    ("Unmatched",     "🔴","No EnergyCAP match found"),
                ]):
                    with tc[i]:
                        st.metric(f"{icon} {label}", tier_counts.get(tier, 0))

            st.markdown("---")

            # ── GEM check results ──────────────────────────────────────────────
            gem_checks = [c for c in res.get("check_results",[])
                          if ("GEM" in c["name"] or "Unmatched" in c["name"]
                              or "Confirmed Bad" in c["name"])]
            st.markdown("#### 🔬 Reconciliation Check Results")
            for chk in gem_checks:
                icon = {"ok":"✅","warning":"⚠️","error":"❌"}.get(chk["status"],"ℹ️")
                with st.expander(
                    f"{icon} {chk['name']} — {chk['count']} flagged",
                    expanded=(chk["status"] in ("error","warning") and chk["count"]>0)
                ):
                    st.markdown(f"**Description:** {chk.get('description','')}")
                    st.markdown(f"**Emissions impact:** {chk.get('emissions_impact','')}")
                    if chk["count"] > 0:
                        st.dataframe(chk["sample"], use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — RISK SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with tab_risks:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        risks_df = st.session_state.qa_results.get("risks_df", pd.DataFrame())
        st.markdown('<div class="section-header">Risk Summary</div>', unsafe_allow_html=True)
        st.markdown("Risks warrant closer review before use in emissions calculations. "
                    "They may not be wrong, but each needs human judgment.")

        risk_meta = {
            "UOM / Rate Schedule Inconsistency":
                ("📐","Rate schedule changes between billing periods.",
                 "HIGH — wrong emission factor applied to wrong UOM."),
            "Unusual Billing Period Length":
                ("📅","Bills with <5 or >95 days.",
                 "MEDIUM — affects calendarization."),
            "Zero Use with Non-Zero Cost":
                ("💰","Zero use but non-zero cost.",
                 "MEDIUM — demand/standby charges; confirms no consumption."),
            "Non-Zero Use with Zero Cost":
                ("📋","Use present but $0 cost.",
                 "LOW — verify this is intentional."),
            "Inactive Meters with Bills":
                ("🔌","Inactive meter still has bills.",
                 "HIGH — emissions tools may exclude inactive meters."),
            "Missing Common Use (kBTU conversion)":
                ("🔄","kBTU conversion is blank.",
                 "HIGH — emissions tools using Common Use will drop these bills."),
            "Use Outliers (Z-score > 2.5)":
                ("📈","Statistically unusual use.",
                 "MEDIUM — may skew annual totals."),
            "Cost Outliers (Z-score > 2.5)":
                ("💵","Statistically unusual cost.",
                 "LOW — may indicate billing errors."),
            "Consecutive Zero-Use Months (≥2)":
                ("⬛","Multiple consecutive zero-use months.",
                 "MEDIUM — may indicate data gaps."),
            "Month-over-Month Use Change > 50%":
                ("📊","Large month-over-month use change.",
                 "MEDIUM — may indicate missing bills."),
            "Deregulated Market Meters":
                ("⚡","Separate distribution and supply vendors.",
                 "HIGH — missing supply bills = incomplete Scope 2."),
            "Accounts Excluded from Audits":
                ("🚫","EnergyCAP audit checks bypassed.",
                 "MEDIUM — data quality issues won't be caught internally."),
            "Missing Meter Serial Numbers":
                ("🔢","No serial number on meter.", "LOW — informational."),
            "Suspicious Account Start Date":
                ("📅","Start date null or unusually old.",
                 "HIGH — GEM may fabricate estimates before meter existed."),
            "Non-Monthly Billing Frequency":
                ("🗓","Quarterly/bi-monthly/annual meter.",
                 "MEDIUM — GEM monthly values are approximations."),
            "GEM Estimate — Structurally Unreliable":
                ("🗓","Non-monthly or equal-split GEM estimate.",
                 "MEDIUM — not metered monthly data."),
            "GEM Estimate — Seasonal Commodity":
                ("🌡","Seasonal commodity estimated where EnergyCAP shows zero.",
                 "MEDIUM — zero may be genuine."),
            "GEM Estimate — Magnitude Implausible":
                ("🔬","GEM estimate far below meter's typical use.",
                 "MEDIUM — likely average-of-zeros artifact."),
            "GEM SANs with No EnergyCAP Match":
                ("❓","GEM entry cannot be matched to any EnergyCAP meter.",
                 "HIGH — may be manual estimates or legacy meters."),
            "GEM Over-reports vs EnergyCAP":
                ("📈","GEM quantity >20% above EnergyCAP.",
                 "HIGH — may indicate double-counting."),
            "GEM Under-reports vs EnergyCAP":
                ("📉","GEM quantity >20% below EnergyCAP.",
                 "HIGH — losses in transit; emissions understated."),
            "Unmatched GEM SAN":
                ("❓","GEM SAN not found in EnergyCAP.",
                 "HIGH — orphan GEM entry."),
        }

        if risks_df.empty:
            st.success("✅ No risks identified.")
        else:
            present = set(risks_df["Category"].unique())
            found_any = False
            for cat, (icon, desc, impact) in risk_meta.items():
                # Fuzzy match — category may be a substring
                matched = [p for p in present if cat in p or p in cat]
                if not matched:
                    continue
                cat_df = risks_df[risks_df["Category"].isin(matched)]
                if cat_df.empty:
                    continue
                found_any = True
                with st.expander(f"{icon} **{cat}** — {len(cat_df)} records", expanded=False):
                    st.markdown(f"**What it means:** {desc}")
                    st.markdown(f"**Emissions impact:** {impact}")
                    st.dataframe(cat_df.drop(columns=["Category"],errors="ignore"),
                                 use_container_width=True, hide_index=True)
            # Catch-all for categories not in risk_meta
            uncovered = present - set(k for k in risk_meta)
            for cat in sorted(uncovered):
                cat_df = risks_df[risks_df["Category"] == cat]
                if cat_df.empty:
                    continue
                found_any = True
                with st.expander(f"ℹ️ **{cat}** — {len(cat_df)} records", expanded=False):
                    st.dataframe(cat_df.drop(columns=["Category"],errors="ignore"),
                                 use_container_width=True, hide_index=True)
            if not found_any:
                st.success("✅ No risks identified.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — ISSUE REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_issues:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        issues_df = st.session_state.qa_results.get("issues_df", pd.DataFrame())
        ty        = st.session_state.target_year
        st.markdown(f'<div class="section-header">🔴 Issue Register — {ty} — '
                    f'Records Requiring Correction</div>', unsafe_allow_html=True)
        st.markdown("These records have **confirmed data quality problems** that must be "
                    "corrected in EnergyCAP or GEM before the data is used for emissions.")

        if issues_df.empty:
            st.success("✅ No issues found!")
        else:
            fc1,fc2,fc3,fc4 = st.columns(4)
            cats  = ["All"] + sorted(issues_df["Category"].dropna().unique().tolist())
            sevs  = ["All"] + sorted(issues_df["Severity"].dropna().unique().tolist())
            sites = ["All"] + sorted(issues_df["Site"].dropna().unique().tolist()) \
                    if "Site" in issues_df.columns else ["All"]
            coms  = ["All"] + sorted(issues_df["Commodity"].dropna().unique().tolist()) \
                    if "Commodity" in issues_df.columns else ["All"]

            with fc1: sel_cat  = st.selectbox("Category",  cats,  key="iss_cat")
            with fc2: sel_sev  = st.selectbox("Severity",  sevs,  key="iss_sev")
            with fc3: sel_site = st.selectbox("Site",      sites, key="iss_site")
            with fc4: sel_com  = st.selectbox("Commodity", coms,  key="iss_com")

            filt = issues_df.copy()
            if sel_cat  != "All": filt = filt[filt["Category"]  == sel_cat]
            if sel_sev  != "All": filt = filt[filt["Severity"]  == sel_sev]
            if sel_site != "All" and "Site" in filt.columns:
                filt = filt[filt["Site"] == sel_site]
            if sel_com  != "All" and "Commodity" in filt.columns:
                filt = filt[filt["Commodity"] == sel_com]

            st.markdown(f"**Showing {len(filt):,} of {len(issues_df):,} issues**")
            st.dataframe(filt, use_container_width=True, hide_index=True)
            st.download_button("⬇ Download Issue Register (CSV)",
                               filt.to_csv(index=False),
                               f"issue_register_{ty}_{date.today()}.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — RISK REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_risk_reg:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        risks_df = st.session_state.qa_results.get("risks_df", pd.DataFrame())
        ty       = st.session_state.target_year
        st.markdown(f'<div class="section-header">🟡 Risk Register — {ty} — '
                    f'Records Requiring Review</div>', unsafe_allow_html=True)
        st.markdown("These records are not necessarily wrong but carry risk of affecting "
                    "emissions accuracy. Each should be reviewed and confirmed or escalated.")

        if risks_df.empty:
            st.success("✅ No risks identified.")
        else:
            rc1,rc2,rc3,rc4 = st.columns(4)
            rcats  = ["All"] + sorted(risks_df["Category"].dropna().unique().tolist())
            rsites = ["All"] + sorted(risks_df["Site"].dropna().unique().tolist()) \
                     if "Site" in risks_df.columns else ["All"]
            rcoms  = ["All"] + sorted(risks_df["Commodity"].dropna().unique().tolist()) \
                     if "Commodity" in risks_df.columns else ["All"]
            rpers  = ["All"] + sorted(risks_df["Period"].dropna().unique().tolist()) \
                     if "Period" in risks_df.columns else ["All"]

            with rc1: sel_rcat  = st.selectbox("Category",  rcats,  key="rsk_cat")
            with rc2: sel_rsite = st.selectbox("Site",      rsites, key="rsk_site")
            with rc3: sel_rcom  = st.selectbox("Commodity", rcoms,  key="rsk_com")
            with rc4: sel_rper  = st.selectbox("Period",    rpers,  key="rsk_per")

            rfilt = risks_df.copy()
            if sel_rcat  != "All": rfilt = rfilt[rfilt["Category"]  == sel_rcat]
            if sel_rsite != "All" and "Site" in rfilt.columns:
                rfilt = rfilt[rfilt["Site"] == sel_rsite]
            if sel_rcom  != "All" and "Commodity" in rfilt.columns:
                rfilt = rfilt[rfilt["Commodity"] == sel_rcom]
            if sel_rper  != "All" and "Period" in rfilt.columns:
                rfilt = rfilt[rfilt["Period"] == sel_rper]

            st.markdown(f"**Showing {len(rfilt):,} of {len(risks_df):,} risks**")
            st.dataframe(rfilt, use_container_width=True, hide_index=True)
            st.download_button("⬇ Download Risk Register (CSV)",
                               rfilt.to_csv(index=False),
                               f"risk_register_{ty}_{date.today()}.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — GEM DETAIL
# ══════════════════════════════════════════════════════════════════════════════
with tab_gem_detail:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    elif "GEM" not in st.session_state.reports:
        st.warning("No GEM file uploaded.")
    else:
        detail = st.session_state.qa_results.get("gem_detail", pd.DataFrame())
        ty     = st.session_state.target_year
        st.markdown(f'<div class="section-header">🔍 GEM ↔ EnergyCAP Detail — {ty}</div>',
                    unsafe_allow_html=True)
        st.markdown(
            "Row-level reconciliation with estimate quality classification. "
            "Use the filters to focus on specific sites, commodities, or estimate types."
        )

        if detail.empty:
            st.info("No detail data available.")
        else:
            gd1,gd2,gd3,gd4 = st.columns(4)
            gsites = ["All"] + sorted(detail["Site"].dropna().unique().tolist()) \
                     if "Site" in detail.columns else ["All"]
            gress  = ["All"] + sorted(detail["Resource"].dropna().unique().tolist()) \
                     if "Resource" in detail.columns else ["All"]
            geqs   = ["All"] + sorted(detail["Estimate_Quality"].dropna().unique().tolist()) \
                     if "Estimate_Quality" in detail.columns else ["All"]
            gtiers = ["All"] + sorted(detail["Match_Tier"].dropna().unique().tolist()) \
                     if "Match_Tier" in detail.columns else ["All"]

            with gd1: sel_gs    = st.selectbox("Site",             gsites, key="gd_site")
            with gd2: sel_gr    = st.selectbox("Resource",         gress,  key="gd_res")
            with gd3: sel_geq   = st.selectbox("Estimate Quality", geqs,   key="gd_eq")
            with gd4: sel_gtier = st.selectbox("Match Tier",       gtiers, key="gd_tier")

            gfilt = detail.copy()
            if sel_gs    != "All" and "Site"             in gfilt.columns:
                gfilt = gfilt[gfilt["Site"]             == sel_gs]
            if sel_gr    != "All" and "Resource"         in gfilt.columns:
                gfilt = gfilt[gfilt["Resource"]         == sel_gr]
            if sel_geq   != "All" and "Estimate_Quality" in gfilt.columns:
                gfilt = gfilt[gfilt["Estimate_Quality"] == sel_geq]
            if sel_gtier != "All" and "Match_Tier"       in gfilt.columns:
                gfilt = gfilt[gfilt["Match_Tier"]       == sel_gtier]

            show_cols = [c for c in [
                "Site","Country","Resource","GEM_SAN","Meter Code","Match_Tier",
                "Year","Month","GEM_MWh","ECAP_MWh","Delta_MWh","Delta_Pct",
                "Estimate_Quality","Estimate_Quality_Label","Estimate_Class"
            ] if c in gfilt.columns]

            st.markdown(f"**Showing {len(gfilt):,} of {len(detail):,} rows**")
            st.dataframe(
                gfilt[show_cols].sort_values(
                    ["Site","Resource","Year","Month"], na_position="last"),
                use_container_width=True, hide_index=True,
                column_config={
                    "GEM_MWh":   st.column_config.NumberColumn("GEM (MWh)",  format="%.2f"),
                    "ECAP_MWh":  st.column_config.NumberColumn("ECAP (MWh)", format="%.2f"),
                    "Delta_MWh": st.column_config.NumberColumn("Δ MWh",      format="%+.2f"),
                    "Delta_Pct": st.column_config.NumberColumn("Δ %",        format="%+.1f%%"),
                    "Estimate_Quality_Label": st.column_config.TextColumn("Quality Note", width="large"),
                }
            )
            st.download_button("⬇ Download GEM Detail (CSV)",
                               gfilt[show_cols].to_csv(index=False),
                               f"gem_detail_{ty}_{date.today()}.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 8 — DATA EXPLORER
# ══════════════════════════════════════════════════════════════════════════════
with tab_explorer:
    st.markdown('<div class="section-header">📂 Raw Data Explorer</div>',
                unsafe_allow_html=True)
    if not st.session_state.reports:
        st.info("No reports loaded yet.")
    else:
        sel = st.selectbox(
            "Select report",
            list(st.session_state.reports.keys()),
            format_func=lambda x: f"{x} — {REPORT_LABELS.get(x,x)}"
        )
        df_exp = st.session_state.reports[sel]
        c1,c2,c3 = st.columns(3)
        c1.metric("Rows",    f"{len(df_exp):,}")
        c2.metric("Columns", f"{len(df_exp.columns):,}")
        c3.metric("Report",  REPORT_LABELS.get(sel,sel))

        if sel == "R19":
            st.info("📌 R-19 is shown here in its full multi-year form. "
                    "Only the target-year data is used for QA checks; "
                    "prior years are reference baselines only.")

        sel_cols = st.multiselect(
            "Columns to show",
            df_exp.columns.tolist(),
            default=df_exp.columns.tolist()[:12]
        )
        if sel_cols:
            st.dataframe(df_exp[sel_cols].head(500),
                         use_container_width=True, hide_index=True)
        st.download_button(
            f"⬇ Download {sel} as CSV",
            df_exp.to_csv(index=False),
            f"ecap_{sel}_{date.today()}.csv", "text/csv"
        )
