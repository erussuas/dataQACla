"""
qa_engine.py — All QA check logic for EnergyCAP data

Each check returns a dict:
  {
    "name":             str,
    "description":      str,
    "emissions_impact": str,
    "status":           "ok" | "warning" | "error",
    "count":            int,
    "sample":           DataFrame (up to 10 rows),
    "issues":           list of issue-row dicts,   # goes to issue register
    "risks":            list of risk-row dicts,     # goes to risk register
  }
"""

import pandas as pd
import numpy as np
from utils import safe_zscore, billing_period_to_date, format_issue_row


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run_all_checks(reports: dict, outlier_zscore=2.5,
                   pct_change_thresh=50, zero_use_months=2) -> dict:
    """
    Run all QA checks across uploaded reports.
    Returns a dict with:
      - check_results: list of check dicts
      - issues_df:     DataFrame of all confirmed issues
      - risks_df:      DataFrame of all risks
      - meta:          summary stats
    """
    r03 = reports.get("R03")
    r11 = reports.get("R11")
    r13 = reports.get("R13")
    r19 = reports.get("R19")
    r21 = reports.get("R21")
    r26 = reports.get("R26")

    check_results = []
    all_issues    = []
    all_risks     = []

    # ── R-11 checks (bill level) ───────────────────────────────────────────────
    if r11 is not None:
        checks = [
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
            check_rate_schedule_change(r11),
        ]
        check_results.extend(checks)
        for c in checks:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks", []))

    # ── R-03 checks (setup) ────────────────────────────────────────────────────
    if r03 is not None:
        checks_03 = [
            check_inactive_meters(r03, r11),
            check_missing_serial(r03),
            check_excluded_from_audits(r03),
            check_deregulated_market(r03, r11),
            check_missing_acct_meter_dates(r03),
            check_meter_no_bills(r03, r11),
        ]
        check_results.extend(checks_03)
        for c in checks_03:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks", []))

    # ── Cross-report checks ────────────────────────────────────────────────────
    if r03 is not None and r11 is not None:
        cross = [
            check_orphan_bills(r03, r11),
        ]
        check_results.extend(cross)
        for c in cross:
            all_issues.extend(c.get("issues", []))
            all_risks.extend(c.get("risks", []))

    # ── Build registers ────────────────────────────────────────────────────────
    issues_df = pd.DataFrame(all_issues) if all_issues else pd.DataFrame(
        columns=["Site", "Account", "Meter", "Bill ID", "Commodity",
                 "Category", "Severity", "Description"])
    risks_df  = pd.DataFrame(all_risks)  if all_risks  else pd.DataFrame(
        columns=["Site", "Account", "Meter", "Bill ID", "Commodity",
                 "Category", "Severity", "Description"])

    # ── Meta stats ─────────────────────────────────────────────────────────────
    meta = {}
    if r11 is not None:
        meta["total_bills"]       = len(r11)
        meta["total_sites"]       = r11["Site"].nunique()       if "Site"      in r11.columns else 0
        meta["total_meters"]      = r11["Meter Code"].nunique() if "Meter Code" in r11.columns else 0
        meta["total_commodities"] = r11["Commodity"].nunique()  if "Commodity"  in r11.columns else 0
    else:
        meta = {"total_bills": 0, "total_sites": 0, "total_meters": 0, "total_commodities": 0}

    return {
        "check_results": check_results,
        "issues_df":     issues_df,
        "risks_df":      risks_df,
        "meta":          meta,
    }


# ══════════════════════════════════════════════════════════════════════════════
# HELPER — build result dict
# ══════════════════════════════════════════════════════════════════════════════

def _result(name, description, emissions_impact, flagged_df,
            issues=None, risks=None, is_risk=False):
    count  = len(flagged_df) if flagged_df is not None else 0
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
            site=r.get("Site", ""),
            account=r.get("Account", r.get("Account Code", "")),
            meter=r.get("Meter Code", ""),
            bill_id=r.get("Bill ID", ""),
            category=category,
            severity=severity,
            description=r[desc_col] if desc_col and desc_col in r else default_desc,
            commodity=r.get("Commodity", ""),
        ))
    return rows


def _to_risks(df, category, desc_col=None, default_desc=""):
    rows = []
    for _, r in df.iterrows():
        rows.append(format_issue_row(
            site=r.get("Site", ""),
            account=r.get("Account", r.get("Account Code", "")),
            meter=r.get("Meter Code", ""),
            bill_id=r.get("Bill ID", ""),
            category=category,
            severity="Risk",
            description=r[desc_col] if desc_col and desc_col in r else default_desc,
            commodity=r.get("Commodity", ""),
        ))
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# R-11 CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_missing_native_use(r11):
    name        = "Missing Native Use"
    description = "Bills where Native Use is null/blank. Cannot contribute to emissions calculations."
    emissions_impact = "CRITICAL — null use means zero contribution to Scope 1/2 totals."

    flagged = r11[r11["Native Use"].isna()].copy()
    flagged["_desc"] = "Native Use is null"
    issues  = _to_issues(flagged, name, "Critical", "_desc")
    return _result(name, description, emissions_impact, flagged, issues=issues)


def check_missing_common_use(r11):
    name        = "Missing Common Use (kBTU conversion)"
    description = ("Native Use is present but Common Use (kBTU) is blank. "
                   "Conversion factor may not be configured in EnergyCAP.")
    emissions_impact = "HIGH — emissions tools using Common Use as input will silently drop these bills."

    flagged = r11[
        r11["Native Use"].notna() & (r11["Native Use"] != 0) &
        (r11["Common Use"].isna() | (r11["Common Use"] == 0))
    ].copy() if "Common Use" in r11.columns else pd.DataFrame()
    flagged["_desc"] = "Native Use present but Common Use (kBTU) is blank or zero"
    risks = _to_risks(flagged, name, "_desc")
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_negative_use(r11):
    name        = "Negative Use Values"
    description = "Bills with negative Native Use. Usually indicates a correction bill or data error."
    emissions_impact = "HIGH — negative values will subtract from totals; confirm if intentional credit."

    flagged = r11[r11["Native Use"].notna() & (r11["Native Use"] < 0)].copy()
    flagged["_desc"] = flagged["Native Use"].apply(lambda v: f"Native Use = {v:.2f}")
    issues  = _to_issues(flagged, name, "Critical", "_desc")
    return _result(name, description, emissions_impact, flagged, issues=issues)


def check_duplicate_bills(r11):
    name        = "Duplicate Bill IDs"
    description = "The same Bill ID appears more than once. Indicates a duplicate record."
    emissions_impact = "CRITICAL — duplicates will double-count consumption and emissions."

    if "Bill ID" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    dupes = r11[r11["Bill ID"].notna() & r11.duplicated(subset=["Bill ID"], keep=False)].copy()
    dupes["_desc"] = dupes["Bill ID"].apply(lambda v: f"Bill ID {v} appears multiple times")
    issues = _to_issues(dupes, name, "Critical", "_desc")
    return _result(name, description, emissions_impact, dupes, issues=issues)


def check_billing_period_gaps(r11):
    name        = "Missing Billing Periods (Gaps)"
    description = ("For each meter, checks whether any months are missing in the billing history. "
                   "Gaps mean consumption for that period is unaccounted for.")
    emissions_impact = "HIGH — missing months will understate annual emissions totals."

    if "Meter Code" not in r11.columns or "Billing Period" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    gap_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        bps = grp["Billing Period"].dropna().unique()
        try:
            dates = sorted([billing_period_to_date(bp) for bp in bps if pd.notna(billing_period_to_date(bp))])
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
                    "Site": site, "Account": acct, "Meter Code": meter,
                    "Commodity": com, "Bill ID": "",
                    "Missing Period": str(exp)[:7],
                    "_desc": f"No bill found for {str(exp)[:7]}"
                })

    flagged = pd.DataFrame(gap_rows)
    issues  = _to_issues(flagged, name, "Critical", "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, issues=issues)


def check_overlapping_bills(r11):
    name        = "Overlapping Billing Periods"
    description = ("Two bills for the same meter have overlapping Start/End date ranges. "
                   "This can cause double-counting of consumption.")
    emissions_impact = "HIGH — overlapping bills will inflate consumption and emissions."

    if not all(c in r11.columns for c in ["Meter Code", "Start Date", "End Date"]):
        return _result(name, description, emissions_impact, pd.DataFrame())

    overlap_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.dropna(subset=["Start Date", "End Date"]).sort_values("Start Date")
        rows = grp.reset_index(drop=True)
        for i in range(len(rows) - 1):
            end_i   = rows.loc[i,   "End Date"]
            start_j = rows.loc[i+1, "Start Date"]
            if pd.notna(end_i) and pd.notna(start_j) and start_j < end_i:
                overlap_rows.append({
                    "Site":      rows.loc[i, "Site"] if "Site" in rows.columns else "",
                    "Account":   rows.loc[i, "Account"] if "Account" in rows.columns else "",
                    "Meter Code": meter,
                    "Commodity": rows.loc[i, "Commodity"] if "Commodity" in rows.columns else "",
                    "Bill ID":   rows.loc[i, "Bill ID"],
                    "_desc":     f"Bill {rows.loc[i,'Bill ID']} ends {end_i} overlaps with bill {rows.loc[i+1,'Bill ID']} starting {start_j}"
                })

    flagged = pd.DataFrame(overlap_rows)
    issues  = _to_issues(flagged, name, "Critical", "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, issues=issues)


def check_days_anomaly(r11):
    name        = "Unusual Billing Period Length"
    description = ("Bills with fewer than 5 days or more than 95 days. "
                   "May indicate catch-up bills, estimated reads, or data entry errors.")
    emissions_impact = "MEDIUM — affects calendarization; catch-up bills can skew monthly reporting."

    if "Days" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged = r11[(r11["Days"].notna()) & ((r11["Days"] < 5) | (r11["Days"] > 95))].copy()
    flagged["_desc"] = flagged["Days"].apply(lambda d: f"Billing period = {int(d)} days")
    risks = _to_risks(flagged, name, "_desc")
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_zero_use_nonzero_cost(r11):
    name        = "Zero Use with Non-Zero Cost"
    description = ("Bill shows zero (or null) Native Use but has a cost. "
                   "May indicate demand-only charges, standby fees, or data errors.")
    emissions_impact = "MEDIUM — confirms no consumption; cost may need separate treatment."

    flagged = r11[
        (r11["Native Use"].isna() | (r11["Native Use"] == 0)) &
        (r11["Total Cost"].notna()) & (r11["Total Cost"] > 0)
    ].copy() if "Total Cost" in r11.columns else pd.DataFrame()
    flagged["_desc"] = "Native Use = 0 but Total Cost > 0"
    risks = _to_risks(flagged, name, "_desc")
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_zero_cost_nonzero_use(r11):
    name        = "Non-Zero Use with Zero Cost"
    description = ("Bill has use but $0 cost. Common for manually entered alternative bills "
                   "(e.g. diesel/propane tracked internally), but should be confirmed.")
    emissions_impact = "LOW — use is captured; verify this is intentional (e.g. cost tracked elsewhere)."

    flagged = r11[
        (r11["Native Use"].notna()) & (r11["Native Use"] > 0) &
        ((r11["Total Cost"].isna()) | (r11["Total Cost"] == 0))
    ].copy() if "Total Cost" in r11.columns else pd.DataFrame()
    flagged["_desc"] = "Native Use > 0 but Total Cost = 0"
    risks = _to_risks(flagged, name, "_desc")
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_consecutive_zero_use(r11, threshold=2):
    name        = f"Consecutive Zero-Use Months (≥{threshold})"
    description = (f"Meters with {threshold} or more consecutive months of zero use. "
                   "Could be seasonal shutdowns, closed facilities, or missing bills.")
    emissions_impact = "MEDIUM — may indicate data gaps that understate actual emissions."

    if "Meter Code" not in r11.columns or "Billing Period" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.sort_values("Billing Period")
        consecutive = 0
        streak_start = None
        for _, row in grp.iterrows():
            use = row.get("Native Use", 0)
            if pd.isna(use) or use == 0:
                if consecutive == 0:
                    streak_start = row.get("Billing Period", "")
                consecutive += 1
            else:
                if consecutive >= threshold:
                    flagged_rows.append({
                        "Site":      row.get("Site", ""),
                        "Account":   row.get("Account", ""),
                        "Meter Code": meter,
                        "Commodity": row.get("Commodity", ""),
                        "Bill ID":   "",
                        "_desc":     f"{consecutive} consecutive zero-use months starting {streak_start}"
                    })
                consecutive = 0
                streak_start = None
        # Check if streak ends at last row
        if consecutive >= threshold:
            flagged_rows.append({
                "Site":      grp["Site"].iloc[-1] if "Site" in grp.columns else "",
                "Account":   grp["Account"].iloc[-1] if "Account" in grp.columns else "",
                "Meter Code": meter,
                "Commodity": grp["Commodity"].iloc[-1] if "Commodity" in grp.columns else "",
                "Bill ID":   "",
                "_desc":     f"{consecutive} consecutive zero-use months starting {streak_start}"
            })

    flagged = pd.DataFrame(flagged_rows)
    risks   = _to_risks(flagged, name, "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_use_outliers(r11, z_thresh=2.5):
    name        = f"Use Outliers (Z-score > {z_thresh})"
    description = ("Bills where Native Use is statistically unusual relative to the meter's "
                   "historical pattern. Could be a real spike or a data entry error.")
    emissions_impact = "MEDIUM — outliers can significantly skew annual totals."

    if "Meter Code" not in r11.columns or "Native Use" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

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
                lambda r: f"Use={r['Native Use']:.1f}, Z-score={r['Z-score']:.2f}", axis=1)
            flagged_list.append(outliers)

    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    risks   = _to_risks(flagged, name, "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_cost_outliers(r11, z_thresh=2.5):
    name        = f"Cost Outliers (Z-score > {z_thresh})"
    description = "Bills where Total Cost is statistically unusual relative to the meter's history."
    emissions_impact = "LOW — cost outliers don't affect emissions quantities but may indicate billing errors."

    if "Meter Code" not in r11.columns or "Total Cost" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

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
                lambda r: f"Cost={r['Total Cost']:.2f}, Z-score={r['Z-score']:.2f}", axis=1)
            flagged_list.append(outliers)

    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    risks   = _to_risks(flagged, name, "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_mom_change(r11, pct_thresh=50):
    name        = f"Month-over-Month Use Change > {pct_thresh}%"
    description = (f"Bills where use changes by more than {pct_thresh}% vs the prior month "
                   "for the same meter. Flags sudden spikes or drops.")
    emissions_impact = "MEDIUM — large swings may indicate missing bills or data errors."

    if "Meter Code" not in r11.columns or "Native Use" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged_list = []
    for meter, grp in r11.groupby("Meter Code"):
        grp = grp.sort_values("Billing Period").copy()
        grp["prior_use"] = grp["Native Use"].shift(1)
        grp["pct_change"] = np.where(
            (grp["prior_use"].notna()) & (grp["prior_use"] != 0),
            ((grp["Native Use"] - grp["prior_use"]) / grp["prior_use"].abs()) * 100,
            np.nan
        )
        spikes = grp[grp["pct_change"].abs() > pct_thresh].copy()
        if not spikes.empty:
            spikes["_desc"] = spikes.apply(
                lambda r: f"Use changed {r['pct_change']:.1f}% vs prior month "
                          f"({r['prior_use']:.1f} → {r['Native Use']:.1f})", axis=1)
            flagged_list.append(spikes)

    flagged = pd.concat(flagged_list) if flagged_list else pd.DataFrame()
    risks   = _to_risks(flagged, name, "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_uom_consistency(r11):
    name        = "UOM Inconsistency Across Billing Periods"
    description = ("A meter's rate schedule or commodity changes between billing periods, "
                   "which may indicate a UOM change. Native Use column has no UOM label — "
                   "changes in rate schedule are a proxy for potential UOM shifts.")
    emissions_impact = "HIGH — applying the wrong emission factor to the wrong UOM silently corrupts Scope 1/2."

    if "Meter Code" not in r11.columns or "Rate Schedule" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged_rows = []
    for meter, grp in r11.groupby("Meter Code"):
        unique_rates = grp["Rate Schedule"].dropna().unique()
        if len(unique_rates) > 1:
            site  = grp["Site"].iloc[0] if "Site" in grp.columns else ""
            acct  = grp["Account"].iloc[0] if "Account" in grp.columns else ""
            com   = grp["Commodity"].iloc[0] if "Commodity" in grp.columns else ""
            flagged_rows.append({
                "Site":       site,
                "Account":    acct,
                "Meter Code": meter,
                "Commodity":  com,
                "Bill ID":    "",
                "Rate Schedules Found": " | ".join(str(r) for r in unique_rates),
                "_desc":      f"Rate schedule changes: {' | '.join(str(r) for r in unique_rates)}"
            })

    flagged = pd.DataFrame(flagged_rows)
    risks   = _to_risks(flagged, name, "_desc") if not flagged.empty else []
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_rate_schedule_change(r11):
    # Already covered in uom_consistency; keep as alias with different framing
    name        = "Rate Schedule Change Mid-Period"
    description = "A meter's rate schedule changes between consecutive bills."
    emissions_impact = "LOW-MEDIUM — may affect cost interpretation but usually not use quantities."
    return _result(name, description, emissions_impact, pd.DataFrame())


# ══════════════════════════════════════════════════════════════════════════════
# R-03 CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_inactive_meters(r03, r11):
    name        = "Inactive Meters with Bills in Reporting Period"
    description = ("Meters marked Inactive in R-03 but have bills in R-11. "
                   "Emissions tools may exclude inactive meters by default.")
    emissions_impact = "HIGH — real consumption may be excluded from emissions calculations."

    if r11 is None or "Meter Status" not in r03.columns or "Meter Code" not in r03.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    inactive_meters = r03[r03["Meter Status"].str.lower().str.strip() == "inactive"]["Meter Code"].unique()
    if len(inactive_meters) == 0:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged = r11[r11["Meter Code"].isin(inactive_meters)].copy()
    flagged["_desc"] = "Meter is Inactive in R-03 but has bills in R-11"
    risks = _to_risks(flagged, name, "_desc")
    return _result(name, description, emissions_impact, flagged, risks=risks, is_risk=True)


def check_missing_serial(r03):
    name        = "Missing Meter Serial Numbers"
    description = "Meters with no serial number or 'NO METER NUMBER'. May indicate virtual/manual meters."
    emissions_impact = "LOW — informational; ensure these meters are intentionally tracked."

    if "Serial Number" not in r03.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    no_serial = r03[
        r03["Serial Number"].isna() |
        r03["Serial Number"].astype(str).str.upper().str.strip().isin(["NO METER NUMBER", "NOT METERED", ""])
    ].copy()
    no_serial["_desc"] = "No serial number assigned"
    risks = _to_risks(no_serial.rename(columns={"Meter Code": "Meter Code"}), name, "_desc")
    # Build risk rows manually since column names differ
    risk_rows = []
    for _, r in no_serial.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center", ""),
            account=r.get("Account Number", ""),
            meter=r.get("Meter Code", ""),
            bill_id="",
            category=name,
            severity="Risk",
            description="No serial number assigned",
            commodity="",
        ))
    return _result(name, description, emissions_impact, no_serial, risks=risk_rows, is_risk=True)


def check_excluded_from_audits(r03):
    name        = "Accounts Excluded from Audits"
    description = "Accounts marked 'Yes' for Excluded From Audits. EnergyCAP's internal outlier checks are bypassed."
    emissions_impact = "MEDIUM — data quality issues on these accounts won't be caught by EnergyCAP."

    col = "Excluded From Audits (Y/N)"
    if col not in r03.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged = r03[r03[col].astype(str).str.upper().str.strip() == "YES"].copy()
    risk_rows = []
    for _, r in flagged.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center", ""),
            account=r.get("Account Number", ""),
            meter=r.get("Meter Code", ""),
            bill_id="",
            category=name,
            severity="Risk",
            description="Account is excluded from EnergyCAP audit checks",
            commodity="",
        ))
    return _result(name, description, emissions_impact, flagged, risks=risk_rows, is_risk=True)


def check_deregulated_market(r03, r11):
    name        = "Deregulated Market Meters"
    description = ("Meters flagged as operating in a deregulated market (GEM Deregulated Mkt = 1). "
                   "Distribution and supply may come from different vendors — verify both are captured.")
    emissions_impact = "HIGH — missing supply bills means Scope 2 market-based calculations are incomplete."

    col = "GEM Deregulated Mkt"
    if col not in r03.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    dereg = r03[r03[col].notna() & (r03[col] != 0) & (r03[col] != "")].copy()
    risk_rows = []
    for _, r in dereg.iterrows():
        risk_rows.append(format_issue_row(
            site=r.get("Cost Center", ""),
            account=r.get("Account Number", ""),
            meter=r.get("Meter Code", ""),
            bill_id="",
            category=name,
            severity="Risk",
            description="Deregulated market meter — verify both distribution and supply bills are present",
            commodity="",
        ))
    return _result(name, description, emissions_impact, dereg, risks=risk_rows, is_risk=True)


def check_missing_acct_meter_dates(r03):
    name        = "Missing Account-Meter Begin Dates"
    description = "Meters with no Acct-Meter Begin Date set. May cause issues with date-range filtering."
    emissions_impact = "LOW — may affect historical period filtering in reporting."

    col = "Acct-Meter Begin Date"
    if col not in r03.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    flagged = r03[r03[col].isna()].copy()
    issue_rows = []
    for _, r in flagged.iterrows():
        issue_rows.append(format_issue_row(
            site=r.get("Cost Center", ""),
            account=r.get("Account Number", ""),
            meter=r.get("Meter Code", ""),
            bill_id="",
            category=name,
            severity="Warning",
            description="Acct-Meter Begin Date is not set",
            commodity="",
        ))
    return _result(name, description, emissions_impact, flagged, issues=issue_rows)


def check_meter_no_bills(r03, r11):
    name        = "Active Meters with No Bills"
    description = "Meters marked Active in R-03 but have no corresponding bills in R-11."
    emissions_impact = "HIGH — active meters with no bills likely represent missing data."

    if r11 is None or "Meter Code" not in r03.columns or "Meter Code" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    active = r03[r03["Meter Status"].str.lower().str.strip() == "active"]["Meter Code"].unique() \
             if "Meter Status" in r03.columns else r03["Meter Code"].unique()
    billed = r11["Meter Code"].unique()
    no_bills = set(active) - set(billed)

    flagged = r03[r03["Meter Code"].isin(no_bills)].copy()
    issue_rows = []
    for _, r in flagged.iterrows():
        issue_rows.append(format_issue_row(
            site=r.get("Cost Center", ""),
            account=r.get("Account Number", ""),
            meter=r.get("Meter Code", ""),
            bill_id="",
            category=name,
            severity="Critical",
            description="Active meter has no bills in R-11 export",
            commodity="",
        ))
    return _result(name, description, emissions_impact, flagged, issues=issue_rows)


# ══════════════════════════════════════════════════════════════════════════════
# CROSS-REPORT CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def check_orphan_bills(r03, r11):
    name        = "Bills for Unknown Meters (Orphan Bills)"
    description = ("Bills in R-11 whose Meter Code does not appear in R-03. "
                   "May indicate meters not yet set up, or a scope mismatch between exports.")
    emissions_impact = "HIGH — orphan bills may be excluded from site rollups in your emissions tool."

    if "Meter Code" not in r03.columns or "Meter Code" not in r11.columns:
        return _result(name, description, emissions_impact, pd.DataFrame())

    known_meters = set(r03["Meter Code"].dropna().unique())
    flagged      = r11[~r11["Meter Code"].isin(known_meters)].copy()
    flagged["_desc"] = "Meter Code not found in R-03 setup report"
    issues = _to_issues(flagged, name, "Critical", "_desc")
    return _result(name, description, emissions_impact, flagged, issues=issues)
