"""
utils.py — Report loading, type detection, period detection, and helper functions
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

# ── Commodity → EnergyCAP code mapping ────────────────────────────────────────
GEM_TO_ECAP_COMMODITY = {
    "Electricity":                     ["ELECTRIC", "ELE", "ELECTRICITY"],
    "Natural Gas":                     ["NATURALGAS", "GAS", "NATGAS", "NG"],
    "Diesel":                          ["DIESEL", "DIE"],
    "Propane":                         ["PROPANE", "PRO", "LPG"],
    "LPG - Liquefied Petroleum Gases": ["LPG", "PROPANE", "PRO"],
    "Biomass":                         ["BIOMASS", "BIO"],
    "Fuel Oil":                        ["FUELOIL", "OIL", "FUELOIL2"],
    "Steam":                           ["STEAM", "STM"],
    "Heat":                            ["HEAT", "HTG"],
    "Methane":                         ["METHANE", "GAS", "NATURALGAS"],
    "BioGas":                          ["BIOGAS", "BIO"],
    "Gasoline":                        ["GASOLINE", "GAS"],
    "Coal":                            ["COAL"],
    "Butane":                          ["BUTANE", "LPG"],
    "Aviation Fuel":                   ["AVFUEL", "KEROSENE", "JET"],
}

# Commodity steady-state classification for GEM estimate quality
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

# MWh conversion factors FROM native unit TO MWh
# Key = EnergyCAP commodity code (uppercase), value = factor
NATIVE_TO_MWH = {
    "ELECTRIC":    1/1000,      # kWh → MWh
    "ELE":         1/1000,
    "ELECTRICITY": 1/1000,
    "NATURALGAS":  0.02931,     # CCF → MWh (1 CCF = 0.02931 MWh)
    "GAS":         0.02931,
    "NATGAS":      0.02931,
    "NG":          0.02931,
    "THERM":       0.02931,     # Therm ≈ CCF for NG
    "MCF":         0.2931,      # MCF → MWh
    "DIESEL":      0.03596,     # gallon → MWh
    "DIE":         0.03596,
    "PROPANE":     0.02558,     # gallon → MWh
    "PRO":         0.02558,
    "LPG":         0.02558,
    "FUELOIL":     0.04026,     # gallon → MWh (No. 2 fuel oil)
    "OIL":         0.04026,
    "STEAM":       0.000293,    # lb → MWh
    "STM":         0.000293,
    "COAL":        7.0,         # short ton → MWh
    "BIOMASS":     4.5,         # short ton → MWh (approx)
    "GASOLINE":    0.03374,     # gallon → MWh
    "BUTANE":      0.03247,     # gallon → MWh
    "AVFUEL":      0.03553,     # gallon → MWh (Jet-A)
    "KEROSENE":    0.03553,
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
    name = file_obj.name.lower()

    # Filename hint
    rtype_hint = None
    for code in ["r03","r11","r13","r19","r21","r26","gem",
                 "report-03","report-11","report-13","report-19","report-21","report-26",
                 "emissions_matrix","energy_quantities"]:
        clean = code.replace("-","").replace("_","")
        fname = name.replace("-","").replace("_","")
        if clean in fname:
            if "gem" in clean or "emissions" in clean or "quantities" in clean:
                rtype_hint = "GEM"
            else:
                num = ''.join(filter(str.isdigit, code))
                if num:
                    rtype_hint = f"R{num.zfill(2)}"
            break

    # Load
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(file_obj)
            header_row = 0
        else:
            raw = pd.read_excel(file_obj, header=None)
            # Find header row
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

    # Detect type
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

    # GEM detection fallback: check if raw file has "Quantity For Emissions" in first rows
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

    return df, rtype


def _load_gem(file_obj):
    """Parse GEM pivot export with 3-row header into a tidy long-format DataFrame."""
    try:
        raw = pd.read_excel(file_obj, header=None)
    except Exception:
        raise ValueError("Could not read GEM file as Excel.")

    # Row 0 = years, row 1 = months, row 2 = column names
    years  = raw.iloc[0, 8:20].tolist()
    months = raw.iloc[1, 8:20].tolist()
    month_cols_raw = [f"{int(y) if not pd.isna(y) else 'NA'}_{m}"
                      for y, m in zip(years, months)]

    # Build column names — handle variable column count
    n_cols = raw.shape[1]
    base_cols = ['Customer','Country','Site','Resource','SAN',
                 'Deregulated_Type','Service_Vendor','Service_Account_Number']
    # month cols start at index 8
    extra = n_cols - 8 - len(month_cols_raw)
    total_cols = base_cols + month_cols_raw + [f"Extra_{i}" for i in range(max(extra,0))]
    total_cols = total_cols[:n_cols]

    df = raw.iloc[3:, :n_cols].copy()
    df.columns = total_cols[:len(df.columns)]

    # Keep only Sonoco ENC data rows, remove subtotals and metadata
    df = df[df['Customer'] == 'Sonoco ENC'].copy()
    df = df[df['SAN'].notna()].copy()
    df = df[df['SAN'].astype(str).str.strip() != 'Total'].copy()
    df = df[~df['SAN'].astype(str).str.startswith('Applied filters')].copy()
    df = df.reset_index(drop=True)

    # Convert monthly columns to numeric
    for mc in month_cols_raw:
        if mc in df.columns:
            df[mc] = pd.to_numeric(df[mc], errors='coerce')

    # Store month columns list as attribute via a workaround
    df.attrs['month_cols'] = month_cols_raw
    df.attrs['year']       = int(years[0]) if not pd.isna(years[0]) else None

    # Add commodity class
    df['Estimate_Class'] = df['Resource'].map(COMMODITY_ESTIMATE_CLASS).fillna('steady')

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
    df = df.rename(columns={k:v for k,v in rename_map.items() if k in df.columns})
    return df


def _process_r03(df):
    for col in ["Acct-Meter Begin Date","Acct-Meter End Date",
                "Account Created Date","Meter Created Date","Account date close"]:
        if col in df.columns:
            df[col] = parse_dates_column(df[col])
    rename_map = {
        "Cost Center Code": "Cost Center",
        "Cost Center Name": "Cost Center Name",
    }
    df = df.rename(columns={k:v for k,v in rename_map.items() if k in df.columns})
    return df


# ── Period detection ───────────────────────────────────────────────────────────
def detect_period(report_type, df):
    """Return (begin_date, end_date) as pd.Timestamp or None."""
    try:
        if report_type == "R11":
            if "Billing Period" in df.columns:
                bps = df["Billing Period"].dropna().astype(str)
                bps = bps[bps.str.match(r'^\d{6}$')]
                if bps.empty:
                    return None, None
                dates = pd.to_datetime(bps, format="%Y%m", errors='coerce').dropna()
                return dates.min(), dates.max() + pd.offsets.MonthEnd(0)
            elif "Start Date" in df.columns:
                d = df["Start Date"].dropna()
                return d.min(), d.max()
        elif report_type == "R03":
            if "Acct-Meter Begin Date" in df.columns:
                d = df["Acct-Meter Begin Date"].dropna()
                if not d.empty:
                    return d.min(), pd.Timestamp.now()
        elif report_type == "GEM":
            mc = df.attrs.get('month_cols', [])
            if mc:
                year = df.attrs.get('year', 2025)
                valid = [c for c in mc if c in df.columns and
                         df[c].notna().any() and (df[c] > 0).any()]
                if valid:
                    first = valid[0].split("_")[1]
                    last  = valid[-1].split("_")[1]
                    month_map = {"01-Jan":1,"02-Feb":2,"03-Mar":3,"04-Apr":4,
                                 "05-May":5,"06-Jun":6,"07-Jul":7,"08-Aug":8,
                                 "09-Sep":9,"10-Oct":10,"11-Nov":11,"12-Dec":12}
                    m1 = month_map.get(first, 1)
                    m2 = month_map.get(last,  12)
                    beg = pd.Timestamp(year=year, month=m1, day=1)
                    end = pd.Timestamp(year=year, month=m2, day=1) + pd.offsets.MonthEnd(0)
                    return beg, end
        elif report_type in ("R19","R21","R26"):
            # Look for period-like columns
            for col in df.columns:
                if any(x in col.lower() for x in ["period","month","date","year"]):
                    pass
            return None, None
    except Exception:
        pass
    return None, None


def compute_overlap(periods: dict):
    """
    Given {report_type: (begin, end)}, compute the overlapping window.
    Returns (overlap_begin, overlap_end) or (None, None).
    """
    valids = [(b, e) for b, e in periods.values()
              if b is not None and e is not None]
    if not valids:
        return None, None
    latest_begin  = max(b for b, e in valids)
    earliest_end  = min(e for b, e in valids)
    if latest_begin <= earliest_end:
        return latest_begin, earliest_end
    return None, None


def filter_r11_to_period(r11, begin, end):
    """Filter R-11 to billing periods within [begin, end]."""
    if begin is None or end is None:
        return r11
    if "Billing Period" in r11.columns:
        def bp_in_range(bp):
            d = billing_period_to_date(bp)
            if pd.isna(d):
                return True
            return begin <= d <= end
        mask = r11["Billing Period"].apply(bp_in_range)
        return r11[mask].copy()
    return r11


def filter_gem_to_period(gem, begin, end):
    """Zero out GEM monthly columns outside [begin, end]."""
    if begin is None or end is None or not hasattr(gem, 'attrs'):
        return gem
    mc = gem.attrs.get('month_cols', [])
    year = gem.attrs.get('year', 2025)
    month_map = {"01-Jan":1,"02-Feb":2,"03-Mar":3,"04-Apr":4,
                 "05-May":5,"06-Jun":6,"07-Jul":7,"08-Aug":8,
                 "09-Sep":9,"10-Oct":10,"11-Nov":11,"12-Dec":12}
    gem = gem.copy()
    gem.attrs = {"month_cols": mc, "year": year}
    for col in mc:
        if col not in gem.columns:
            continue
        parts = col.split("_", 1)
        if len(parts) < 2:
            continue
        try:
            yr = int(parts[0])
            mo = month_map.get(parts[1], 0)
            col_date = pd.Timestamp(year=yr, month=mo, day=1)
            if col_date < begin or col_date > end:
                gem[col] = np.nan
        except Exception:
            pass
    return gem
