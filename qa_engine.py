"""
qa_engine.py — All QA and GEM reconciliation check logic.
"""

import pandas as pd
import numpy as np
from utils import (safe_zscore, billing_period_to_date, format_issue_row,
                   GEM_TO_ECAP_COMMODITY, NATIVE_TO_MWH, COMMODITY_ESTIMATE_CLASS)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_all_checks(reports, overlap_begin=None, overlap_end=None,
                   outlier_zscore=2.5, pct_change_thresh=50,
                   zero_use_months=2, acct_start_year_threshold=2000,
                   conversion_factors=None):
    """Run all QA + GEM reconciliation checks."""
    r03 = reports.get("R03")
    r11 = reports.get("R11")
    gem = reports.get("GEM")

    if conversion_factors is None:
        conversion_factors = NATIVE_TO_MWH

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
            check_deregulated_market(r03, r11),
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
            overlap_begin=overlap_begin,
            overlap_end=overlap_end,
            acct_start_year_threshold=acct_start_year_threshold,
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
        "sample":           flagged_df.head(10) if flagged_df is not None and count > 0 else pd.DataFrame(),
        "issues":           issues or [],
        "risks":            risks  or [],
    }


def _to_issues(df, category, severity, desc_col=None, default_desc=""):
    rows = []
    for _, r in df.iterrows():
        rows.append(format_issue_row(
            site=r.get("Site",""), account=r.get("Account",r.get("Account Code","")),
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
    name = "Missing Native Use"
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
    name = "Negative Use Values"
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
    dupes = r11[r11["Bill ID"].notna() & r11.duplicated(subset=["Bill ID"], keep=False)].copy()
    dupes["_desc"] = dupes["Bill ID"].apply(lambda v: f"Bill ID {v} appears multiple times")
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
                site = grp["Site"].iloc[0] if "Site" in grp.columns else ""
                acct = grp["Account"].iloc[0] if "Account" in grp.columns else ""
                com  = grp["Commodity"].iloc[0] if "Commodity" in grp.columns else ""
                gap_rows.append({
                    "Site":site,"Account":acct,"Meter Code":meter,"Commodity":com,
                    "Bill ID":"","Billing Period":str(exp)[:7],
                    "_desc":f"No bill found for {str(exp)[:7]}"
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
        grp = grp.dropna(subset=["Start Date","End Date"]).sort_values("Start Date").reset_index(drop=True)
        for i in range(len(grp)-1):
            end_i   = grp.loc[i,   "End Date"]
            start_j = grp.loc[i+1, "Start Date"]
            if pd.notna(end_i) and pd.notna(start_j) and start_j < end_i:
                overlap_rows.append({
                    "Site":grp.loc[i,"Site"] if "Site" in grp.columns else "",
                    "Account":grp.loc[i,"Account"] if "Account" in grp.columns else "",
                    "Meter Code":meter,
                    "Commodity":grp.loc[i,"Commodity"] if "Commodity" in grp.columns else "",
                    "Bill ID":grp.loc[i,"Bill ID"],"Billing Period":"",
                    "_desc":f"Bill {grp.loc[i,'Bill ID']} ends {end_i.date()} overlaps next bill starting {start_j.date()}"
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
    flagged = r11[(r11["Days"].notna()) & ((r11["Days"] < 5) | (r11["Days"] > 95))].copy()
    flagged["_desc"] = flagged["Days"].apply(lambda d: f"Billing period = {int(d)} days")
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
                if consecutive == 0: streak_start = row.get("Billing Period","")
                consecutive += 1
            else:
                if consecutive >= threshold:
                    flagged_rows.append({
                        "Site":row.get("Site",""),"Account":row.get("Account",""),
                        "Meter Code":meter,"Commodity":row.get("Commodity",""),
                        "Bill ID":"","Billing Period":streak_start,
                        "_desc":f"{consecutive} consecutive zero-use months from {streak_start}"
                    })
                consecutive = 0; streak_start = None
        if consecutive >= threshold:
            flagged_rows.append({
                "Site":grp["Site"].iloc[-1] if "Site" in grp.columns else "",
                "Account":grp["Account"].iloc[-1] if "Account" in grp.columns else "",
                "Meter Code":meter,
                "Commodity":grp["Commodity"].iloc[-1] if "Commodity" in grp.columns else "",
                "Bill ID":"","Billing Period":streak_start,
                "_desc":f"{consecutive} consecutive zero-use months from {streak_start}"
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
        if len(use) < 4: continue
        z = safe_zscore(use)
        outliers = grp.loc[z[z.abs() > z_thresh].index].copy()
        if not outliers.empty:
            outliers["Z-score"] = z[z.abs() > z_thresh].round(2)
            outliers["_desc"] = outliers.apply(
                lambda r: f"Use={r['Native Use']:.1f}, Z={r['Z-score']:.2f}", axis=1)
            flagged_list.append(outliers)
    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    return _result(name,
        "Bills with statistically unusual use (quadratic regression outlier).",
        "MEDIUM — outliers can skew annual totals.",
        flagged, risks=_to_risks(flagged, name, "_desc"), is_risk=True)


def check_cost_outliers(r11, z_thresh=2.5):
    name = f"Cost Outliers (Z-score > {z_thresh})"
    if "Meter Code" not in r11.columns or "Total Cost" not in r11.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged_list = []
    for meter, grp in r11.groupby("Meter Code"):
        cost = grp["Total Cost"].dropna()
        if len(cost) < 4: continue
        z = safe_zscore(cost)
        outliers = grp.loc[z[z.abs() > z_thresh].index].copy()
        if not outliers.empty:
            outliers["Z-score"] = z[z.abs() > z_thresh].round(2)
            outliers["_desc"] = outliers.apply(
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
        grp["prior_use"] = grp["Native Use"].shift(1)
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
                "Site":grp["Site"].iloc[0] if "Site" in grp.columns else "",
                "Account":grp["Account"].iloc[0] if "Account" in grp.columns else "",
                "Meter Code":meter,
                "Commodity":grp["Commodity"].iloc[0] if "Commodity" in grp.columns else "",
                "Bill ID":"","Billing Period":"",
                "_desc":f"Rate schedule changes: {' | '.join(str(r) for r in unique_rates)}"
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
    inactive = r03[r03["Meter Status"].str.lower().str.strip() == "inactive"]["Meter Code"].unique()
    flagged = r11[r11["Meter Code"].isin(inactive)].copy() if len(inactive) else pd.DataFrame()
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
    return _result(name, "Meters with no serial number.", "LOW", no_serial, risks=risk_rows, is_risk=True)


def check_excluded_from_audits(r03):
    name = "Accounts Excluded from Audits"
    col = "Excluded From Audits (Y/N)"
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
        "MEDIUM — data quality issues won't be caught by EnergyCAP.",
        flagged, risks=risk_rows, is_risk=True)


def check_deregulated_market(r03, r11):
    name = "Deregulated Market Meters"
    col = "GEM Deregulated Mkt"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    dereg = r03[r03[col].notna() & (r03[col] != 0) & (r03[col] != "")].copy()
    risk_rows = []
    for _, r in dereg.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Risk",
            description="Deregulated market — verify both distribution and supply bills are present",
            commodity="", period=""))
    return _result(name,
        "Meters in deregulated markets — distribution and supply may be separate.",
        "HIGH — missing supply bills means Scope 2 market-based calculations are incomplete.",
        dereg, risks=risk_rows, is_risk=True)


def check_missing_acct_meter_dates(r03):
    name = "Missing Account-Meter Begin Dates"
    col = "Acct-Meter Begin Date"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    flagged = r03[r03[col].isna()].copy()
    issue_rows = []
    for _, r in flagged.iterrows():
        issue_rows.append(format_issue_row(
            site=r.get("Cost Center",""), account=r.get("Account Number",""),
            meter=r.get("Meter Code",""), bill_id="", category=name, severity="Critical",
            description="Acct-Meter Begin Date is not set — GEM may backfill estimates before meter existed",
            commodity="", period=""))
    return _result(name,
        "Meters with no begin date — GEM may estimate for periods before meter existed.",
        "HIGH — causes fabricated GEM estimates prior to meter activation.",
        flagged, issues=issue_rows)


def check_account_start_date(r03, threshold_year=2000):
    """Flag accounts whose start date is null OR suspiciously far back."""
    name = "Suspicious Account Start Date"
    col = "Acct-Meter Begin Date"
    if col not in r03.columns:
        return _result(name, "", "", pd.DataFrame())

    flagged_rows = []
    threshold = pd.Timestamp(year=threshold_year, month=1, day=1)

    for _, r in r03.iterrows():
        date_val = r.get(col)
        desc = None
        if pd.isna(date_val):
            desc = "Acct-Meter Begin Date is empty"
        elif pd.notna(date_val) and pd.Timestamp(date_val) < threshold:
            desc = f"Acct-Meter Begin Date ({pd.Timestamp(date_val).date()}) is before {threshold_year} — verify"
        if desc:
            flagged_rows.append({
                "Site": r.get("Cost Center",""),
                "Account Number": r.get("Account Number",""),
                "Meter Code": r.get("Meter Code",""),
                "Acct-Meter Begin Date": date_val,
                "_desc": desc,
            })

    flagged = pd.DataFrame(flagged_rows)
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
    """Flag meters with non-monthly billing frequency."""
    name = "Non-Monthly Billing Frequency"
    non_monthly = ["quarterly","bimonthly","bi-monthly","bi monthly",
                   "annual","annually","semi-annual","semi annual","biannual"]
    # Check both Bill Frequency columns
    flagged_rows = []
    for col in ["Bill Frequency","Billing Frequency"]:
        if col not in r03.columns:
            continue
        mask = r03[col].astype(str).str.lower().str.strip().isin(non_monthly)
        sub = r03[mask].copy()
        for _, r in sub.iterrows():
            flagged_rows.append({
                "Site": r.get("Cost Center",""),
                "Account Number": r.get("Account Number",""),
                "Meter Code": r.get("Meter Code",""),
                "Frequency": r.get(col,""),
                "_desc": f"Billing frequency is '{r.get(col,'')}' — GEM monthly distribution is an approximation",
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
        "Meters billed quarterly, bi-monthly, or annually. GEM spreads the bill equally across months.",
        "MEDIUM — monthly GEM values are approximations, not metered monthly data.",
        flagged, risks=risk_rows, is_risk=True)


def check_meter_no_bills(r03, r11):
    name = "Active Meters with No Bills"
    if r11 is None or "Meter Code" not in r03.columns:
        return _result(name, "", "", pd.DataFrame())
    active = r03[r03["Meter Status"].str.lower().str.strip() == "active"]["Meter Code"].unique() \
             if "Meter Status" in r03.columns else r03["Meter Code"].unique()
    billed  = r11["Meter Code"].unique()
    no_bills = set(active) - set(billed)
    flagged = r03[r03["Meter Code"].isin(no_bills)].copy()
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
    known = set(r03["Meter Code"].dropna().unique())
    flagged = r11[~r11["Meter Code"].isin(known)].copy()
    flagged["_desc"] = "Meter Code not found in R-03 setup report"
    return _result(name,
        "Bills in R-11 whose Meter Code doesn't appear in R-03.",
        "HIGH — orphan bills may be excluded from site rollups.",
        flagged, issues=_to_issues(flagged, name, "Critical", "_desc"))


# ══════════════════════════════════════════════════════════════════════════════
# GEM RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════════

def run_gem_reconciliation(gem, r11, r03, overlap_begin=None, overlap_end=None,
                           acct_start_year_threshold=2000, conversion_factors=None):
    """Full GEM ↔ EnergyCAP reconciliation."""
    if conversion_factors is None:
        conversion_factors = NATIVE_TO_MWH

    month_cols = gem.attrs.get('month_cols', [])
    gem_year   = gem.attrs.get('year', 2025)

    month_map = {"01-Jan":1,"02-Feb":2,"03-Mar":3,"04-Apr":4,
                 "05-May":5,"06-Jun":6,"07-Jul":7,"08-Aug":8,
                 "09-Sep":9,"10-Oct":10,"11-Nov":11,"12-Dec":12}

    # ── Build R-11 monthly pivot by meter+commodity ───────────────────────────
    r11_pivot = _build_r11_pivot(r11, gem_year, conversion_factors)

    # ── Build GEM long format ─────────────────────────────────────────────────
    gem_long = _gem_to_long(gem, month_cols, month_map, gem_year)

    # ── Match GEM SANs to EnergyCAP Meter Codes ──────────────────────────────
    gem_long = _match_san_to_meter(gem_long, r11, r03)

    # ── Merge ─────────────────────────────────────────────────────────────────
    merged = pd.merge(gem_long, r11_pivot,
                      on=["Meter Code","Year","Month"], how="outer",
                      suffixes=("_gem","_ecap"))

    # Apply period filter
    if overlap_begin and overlap_end:
        merged = merged[
            (merged["Year"] == overlap_begin.year) |
            ((merged["Year"] == overlap_begin.year) &
             (merged["Month"] >= overlap_begin.month))
        ]
        # Simpler: keep rows within overlap
        def in_overlap(row):
            try:
                d = pd.Timestamp(year=int(row["Year"]), month=int(row["Month"]), day=1)
                return overlap_begin <= d <= overlap_end
            except Exception:
                return True
        merged = merged[merged.apply(in_overlap, axis=1)].copy()

    # ── Compute variance ──────────────────────────────────────────────────────
    merged["GEM_MWh"]  = pd.to_numeric(merged.get("Qty_MWh_gem",  merged.get("Qty_MWh", np.nan)), errors='coerce')
    merged["ECAP_MWh"] = pd.to_numeric(merged.get("Qty_MWh_ecap", merged.get("Qty_MWh", np.nan)), errors='coerce')
    merged["Delta_MWh"] = merged["GEM_MWh"] - merged["ECAP_MWh"]
    merged["Delta_Pct"] = np.where(
        merged["ECAP_MWh"].notna() & (merged["ECAP_MWh"] != 0),
        (merged["Delta_MWh"] / merged["ECAP_MWh"].abs()) * 100,
        np.nan)

    # ── Classify each row ─────────────────────────────────────────────────────
    merged = _classify_gem_rows(merged, r03, acct_start_year_threshold)

    # ── Generate checks, issues, risks ───────────────────────────────────────
    checks, issues, risks = _gem_checks_from_merged(merged)

    return {
        "check_results": checks,
        "issues":        issues,
        "risks":         risks,
        "detail_df":     merged,
    }


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
            yr = int(bp[:4])
            mo = int(bp[4:6])
        except Exception:
            continue
        if yr != gem_year:
            continue
        use = pd.to_numeric(row.get("Native Use", np.nan), errors='coerce')
        if pd.isna(use):
            continue
        commodity = str(row.get("Commodity","")).upper().strip()
        factor = conversion_factors.get(commodity, None)
        mwh    = use * factor if factor else np.nan
        rows.append({
            "Meter Code": row.get("Meter Code",""),
            "Year":       yr,
            "Month":      mo,
            "Native_Use": use,
            "Commodity":  row.get("Commodity",""),
            "Qty_MWh":    mwh,
        })

    if not rows:
        return pd.DataFrame(columns=["Meter Code","Year","Month","Qty_MWh","Native_Use"])

    df = pd.DataFrame(rows)
    pivot = df.groupby(["Meter Code","Year","Month","Commodity"]).agg(
        Qty_MWh=("Qty_MWh","sum"),
        Native_Use=("Native_Use","sum")
    ).reset_index()
    return pivot


def _gem_to_long(gem, month_cols, month_map, gem_year):
    """Convert GEM wide format to long format."""
    rows = []
    for _, row in gem.iterrows():
        san      = str(row.get("SAN","")).strip()
        resource = str(row.get("Resource","")).strip()
        site     = str(row.get("Site","")).strip()
        country  = str(row.get("Country","")).strip()
        est_class = str(row.get("Estimate_Class","steady"))
        svc_acct = str(row.get("Service_Account_Number","")).strip()
        vendor   = str(row.get("Service_Vendor","")).strip()
        for mc in month_cols:
            if mc not in gem.columns:
                continue
            parts = mc.split("_",1)
            if len(parts) < 2:
                continue
            try:
                yr = int(parts[0])
                mo = month_map.get(parts[1], 0)
            except Exception:
                continue
            val = pd.to_numeric(row.get(mc, np.nan), errors='coerce')
            rows.append({
                "GEM_SAN":       san,
                "Resource":      resource,
                "Site":          site,
                "Country":       country,
                "Estimate_Class": est_class,
                "Service_Account_Number": svc_acct,
                "Service_Vendor": vendor,
                "Year":          yr,
                "Month":         mo,
                "Qty_MWh":       val,
            })
    return pd.DataFrame(rows)


def _match_san_to_meter(gem_long, r11, r03):
    """Add Meter Code column to GEM long by matching SAN."""
    # Build lookup tables
    # Tier 1: SAN == Meter Code
    r11_meters = set(r11["Meter Code"].dropna().astype(str).unique()) if r11 is not None else set()

    # Tier 2: SAN == Serial Number (from R-03)
    serial_map = {}
    if r03 is not None and "Serial Number" in r03.columns and "Meter Code" in r03.columns:
        for _, r in r03.iterrows():
            sn = str(r.get("Serial Number","")).strip()
            if sn and sn not in ("NO METER NUMBER","NOT METERED","nan",""):
                serial_map[sn] = str(r.get("Meter Code",""))

    # Tier 2b: SAN == Account Code (from R-11)
    acct_map = {}
    if r11 is not None and "Account" in r11.columns and "Meter Code" in r11.columns:
        for _, r in r11.iterrows():
            acct = str(r.get("Account","")).strip()
            if acct and acct not in ("nan",""):
                acct_map[acct] = str(r.get("Meter Code",""))

    def match(san):
        san = str(san).strip()
        if san in r11_meters:
            return san, "Tier1-Direct"
        if san in serial_map:
            return serial_map[san], "Tier2-Serial"
        if san in acct_map:
            return acct_map[san], "Tier2-Account"
        return None, "Unmatched"

    gem_long[["Meter Code","Match_Tier"]] = gem_long["GEM_SAN"].apply(
        lambda s: pd.Series(match(s)))
    return gem_long


def _classify_gem_rows(merged, r03, threshold_year):
    """Classify each merged row for estimate quality."""

    # Build meter→begin_date lookup
    begin_dates = {}
    non_monthly = {"quarterly","bimonthly","bi-monthly","bi monthly",
                   "annual","annually","semi-annual"}
    billing_freq = {}
    if r03 is not None:
        if "Acct-Meter Begin Date" in r03.columns and "Meter Code" in r03.columns:
            for _, r in r03.iterrows():
                mc = str(r.get("Meter Code",""))
                bd = r.get("Acct-Meter Begin Date")
                if mc and pd.notna(bd):
                    begin_dates[mc] = pd.Timestamp(bd)
        for col in ["Bill Frequency","Billing Frequency"]:
            if col in r03.columns:
                for _, r in r03.iterrows():
                    mc = str(r.get("Meter Code",""))
                    freq = str(r.get(col,"")).lower().strip()
                    if mc:
                        billing_freq[mc] = freq

    def classify_row(row):
        mc    = str(row.get("Meter Code",""))
        yr    = row.get("Year", 0)
        mo    = row.get("Month", 0)
        gem   = row.get("GEM_MWh", np.nan)
        ecap  = row.get("ECAP_MWh", np.nan)
        ecls  = row.get("Estimate_Class","steady")

        has_gem  = pd.notna(gem)  and gem  != 0
        has_ecap = pd.notna(ecap) and ecap != 0

        # GEM has data, EnergyCAP doesn't → GEM estimated
        is_gem_estimated = has_gem and not has_ecap

        if not is_gem_estimated:
            return "Normal"

        # Check 1: before account start date
        if mc in begin_dates:
            try:
                row_date = pd.Timestamp(year=int(yr), month=int(mo), day=1)
                if row_date < begin_dates[mc]:
                    return "Confirmed Bad Estimate"
            except Exception:
                pass

        # Check 1b: no start date at all
        if mc not in begin_dates and r03 is not None:
            return "Confirmed Bad Estimate"

        # Check 2: non-monthly billing
        freq = billing_freq.get(mc,"")
        if any(nm in freq for nm in non_monthly):
            return "Structurally Unreliable Estimate"

        # Check 3: seasonal commodity + GEM non-zero + EnergyCAP zero
        if ecls in ("seasonal","event"):
            return "Suspect Estimate"

        return "Defensible Estimate"

    merged["Estimate_Quality"] = merged.apply(classify_row, axis=1)
    return merged


def _gem_checks_from_merged(merged):
    """Generate check results, issues, risks from the merged GEM↔ECAP dataframe."""
    checks = []
    issues = []
    risks  = []

    # ── Check: EnergyCAP has data, GEM doesn't ────────────────────────────────
    ecap_only = merged[
        merged["ECAP_MWh"].notna() & (merged["ECAP_MWh"] != 0) &
        (merged["GEM_MWh"].isna() | (merged["GEM_MWh"] == 0))
    ].copy()
    ecap_only["_desc"] = ecap_only.apply(
        lambda r: f"EnergyCAP={r['ECAP_MWh']:.2f} MWh, GEM=null — bill not flowing to GEM", axis=1)
    checks.append(_result("Bills in EnergyCAP Missing from GEM",
        "EnergyCAP has consumption but GEM shows null/zero for same meter+month.",
        "CRITICAL — these bills are excluded from emissions calculations entirely.",
        ecap_only,
        issues=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="Bills Missing from GEM", severity="Critical",
            description=r["_desc"], commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in ecap_only.iterrows()]))

    # ── Check: GEM significantly > EnergyCAP (over-reporting) ────────────────
    over = merged[
        merged["Delta_Pct"].notna() & (merged["Delta_Pct"] > 20)
    ].copy()
    if not over.empty:
        over = over.copy()
        over["_desc"] = [
            f"GEM={float(row.get('GEM_MWh',0) or 0):.2f} vs EnergyCAP={float(row.get('ECAP_MWh',0) or 0):.2f} MWh "
            f"(+{float(row.get('Delta_Pct',0) or 0):.1f}%)"
            for _, row in over.iterrows()
        ]
    checks.append(_result("GEM Over-reports vs EnergyCAP (>20%)",
        "GEM quantity is more than 20% above EnergyCAP for same meter+month.",
        "HIGH — may indicate double-counting or GEM using estimated values when actuals exist.",
        over,
        risks=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="GEM Over-reports vs EnergyCAP", severity="Risk",
            description=r["_desc"], commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in over.iterrows()]))

    # ── Check: GEM significantly < EnergyCAP (under-reporting) ───────────────
    under = merged[
        merged["Delta_Pct"].notna() & (merged["Delta_Pct"] < -20)
    ].copy()
    if not under.empty:
        under = under.copy()
        under["_desc"] = [
            f"GEM={float(row.get('GEM_MWh',0) or 0):.2f} vs EnergyCAP={float(row.get('ECAP_MWh',0) or 0):.2f} MWh "
            f"({float(row.get('Delta_Pct',0) or 0):.1f}%)"
            for _, row in under.iterrows()
        ]
    checks.append(_result("GEM Under-reports vs EnergyCAP (>20% gap)",
        "GEM quantity is more than 20% below EnergyCAP.",
        "HIGH — losses in transit from EnergyCAP to GEM; emissions understated.",
        under,
        risks=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="GEM Under-reports vs EnergyCAP", severity="Risk",
            description=r["_desc"], commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in under.iterrows()]))

    # ── Check: Confirmed bad estimates ────────────────────────────────────────
    bad_est = merged[merged["Estimate_Quality"] == "Confirmed Bad Estimate"].copy()
    if not bad_est.empty:
        bad_est = bad_est.copy()
        bad_est["_desc"] = [
            f"GEM estimates {float(row.get('GEM_MWh',0) or 0):.2f} MWh for "
            f"{int(row.get('Year',0))}-{int(row.get('Month',0)):02d} but no EnergyCAP data — "
            f"period may be before account start date"
            for _, row in bad_est.iterrows()
        ]
    checks.append(_result("Confirmed Bad GEM Estimates",
        "GEM has estimated values for periods before the account existed in EnergyCAP, "
        "or for accounts with no start date.",
        "CRITICAL — fabricated emissions data in historical periods.",
        bad_est,
        issues=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="Confirmed Bad GEM Estimate", severity="Critical",
            description=r["_desc"], commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in bad_est.iterrows()]))

    # ── Check: Structurally unreliable estimates ──────────────────────────────
    struct = merged[merged["Estimate_Quality"] == "Structurally Unreliable Estimate"].copy()
    checks.append(_result("GEM Estimates on Non-Monthly Meters",
        "GEM distributes a quarterly/bi-monthly bill equally across months.",
        "MEDIUM — monthly values are approximations, not metered data.",
        struct,
        risks=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="GEM Estimate — Non-Monthly Billing", severity="Risk",
            description=f"Non-monthly meter: GEM monthly value is equal-split approximation",
            commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in struct.iterrows()]))

    # ── Check: Suspect estimates (seasonal commodity) ─────────────────────────
    suspect = merged[merged["Estimate_Quality"] == "Suspect Estimate"].copy()
    checks.append(_result("GEM Estimates on Seasonal/Event-Driven Commodities",
        "GEM fills a zero-bill month with an estimate for a seasonal commodity "
        "(diesel, propane, fuel oil, etc.) — zero may be genuinely correct.",
        "MEDIUM — GEM smooths what is legitimately zero consumption.",
        suspect,
        risks=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("Meter Code",""),
            bill_id="", category="GEM Estimate — Seasonal Commodity", severity="Risk",
            description=f"Seasonal commodity: GEM estimated {r.get('GEM_MWh',0):.2f} MWh "
                        f"but EnergyCAP shows zero — may be genuine zero consumption",
            commodity=r.get("Resource",""),
            period=f"{int(r['Year'])}-{int(r['Month']):02d}")
            for _, r in suspect.iterrows()]))

    # ── Check: Unmatched GEM SANs ─────────────────────────────────────────────
    unmatched = merged[merged["Match_Tier"] == "Unmatched"].drop_duplicates("GEM_SAN").copy() \
                if "Match_Tier" in merged.columns else pd.DataFrame()
    checks.append(_result("GEM SANs with No EnergyCAP Match",
        "GEM Service Aggregator Numbers that cannot be matched to any EnergyCAP meter, "
        "account, or serial number.",
        "HIGH — orphan GEM entries may be manual estimates or legacy meters.",
        unmatched,
        risks=[format_issue_row(
            site=r.get("Site",""), account="", meter=r.get("GEM_SAN",""),
            bill_id="", category="Unmatched GEM SAN", severity="Risk",
            description=f"SAN '{r.get('GEM_SAN','')}' cannot be matched to EnergyCAP",
            commodity=r.get("Resource",""), period="")
            for _, r in unmatched.iterrows()]))

    # Flatten issues/risks
    for c in checks:
        issues.extend(c.get("issues",[]))
        risks.extend(c.get("risks",[]))

    return checks, issues, risks
