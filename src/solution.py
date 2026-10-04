from __future__ import annotations

from src.agents import run_architecture
from src.contracts import Architecture, ProcurementDecision
from src.data_access import get_request


def analyze_request(request: dict, architecture: Architecture = "single") -> ProcurementDecision:
    """Run an arbitrary request dict (UI free-form input, synthetic eval cases) through an architecture."""
    return run_architecture(request, architecture)


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the public/hidden evaluation harness.

    architecture="single": one tool-using agent (+ deterministic policy engine and guardrails)
    architecture="staged": evidence agent -> policy reviewer agent (same tools, policy engine and guardrails)
    With no LLM key configured both run the deterministic pipeline (see README).
    """
    return analyze_request(get_request(request_id), architecture)
