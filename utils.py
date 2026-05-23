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
