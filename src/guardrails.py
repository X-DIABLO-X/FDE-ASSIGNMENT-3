"""Deterministic guardrails shared by BOTH architectures.

Principle: the model may *add* caution (extra flags / approvals / missing items / data classes / a stricter
label) but can never remove anything the rule engine produced. Evidence the model writes is only kept if it is
grounded in tool results. ``human_review_required`` is always True - the copilot never approves anything.
"""
from __future__ import annotations

import json
import re

from src import policy
from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.policy import ALLOWED_FLAGS, APPROVAL_ORDER, LABELS, PolicyResult, money
from src.tools import ToolBox

_POLICY_FIELD_RE = re.compile(r"requester|department|product|vendor|cost|price|budget estimate|user|seat|licen|purpose|justification|"
                              r"data.?access|integration", re.I)

FLAG_TO_APPROVAL = {
    "security_review_required": "Security",
    "privacy_review_required": "Privacy",
    "legal_review_required": "Legal",
    "budget_insufficient": "Finance",
    "existing_tool_overlap": "Procurement",
}


# --------------------------------------------------------------------------- deterministic evidence

def build_evidence(tb: ToolBox, res: PolicyResult) -> list[EvidenceItem]:
    ev = tb.gather_evidence()
    r = tb.request
    items: list[EvidenceItem] = []
    ctx = ev["context"]
    who = ctx.get("requester")
    if who:
        mgr, head = ctx.get("manager"), ctx.get("department_head")
        items.append(EvidenceItem(
            source="get_request_context",
            finding=f"{r.get('product_name')} ({r.get('vendor_name')}) requested by {who['name']} ({who['department']}); "
                    f"manager {mgr['name'] if mgr else 'n/a'}, department head {head['name'] if head else 'n/a'}; "
                    f"cost {money(r['annual_cost_usd']) if r.get('annual_cost_usd') is not None else 'not stated'}, "
                    f"{r.get('user_count') if r.get('user_count') is not None else 'unknown number of'} users, "
                    f"data access '{r.get('data_access_level')}'.",
            reference=r.get("request_id")))
    else:
        items.append(EvidenceItem(source="get_request_context", finding="Requester could not be resolved in the employee directory.",
                                  reference=r.get("request_id")))

    b = ev["budget"]
    if b.get("found"):
        if b.get("within_budget") is None:
            f = f"{b['department']} has {money(b['available_usd'])} available; cost unknown so the budget check is incomplete."
        elif b["within_budget"]:
            f = f"Cost {money(b['annual_cost_usd'])} is within {b['department']} available budget {money(b['available_usd'])}."
        else:
            f = (f"Cost {money(b['annual_cost_usd'])} EXCEEDS {b['department']} available budget {money(b['available_usd'])} "
                 f"(shortfall {money(b['shortfall_usd'])}).")
        items.append(EvidenceItem(source="check_budget", finding=f, reference=f"department_budgets:{b['department']}"))

    matches = ev["overlap"]["matches"]
    if matches:
        for m in matches[:4]:
            items.append(EvidenceItem(
                source="find_existing_software",
                finding=f"{m['product_name']} ({m['vendor_name']}, {m['category']}, {m['status']}, {m['scope']}, "
                        f"{m['licensed_seats']} seats, {money(m['annual_cost_usd'])}/yr) matches: {', '.join(m['match_types'])}.",
                reference=m["software_id"]))
    else:
        items.append(EvidenceItem(source="find_existing_software", finding="No same-product, same-vendor or same-category entry in the software catalog.",
                                  reference="software_catalog.csv"))

    purchases = ev["history"]["purchases"]
    if purchases:
        p = purchases[0]
        items.append(EvidenceItem(source="get_purchase_history",
                                  finding=f"{len(purchases)} prior purchase(s) with {r.get('vendor_name')}; latest {p['purchase_id']} "
                                          f"on {p['purchase_date']} ({p['department']}, {money(p['annual_amount_usd'])}, {p['notes']}).",
                                  reference=p["purchase_id"]))
    else:
        items.append(EvidenceItem(source="get_purchase_history", finding=f"No purchase history with {r.get('vendor_name')}.",
                                  reference="purchase_history.csv"))

    reg = ev["registry"]
    if reg:
        items.append(EvidenceItem(
            source="lookup_vendor_registry",
            finding=f"Registry: procurement '{reg['procurement_status']}', security '{reg['security_status']}' "
                    f"(reviewed {reg['security_review_date'] or 'never'}), legal terms '{reg['legal_terms_status']}'.",
            reference=reg["vendor_id"]))
    else:
        items.append(EvidenceItem(source="lookup_vendor_registry", finding="Vendor is not in the internal registry.",
                                  reference="vendors.csv"))

    risk = ev["risk"]
    ref = f"/vendor-risk/{r.get('vendor_name')}"
    if risk["status"] == "ok":
        d = risk["data"]
        items.append(EvidenceItem(
            source="get_vendor_risk",
            finding=f"Risk service: level '{d['risk_level']}', security review '{d['security_review_status']}' "
                    f"(last reviewed {d['last_review_date'] or 'never'}), personal data: {d['processes_personal_data']}, "
                    f"stored outside region: {d['stores_data_outside_region']}.",
            reference=ref))
    else:
        items.append(EvidenceItem(source="get_vendor_risk",
                                  finding=f"Vendor-risk lookup {risk['status'].replace('_', ' ')}: {risk['error']}. Status NOT verified.",
                                  reference=ref))

    for f in res.findings:
        items.append(EvidenceItem(source=f.source, finding=f.text, reference=f.reference))
    return items


# --------------------------------------------------------------------------- grounding

def _haystack(tb: ToolBox) -> str:
    blob = json.dumps({"tools": tb.results, "request": tb.request}, default=str)
    return blob.replace(",", "")


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".").replace(",", "") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def grounded(item: dict, tb: ToolBox, haystack: str) -> bool:
    if item.get("source") not in tb.results or not str(item.get("finding", "")).strip():
        return False
    return all(n in haystack for n in _numbers(item["finding"]))


# --------------------------------------------------------------------------- finalisation

def next_step_template(tb: ToolBox, res: PolicyResult) -> str:
    r, ev = tb.request, tb.gather_evidence()
    who = (ev["context"].get("requester") or {}).get("name", "the requester")
    parts: list[str] = []
    if "missing_information" in res.flags:
        parts.append(f"Return the request to {who} to provide: " + "; ".join(res.missing) + ". Do not progress it until complete.")
    if "prompt_injection_detected" in res.flags:
        parts.append("Embedded instructions in the request were ignored; normal policy applies and no approval is implied.")
    if "vendor_risk_unavailable" in res.flags:
        parts.append(f"Ask Security to verify {r.get('vendor_name')}'s assessment manually and retry the vendor-risk lookup later.")
    if "conflicting_vendor_evidence" in res.flags:
        parts.append("Security to reconcile the conflicting registry vs vendor-risk records before any approval.")
    if "vendor_review_expired" in res.flags:
        parts.append("Security to refresh the expired vendor assessment.")
    if "existing_tool_overlap" in res.flags:
        names = sorted({m["product_name"] for m in ev["overlap"]["matches"] if "same_category" in m["match_types"]})
        parts.append(f"Ask the requester to justify the gap versus existing tool(s): {', '.join(names)}.")
    if "budget_insufficient" in res.flags:
        parts.append("Finance to review a budget exception.")
    gates = [a for a in res.approvals]
    if gates:
        parts.append("Send the evidence package to " + ", ".join(gates) + " for human review and approval.")
    return " ".join(parts) or "Send the evidence package to the approvers for human review."


def finalize(tb: ToolBox, proposal: dict | None, architecture: str, mode: str, llm_calls: int,
             tokens: tuple[int, int] = (0, 0), notes: list[str] | None = None) -> ProcurementDecision:
    notes = list(notes or [])
    proposal = proposal or {}

    # 1) deterministic result, widened (never narrowed) by data classes the model read from free text
    inferred = [c for c in proposal.get("inferred_data_classes", []) if c in policy.DATA_CLASSES]
    res = tb.evaluate(extra_classes=inferred)
    if set(inferred) - set(res.data_classes):  # defensive: should not happen
        notes.append("inferred data classes were not applied")

    # 2) model-added caution, validated against the closed taxonomy
    for flag in proposal.get("additional_risk_flags", []):
        if flag in ALLOWED_FLAGS and flag not in res.flags:
            res.add_flag(flag)
            res.note(f"Agent added flag '{flag}' (additive judgement, not rule-derived).", source="agent_judgement")
            if flag in FLAG_TO_APPROVAL:
                res.add_approval(FLAG_TO_APPROVAL[flag])
    for name in proposal.get("additional_approvals", []):
        if name in APPROVAL_ORDER:
            res.add_approval(name)
    for m in [str(m).strip()[:140] for m in proposal.get("additional_missing_information", []) if str(m).strip()][:5]:
        if not _POLICY_FIELD_RE.search(m):  # only genuine policy-section-1 fields may block a request
            notes.append(f"ignored non-field 'missing information' item: {m[:60]}")
        elif m not in res.missing:
            res.missing.append(m)
            res.add_flag("missing_information")
    overlap = proposal.get("overlap_assessment") or {}
    if overlap.get("is_overlap") and "existing_tool_overlap" not in res.flags:
        res.add_flag("existing_tool_overlap")
        res.add_approval("Procurement")
        res.note(f"Agent judged an existing-tool overlap: {str(overlap.get('reason', ''))[:200]}", "Policy section 3", "agent_judgement")
    res.approvals = [a for a in APPROVAL_ORDER if a in res.approvals]

    # 3) label can only get stricter than the rules'
    floor = policy.deterministic_label(res)
    label = policy.most_restrictive(floor, proposal.get("recommendation"))
    if proposal.get("recommendation") in LABELS and LABELS.index(proposal["recommendation"]) < LABELS.index(floor):
        notes.append(f"model label '{proposal['recommendation']}' was less restrictive than rules; raised to '{floor}'")

    # 4) evidence: rule/tool-derived first, then grounded model evidence
    evidence = build_evidence(tb, res)
    seen = {(e.source, e.finding) for e in evidence}
    dropped, hay = 0, _haystack(tb)
    for item in proposal.get("evidence", []):
        if grounded(item, tb, hay):
            key = (item["source"], item["finding"])
            if key not in seen:
                evidence.append(EvidenceItem(source=item["source"], finding=item["finding"], reference=item.get("reference")))
                seen.add(key)
        else:
            dropped += 1

    next_step = (proposal.get("next_step") or "").strip() or next_step_template(tb, res)
    tel = tb.telemetry
    return ProcurementDecision(
        request_id=tb.request.get("request_id", "REQ-ADHOC"),
        recommendation=label,
        evidence=evidence,
        required_approvals=res.approvals,
        missing_information=res.missing,
        risk_flags=res.flags,
        next_step=next_step,
        human_review_required=True,
        telemetry=RunTelemetry(
            llm_calls=llm_calls, tool_calls=tel.tool_calls, tool_names=list(tel.tool_names), mode=mode,
            architecture=architecture, input_tokens=tokens[0], output_tokens=tokens[1],
            ungrounded_evidence_dropped=dropped, agent_notes=notes),
    )
