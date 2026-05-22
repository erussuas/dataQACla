"""
EnergyCAP Pre-Export QA Tool
Streamlit app for quality-assuring EnergyCAP data before emissions calculation export.

Reports supported:
  R-03  Setup Report (Accounts, Vendors, Cost Centers, Meters, Sites)
  R-11  Bill Transfer Format (bill-level detail with native use & UOM)
  R-13  Bill Analysis (outlier flags)
  R-19  Monthly Utility Use and Cost
  R-21  Monthly Comparison
  R-26  Use and Cost Summary
"""

import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, date
import io
import warnings
warnings.filterwarnings("ignore")

from qa_engine import run_all_checks
from utils import (
    load_report,
    REPORT_LABELS, excel_serial_to_date
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="EnergyCAP QA Tool",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .main-header {
        font-size: 2rem; font-weight: 700; color: #1a3a5c;
        border-bottom: 3px solid #2196F3; padding-bottom: 0.5rem;
        margin-bottom: 1.5rem;
    }
    .section-header {
        font-size: 1.2rem; font-weight: 600; color: #1a3a5c;
        margin-top: 1rem; margin-bottom: 0.5rem;
    }
    .metric-card {
        background: #f0f4f8; border-radius: 8px;
        padding: 1rem; text-align: center;
    }
    .issue-critical { background-color: #fde8e8; border-left: 4px solid #e53e3e; padding: 8px; border-radius: 4px; margin: 4px 0; }
    .issue-warning  { background-color: #fef3cd; border-left: 4px solid #d69e2e; padding: 8px; border-radius: 4px; margin: 4px 0; }
    .issue-info     { background-color: #e8f4fd; border-left: 4px solid #2196F3; padding: 8px; border-radius: 4px; margin: 4px 0; }
    .status-ok      { color: #38a169; font-weight: 600; }
    .status-warn    { color: #d69e2e; font-weight: 600; }
    .status-err     { color: #e53e3e; font-weight: 600; }
    div[data-testid="stMetricValue"] { font-size: 2rem !important; }
</style>
""", unsafe_allow_html=True)

# ── Session state init ─────────────────────────────────────────────────────────
for key in ["reports", "qa_results", "reconciled"]:
    if key not in st.session_state:
        st.session_state[key] = {} if key in ["reports", "qa_results"] else False

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://www.energycap.com/wp-content/uploads/2021/03/energycap-logo.png",
             width=180, use_container_width=False)
    st.markdown("---")
    st.markdown("### 📋 Report Guide")
    st.markdown("""
| Report | Description |
|--------|-------------|
| **R-03** | Setup / Config |
| **R-11** | Bill Detail + UOM |
| **R-13** | Bill Outliers |
| **R-19** | Monthly Use & Cost |
| **R-21** | Monthly Comparison |
| **R-26** | Use & Cost Summary |
""")
    st.markdown("---")
    st.markdown("### ⚙️ QA Settings")
    outlier_zscore    = st.slider("Outlier Z-score threshold", 1.5, 4.0, 2.5, 0.1,
                                  help="Bills with use/cost Z-score above this are flagged")
    pct_change_thresh = st.slider("Month-over-month % change alert", 20, 200, 50, 5,
                                  help="Flag if use changes by more than this % vs prior month")
    zero_use_months   = st.slider("Consecutive zero-use months to flag", 1, 6, 2,
                                  help="Flag meters with this many consecutive zero-use months")
    st.markdown("---")
    st.caption("EnergyCAP QA Tool v1.0 | Emissions Readiness")

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown('<div class="main-header">⚡ EnergyCAP Pre-Export QA Tool</div>',
            unsafe_allow_html=True)
st.markdown("Upload your EnergyCAP report exports, then run reconciliation to identify "
            "data issues and risks before feeding your emissions calculation tool.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB LAYOUT
# ══════════════════════════════════════════════════════════════════════════════
tab_upload, tab_summary, tab_risks, tab_issues, tab_risk_reg, tab_explorer = st.tabs([
    "📁 Upload & Run",
    "📊 QA Summary",
    "⚠️ Risk Summary",
    "🔴 Issue Register",
    "🟡 Risk Register",
    "🔍 Data Explorer",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — UPLOAD & RUN
# ══════════════════════════════════════════════════════════════════════════════
with tab_upload:
    st.markdown('<div class="section-header">Step 1 — Upload EnergyCAP Report Exports</div>',
                unsafe_allow_html=True)
    st.info("Upload one or more EnergyCAP Excel/CSV exports. The tool auto-detects each report type. "
            "R-03 and R-11 are required; R-13, R-19, R-21, R-26 add depth to the analysis.")

    uploaded_files = st.file_uploader(
        "Drop files here or click Browse",
        type=["xlsx", "xls", "csv"],
        accept_multiple_files=True,
        help="Supports R-03, R-11, R-13, R-19, R-21, R-26 exports"
    )

    if uploaded_files:
        st.markdown("**Uploaded files:**")
        load_errors = []
        for f in uploaded_files:
            try:
                df, rtype = load_report(f)
                if rtype:
                    st.session_state.reports[rtype] = df
                    st.success(f"✅ `{f.name}` → detected as **{REPORT_LABELS.get(rtype, rtype)}**  "
                               f"({len(df):,} rows)")
                else:
                    st.warning(f"⚠️ `{f.name}` — could not detect report type. "
                               "Rename to include R-03, R-11, etc. or check column headers.")
            except Exception as e:
                load_errors.append((f.name, str(e)))
                st.error(f"❌ `{f.name}` — load error: {e}")

        # Show loaded reports status
        st.markdown("---")
        st.markdown("**Report coverage:**")
        required = ["R03", "R11"]
        optional = ["R13", "R19", "R21", "R26"]
        cols = st.columns(6)
        for i, r in enumerate(required + optional):
            with cols[i]:
                loaded = r in st.session_state.reports
                color  = "🟢" if loaded else ("🔴" if r in required else "⚪")
                label  = REPORT_LABELS.get(r, r)
                req    = " *(required)*" if r in required else " *(optional)*"
                st.markdown(f"{color} **{r}**{req}  \n{label}")

    st.markdown("---")
    st.markdown('<div class="section-header">Step 2 — Run QA Reconciliation</div>',
                unsafe_allow_html=True)

    has_required = all(r in st.session_state.reports for r in ["R03", "R11"])
    if not has_required:
        st.warning("⚠️ Please upload at least **R-03** (Setup) and **R-11** (Bill Detail) to run QA.")

    run_col, clear_col = st.columns([2, 1])
    with run_col:
        run_btn = st.button(
            "▶ Run QA Reconciliation",
            type="primary",
            disabled=not has_required,
            use_container_width=True,
        )
    with clear_col:
        if st.button("🗑 Clear All", use_container_width=True):
            st.session_state.reports    = {}
            st.session_state.qa_results = {}
            st.session_state.reconciled = False
            st.rerun()

    if run_btn:
        with st.spinner("Running QA checks across all reports…"):
            try:
                results = run_all_checks(
                    st.session_state.reports,
                    outlier_zscore=outlier_zscore,
                    pct_change_thresh=pct_change_thresh,
                    zero_use_months=zero_use_months,
                )
                st.session_state.qa_results = results
                st.session_state.reconciled = True
                st.success("✅ QA reconciliation complete. Review the tabs above.")
                st.balloons()
            except Exception as e:
                st.error(f"QA engine error: {e}")
                raise e

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — QA SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with tab_summary:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first (Upload & Run tab).")
    else:
        res = st.session_state.qa_results
        issues_df = res.get("issues_df", pd.DataFrame())
        risks_df  = res.get("risks_df",  pd.DataFrame())
        meta      = res.get("meta", {})

        st.markdown('<div class="section-header">QA Summary Dashboard</div>',
                    unsafe_allow_html=True)

        # ── Top metrics ──
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Total Bills",        f"{meta.get('total_bills', 0):,}")
        c2.metric("Sites",              f"{meta.get('total_sites', 0):,}")
        c3.metric("Meters",             f"{meta.get('total_meters', 0):,}")
        c4.metric("Commodities",        f"{meta.get('total_commodities', 0):,}")
        c5.metric("🔴 Issues",          f"{len(issues_df):,}",
                  delta=None if issues_df.empty else "Needs correction")
        c6.metric("🟡 Risks",           f"{len(risks_df):,}",
                  delta=None if risks_df.empty else "Needs review")

        st.markdown("---")

        # ── Issues by category ──
        col_l, col_r = st.columns(2)
        with col_l:
            st.markdown("#### 🔴 Issues by Category")
            if issues_df.empty:
                st.success("No issues found.")
            else:
                cat_counts = issues_df.groupby("Category").size().reset_index(name="Count")
                cat_counts = cat_counts.sort_values("Count", ascending=False)
                for _, row in cat_counts.iterrows():
                    severity = issues_df[issues_df["Category"] == row["Category"]]["Severity"].iloc[0]
                    badge = "🔴" if severity == "Critical" else "🟠"
                    st.markdown(f'{badge} **{row["Category"]}** — {row["Count"]} records',
                                unsafe_allow_html=True)

        with col_r:
            st.markdown("#### 🟡 Risks by Category")
            if risks_df.empty:
                st.success("No risks found.")
            else:
                rcat_counts = risks_df.groupby("Category").size().reset_index(name="Count")
                rcat_counts = rcat_counts.sort_values("Count", ascending=False)
                for _, row in rcat_counts.iterrows():
                    st.markdown(f'🟡 **{row["Category"]}** — {row["Count"]} records')

        st.markdown("---")

        # ── Check-by-check results ──
        st.markdown("#### 🔬 Check Results")
        check_results = res.get("check_results", [])
        for chk in check_results:
            status  = chk.get("status", "ok")
            icon    = {"ok": "✅", "warning": "⚠️", "error": "❌"}.get(status, "ℹ️")
            count   = chk.get("count", 0)
            expander_label = f"{icon} {chk['name']}  —  {count} record(s) flagged"
            with st.expander(expander_label, expanded=(status == "error")):
                st.markdown(f"**Description:** {chk.get('description', '')}")
                st.markdown(f"**Impact on emissions:** {chk.get('emissions_impact', '')}")
                if count > 0 and "sample" in chk:
                    st.markdown("**Sample records:**")
                    st.dataframe(chk["sample"], use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — RISK SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
with tab_risks:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first (Upload & Run tab).")
    else:
        res      = st.session_state.qa_results
        risks_df = res.get("risks_df", pd.DataFrame())

        st.markdown('<div class="section-header">Risk Summary</div>', unsafe_allow_html=True)
        st.markdown(
            "Risks are records that are **not necessarily wrong** but warrant closer review "
            "before being used in emissions calculations. They may indicate data anomalies, "
            "configuration choices that could affect results, or edge cases that need "
            "human judgment."
        )

        risk_categories = {
            "UOM Inconsistency": {
                "icon": "📐",
                "description": "A meter's unit of measure changes between billing periods. "
                               "This can happen legitimately (utility switches from CCF to Therms) "
                               "but must be verified and conversion factors confirmed before "
                               "emissions factors are applied.",
                "emissions_impact": "HIGH — applying the wrong emission factor to the wrong UOM "
                                    "will silently corrupt your Scope 1/2 calculations."
            },
            "Billing Period Anomaly": {
                "icon": "📅",
                "description": "Bills with unusual period lengths (very short or very long) "
                               "relative to the meter's standard billing frequency. "
                               "Could indicate catch-up bills, estimated reads, or data entry errors.",
                "emissions_impact": "MEDIUM — period length affects calendarization; "
                                    "catch-up bills can double-count or create gaps in monthly reporting."
            },
            "Zero Use / Non-Zero Cost": {
                "icon": "💰",
                "description": "Bill shows $0 use but has a cost, or has use with $0 cost. "
                               "Common for demand charges, standby fees, or manual entry accounts, "
                               "but needs confirmation.",
                "emissions_impact": "MEDIUM — zero-use bills with costs may indicate "
                                    "fixed charges that should be excluded from emissions calculations."
            },
            "Inactive Meter with Recent Bills": {
                "icon": "🔌",
                "description": "Meter is marked Inactive in R-03 but has bills in R-11 "
                               "within the reporting period.",
                "emissions_impact": "HIGH — emissions tools may exclude inactive meters, "
                                    "causing real consumption to be missed."
            },
            "Missing Common Use (kBTU)": {
                "icon": "🔄",
                "description": "Native use is populated but Common Use (kBTU conversion) is blank. "
                               "The conversion factor may not be configured in EnergyCAP.",
                "emissions_impact": "HIGH — if your emissions tool uses Common Use as input, "
                                    "these bills will contribute zero to calculations."
            },
            "Statistical Outlier": {
                "icon": "📈",
                "description": "Use or cost is statistically unusual relative to the meter's "
                               "historical pattern (based on Z-score analysis). "
                               "Could be a real spike or a data error.",
                "emissions_impact": "MEDIUM — outliers can significantly skew annual totals "
                                    "and emissions intensity metrics."
            },
            "Consecutive Zero Use": {
                "icon": "⬛",
                "description": "A meter shows zero use for multiple consecutive months. "
                               "Could be seasonal, a closed facility, or missing bills.",
                "emissions_impact": "MEDIUM — may indicate gaps in data coverage "
                                    "that would understate actual emissions."
            },
            "Deregulated Market Flag": {
                "icon": "⚡",
                "description": "Meter is flagged as operating in a deregulated market. "
                               "Distribution and supply may come from different vendors "
                               "with separate bills — verify both are captured.",
                "emissions_impact": "HIGH — if supply bills are missing, market-based "
                                    "Scope 2 calculations will be incomplete."
            },
            "Rate Schedule Change": {
                "icon": "📋",
                "description": "The rate schedule changes between billing periods for the same meter. "
                               "May indicate a legitimate tariff change or a data entry error.",
                "emissions_impact": "LOW-MEDIUM — rate schedule changes can affect "
                                    "how costs are interpreted but rarely affect use quantities."
            },
            "Account Excluded from Audits": {
                "icon": "🚫",
                "description": "Account is marked 'Excluded from Audits' in R-03. "
                               "These accounts bypass EnergyCAP's own outlier detection.",
                "emissions_impact": "MEDIUM — data quality issues on these accounts "
                                    "will not be caught by EnergyCAP's internal checks."
            },
        }

        if risks_df.empty:
            st.success("✅ No risks identified.")
        else:
            present_cats = risks_df["Category"].unique() if not risks_df.empty else []
            for cat, info in risk_categories.items():
                if cat not in present_cats:
                    continue
                cat_df = risks_df[risks_df["Category"] == cat]
                with st.expander(
                    f"{info['icon']} **{cat}** — {len(cat_df)} record(s)",
                    expanded=True
                ):
                    st.markdown(f"**What it means:** {info['description']}")
                    st.markdown(f"**Emissions calculation impact:** {info['emissions_impact']}")
                    st.dataframe(
                        cat_df.drop(columns=["Category"], errors="ignore"),
                        use_container_width=True,
                        hide_index=True
                    )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — ISSUE REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_issues:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first (Upload & Run tab).")
    else:
        issues_df = st.session_state.qa_results.get("issues_df", pd.DataFrame())
        st.markdown('<div class="section-header">🔴 Issue Register — Records Requiring Correction</div>',
                    unsafe_allow_html=True)
        st.markdown(
            "These records have **confirmed data quality problems** that must be corrected "
            "in EnergyCAP before the data is exported for emissions calculations."
        )

        if issues_df.empty:
            st.success("✅ No issues found. Data looks clean!")
        else:
            # Filters
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                cats = ["All"] + sorted(issues_df["Category"].unique().tolist())
                sel_cat = st.selectbox("Filter by Category", cats, key="iss_cat")
            with fc2:
                sevs = ["All"] + sorted(issues_df["Severity"].unique().tolist())
                sel_sev = st.selectbox("Filter by Severity", sevs, key="iss_sev")
            with fc3:
                if "Site" in issues_df.columns:
                    sites = ["All"] + sorted(issues_df["Site"].dropna().unique().tolist())
                    sel_site = st.selectbox("Filter by Site", sites, key="iss_site")
                else:
                    sel_site = "All"

            filtered = issues_df.copy()
            if sel_cat  != "All": filtered = filtered[filtered["Category"] == sel_cat]
            if sel_sev  != "All": filtered = filtered[filtered["Severity"] == sel_sev]
            if sel_site != "All" and "Site" in filtered.columns:
                filtered = filtered[filtered["Site"] == sel_site]

            st.markdown(f"**Showing {len(filtered):,} of {len(issues_df):,} issues**")
            st.dataframe(filtered, use_container_width=True, hide_index=True,
                         column_config={
                             "Severity": st.column_config.TextColumn("Severity", width="small"),
                             "Bill ID":  st.column_config.NumberColumn("Bill ID", format="%d"),
                         })

            # Download
            csv = filtered.to_csv(index=False)
            st.download_button(
                "⬇ Download Issue Register (CSV)",
                data=csv,
                file_name=f"energycap_issue_register_{date.today()}.csv",
                mime="text/csv",
            )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — RISK REGISTER
# ══════════════════════════════════════════════════════════════════════════════
with tab_risk_reg:
    if not st.session_state.reconciled:
        st.info("Run QA reconciliation first (Upload & Run tab).")
    else:
        risks_df = st.session_state.qa_results.get("risks_df", pd.DataFrame())
        st.markdown('<div class="section-header">🟡 Risk Register — Records Requiring Review</div>',
                    unsafe_allow_html=True)
        st.markdown(
            "These records are **not necessarily wrong** but carry risk of affecting emissions "
            "calculation accuracy. Each should be reviewed and either confirmed as acceptable "
            "or escalated for correction."
        )

        if risks_df.empty:
            st.success("✅ No risks identified.")
        else:
            rc1, rc2, rc3 = st.columns(3)
            with rc1:
                rcats = ["All"] + sorted(risks_df["Category"].unique().tolist())
                sel_rcat = st.selectbox("Filter by Category", rcats, key="rsk_cat")
            with rc2:
                if "Site" in risks_df.columns:
                    rsites = ["All"] + sorted(risks_df["Site"].dropna().unique().tolist())
                    sel_rsite = st.selectbox("Filter by Site", rsites, key="rsk_site")
                else:
                    sel_rsite = "All"
            with rc3:
                if "Commodity" in risks_df.columns:
                    rcoms = ["All"] + sorted(risks_df["Commodity"].dropna().unique().tolist())
                    sel_rcom = st.selectbox("Filter by Commodity", rcoms, key="rsk_com")
                else:
                    sel_rcom = "All"

            rfiltered = risks_df.copy()
            if sel_rcat  != "All": rfiltered = rfiltered[rfiltered["Category"] == sel_rcat]
            if sel_rsite != "All" and "Site" in rfiltered.columns:
                rfiltered = rfiltered[rfiltered["Site"] == sel_rsite]
            if sel_rcom  != "All" and "Commodity" in rfiltered.columns:
                rfiltered = rfiltered[rfiltered["Commodity"] == sel_rcom]

            st.markdown(f"**Showing {len(rfiltered):,} of {len(risks_df):,} risks**")
            st.dataframe(rfiltered, use_container_width=True, hide_index=True)

            rcsv = rfiltered.to_csv(index=False)
            st.download_button(
                "⬇ Download Risk Register (CSV)",
                data=rcsv,
                file_name=f"energycap_risk_register_{date.today()}.csv",
                mime="text/csv",
            )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — DATA EXPLORER
# ══════════════════════════════════════════════════════════════════════════════
with tab_explorer:
    st.markdown('<div class="section-header">🔍 Raw Data Explorer</div>',
                unsafe_allow_html=True)
    st.markdown("Browse the raw data from each uploaded report.")

    if not st.session_state.reports:
        st.info("No reports loaded yet.")
    else:
        sel_report = st.selectbox(
            "Select report to explore",
            options=list(st.session_state.reports.keys()),
            format_func=lambda x: f"{x} — {REPORT_LABELS.get(x, x)}"
        )
        df_exp = st.session_state.reports[sel_report]

        col_info1, col_info2, col_info3 = st.columns(3)
        col_info1.metric("Rows", f"{len(df_exp):,}")
        col_info2.metric("Columns", f"{len(df_exp.columns):,}")
        col_info3.metric("Report", REPORT_LABELS.get(sel_report, sel_report))

        # Column filter
        all_cols = df_exp.columns.tolist()
        sel_cols = st.multiselect("Show columns", all_cols, default=all_cols[:10])
        if sel_cols:
            st.dataframe(df_exp[sel_cols].head(500), use_container_width=True, hide_index=True)
        else:
            st.dataframe(df_exp.head(500), use_container_width=True, hide_index=True)

        csv_exp = df_exp.to_csv(index=False)
        st.download_button(
            f"⬇ Download {sel_report} as CSV",
            data=csv_exp,
            file_name=f"energycap_{sel_report}_{date.today()}.csv",
            mime="text/csv",
        )
