"""
utils.py — Report loading, type detection, and helper functions
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import io

# ── Report metadata ────────────────────────────────────────────────────────────
REPORT_LABELS = {
    "R03": "Setup Report (Accounts / Meters / Sites)",
    "R11": "Bill Transfer Format (Bill Detail + UOM)",
    "R13": "Bill Analysis (Outliers)",
    "R19": "Monthly Utility Use and Cost",
    "R21": "Monthly Comparison",
    "R26": "Use and Cost Summary",
}

# Signature columns used to detect report type
REPORT_SIGNATURES = {
    "R03": ["account number", "meter code", "meter status", "acct-meter begin date", "vendor code"],
    "R11": ["bill id", "billing period", "native use", "common use", "commodity code", "place code"],
    "R13": ["bill id", "cost var dec", "use std dev", "est bill"],
    "R19": ["use kwh", "use therm", "demand"],          # broad — checked after R11
    "R21": ["var %", "ytd var %", "base year"],
    "R26": ["cost/day", "cost/unit", "#days"],
}

# ── Excel serial date conversion ───────────────────────────────────────────────
def excel_serial_to_date(serial):
    """Convert Excel date serial number to Python date."""
    try:
        serial = float(serial)
        if serial < 1:
            return None
        # Excel epoch is Dec 30, 1899
        delta = timedelta(days=serial - 2)  # -2 accounts for Excel's leap year bug
        return (datetime(1899, 12, 31) + delta).date()
    except (TypeError, ValueError):
        return None


def parse_dates_column(series):
    """Try to parse a column as dates — handles Excel serials and strings."""
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


# ── Report loading ─────────────────────────────────────────────────────────────
def load_report(file_obj):
    """
    Load an uploaded file into a DataFrame and detect its report type.
    Returns (df, report_type_str) or (df, None) if type can't be detected.
    """
    name = file_obj.name.lower()

    # Try filename hint first
    rtype_hint = None
    for code in ["r03", "r11", "r13", "r19", "r21", "r26",
                  "report-03", "report-11", "report-13",
                  "report-19", "report-21", "report-26"]:
        if code.replace("-", "") in name.replace("-", "").replace("_", ""):
            rtype_hint = "R" + code.replace("report", "").replace("-", "").replace("r", "").zfill(2)
            break

    # Load file
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(file_obj)
        else:
            # Excel — try to skip blank header rows (R-11 has 4 blank rows)
            raw = pd.read_excel(file_obj, header=None)
            # Find first row that looks like a header (has >3 non-null string values)
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

    # Normalize column names for detection
    df.columns = [str(c).strip() for c in df.columns]
    cols_lower = [c.lower() for c in df.columns]

    # Detect report type by signature columns
    rtype = rtype_hint
    if not rtype:
        best_match, best_score = None, 0
        for code, sig_cols in REPORT_SIGNATURES.items():
            score = sum(1 for s in sig_cols if any(s in c for c in cols_lower))
            if score > best_score:
                best_score = score
                best_match = code
        if best_score >= 2:
            rtype = best_match

    # Post-process known report types
    if rtype == "R11":
        df = _process_r11(df)
    elif rtype == "R03":
        df = _process_r03(df)

    return df, rtype


def _process_r11(df):
    """Clean and type-cast R-11 columns."""
    # Convert date serials
    for col in ["Start Date", "End Date"]:
        if col in df.columns:
            df[col] = parse_dates_column(df[col])

    # Numeric columns
    for col in ["Native Use", "Cost", "Demand", "Common Use", "Total Cost", "Days"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Billing Period as string YYYYMM
    if "Billing Period" in df.columns:
        df["Billing Period"] = df["Billing Period"].astype(str).str.strip()

    # Rename for consistency
    rename_map = {
        "Place Code":      "Site",
        "C Ctr Code":      "Cost Center",
        "Commodity Code":  "Commodity",
        "Account Code":    "Account",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    return df


def _process_r03(df):
    """Clean and type-cast R-03 columns."""
    for col in ["Acct-Meter Begin Date", "Acct-Meter End Date",
                "Account Created Date", "Meter Created Date", "Account date close"]:
        if col in df.columns:
            df[col] = parse_dates_column(df[col])

    # Rename Cost Center Code → Site proxy
    rename_map = {
        "Cost Center Code": "Cost Center",
        "Cost Center Name": "Cost Center Name",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    return df


# ── Helpers ────────────────────────────────────────────────────────────────────
def safe_zscore(series):
    """Compute Z-scores, returning NaN where std=0."""
    mean = series.mean()
    std  = series.std()
    if std == 0 or pd.isna(std):
        return pd.Series([0.0] * len(series), index=series.index)
    return (series - mean) / std


def billing_period_to_date(bp_str):
    """Convert YYYYMM string to a datetime."""
    try:
        return pd.to_datetime(str(bp_str), format="%Y%m")
    except Exception:
        return pd.NaT


def format_issue_row(site, account, meter, bill_id, category, severity, description, commodity=None):
    """Build a standardized issue/risk row dict."""
    return {
        "Site":        site,
        "Account":     account,
        "Meter":       meter,
        "Bill ID":     bill_id,
        "Commodity":   commodity,
        "Category":    category,
        "Severity":    severity,
        "Description": description,
    }
