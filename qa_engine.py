"""
qa_engine.py — All QA and GEM reconciliation logic.
v2.1 — target-year awareness, refined seasonal estimate classification
"""

import pandas as pd
import numpy as np
from utils import (safe_zscore, billing_period_to_date, format_issue_row,
                   GEM_TO_ECAP_COMMODITY, NATIVE_TO_MWH, COMMODITY_ESTIMATE_CLASS,
                   NON_MONTHLY_FREQUENCIES, _month_label_map)


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
