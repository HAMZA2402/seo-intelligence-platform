"""SEO Intelligence Platform V4: upload-first Streamlit dashboard, no paid APIs.
Run: streamlit run app.py
"""
from __future__ import annotations

import html
import io
import re
import tempfile
import zipfile
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from seo_engine import (
    parse_upload, normalize_inputs, analyze_queries, analyze_pages,
    build_action_plan, compute_summary, make_excel, save_opportunity_csv,
)

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "fictional_GSC_demo.zip"

st.set_page_config(
    page_title="SEO Intelligence Platform | V4", page_icon="🔎",
    layout="wide", initial_sidebar_state="expanded",
)

st.markdown("""
<style>
.block-container{max-width:1440px;padding-top:1.7rem;padding-bottom:4rem}
[data-testid="stSidebar"]{background-color:#f2f6fa}
div[data-testid="stMetric"]{padding:1.1rem 1.1rem;border:1px solid #dfebf0;border-radius:14px;background:#fff}
div[data-testid="stMetricLabel"]{color:#5b7286}
div[data-testid="stMetricValue"]{color:#132a41;font-weight:760}
.hero{background:linear-gradient(115deg,#122b42,#14556a);padding:27px 30px;border-radius:18px;color:white;margin-bottom:20px}
.hero .eyebrow{letter-spacing:.16em;text-transform:uppercase;font-size:11px;font-weight:800;color:#93e7dc}
.hero h1{font-size:32px;line-height:1.16;margin:10px 0 6px;font-weight:800;color:#fff}
.hero p{font-size:14px;color:#d6e7ef;margin:0}
.subtle{font-size:12px;color:#718096}
div[data-testid="stTabs"] [role="tablist"]{gap:6px}
</style>
""", unsafe_allow_html=True)


AVALON_THEMES = {
    "Wrongful Death": ["wrongful death"],
    "Truck Accidents": ["truck accident", "trucking accident", "semi truck"],
    "Car Accidents": ["car accident", "auto accident", "vehicle accident"],
    "Dog Bites": ["dog bite", "mordedura de perro"],
    "Pedestrian Accidents": ["pedestrian accident", "pedestrian injury"],
    "Slip and Fall": ["slip and fall", "trip and fall", "premises liability"],
    "Personal Injury": ["personal injury", "injury attorney", "injury lawyer"],
    "Burn Injuries": ["burn injuries", "burn injury"],
    "Brain Injuries": ["brain injury", "traumatic brain"],
}


def split_terms(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def safe_csv(df: pd.DataFrame) -> bytes:
    """Protect spreadsheet applications from formula-like, untrusted GSC text."""
    out = df.copy()
    for field in out.select_dtypes(include=["object", "string"]).columns:
        out[field] = out[field].map(
            lambda v: "'" + v if isinstance(v, str) and v and v[0] in "=+-@" else v
        )
    return out.to_csv(index=False).encode("utf-8-sig")


def short_path(url: str, n: int = 62) -> str:
    value = str(url).replace("https://", "").replace("http://", "")
    return value if len(value) <= n else value[:n-1] + "…"


# ----- Sidebar controls -----
with st.sidebar:
    st.markdown("### ⚙️ Workspace setup")
    preset = st.selectbox("Client preset", ["Avalon Law", "Other client"],
                          help="You can use the tool for any client. Update the business context when switching.")
    if preset == "Avalon Law":
        default_name = "Avalon Law"
        default_brands = "avalon law, avalon law firm, avalonlaw"
        default_geo = "avalon ca | avalon, ca | avalon california"
        default_mismatch = "criminal defense"
        default_themes = AVALON_THEMES
    else:
        default_name = "My SEO Client"
        default_brands = ""
        default_geo = ""
        default_mismatch = ""
        default_themes = {}
    client = st.text_input("Client or website name", value=default_name)
    with st.expander("Business relevance settings", expanded=False):
        brand_terms = st.text_area("Brand terms (comma-separated)", value=default_brands,
                                   height=75, help="Examples: company name, spelling variants")
        geo_terms = st.text_area("Ambiguous place names to review (separate with |)", value=default_geo,
                                 height=75, help="Use a vertical bar | between phrases, so terms like Avalon, CA are preserved.")
        mismatch_terms = st.text_area("Potential off-service phrases", value=default_mismatch,
                                      height=70, help="These are review flags, not permanent exclusions.")
        topic_lines = st.text_area(
            "Optional topic groups (one per line: Topic: phrase | phrase)",
            value="\n".join(k+": "+" | ".join(v) for k,v in default_themes.items()),
            height=190,
        )
    with st.expander("Opportunity thresholds", expanded=False):
        min_impressions = st.number_input("Minimum query impressions", 10, 5000, 100, step=10)
        low_ctr = st.number_input("Low CTR threshold (%)", 0.1, 25.0, 1.5, step=0.1)
        visibility_min = st.number_input("Visibility gap: min impressions", 50, 10000, 300, step=50)
        page_low_ctr = st.number_input("Low page CTR threshold (%)", 0.1, 25.0, 1.5, step=0.1)
    st.caption("No paid AI API. V4 scores opportunities using transparent rules.")


themes = {}
for raw_line in topic_lines.splitlines():
    if ":" in raw_line:
        label, values = raw_line.split(":", 1)
        vals = [v.strip() for v in values.split("|") if v.strip()]
        if label.strip() and vals:
            themes[label.strip()] = vals

cfg = {
    "CLIENT_NAME": client,
    "BRAND_TERMS": split_terms(brand_terms),
    "GEO_AMBIGUITY_TERMS": [x.strip() for x in geo_terms.split("|") if x.strip()],
    "SERVICE_MISMATCH_TERMS": split_terms(mismatch_terms),
    "TOPIC_THEMES": themes,
    "MIN_IMPRESSIONS": int(min_impressions),
    "LOW_CTR_PERCENT": float(low_ctr),
    "VISIBILITY_GAP_MIN_IMPRESSIONS": int(visibility_min),
    "PAGE_LOW_CTR_PERCENT": float(page_low_ctr),
}

st.markdown(f"""<div class="hero"><div class="eyebrow">SEO Intelligence / V4</div>
<h1>Search performance → clear priorities.</h1>
<p>{html.escape(client)} · Upload Google Search Console data, explore opportunities, and export an action plan.</p>
</div>""", unsafe_allow_html=True)

st.markdown("#### 1. Add your GSC data")
left, right = st.columns([2.1, 1], vertical_alignment="bottom")
with left:
    uploaded = st.file_uploader(
        "Upload your original GSC Performance ZIP or Queries / Pages CSV",
        type=["zip", "csv"], help="One ZIP is enough. It should contain Queries.csv and Pages.csv."
    )
with right:
    demo = st.toggle("Explore fictional demo instead", value=False,
                     help="Demo includes made-up data. Real client data is never bundled in this app.")

with st.expander("Advanced: I also have a combined Query + Page CSV (optional)", expanded=False):
    paired = st.file_uploader("Upload combined query-page CSV for verified mapping", type=["csv"],
                             help="This must contain Query, Page, Clicks, Impressions, and Position.",
                             key="paired_file")

if uploaded is None and not demo:
    st.info("Upload your **Avalon Law GSC ZIP** above, or switch on the fictional demo to explore the dashboard.")
    st.markdown("""**What happens next?** The app screens your queries and pages, highlights ranking and CTR signals, filters opportunities, and creates a downloadable Excel audit. You do **not** need to upload a previous-period file.""")
    st.caption("Privacy: do not upload confidential or restricted client data to publicly accessible deployments without authorization. The tool does not intentionally save exports to disk; an app host can still process data in memory and logs.")
    st.stop()

if uploaded is not None:
    file_name, file_bytes = uploaded.name, uploaded.getvalue()
    source_kind = "Uploaded GSC file"
else:
    if not SAMPLE.is_file():
        st.error("Demo example is missing. Please upload your own GSC ZIP file.")
        st.stop()
    file_name, file_bytes = SAMPLE.name, SAMPLE.read_bytes()
    source_kind = "Fictional sample - not a real client"

if len(file_bytes) > 75_000_000:
    st.error("File too large. Please export GSC data and upload a file under 75 MB.")
    st.stop()

try:
    frames, meta = parse_upload(file_name, file_bytes)
    if paired is not None:
        pair_frames, _ = parse_upload(paired.name, paired.getvalue())
        if pair_frames["QueryPage"].empty:
            st.warning("The optional CSV has no Query + Page columns; ignored. Main data still works.")
        else:
            frames["QueryPage"] = pair_frames["QueryPage"]
    q, p, qp, chart, meta = normalize_inputs(frames, meta)
    all_queries, opps = analyze_queries(q, qp, cfg)
    all_pages, page_opps = analyze_pages(p, cfg)
    plan = build_action_plan(opps, page_opps, has_mapping=not qp.empty)
    summary = compute_summary(all_queries, opps, all_pages, page_opps, qp, chart, meta)
except (ValueError, zipfile.BadZipFile, KeyError) as exc:
    st.error(f"Could not process this export: {exc}")
    st.caption("Upload the original GSC Search results ZIP (not your older Excel audit).")
    st.stop()
except Exception as exc:
    st.error("Unexpected file processing error. Try downloading a new original GSC ZIP.")
    with st.expander("Technical error details"):
        st.code(f"{type(exc).__name__}: {exc}")
    st.stop()

st.success(f"Data ready · {source_kind} · {meta['date_range']}")
if qp.empty:
    st.warning("**Ranking-page mapping unavailable:** GSC's standard ZIP contains separate query and page totals. V4 will not guess the page for a keyword. Upload a combined Query + Page CSV if you have one.", icon="⚠️")
else:
    st.success(f"Verified keyword-to-page data: {len(qp):,} query/page rows loaded.")

st.markdown("#### 2. Explore performance")
metrics = [
    ("Queries analyzed", summary["Queries analyzed"]),
    ("Pages analyzed", summary["Pages analyzed"]),
    ("Keyword flags", summary["Keyword opportunities"]),
    ("Page flags", summary["Page opportunities"]),
    ("Relevance reviews", summary["Manual review flags"]),
]
cols = st.columns(len(metrics))
for col, (label, count) in zip(cols, metrics):
    col.metric(label, f"{count:,}")

overview_tab, kw_tab, pg_tab, plan_tab, download_tab = st.tabs(
    ["📊 Overview", "🔎 Keywords", "📄 Landing pages", "🗓️ Action plan", "⬇️ Downloads"]
)

with overview_tab:
    chart_col, breakdown_col = st.columns([1.6, 1])
    with chart_col:
        st.subheader("Clicks and impressions over time")
        if not chart.empty and "Date" in chart.columns and chart["Date"].notna().any():
            selected = st.radio("Chart metric", ["Clicks", "Impressions"], horizontal=True)
            fig = px.line(chart.sort_values("Date"), x="Date", y=selected,
                          markers=False, template="plotly_white")
            fig.update_traces(line_color="#128B94", line_width=2.8)
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=15),height=310,
                              xaxis_title="Date", yaxis_title=selected)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No daily chart in this export. Query and page tabs still work.")
    with breakdown_col:
        st.subheader("Keyword opportunity mix")
        if not opps.empty:
            order = ["High", "Medium", "Low", "Review"]
            counts = opps.Priority.value_counts().reindex(order,fill_value=0)
            chart_frame = pd.DataFrame({"Priority":order,"Count":counts.values})
            fig = px.bar(chart_frame, x="Priority",y="Count",color="Priority",
                         color_discrete_map={"High":"#0C837F", "Medium":"#377EC8",
                                             "Low":"#9AAEC2","Review":"#D18C35"},
                         template="plotly_white")
            fig.update_layout(height=310,showlegend=False,margin=dict(l=10,r=10,t=10,b=15))
            st.plotly_chart(fig,use_container_width=True)
        else:
            st.info("No queries met current scoring thresholds.")
    st.subheader("Top areas to investigate")
    keytop = opps[["Query","Impressions","CTR (%)","Position","Priority","Suggested Next Action"]].head(8) if not opps.empty else pd.DataFrame()
    if not keytop.empty:
        st.dataframe(keytop, hide_index=True, use_container_width=True,
                     column_config={"CTR (%)":st.column_config.NumberColumn(format="%.2f%%"),
                                    "Position":st.column_config.NumberColumn(format="%.1f")})
    else:
        st.info("No keyword flags yet. Try lowering minimum impressions in the sidebar.")

with kw_tab:
    st.subheader("Keyword opportunity explorer")
    a,b,c = st.columns([2.2,1,1.2])
    with a:
        query_search = st.text_input("Search query / URL",placeholder="e.g., wrongful death")
    with b:
        priorities = st.multiselect("Priority",["High","Medium","Low","Review"],
                                    default=["High","Medium","Low","Review"])
    with c:
        topics = st.multiselect("Topic",sorted(opps.Topic.unique().tolist()) if not opps.empty else [])
    view = opps.copy()
    if not view.empty:
        view = view[view.Priority.isin(priorities)]
        if topics:
            view = view[view.Topic.isin(topics)]
        if query_search.strip():
            mask = view.Query.astype(str).str.contains(re.escape(query_search.strip()),case=False,regex=True)
            mask |= view["Top Ranking Page"].astype(str).str.contains(re.escape(query_search.strip()),case=False,regex=True)
            view = view[mask]
    st.caption(f"{len(view):,} matching keyword flags · All exported query rows: {len(all_queries):,}")
    if not view.empty:
        display_cols=["Query","Clicks","Impressions","CTR (%)","Position","Priority","Topic",
                      "Opportunity Signals","Top Ranking Page","Review Required","Suggested Next Action"]
        st.dataframe(view[display_cols],hide_index=True,use_container_width=True,height=530,
                     column_config={"CTR (%)":st.column_config.NumberColumn(format="%.2f%%"),
                                    "Position":st.column_config.NumberColumn(format="%.1f")})
        st.download_button("Download filtered keyword CSV",safe_csv(view),
                           file_name="V4_Filtered_Keyword_Opportunities.csv",mime="text/csv")
    else:
        st.info("No matching keywords. Reset filters or adjust the thresholds in the sidebar.")
    if not all_queries.empty:
        with st.expander("Search ALL GSC queries (not only flagged ones)"):
            all_search=st.text_input("All-query search",key="all_query_search")
            subset=all_queries
            if all_search.strip():
                subset=subset[subset.Query.astype(str).str.contains(re.escape(all_search.strip()),case=False,regex=True)]
            st.dataframe(subset[["Query","Clicks","Impressions","CTR (%)","Position","Priority","Topic"]].head(500),
                         hide_index=True,use_container_width=True,height=300)
            st.caption("For full detail, use the Excel report rather than the first 500 rows shown here.")

with pg_tab:
    st.subheader("Landing-page performance")
    st.caption("Each row is a **page-level aggregate**. It is not the ranking page for a specific keyword unless a combined query-page export is provided.")
    ps, priority_filter = st.columns([2,1])
    with ps:
        page_search = st.text_input("Search page URL", placeholder="e.g., personal-injury")
    with priority_filter:
        page_priorities = st.multiselect("Page priority",["High","Medium"],default=["High","Medium"])
    pv=page_opps.copy()
    if not pv.empty:
        pv=pv[pv.Priority.isin(page_priorities)]
        if page_search.strip():
            pv=pv[pv.Page.astype(str).str.contains(re.escape(page_search.strip()),case=False,regex=True)]
    st.caption(f"{len(pv):,} flagged pages · {len(all_pages):,} pages in original export")
    if not pv.empty:
        pv_show = pv[["Page","Clicks","Impressions","CTR (%)","Position","Priority","Page Signal","Next Action"]]
        st.dataframe(pv_show,hide_index=True,use_container_width=True,height=460,
                     column_config={"CTR (%)":st.column_config.NumberColumn(format="%.2f%%"),
                                    "Position":st.column_config.NumberColumn(format="%.1f")})
        fig = px.bar(pv.head(12).sort_values("Impressions"),
                     x="Impressions",y="Page",orientation="h",color="Priority",
                     color_discrete_map={"High":"#0C837F","Medium":"#3B86C7"},
                     template="plotly_white", hover_data=["Clicks","Position"])
        fig.update_layout(height=410,margin=dict(l=10,r=10,t=10,b=10),
                          yaxis=dict(tickmode="array",
                                     tickvals=pv.head(12).sort_values("Impressions").Page.tolist(),
                                     ticktext=[short_path(x,52) for x in pv.head(12).sort_values("Impressions").Page]),
                          showlegend=True)
        st.plotly_chart(fig,use_container_width=True)
        st.download_button("Download filtered pages CSV",safe_csv(pv),
                           file_name="V4_Filtered_Page_Opportunities.csv",mime="text/csv")
    else:
        st.info("No matching page opportunities.")

with plan_tab:
    st.subheader("30-day execution plan")
    st.caption("Edit the Status field to track work. The Excel export will include your latest changes while this session remains active.")
    edited_plan = st.data_editor(
        plan,hide_index=True,use_container_width=True,num_rows="fixed",key="plan_editor",
        disabled=["Week","Workstream","What to Do","Owner","Suggested KPI"],
        column_config={"Status":st.column_config.SelectboxColumn(
            "Status",options=["Not Started","In Progress","Done","Blocked"],required=True)},
    )
    st.markdown("##### Priority keyword investigations")
    if not opps.empty:
        st.dataframe(opps[["Query","Impressions","Position","Priority","Suggested Next Action"]].head(8),
                     hide_index=True,use_container_width=True)
    else:
        st.caption("No flagged keywords at current thresholds.")
    st.info("Treat flags as **investigation prompts**. Confirm business fit, actual ranking URLs, SERP intent, and lead potential before changing a client's website.")

with download_tab:
    st.subheader("Download your client-ready working files")
    st.caption("No paid API or account connection required. Exports contain your uploaded data, so store them securely.")
    try:
        with tempfile.TemporaryDirectory(prefix="seo_v4_") as tmp:
            dest = Path(tmp) / "seo_audit.xlsx"
            make_excel(str(dest), all_queries, opps, all_pages, page_opps, qp,
                       edited_plan, meta, cfg)
            excel_bytes = dest.read_bytes()
        st.download_button("⬇️ Download full Excel audit", excel_bytes,
                           file_name="SEO_Intelligence_V4_Audit.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           type="primary")
    except Exception as exc:
        st.error("Excel export could not be created.")
        with st.expander("Technical details"):
            st.code(f"{type(exc).__name__}: {exc}")
    col1,col2 = st.columns(2)
    with col1:
        st.download_button("Keyword opportunities (.csv)",safe_csv(opps),
                           file_name="SEO_V4_Keyword_Opportunities.csv",mime="text/csv")
    with col2:
        st.download_button("Page opportunities (.csv)",safe_csv(page_opps),
                           file_name="SEO_V4_Page_Opportunities.csv",mime="text/csv")
    st.markdown("""**Included in the Excel file:** Executive Summary, Keyword Opportunities, Page Opportunities, All Queries, All Pages, Query Page Pairs, 30-Day Action Plan, and Methodology.""")

st.caption("V4 · Transparent rules + GSC exports · No automatic page rewriting or AI-generated assertions · Validate before implementation.")
