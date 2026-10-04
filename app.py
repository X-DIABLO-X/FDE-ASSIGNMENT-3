from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from src.contracts import ProcurementDecision
from src.data_access import load_employees, load_requests
from src.llm import get_llm
from src.policy import LABELS, REFERENCE_DATE
from src.solution import analyze_request
from src.tools import ToolBox
from src.vendor_client import ensure_mock_api

ROOT = Path(__file__).resolve().parent
DECISION_LOG = ROOT / "runtime" / "human_decisions.jsonl"  # gitignored; the only thing a human click writes

st.set_page_config(page_title="Procurement Request Copilot", layout="wide", page_icon="🧾")

LABEL_STYLE = {
    LABELS[0]: ("#1a7f37", "Standard path"),
    LABELS[1]: ("#9a6700", "Check existing tools first"),
    LABELS[2]: ("#bc4c00", "Specialist reviews needed"),
    LABELS[3]: ("#cf222e", "Evidence unverified / conflicting"),
    LABELS[4]: ("#8250df", "Blocked on missing information"),
}
DATA_LEVELS = ["none", "internal_marketing", "internal_documents", "confidential_documents", "source_code", "employee_pii",
               "customer_pii", "credentials_secrets", "production_telemetry", "unknown"]


@st.cache_resource
def _services_ready() -> bool:
    return ensure_mock_api()


def _employee_options() -> dict[str, str]:
    emp = load_employees()
    return {r["employee_id"]: f"{r['name']} - {r['department']}" for r in emp.to_dict("records")}


def request_form(defaults: dict) -> dict:
    emp = _employee_options()
    with st.form("custom_request"):
        c1, c2 = st.columns(2)
        requester = c1.selectbox("Requester", list(emp), format_func=emp.get, index=list(emp).index(defaults["requester_id"]))
        product = c1.text_input("Product", defaults["product_name"])
        vendor = c2.text_input("Vendor", defaults["vendor_name"])
        category = c1.text_input("Category", defaults["category"])
        cost = c2.text_input("Annual cost (USD, blank = not stated)", "" if defaults["annual_cost_usd"] is None else str(defaults["annual_cost_usd"]))
        users = c1.text_input("Users / seats (blank = not stated)", "" if defaults["user_count"] is None else str(defaults["user_count"]))
        level = c2.selectbox("Data access level", DATA_LEVELS, index=DATA_LEVELS.index(defaults["data_access_level"]) if defaults["data_access_level"] in DATA_LEVELS else len(DATA_LEVELS) - 1)
        integ = st.text_input("Integrations (comma separated)", ", ".join(defaults["requested_integrations"] or []))
        why = st.text_area("Business justification", defaults["business_justification"])
        submitted = st.form_submit_button("Use this request")
    if submitted or "custom" not in st.session_state:
        try:
            cost_v = float(cost) if cost.strip() else None
            users_v = int(users) if users.strip() else None
        except ValueError:
            st.error("Cost and users must be numbers.")
            cost_v = users_v = None
        st.session_state["custom"] = {
            "request_id": "REQ-CUSTOM", "requester_id": requester, "product_name": product, "vendor_name": vendor,
            "category": category, "annual_cost_usd": cost_v, "user_count": users_v, "business_justification": why,
            "data_access_level": level, "requested_integrations": [i.strip() for i in integ.split(",") if i.strip()],
            "urgency": "normal"}
    return st.session_state["custom"]


def approver_names(request: dict) -> dict[str, str]:
    ctx = ToolBox(request).call("get_request_context")
    names = {}
    if ctx.get("manager"):
        names["Manager"] = ctx["manager"]["name"]
    if ctx.get("department_head"):
        names["Department Head"] = ctx["department_head"]["name"]
    return names


def render_decision(d: ProcurementDecision, request: dict, key: str, latency_ms: float) -> None:
    colour, hint = LABEL_STYLE.get(d.recommendation, ("#57606a", ""))
    st.markdown(
        f"<div style='border-left:6px solid {colour};padding:10px 14px;background:rgba(127,127,127,.08);border-radius:4px'>"
        f"<div style='font-size:.8rem;opacity:.7'>RECOMMENDATION - {hint}</div>"
        f"<div style='font-size:1.25rem;font-weight:600'>{d.recommendation}</div>"
        f"<div style='font-size:.85rem;margin-top:4px'>Advisory only - a human makes the final decision.</div></div>",
        unsafe_allow_html=True)
    st.markdown("**Next step**")
    st.write(d.next_step)

    c1, c2 = st.columns(2)
    names = approver_names(request)
    with c1:
        st.markdown("**Approvals required (humans)**")
        if d.required_approvals:
            for a in d.required_approvals:
                st.markdown(f"- {a}" + (f" - {names[a]}" if a in names else ""))
        else:
            st.caption("None determined")
        st.markdown("**Missing information**")
        for m in d.missing_information or []:
            st.markdown(f"- {m}")
        if not d.missing_information:
            st.caption("Nothing missing")
    with c2:
        st.markdown("**Risk flags**")
        for f in d.risk_flags:
            st.markdown(f"- `{f}`")
        if not d.risk_flags:
            st.caption("No flags raised")

    with st.expander(f"Evidence panel ({len(d.evidence)} items, each tied to a tool result)", expanded=True):
        st.dataframe([{"source": e.source, "finding": e.finding, "reference": e.reference or ""} for e in d.evidence],
                     hide_index=True, width="stretch")

    t = d.telemetry
    if t:
        cols = st.columns(5)
        cols[0].metric("Mode", t.mode or "-")
        cols[1].metric("LLM calls", t.llm_calls)
        cols[2].metric("Tool calls", t.tool_calls)
        cols[3].metric("Latency", f"{latency_ms / 1000:.1f}s")
        cols[4].metric("Tokens", (t.input_tokens or 0) + (t.output_tokens or 0))
        for n in t.agent_notes:
            st.warning(n)


def human_handoff(d: ProcurementDecision, request: dict, key: str) -> None:
    st.markdown("### Human decision")
    st.caption("The copilot never approves, purchases or changes budgets. This records a reviewer's decision locally for audit; "
               "it triggers nothing else.")
    note = st.text_input("Reviewer note", key=f"note_{key}")
    c1, c2, c3 = st.columns(3)
    choice = None
    if c1.button("Accept recommendation", key=f"acc_{key}"):
        choice = "accepted"
    if c2.button("Send back to requester", key=f"back_{key}"):
        choice = "returned"
    if c3.button("Reject / override", key=f"rej_{key}"):
        choice = "overridden"
    if choice:
        DECISION_LOG.parent.mkdir(exist_ok=True)
        with DECISION_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "request_id": d.request_id,
                                "copilot_recommendation": d.recommendation, "human_decision": choice, "note": note}) + "\n")
        st.success(f"Recorded: {choice}")
    packet = "\n".join([
        f"# Review packet - {d.request_id}: {request.get('product_name')} ({request.get('vendor_name')})",
        f"**Recommendation:** {d.recommendation}", f"**Next step:** {d.next_step}", "",
        "## Approvals required", *[f"- {a}" for a in d.required_approvals], "",
        "## Risk flags", *[f"- {f}" for f in d.risk_flags], "",
        "## Missing information", *[f"- {m}" for m in d.missing_information], "",
        "## Evidence", *[f"- [{e.source}] {e.finding} ({e.reference or 'n/a'})" for e in d.evidence]])
    st.download_button("Download reviewer packet (.md)", packet, file_name=f"{d.request_id}_review_packet.md", key=f"dl_{key}")


# --------------------------------------------------------------------------- page

st.title("AI Procurement Request Copilot")
llm = get_llm()
api_ok = _services_ready()
st.caption(
    f"Policy v2026.09 - reference date {REFERENCE_DATE} - "
    + (f"model `{llm.model}`" if llm else "**deterministic mode** (no ANTHROPIC_API_KEY: rules + templated text, 0 LLM calls)")
    + " - vendor-risk API " + ("online" if api_ok else "**offline** (lookups will be reported as unavailable)"))

requests = {r["request_id"]: r for r in load_requests()}
with st.sidebar:
    st.header("Request")
    source = st.radio("Source", ["Dataset request", "New / edited request"])
    if source == "Dataset request":
        rid = st.selectbox("Request", list(requests), format_func=lambda x: f"{x} - {requests[x]['product_name']}")
    else:
        rid = st.selectbox("Start from", list(requests), format_func=lambda x: f"{x} - {requests[x]['product_name']}")
    st.header("Architecture")
    arch = st.radio("Run", ["single", "staged", "compare both"], help="A = single agent, B = evidence agent -> policy reviewer")
    st.divider()
    st.caption("Request text, vendor notes and API text are treated as untrusted data. Thresholds and review triggers are deterministic code.")

request = requests[rid] if source == "Dataset request" else None
left, right = st.columns([0.9, 1.1], gap="large")
with left:
    st.subheader("Request details")
    if request is None:
        request = request_form(requests[rid])
    else:
        emp = _employee_options()
        st.markdown(f"**{request['product_name']}** - {request['vendor_name']}  \nRequested by **{emp[request['requester_id']]}**")
        st.write(request["business_justification"])
        c1, c2, c3 = st.columns(3)
        c1.metric("Annual cost", "not stated" if request["annual_cost_usd"] is None else f"${request['annual_cost_usd']:,.0f}")
        c2.metric("Users", "not stated" if request["user_count"] is None else request["user_count"])
        c3.metric("Urgency", request["urgency"])
        st.markdown(f"Data access: `{request['data_access_level']}` - Integrations: {', '.join(request['requested_integrations']) or 'none'}")
        with st.expander("Raw request JSON"):
            st.json(request)
    run = st.button("Analyse request", type="primary", width="stretch")

with right:
    st.subheader("Copilot recommendation")
    state_key = "results"
    if run:
        archs = ["single", "staged"] if arch == "compare both" else [arch]
        out = {}
        for a in archs:
            with st.spinner(f"Running architecture {'A (single agent)' if a == 'single' else 'B (staged)'} ..."):
                start = time.perf_counter()
                decision = analyze_request(dict(request), a)
                out[a] = (decision, (time.perf_counter() - start) * 1000)
        st.session_state[state_key] = {"request": request, "results": out}
    saved = st.session_state.get(state_key)
    if not saved:
        st.info("Pick a request and press **Analyse request**.")
    else:
        results = saved["results"]
        if len(results) == 2:
            tabs = st.tabs(["A - Single agent", "B - Staged (2 agents)", "Differences"])
            for tab, a in zip(tabs[:2], ["single", "staged"]):
                with tab:
                    render_decision(results[a][0], saved["request"], a, results[a][1])
            with tabs[2]:
                da, db = results["single"][0], results["staged"][0]
                rows = [
                    ("Recommendation", da.recommendation, db.recommendation),
                    ("Approvals", ", ".join(da.required_approvals), ", ".join(db.required_approvals)),
                    ("Risk flags", ", ".join(da.risk_flags), ", ".join(db.risk_flags)),
                    ("LLM calls", da.telemetry.llm_calls, db.telemetry.llm_calls),
                    ("Tool calls", da.telemetry.tool_calls, db.telemetry.tool_calls),
                    ("Latency (ms)", round(results["single"][1]), round(results["staged"][1]))]
                st.dataframe([{"": k, "A single": str(a), "B staged": str(b), "same": "yes" if str(a) == str(b) else "NO"}
                              for k, a, b in rows], hide_index=True, width="stretch")
            human_handoff(results["single"][0], saved["request"], "cmp")
        else:
            a = next(iter(results))
            render_decision(results[a][0], saved["request"], a, results[a][1])
            human_handoff(results[a][0], saved["request"], a)
