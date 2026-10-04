"""Deterministic procurement policy engine.

Everything in this module is plain code: thresholds, date arithmetic, review triggers, injection
detection. It never calls an LLM. The agents may *add* judgement on top of its output (see
``guardrails.py``) but can never remove an approval, flag or missing-information item it produced.

Policy references follow ``data/procurement_policy.md`` (version 2026.09).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# Policy / data snapshot reference date (data/README.md + policy header). Never use date.today().
REFERENCE_DATE = date(2026, 9, 30)
REVIEW_VALIDITY_DAYS = 365

# Canonical approval names (contracts.py) in a stable display order.
APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

# Allowed risk-flag taxonomy (contracts.py). Agents may only add flags from this set.
ALLOWED_FLAGS = [
    "existing_tool_overlap",
    "budget_insufficient",
    "security_review_required",
    "privacy_review_required",
    "legal_review_required",
    "vendor_review_expired",
    "conflicting_vendor_evidence",
    "vendor_risk_unavailable",
    "prompt_injection_detected",
    "missing_information",
]

# Recommendation labels, ordered from least to most restrictive. A model may pick a label, but the
# final label is never less restrictive than the deterministic one.
LABELS = [
    "Proceed with standard approvals",
    "Review existing tool before new purchase",
    "Route for required reviews before approval",
    "Escalate to Security for manual review",
    "Request clarification from requester",
]

DATA_CLASSES = [
    "source_code",
    "confidential_documents",
    "employee_pii",
    "customer_pii",
    "credentials_secrets",
    "production_access",
]
SECURITY_CLASSES = set(DATA_CLASSES)
PRIVACY_CLASSES = {"employee_pii", "customer_pii"}
RESIDENCY_CLASSES = {"source_code", "confidential_documents", "employee_pii", "customer_pii", "credentials_secrets"}


@dataclass
class Finding:
    source: str
    text: str
    reference: str | None = None


@dataclass
class PolicyResult:
    approvals: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    data_classes: list[str] = field(default_factory=list)
    label: str = LABELS[0]
    reasons: list[str] = field(default_factory=list)

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)

    def add_approval(self, name: str) -> None:
        if name not in self.approvals:
            self.approvals.append(name)

    def note(self, text: str, reference: str | None = None, source: str = "evaluate_policy") -> None:
        self.findings.append(Finding(source, text, reference))


# --------------------------------------------------------------------------- money / thresholds

def approval_tier(amount: float) -> list[str]:
    """Policy section 4 - minimum business approvals for an annualised amount."""
    if amount <= 1000:
        return ["Manager"]
    if amount <= 10000:
        return ["Department Head", "Procurement"]
    if amount <= 25000:
        return ["Department Head", "Finance", "Procurement"]
    return ["Department Head", "Finance", "CFO", "Procurement"]


def tier_label(amount: float) -> str:
    if amount <= 1000:
        return "up to $1,000"
    if amount <= 10000:
        return "$1,000.01 - $10,000"
    if amount <= 25000:
        return "$10,000.01 - $25,000"
    return "above $25,000"


def money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


# --------------------------------------------------------------------------- data classification

def classify_data(request: dict) -> tuple[list[str], str]:
    """Map the free-form ``data_access_level`` + integrations onto policy data classes.

    Returns (classes, declared) where declared is 'unknown', 'none' or 'declared'.
    """
    level = str(request.get("data_access_level") or "").strip().lower()
    integrations = " ".join(str(i) for i in (request.get("requested_integrations") or [])).lower()
    text = f"{level} {integrations}"
    classes: set[str] = set()

    if re.search(r"source|\bcode\b|repositor|\bgit(hub|lab)?\b", text):
        classes.add("source_code")
    if "confidential" in text:
        classes.add("confidential_documents")
    if re.search(r"employee|\bhr\b|payroll", text) and re.search(r"pii|personal|record|data", text):
        classes.add("employee_pii")
    if re.search(r"customer|client|\bpii\b|personal", text):
        classes.add("customer_pii")  # bare "pii" is treated conservatively as customer PII
    if re.search(r"secret|credential|password|api[ _-]?key|token", text):
        classes.add("credentials_secrets")
    if re.search(r"production|\bprod\b|cloud[ _-]?account|\baws\b|\bgcp\b|\bazure\b", text):
        classes.add("production_access")

    if level in ("", "unknown", "n/a", "tbd", "not_sure") or request.get("data_access_level") is None:
        declared = "unknown"
    elif level in ("none", "no_data", "no data"):
        declared = "none"
    else:
        declared = "declared"
    return [c for c in DATA_CLASSES if c in classes], declared


# --------------------------------------------------------------------------- prompt injection

_INJECTION_PATTERNS = [
    r"ignore\b.{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|guidelines?)",
    r"disregard\b.{0,40}\b(rules?|instructions?|polic(y|ies)|previous)",
    r"(bypass|skip|override|circumvent|waive)\b.{0,40}\b(rules?|polic(y|ies)|controls?|review|approvals?|security|checks?)",
    r"treat\b.{0,30}\b(as|it as)\b.{0,30}\b(approved|cfo|pre-?approved|authori[sz]ed)",
    r"(cfo|ceo|manager|security|legal|finance)[- ]approved",
    r"already\s+(been\s+)?(approved|authori[sz]ed|cleared)",
    r"\bapprove\b.{0,25}\b(immediately|now|automatically|without)",
    r"(system|developer)\s*(prompt|message|instruction)",
    r"you\s+are\s+now\b|act\s+as\b|new\s+instructions?",
    r"(reveal|print|show|expose|leak)\b.{0,30}\b(secret|api[ _-]?key|password|credential|prompt)",
    r"do\s+not\s+(flag|mention|escalate|require)",
    r"mark\b.{0,30}\b(low[- ]risk|safe|approved|compliant)",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE | re.DOTALL)


def detect_injection(fields: dict[str, object]) -> list[dict]:
    """Scan untrusted business-data strings for instruction-like text (Policy section 9)."""
    hits = []
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            continue
        for sentence in re.split(r"(?<=[.!?\n])\s+", value):
            if _INJECTION_RE.search(sentence):
                hits.append({"field": name, "excerpt": sentence.strip()[:160]})
    return hits


def strip_injection(text: str) -> str:
    return " ".join(s for s in re.split(r"(?<=[.!?\n])\s+", text) if not _INJECTION_RE.search(s)).strip()


# --------------------------------------------------------------------------- vendor assessment

def _parse_date(value: object) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def review_age_days(review_date: date | None, ref: date = REFERENCE_DATE) -> int | None:
    return None if review_date is None else (ref - review_date).days


def review_is_current(review_date: date | None, ref: date = REFERENCE_DATE) -> bool:
    age = review_age_days(review_date, ref)
    return age is not None and 0 <= age <= REVIEW_VALIDITY_DAYS


def _bucket(status: str | None) -> str:
    s = (status or "").strip().lower()
    if s == "approved":
        return "approved"
    if s == "expired":
        return "expired"
    return "not_approved"  # pending / not_completed / unknown / empty


def assess_vendor(registry: dict | None, risk: dict) -> dict:
    """Combine internal registry + external risk service into one explicit view.

    Never silently picks one source: conflicts and failures are surfaced as flags.
    """
    flags: list[str] = []
    notes: list[str] = []
    reg_status = _bucket(registry.get("security_status")) if registry else "not_approved"
    reg_date = _parse_date(registry.get("security_review_date")) if registry else None
    reg_current = reg_status == "approved" and review_is_current(reg_date)

    api_ok = risk.get("status") == "ok"
    api = risk.get("data") or {}
    api_status = _bucket(api.get("security_review_status")) if api_ok else None
    api_date = _parse_date(api.get("last_review_date")) if api_ok else None
    api_current = api_ok and api_status == "approved" and review_is_current(api_date)

    if not api_ok:
        flags.append("vendor_risk_unavailable")
        why = risk.get("error") or risk.get("status")
        notes.append(f"Vendor-risk service could not be verified ({why}); no favourable status is assumed.")
    else:
        if registry and (reg_status != api_status or (reg_date and api_date and reg_date != api_date)):
            flags.append("conflicting_vendor_evidence")
            notes.append(
                f"Registry says '{registry.get('security_status')}' (reviewed {registry.get('security_review_date') or 'n/a'}) "
                f"but vendor-risk service says '{api.get('security_review_status')}' (reviewed {api.get('last_review_date') or 'n/a'})."
            )

    expired_sources = []
    if registry and reg_date and not review_is_current(reg_date):
        expired_sources.append(f"registry review {reg_date} is {review_age_days(reg_date)} days old")
    if api_ok and api_date and not review_is_current(api_date):
        expired_sources.append(f"risk-service review {api_date} is {review_age_days(api_date)} days old")
    if api_ok and api_status == "expired":
        expired_sources.append("risk service marks the review 'expired'")
    if registry and reg_status == "expired":
        expired_sources.append("registry marks the review 'expired'")
    if expired_sources:
        flags.append("vendor_review_expired")
        notes.append("Security assessment older than 365 days: " + "; ".join(expired_sources) + ".")

    security_current = bool(registry) and reg_current and api_current
    if not security_current:
        flags.append("security_review_required")
        if not expired_sources and api_ok:
            notes.append(
                f"Vendor security assessment is not current (registry '{(registry or {}).get('security_status', 'no record')}', "
                f"risk service '{api.get('security_review_status')}')."
            )
    return {"flags": flags, "notes": notes, "security_current": security_current}


# --------------------------------------------------------------------------- overlap

_EXPANSION_RE = re.compile(r"add-?on|expan|additional|extra|more (seats|licen|users)|extend|renewal|upgrade seats", re.I)


def is_expansion(request: dict) -> bool:
    text = f"{request.get('product_name', '')} {request.get('business_justification', '')}"
    return bool(_EXPANSION_RE.search(strip_injection(text)))


def overlap_flag(request: dict, overlap: dict) -> tuple[bool, list[str]]:
    """Decide ``existing_tool_overlap`` from catalog candidates (Policy section 3)."""
    reasons = []
    expansion = is_expansion(request)
    for m in overlap.get("matches", []):
        types = set(m["match_types"])
        if "same_category" in types and "same_vendor" not in types:
            reasons.append(f"{m['product_name']} ({m['vendor_name']}) already covers category '{m['category']}'")
        elif {"same_vendor", "same_category"} <= types and m.get("covers_requester_department") and not expansion:
            reasons.append(
                f"{m['product_name']} ({m['scope']}, {m['licensed_seats']} seats) already covers the requester's department"
            )
    return bool(reasons), reasons


# --------------------------------------------------------------------------- main evaluation

def evaluate(request: dict, evidence: dict, extra_classes: list[str] | None = None,
             extra_injection: list[dict] | None = None) -> PolicyResult:
    """Run every deterministic check over request + gathered tool evidence."""
    res = PolicyResult()
    ctx = evidence["context"]
    budget = evidence["budget"]
    overlap = evidence["overlap"]
    registry = evidence["registry"]
    risk = evidence["risk"]
    cost = request.get("annual_cost_usd")

    # ---- section 1: required information
    missing = res.missing
    if not request.get("requester_id") or not ctx.get("requester"):
        missing.append("requester and department")
    if not request.get("product_name") or not request.get("vendor_name"):
        missing.append("product/vendor")
    if cost is None:
        missing.append("annual cost (or reasonable annual estimate)")
    if request.get("user_count") is None:
        missing.append("number of users/licenses")
    justification = request.get("business_justification") or ""
    if len(strip_injection(justification).split()) < 5:
        missing.append("business purpose (stated purpose is absent or only contains instructions)")
    classes, declared = classify_data(request)
    if declared == "unknown":
        missing.append("intended data-access level")
    if request.get("requested_integrations") is None:
        missing.append("required integrations")
    if missing:
        res.add_flag("missing_information")
        res.note("Required request information is missing: " + "; ".join(missing), "Policy section 1")

    # ---- section 9: untrusted content
    scan = {
        "product_name": request.get("product_name"),
        "business_justification": justification,
        "vendor_name": request.get("vendor_name"),
    }
    if registry:
        scan["registry.notes"] = registry.get("notes")
    if risk.get("status") == "ok":
        scan["vendor_risk.notes"] = (risk.get("data") or {}).get("notes")
    for m in overlap.get("matches", []):
        scan[f"catalog.{m['software_id']}.notes"] = m.get("notes")
    hits = detect_injection(scan) + list(extra_injection or [])
    if hits:
        res.add_flag("prompt_injection_detected")
        res.note(
            "Instruction-like text found in business data and ignored: "
            + "; ".join(f"{h['field']}: \"{h['excerpt']}\"" for h in hits[:3]),
            "Policy section 9",
        )

    # ---- data classes (declared + any extra the agent inferred from free text; additive only)
    all_classes = [c for c in DATA_CLASSES if c in set(classes) | set(extra_classes or [])]
    res.data_classes = all_classes

    # ---- section 4: financial thresholds
    if cost is not None:
        for name in approval_tier(float(cost)):
            res.add_approval(name)
        res.note(
            f"Annual cost {money(cost)} falls in '{tier_label(float(cost))}' -> minimum approvals: "
            + ", ".join(approval_tier(float(cost))),
            "Policy section 4",
        )
    else:
        res.add_approval("Manager")
        res.add_approval("Procurement")
        res.note("Approval tier cannot be finalised without an annual cost; Manager and Procurement shown as the floor.",
                 "Policy section 4")

    # ---- section 2: budget
    if cost is not None and budget.get("available_usd") is not None:
        if float(cost) > budget["available_usd"]:
            res.add_flag("budget_insufficient")
            res.add_approval("Finance")
            short = float(cost) - budget["available_usd"]
            res.note(f"Cost {money(cost)} exceeds {budget['department']} available budget "
                     f"{money(budget['available_usd'])} by {money(short)}; route to Finance for budget exception.",
                     "Policy section 2")
    elif cost is not None:
        res.note("Department budget could not be determined; budget check not completed.", "Policy section 2")

    # ---- section 3: overlap
    flagged, reasons = overlap_flag(request, overlap)
    if flagged:
        res.add_flag("existing_tool_overlap")
        res.add_approval("Procurement")
        res.note("Existing tool overlap: " + "; ".join(reasons), "Policy section 3")

    # ---- section 5: security
    vendor = assess_vendor(registry, risk)
    for f in vendor["flags"]:
        res.add_flag(f)
    for n in vendor["notes"]:
        res.note(n, "Policy section 5/10")
    security_reasons = [c for c in all_classes if c in SECURITY_CLASSES]
    if security_reasons:
        res.add_flag("security_review_required")
        res.note("Security review required for data/access class: " + ", ".join(security_reasons), "Policy section 5")
    if "security_review_required" in res.flags:
        res.add_approval("Security")

    # ---- section 6: privacy
    api = risk.get("data") or {}
    outside_region = bool(api.get("stores_data_outside_region")) if risk.get("status") == "ok" else False
    residency_issue = outside_region and any(c in RESIDENCY_CLASSES for c in all_classes)
    if any(c in PRIVACY_CLASSES for c in all_classes) or residency_issue:
        res.add_flag("privacy_review_required")
        res.add_approval("Privacy")
        why = "employee/customer PII" if any(c in PRIVACY_CLASSES for c in all_classes) else "sensitive data stored outside region"
        res.note(f"Privacy review required: {why}", "Policy section 6")

    # ---- section 7: legal
    new_vendor = (not registry) or (registry.get("procurement_status", "").strip().lower() != "approved")
    terms = (registry or {}).get("legal_terms_status", "")
    terms_ok = terms.strip().lower() in ("approved", "standard")
    legal_reasons = []
    if new_vendor and cost is not None and float(cost) >= 10000:
        legal_reasons.append(f"new vendor with annual spend {money(cost)} (>= $10,000)")
    if not terms_ok:
        legal_reasons.append(f"legal terms status is '{terms or 'no registry record'}', not approved/standard")
    if residency_issue:
        legal_reasons.append("sensitive data stored outside the operating region (cross-region issue)")
    if legal_reasons:
        res.add_flag("legal_review_required")
        res.add_approval("Legal")
        res.note("Legal review required: " + "; ".join(legal_reasons), "Policy section 7")

    # ---- section 8: AI tools with limited prior approval
    limited = [m for m in overlap.get("matches", [])
               if "same_vendor" in m["match_types"] and "limited" in str(m["status"]).lower()]
    if limited and security_reasons:
        res.note(f"{limited[0]['product_name']} is only '{limited[0]['status']}'; the prior purchase does not authorise "
                 f"{', '.join(security_reasons)}.", "Policy section 8")

    res.approvals = [a for a in APPROVAL_ORDER if a in res.approvals]
    res.label = deterministic_label(res)
    return res


def deterministic_label(res: PolicyResult) -> str:
    f = set(res.flags)
    if "missing_information" in f:
        return LABELS[4]
    if f & {"vendor_risk_unavailable", "conflicting_vendor_evidence"}:
        return LABELS[3]
    if f & {"security_review_required", "privacy_review_required", "legal_review_required", "budget_insufficient",
            "vendor_review_expired"}:
        return LABELS[2]
    if "existing_tool_overlap" in f:
        return LABELS[1]
    return LABELS[0]


def most_restrictive(*labels: str | None) -> str:
    valid = [l for l in labels if l in LABELS]
    return max(valid, key=LABELS.index) if valid else LABELS[0]
