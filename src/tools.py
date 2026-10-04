"""Tool layer shared by both architectures.

Seven tools, six of them deterministic data/rule lookups and one external API call:

| tool                    | kind                         | purpose                                           |
|-------------------------|------------------------------|---------------------------------------------------|
| get_request_context     | deterministic (CSV lookup)   | requester, manager chain, department head         |
| check_budget            | deterministic (arithmetic)   | cost vs available department budget               |
| find_existing_software  | deterministic (catalog match)| same product / vendor / category candidates       |
| get_purchase_history    | deterministic (CSV lookup)   | prior purchases with the vendor                   |
| lookup_vendor_registry  | deterministic (CSV lookup)   | internal vendor registry row                      |
| get_vendor_risk         | external API (mock service)  | external security / risk status, failure-aware    |
| evaluate_policy         | deterministic (rules engine) | approvals, flags, missing info (policy.py)        |

The LLM may call these with arguments, but ``evaluate_policy`` always re-derives its inputs from the
*real* request, so a wrong/injected argument can never corrupt a policy decision.
"""
from __future__ import annotations

import json
from typing import Any

from src import data_access as da
from src import policy
from src.telemetry import RunTelemetryCounter
from src.vendor_client import fetch_vendor_risk

_AI_FAMILY = {"developer ai", "general ai", "legal ai", "ai assistant"}


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


class ToolBox:
    """Executes tools for one request, caches results, and records telemetry."""

    def __init__(self, request: dict, telemetry: RunTelemetryCounter | None = None):
        self.request = request
        self.telemetry = telemetry or RunTelemetryCounter()
        self._cache: dict[str, dict] = {}
        self.results: dict[str, Any] = {}  # tool name -> last output (used for grounding checks)
        self._policy: dict[tuple, policy.PolicyResult] = {}  # extra data classes -> rich result object

    # ------------------------------------------------------------------ dispatch
    def _fill_defaults(self, name: str, args: dict) -> dict:
        """Bind omitted arguments to the real request so equivalent calls share one cached execution."""
        r = self.request
        args = {k: v for k, v in args.items() if k != "request_id"}
        if name == "get_request_context":
            return {}
        if name == "evaluate_policy":
            extra = args.get("extra_data_classes") or []
            return {"extra_data_classes": sorted({c for c in extra if c in policy.DATA_CLASSES})}
        defaults = {
            "check_budget": {"department": (self._t_get_request_context().get("requester") or {}).get("department"),
                             "annual_cost_usd": r.get("annual_cost_usd")},
            "find_existing_software": {"category": r.get("category"), "vendor_name": r.get("vendor_name"),
                                       "product_name": r.get("product_name")},
        }.get(name, {"vendor_name": r.get("vendor_name")})
        return {**defaults, **{k: v for k, v in args.items() if v is not None and k in defaults}}

    def call(self, name: str, args: dict | None = None) -> dict:
        impl = getattr(self, f"_t_{name}", None)
        if impl is None:
            return {"error": f"unknown tool '{name}'"}
        args = self._fill_defaults(name, args or {})
        key = name + json.dumps(args, sort_keys=True, default=str)
        if key not in self._cache:
            self.telemetry.record_tool_call(name)
            try:
                self._cache[key] = impl(**args)
            except TypeError as exc:
                return {"error": f"bad arguments for {name}: {exc}"}
            except Exception as exc:  # a tool failure is data, not a crash
                self._cache[key] = {"error": f"{type(exc).__name__}: {exc}"}
        self.results[name] = self._cache[key]
        return self._cache[key]

    # ------------------------------------------------------------------ tools
    def _t_get_request_context(self, request_id: str | None = None) -> dict:
        req = self.request
        employees = da.load_employees()
        by_id = {r["employee_id"]: r for r in employees.to_dict("records")}
        requester = by_id.get(req.get("requester_id"))
        chain = []
        cursor = requester
        seen = set()
        while cursor and cursor["manager_id"] and cursor["manager_id"] not in seen:
            seen.add(cursor["manager_id"])
            cursor = by_id.get(cursor["manager_id"])
            if cursor:
                chain.append(cursor)
        manager = chain[0] if chain else None
        # The department head is the first Director/VP in the management chain. (Starter data note: Sales and
        # Customer Success have no Director in their own department, so the chain is the only reliable source.)
        head = next((p for p in chain if p["level"] in ("Director", "VP")), None)
        slim = lambda p: None if p is None else {k: p[k] for k in ("employee_id", "name", "department", "level", "country")}
        return {
            "request_id": req.get("request_id"),
            "requester": slim(requester),
            "manager": slim(manager),
            "department_head": slim(head),
            "request": {k: req.get(k) for k in (
                "product_name", "vendor_name", "category", "annual_cost_usd", "user_count",
                "business_justification", "data_access_level", "requested_integrations", "urgency")},
        }

    def _t_check_budget(self, department: str | None = None, annual_cost_usd: float | None = None) -> dict:
        budgets = da.load_budgets()
        dept = department or (self._t_get_request_context().get("requester") or {}).get("department")
        row = budgets[budgets["department"].str.lower() == _norm(dept)]
        if row.empty:
            return {"department": dept, "found": False, "available_usd": None, "within_budget": None}
        r = row.iloc[0]
        cost = annual_cost_usd
        return {
            "department": r["department"], "found": True,
            "annual_software_budget_usd": int(r["annual_software_budget_usd"]),
            "committed_usd": int(r["committed_usd"]), "available_usd": int(r["available_usd"]),
            "annual_cost_usd": cost,
            "within_budget": None if cost is None else float(cost) <= int(r["available_usd"]),
            "shortfall_usd": None if cost is None else max(0.0, float(cost) - int(r["available_usd"])),
        }

    def _t_find_existing_software(self, category: str | None = None, vendor_name: str | None = None,
                                  product_name: str | None = None) -> dict:
        catalog = da.load_software_catalog()
        req_dept = (self._t_get_request_context().get("requester") or {}).get("department")
        cat, vendor, product = _norm(category), _norm(vendor_name), _norm(product_name)
        matches = []
        for row in catalog.to_dict("records"):
            types = []
            if vendor and _norm(row["vendor_name"]) == vendor:
                types.append("same_vendor")
            if product and _norm(row["product_name"]) in product:
                types.append("same_product")
            if cat and _norm(row["category"]) == cat:
                types.append("same_category")
            elif cat in _AI_FAMILY and _norm(row["category"]) in _AI_FAMILY:
                types.append("related_category")
            if types:
                matches.append({
                    **{k: row[k] for k in ("software_id", "product_name", "category", "vendor_name", "status",
                                           "annual_cost_usd", "licensed_seats", "scope", "notes")},
                    "match_types": types,
                    "covers_requester_department": _norm(row["scope"]) in ("company-wide", _norm(req_dept)),
                })
        return {"query": {"category": category, "vendor_name": vendor_name, "product_name": product_name},
                "matches": matches}

    def _t_get_purchase_history(self, vendor_name: str | None = None) -> dict:
        hist = da.load_purchase_history()
        rows = hist[hist["vendor_name"].str.lower() == _norm(vendor_name)].to_dict("records")
        return {"vendor_name": vendor_name, "purchases": rows}

    def _t_lookup_vendor_registry(self, vendor_name: str | None = None) -> dict:
        vendors = da.load_vendors()
        row = vendors[vendors["vendor_name"].str.lower() == _norm(vendor_name)]
        if row.empty:
            return {"vendor_name": vendor_name, "found": False, "record": None}
        rec = row.iloc[0].to_dict()
        return {"vendor_name": vendor_name, "found": True, "record": rec}

    def _t_get_vendor_risk(self, vendor_name: str | None = None) -> dict:
        return fetch_vendor_risk(vendor_name or "")

    def _t_evaluate_policy(self, request_id: str | None = None, extra_data_classes: list[str] | None = None) -> dict:
        extra = tuple(sorted(set(extra_data_classes or [])))
        res = policy.evaluate(self.request, self.gather_evidence(), list(extra))
        self._policy[extra] = res
        return policy_result_to_dict(res)

    # ------------------------------------------------------------------ deterministic evidence + policy
    def gather_evidence(self) -> dict:
        """Ground-truth evidence for the *real* request (independent of whatever the LLM asked for)."""
        r = self.request
        ctx = self.call("get_request_context")
        dept = (ctx.get("requester") or {}).get("department")
        budget = self.call("check_budget", {"department": dept, "annual_cost_usd": r.get("annual_cost_usd")})
        overlap = self.call("find_existing_software", {
            "category": r.get("category"), "vendor_name": r.get("vendor_name"), "product_name": r.get("product_name")})
        history = self.call("get_purchase_history", {"vendor_name": r.get("vendor_name")})
        registry = self.call("lookup_vendor_registry", {"vendor_name": r.get("vendor_name")})
        risk = self.call("get_vendor_risk", {"vendor_name": r.get("vendor_name")})
        return {"context": ctx, "budget": budget, "overlap": overlap, "history": history,
                "registry": registry.get("record"), "risk": risk}

    def evaluate(self, extra_classes: list[str] | None = None) -> policy.PolicyResult:
        """Run (or reuse) the deterministic policy tool and return the rich result object."""
        extra = sorted({c for c in (extra_classes or []) if c in policy.DATA_CLASSES})
        self.call("evaluate_policy", {"extra_data_classes": extra})
        return self._policy[tuple(extra)]


def policy_result_to_dict(res: policy.PolicyResult) -> dict:
    return {
        "required_approvals": res.approvals, "risk_flags": res.flags, "missing_information": res.missing,
        "data_classes": res.data_classes, "baseline_label": res.label,
        "findings": [{"source": f.source, "finding": f.text, "reference": f.reference} for f in res.findings],
    }


# --------------------------------------------------------------------------- LLM-facing tool specs

TOOL_SPECS = [
    {"name": "get_request_context",
     "description": "Look up the requester, their manager and department head, and the request fields. Call first.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "check_budget",
     "description": "Deterministic: compare the annual cost with the requester department's available software budget.",
     "input_schema": {"type": "object", "properties": {"department": {"type": "string"}, "annual_cost_usd": {"type": ["number", "null"]}},
                      "required": ["department"]}},
    {"name": "find_existing_software",
     "description": "Deterministic: search the approved software catalog for the same product, vendor or category.",
     "input_schema": {"type": "object", "properties": {"category": {"type": "string"}, "vendor_name": {"type": "string"},
                                                       "product_name": {"type": "string"}}, "required": ["category", "vendor_name"]}},
    {"name": "get_purchase_history",
     "description": "Deterministic: previous purchases and renewals with a vendor.",
     "input_schema": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]}},
    {"name": "lookup_vendor_registry",
     "description": "Deterministic: internal vendor registry row (procurement/security/legal status, review date).",
     "input_schema": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]}},
    {"name": "get_vendor_risk",
     "description": "External vendor-risk API. May be unavailable or have no record; failures are returned, never hidden.",
     "input_schema": {"type": "object", "properties": {"vendor_name": {"type": "string"}}, "required": ["vendor_name"]}},
    {"name": "evaluate_policy",
     "description": "Deterministic policy engine: approval thresholds, security/privacy/legal triggers, missing information, "
                    "prompt-injection scan. Call after gathering evidence. Optionally pass extra_data_classes you inferred "
                    "from the free-text justification (only add, never remove).",
     "input_schema": {"type": "object", "properties": {
         "request_id": {"type": "string"},
         "extra_data_classes": {"type": "array", "items": {"type": "string", "enum": policy.DATA_CLASSES}}},
         "required": ["request_id"]}},
]
