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

from qa_engine import run_all_checks
from utils import (load_report, REPORT_LABELS, detect_period,
                   compute_overlap, filter_r11_to_year, filter_gem_to_year,
                   NATIVE_TO_MWH)

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
