"""
EnergyCAP Pre-Export QA Tool — v2.0
Streamlit app for QA-ing EnergyCAP data and reconciling with GEM
before emissions calculation export.
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import date, datetime
import warnings
warnings.filterwarnings("ignore")

from qa_engine import run_all_checks
from utils import (load_report, REPORT_LABELS, detect_period,
                   compute_overlap, filter_r11_to_period, filter_gem_to_period,
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
.badge-critical{background:#fde8e8;color:#c53030;padding:2px 8px;
  border-radius:12px;font-size:.8rem;font-weight:600;}
.badge-risk{background:#fef3cd;color:#92400e;padding:2px 8px;
  border-radius:12px;font-size:.8rem;font-weight:600;}
.badge-ok{background:#d1fae5;color:#065f46;padding:2px 8px;
  border-radius:12px;font-size:.8rem;font-weight:600;}
.period-box{background:#f0f4f8;border-radius:8px;padding:12px;
  border-left:4px solid #2196F3;margin:6px 0;}
.overlap-box{background:#e8f5e9;border-radius:8px;padding:12px;
  border-left:4px solid #4caf50;margin:6px 0;}
.warn-box{background:#fff8e1;border-radius:8px;padding:12px;
  border-left:4px solid #ff9800;margin:6px 0;}
div[data-testid="stMetricValue"]{font-size:1.8rem!important;}
</style>
""", unsafe_allow_html=True)

# ── Session state ──────────────────────────────────────────────────────────────
for k, v in [("reports",{}), ("periods",{}), ("overlap",(None,None)),
             ("qa_results",{}), ("reconciled",False)]:
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

    st.markdown("### 📋 Supported Reports")
    st.markdown("""
| Report | Description |
|--------|-------------|
| **R-03** | Setup / Config |
| **R-11** | Bill Detail + UOM |
| **R-13** | Bill Outliers |
| **R-19** | Monthly Use & Cost |
| **R-21** | Monthly Comparison |
| **R-26** | Use & Cost Summary |
| **GEM**  | Emissions Data Export |
""")
    st.markdown("---")

    st.markdown("### ⚙️ QA Settings")
    outlier_z     = st.slider("Outlier Z-score threshold", 1.5, 4.0, 2.5, 0.1)
    pct_change    = st.slider("MoM % change alert", 20, 200, 50, 5)
    zero_months   = st.slider("Consecutive zero-use months", 1, 6, 2)

    st.markdown("---")
    st.markdown("### 📅 Account Start Date")
    acct_thresh = st.slider("Flag start dates before year",
                            1970, date.today().year, 2000, 1)
    st.caption("Accounts with start dates before this year (or no date) "
               "will be flagged as potential GEM estimate quality issues.")

    st.markdown("---")
    st.markdown("### 🔄 Unit Conversion (MWh)")
    with st.expander("Edit conversion factors"):
        st.caption("Native unit → MWh. Used to compare EnergyCAP vs GEM quantities.")
        custom_factors = {}
        defaults = {
            "ELECTRIC (kWh)":   ("ELECTRIC",  1/1000),
            "Nat Gas CCF":      ("NATURALGAS", 0.02931),
            "Nat Gas MCF":      ("MCF",        0.2931),
            "Diesel (gal)":     ("DIESEL",     0.03596),
            "Propane (gal)":    ("PROPANE",    0.02558),
            "Fuel Oil (gal)":   ("FUELOIL",    0.04026),
        }
        for label, (key, default) in defaults.items():
            val = st.number_input(label, value=float(default),
                                  format="%.6f", key=f"cf_{key}")
            custom_factors[key] = val
        # Merge with full table
        merged_factors = {**NATIVE_TO_MWH, **custom_factors}

    st.markdown("---")
    st.caption("EnergyCAP QA Tool v2.0 | Emissions Readiness")

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
    st.info("R-03 and R-11 are required. GEM export enables the EnergyCAP↔GEM "
            "reconciliation. The tool auto-detects each report type from filename and columns.")

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
                    period = detect_period(rtype, df)
                    st.session_state.periods[rtype] = period
                    label = REPORT_LABELS.get(rtype, rtype)
                    rows  = len(df)
                    p_str = ""
                    if period[0] and period[1]:
                        p_str = f" | Period: **{period[0].strftime('%b %Y')}** → **{period[1].strftime('%b %Y')}**"
                    st.success(f"✅ `{f.name}` → **{label}** ({rows:,} rows){p_str}")
                else:
                    st.warning(f"⚠️ `{f.name}` — could not detect report type. "
                               "Rename to include R-03, R-11, GEM, etc.")
            except Exception as e:
                st.error(f"❌ `{f.name}` — {e}")

        # ── Period overview ────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown('<div class="section-header">📅 Period Coverage & Overlap Detection</div>',
                    unsafe_allow_html=True)

        period_data = {k: v for k, v in st.session_state.periods.items()
                       if v[0] is not None}

        if period_data:
            pcols = st.columns(len(period_data))
            for i, (rtype, (beg, end)) in enumerate(period_data.items()):
                with pcols[i]:
                    st.markdown(
                        f'<div class="period-box"><b>{rtype}</b><br>'
                        f'{REPORT_LABELS.get(rtype,rtype)}<br>'
                        f'<b>{beg.strftime("%b %Y")}</b> → <b>{end.strftime("%b %Y")}</b></div>',
                        unsafe_allow_html=True)

            overlap_b, overlap_e = compute_overlap(period_data)
            st.session_state.overlap = (overlap_b, overlap_e)

            if overlap_b and overlap_e:
                st.markdown(
                    f'<div class="overlap-box">✅ <b>Overlapping period detected: '
                    f'{overlap_b.strftime("%B %Y")} → {overlap_e.strftime("%B %Y")}</b><br>'
                    f'QA and reconciliation will be focused on this window.</div>',
                    unsafe_allow_html=True)
            else:
                st.markdown(
                    '<div class="warn-box">⚠️ <b>No overlapping period found.</b> '
                    'Check that your files cover the same time range.</div>',
                    unsafe_allow_html=True)
        else:
            st.info("Period information will appear here after files are uploaded.")

        # ── Report coverage ────────────────────────────────────────────────────
        st.markdown("---")
        st.markdown("**Report coverage:**")
        cols = st.columns(7)
        for i, r in enumerate(["R03","R11","R13","R19","R21","R26","GEM"]):
            with cols[i]:
                loaded  = r in st.session_state.reports
                req     = r in ("R03","R11")
                icon    = "🟢" if loaded else ("🔴" if req else "⚪")
                reqtxt  = " *(req)*" if req else ""
                st.markdown(f"{icon} **{r}**{reqtxt}")

    # ── Run ────────────────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown('<div class="section-header">Step 2 — Run QA & Reconciliation</div>',
                unsafe_allow_html=True)

    has_required = all(r in st.session_state.reports for r in ["R03","R11"])
    has_gem      = "GEM" in st.session_state.reports
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
        with st.spinner("Running QA checks and GEM reconciliation…"):
            try:
                rpts = st.session_state.reports.copy()
                # Filter to overlap period
                if "R11" in rpts and ob:
                    rpts["R11"] = filter_r11_to_period(rpts["R11"], ob, oe)
                if "GEM" in rpts and ob:
                    rpts["GEM"] = filter_gem_to_period(rpts["GEM"], ob, oe)

                results = run_all_checks(
                    rpts,
                    overlap_begin=ob, overlap_end=oe,
                    outlier_zscore=outlier_z,
                    pct_change_thresh=pct_change,
                    zero_use_months=zero_months,
                    acct_start_year_threshold=acct_thresh,
                    conversion_factors=merged_factors,
                )
                st.session_state.qa_results  = results
                st.session_state.reconciled  = True
                n_issues = len(results.get("issues_df", []))
                n_risks  = len(results.get("risks_df",  []))
                st.success(f"✅ Complete — **{n_issues}** issues, **{n_risks}** risks found. "
                           "Review the tabs above.")
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
        ob, oe    = st.session_state.overlap

        st.markdown('<div class="section-header">EnergyCAP Data Quality Summary</div>',
                    unsafe_allow_html=True)

        if ob and oe:
            st.info(f"📅 Analysis focused on overlapping period: "
                    f"**{ob.strftime('%B %Y')}** → **{oe.strftime('%B %Y')}**")

        # Top metrics
        c1,c2,c3,c4,c5,c6 = st.columns(6)
        c1.metric("Total Bills",   f"{meta.get('total_bills',0):,}")
        c2.metric("Sites",         f"{meta.get('total_sites',0):,}")
        c3.metric("Meters",        f"{meta.get('total_meters',0):,}")
        c4.metric("Commodities",   f"{meta.get('total_commodities',0):,}")
        c5.metric("🔴 Issues",     f"{len(issues_df):,}")
        c6.metric("🟡 Risks",      f"{len(risks_df):,}")

        st.markdown("---")

        # Issues by category
        ecap_checks = [c for c in res.get("check_results",[])
                       if "GEM" not in c["name"] and "gem" not in c["name"].lower()]
        gem_checks  = [c for c in res.get("check_results",[])
                       if "GEM" in c["name"] or "gem" in c["name"].lower()]

        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("#### 🔴 Issues by Category")
            if issues_df.empty:
                st.success("No issues found.")
            else:
                ecap_issues = issues_df[~issues_df["Category"].str.contains("GEM",na=False)]
                if ecap_issues.empty:
                    st.success("No EnergyCAP-specific issues.")
                else:
                    cat = ecap_issues.groupby("Category").size().reset_index(name="Count")
                    for _, r in cat.sort_values("Count",ascending=False).iterrows():
                        st.markdown(f'🔴 **{r["Category"]}** — {r["Count"]} records')

        with col_r:
            st.markdown("#### 🟡 Risks by Category")
            if risks_df.empty:
                st.success("No risks found.")
            else:
                ecap_risks = risks_df[~risks_df["Category"].str.contains("GEM",na=False)]
                if ecap_risks.empty:
                    st.success("No EnergyCAP-specific risks.")
                else:
                    cat = ecap_risks.groupby("Category").size().reset_index(name="Count")
                    for _, r in cat.sort_values("Count",ascending=False).iterrows():
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
        res      = st.session_state.qa_results
        detail   = res.get("gem_detail", pd.DataFrame())
        meta     = res.get("meta", {})
        ob, oe   = st.session_state.overlap

        st.markdown('<div class="section-header">EnergyCAP ↔ GEM Reconciliation Summary</div>',
                    unsafe_allow_html=True)
        if ob and oe:
            st.info(f"📅 Reconciliation period: **{ob.strftime('%B %Y')}** → **{oe.strftime('%B %Y')}**")

        if detail.empty:
            st.warning("No reconciliation data available — check that Meter Codes overlap between R-11 and GEM.")
        else:
            # Top metrics
            matched   = detail[detail.get("Match_Tier","") != "Unmatched"] \
                        if "Match_Tier" in detail.columns else detail
            unmatched = detail[detail.get("Match_Tier","") == "Unmatched"] \
                        if "Match_Tier" in detail.columns else pd.DataFrame()

            total_gem  = detail["GEM_MWh"].sum()  if "GEM_MWh"  in detail.columns else 0
            total_ecap = detail["ECAP_MWh"].sum() if "ECAP_MWh" in detail.columns else 0
            total_delta = total_gem - total_ecap
            pct_delta   = (total_delta / total_ecap * 100) if total_ecap != 0 else 0

            c1,c2,c3,c4,c5 = st.columns(5)
            c1.metric("GEM Total (MWh)",   f"{total_gem:,.1f}")
            c2.metric("EnergyCAP Total (MWh)", f"{total_ecap:,.1f}")
            c3.metric("Delta (MWh)",       f"{total_delta:+,.1f}",
                      delta=f"{pct_delta:+.1f}%")
            c4.metric("GEM Rows",          f"{meta.get('gem_rows',0):,}")
            c5.metric("Unmatched SANs",    f"{unmatched['GEM_SAN'].nunique() if not unmatched.empty and 'GEM_SAN' in unmatched.columns else 0:,}")

            st.markdown("---")

            # Estimate quality breakdown
            if "Estimate_Quality" in detail.columns:
                st.markdown("#### 🧠 GEM Estimate Quality Classification")
                eq_desc = {
                    "Normal":                       ("✅","Actual bill data — GEM matches EnergyCAP","#d1fae5"),
                    "Defensible Estimate":          ("🟦","Steady-state commodity, GEM fills missing bill gap","#dbeafe"),
                    "Structurally Unreliable Estimate":("🟡","Non-monthly meter — GEM equal-splits the bill","#fef3cd"),
                    "Suspect Estimate":             ("🟠","Seasonal commodity — zero may be genuine","#ffedd5"),
                    "Confirmed Bad Estimate":       ("🔴","Before account start date — fabricated data","#fde8e8"),
                }
                eq_counts = detail.groupby("Estimate_Quality").size().reset_index(name="Count")
                for _, r in eq_counts.iterrows():
                    eq = r["Estimate_Quality"]
                    icon, desc, bg = eq_desc.get(eq, ("ℹ️",eq,"#f9fafb"))
                    st.markdown(
                        f'<div style="background:{bg};border-radius:6px;padding:8px 12px;margin:4px 0;">'
                        f'{icon} <b>{eq}</b> — {r["Count"]} meter-months<br>'
                        f'<span style="font-size:.85rem;color:#555;">{desc}</span></div>',
                        unsafe_allow_html=True)

            st.markdown("---")

            # Match tier breakdown
            if "Match_Tier" in detail.columns:
                st.markdown("#### 🔗 SAN → Meter Code Match Coverage")
                tier_counts = detail.drop_duplicates(["GEM_SAN","Match_Tier"]).groupby("Match_Tier").size()
                tc1, tc2, tc3, tc4 = st.columns(4)
                cols_t = [tc1,tc2,tc3,tc4]
                tier_labels = {
                    "Tier1-Direct":  ("🟢","Direct SAN = Meter Code"),
                    "Tier2-Serial":  ("🟡","Matched via Serial Number"),
                    "Tier2-Account": ("🟡","Matched via Account Code"),
                    "Unmatched":     ("🔴","No EnergyCAP match found"),
                }
                for i, (tier, (icon,label)) in enumerate(tier_labels.items()):
                    cnt = tier_counts.get(tier, 0)
                    if i < len(cols_t):
                        with cols_t[i]:
                            st.metric(f"{icon} {label}", cnt)

            st.markdown("---")

            # GEM checks
            gem_checks = [c for c in res.get("check_results",[])
                          if "GEM" in c["name"] or "gem" in c["name"].lower()]
            st.markdown("#### 🔬 Reconciliation Check Results")
            for chk in gem_checks:
                icon = {"ok":"✅","warning":"⚠️","error":"❌"}.get(chk["status"],"ℹ️")
                with st.expander(f"{icon} {chk['name']} — {chk['count']} flagged",
                                 expanded=(chk["status"] in ("error","warning") and chk["count"]>0)):
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
        st.markdown("Risks are records that warrant closer review before use in emissions "
                    "calculations. They may not be wrong, but each needs human judgment.")

        risk_info = {
            "UOM / Rate Schedule Inconsistency":
                ("📐","Rate schedule changes between billing periods — possible UOM shift.",
                 "HIGH — wrong emission factor applied to wrong UOM silently corrupts Scope 1/2."),
            "Unusual Billing Period Length":
                ("📅","Bills with <5 or >95 days.",
                 "MEDIUM — affects calendarization; catch-up bills can skew monthly reporting."),
            "Zero Use with Non-Zero Cost":
                ("💰","Zero use but non-zero cost — demand/standby charges.",
                 "MEDIUM — confirms no consumption; cost may need separate treatment."),
            "Non-Zero Use with Zero Cost":
                ("📋","Use present but $0 cost — common for manual alternative bills.",
                 "LOW — verify this is intentional."),
            "Inactive Meters with Bills":
                ("🔌","Inactive meter still has bills.",
                 "HIGH — emissions tools may exclude inactive meters."),
            "Missing Common Use (kBTU conversion)":
                ("🔄","kBTU conversion is blank.",
                 "HIGH — emissions tools using Common Use will drop these bills."),
            "Use Outliers (Z-score > 2.5)":
                ("📈","Statistically unusual use.",
                 "MEDIUM — outliers can skew annual totals."),
            "Cost Outliers (Z-score > 2.5)":
                ("💵","Statistically unusual cost.",
                 "LOW — may indicate billing errors."),
            "Consecutive Zero-Use Months":
                ("⬛","Multiple consecutive zero-use months.",
                 "MEDIUM — may indicate data gaps."),
            "Month-over-Month Use Change > 50%":
                ("📊","Large month-over-month use change.",
                 "MEDIUM — may indicate missing bills."),
            "Deregulated Market Meters":
                ("⚡","Deregulated market — separate distribution and supply vendors.",
                 "HIGH — missing supply bills means incomplete Scope 2 market-based calc."),
            "Accounts Excluded from Audits":
                ("🚫","EnergyCAP audit checks bypassed for this account.",
                 "MEDIUM — data quality issues won't be caught internally."),
            "Missing Meter Serial Numbers":
                ("🔢","No serial number on meter.",
                 "LOW — informational."),
            "Suspicious Account Start Date":
                ("📅","Start date is null or unusually far back.",
                 "HIGH — GEM may generate estimates before meter existed."),
            "Non-Monthly Billing Frequency":
                ("🗓","Quarterly/bi-monthly/annual meter — GEM equal-splits the bill.",
                 "MEDIUM — monthly GEM values are approximations."),
            "GEM Estimates on Non-Monthly Meters":
                ("🗓","GEM monthly distribution approximation.",
                 "MEDIUM — not metered monthly data."),
            "GEM Estimates on Seasonal/Event-Driven Commodities":
                ("🌡","Seasonal commodity with GEM estimate where EnergyCAP shows zero.",
                 "MEDIUM — zero may be genuine, not a gap."),
            "GEM SANs with No EnergyCAP Match":
                ("❓","GEM entry cannot be matched to any EnergyCAP meter.",
                 "HIGH — may be manual estimates or legacy meters."),
            "GEM Over-reports vs EnergyCAP (>20%)":
                ("📈","GEM quantity >20% above EnergyCAP.",
                 "HIGH — may indicate double-counting."),
            "GEM Under-reports vs EnergyCAP (>20% gap)":
                ("📉","GEM quantity >20% below EnergyCAP.",
                 "HIGH — losses in transit; emissions understated."),
        }

        if risks_df.empty:
            st.success("✅ No risks identified.")
        else:
            present = risks_df["Category"].unique()
            for cat, (icon, desc, impact) in risk_info.items():
                if cat not in present:
                    continue
                cat_df = risks_df[risks_df["Category"] == cat]
                with st.expander(f"{icon} **{cat}** — {len(cat_df)} records", expanded=False):
                    st.markdown(f"**What it means:** {desc}")
                    st.markdown(f"**Emissions impact:** {impact}")
                    st.dataframe(cat_df.drop(columns=["Category"],errors="ignore"),
                                 use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — ISSUE REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_issues:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        issues_df = st.session_state.qa_results.get("issues_df", pd.DataFrame())
        st.markdown('<div class="section-header">🔴 Issue Register — Records Requiring Correction</div>',
                    unsafe_allow_html=True)
        st.markdown("These records have **confirmed data quality problems** that must be "
                    "corrected in EnergyCAP or GEM before the data is used for emissions.")

        if issues_df.empty:
            st.success("✅ No issues found!")
        else:
            fc1,fc2,fc3,fc4 = st.columns(4)
            cats   = ["All"] + sorted(issues_df["Category"].dropna().unique().tolist())
            sevs   = ["All"] + sorted(issues_df["Severity"].dropna().unique().tolist())
            sites  = ["All"] + sorted(issues_df["Site"].dropna().unique().tolist()) \
                     if "Site" in issues_df.columns else ["All"]
            coms   = ["All"] + sorted(issues_df["Commodity"].dropna().unique().tolist()) \
                     if "Commodity" in issues_df.columns else ["All"]

            with fc1: sel_cat  = st.selectbox("Category", cats,  key="iss_cat")
            with fc2: sel_sev  = st.selectbox("Severity", sevs,  key="iss_sev")
            with fc3: sel_site = st.selectbox("Site",     sites, key="iss_site")
            with fc4: sel_com  = st.selectbox("Commodity",coms,  key="iss_com")

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
                               f"issue_register_{date.today()}.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — RISK REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_risk_reg:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first.")
    else:
        risks_df = st.session_state.qa_results.get("risks_df", pd.DataFrame())
        st.markdown('<div class="section-header">🟡 Risk Register — Records Requiring Review</div>',
                    unsafe_allow_html=True)
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
            with rc2: sel_rsite = st.selectbox("Site",       rsites, key="rsk_site")
            with rc3: sel_rcom  = st.selectbox("Commodity",  rcoms,  key="rsk_com")
            with rc4: sel_rper  = st.selectbox("Period",     rpers,  key="rsk_per")

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
                               f"risk_register_{date.today()}.csv", "text/csv")

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
        st.markdown('<div class="section-header">🔍 GEM ↔ EnergyCAP Detail View</div>',
                    unsafe_allow_html=True)
        st.markdown("Row-level reconciliation between GEM and EnergyCAP, with estimate "
                    "quality classification and variance.")

        if detail.empty:
            st.info("No detail data available.")
        else:
            # Filters
            gd1,gd2,gd3,gd4 = st.columns(4)
            gsites = ["All"] + sorted(detail["Site"].dropna().unique().tolist()) \
                     if "Site" in detail.columns else ["All"]
            gress  = ["All"] + sorted(detail["Resource"].dropna().unique().tolist()) \
                     if "Resource" in detail.columns else ["All"]
            geqs   = ["All"] + sorted(detail["Estimate_Quality"].dropna().unique().tolist()) \
                     if "Estimate_Quality" in detail.columns else ["All"]
            gtiers = ["All"] + sorted(detail["Match_Tier"].dropna().unique().tolist()) \
                     if "Match_Tier" in detail.columns else ["All"]

            with gd1: sel_gs   = st.selectbox("Site",             gsites, key="gd_site")
            with gd2: sel_gr   = st.selectbox("Resource",         gress,  key="gd_res")
            with gd3: sel_geq  = st.selectbox("Estimate Quality", geqs,   key="gd_eq")
            with gd4: sel_gtier= st.selectbox("Match Tier",       gtiers, key="gd_tier")

            gfilt = detail.copy()
            if sel_gs    != "All" and "Site"             in gfilt.columns:
                gfilt = gfilt[gfilt["Site"]             == sel_gs]
            if sel_gr    != "All" and "Resource"         in gfilt.columns:
                gfilt = gfilt[gfilt["Resource"]         == sel_gr]
            if sel_geq   != "All" and "Estimate_Quality" in gfilt.columns:
                gfilt = gfilt[gfilt["Estimate_Quality"] == sel_geq]
            if sel_gtier != "All" and "Match_Tier"       in gfilt.columns:
                gfilt = gfilt[gfilt["Match_Tier"]       == sel_gtier]

            # Show key columns
            show_cols = [c for c in ["Site","Country","Resource","GEM_SAN","Meter Code",
                                      "Match_Tier","Year","Month","GEM_MWh","ECAP_MWh",
                                      "Delta_MWh","Delta_Pct","Estimate_Quality","Estimate_Class"]
                         if c in gfilt.columns]
            st.markdown(f"**Showing {len(gfilt):,} of {len(detail):,} rows**")
            st.dataframe(
                gfilt[show_cols].sort_values(["Site","Resource","Year","Month"],
                                             na_position="last"),
                use_container_width=True, hide_index=True,
                column_config={
                    "GEM_MWh":   st.column_config.NumberColumn("GEM (MWh)",  format="%.2f"),
                    "ECAP_MWh":  st.column_config.NumberColumn("ECAP (MWh)", format="%.2f"),
                    "Delta_MWh": st.column_config.NumberColumn("Δ MWh",      format="%+.2f"),
                    "Delta_Pct": st.column_config.NumberColumn("Δ %",        format="%+.1f%%"),
                }
            )
            st.download_button("⬇ Download GEM Detail (CSV)",
                               gfilt[show_cols].to_csv(index=False),
                               f"gem_detail_{date.today()}.csv", "text/csv")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 8 — DATA EXPLORER
# ══════════════════════════════════════════════════════════════════════════════
with tab_explorer:
    st.markdown('<div class="section-header">📂 Raw Data Explorer</div>',
                unsafe_allow_html=True)
    if not st.session_state.reports:
        st.info("No reports loaded yet.")
    else:
        sel = st.selectbox("Select report",
                           list(st.session_state.reports.keys()),
                           format_func=lambda x: f"{x} — {REPORT_LABELS.get(x,x)}")
        df_exp = st.session_state.reports[sel]
        c1,c2,c3 = st.columns(3)
        c1.metric("Rows",    f"{len(df_exp):,}")
        c2.metric("Columns", f"{len(df_exp.columns):,}")
        c3.metric("Report",  REPORT_LABELS.get(sel,sel))
        sel_cols = st.multiselect("Columns to show",
                                  df_exp.columns.tolist(),
                                  default=df_exp.columns.tolist()[:12])
        if sel_cols:
            st.dataframe(df_exp[sel_cols].head(500),
                         use_container_width=True, hide_index=True)
        st.download_button(f"⬇ Download {sel} as CSV",
                           df_exp.to_csv(index=False),
                           f"ecap_{sel}_{date.today()}.csv","text/csv")
