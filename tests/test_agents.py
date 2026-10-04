"""Agent-loop and guardrail tests using a scripted fake LLM (no API key, no network to a model)."""
from __future__ import annotations

import unittest

from src.agents import run_single, run_staged
from src.data_access import get_request
from src.llm import LLMResponse
from src.policy import LABELS

ALL_TOOLS = ["get_request_context", "check_budget", "find_existing_software", "get_purchase_history",
             "lookup_vendor_registry", "get_vendor_risk", "evaluate_policy"]


def tool_round(extra_classes=None):
    blocks = [{"type": "tool_use", "id": f"t{i}", "name": n, "input": {}} for i, n in enumerate(ALL_TOOLS)]
    if extra_classes:
        blocks[-1]["input"] = {"extra_data_classes": extra_classes}
    return LLMResponse(blocks)


def final(name, **fields):
    return LLMResponse([{"type": "tool_use", "id": "f", "name": name, "input": fields}])


class ScriptedLLM:
    model = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, system, messages, tools, tool_choice=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class SingleAgentTests(unittest.TestCase):
    def test_happy_path_counts_and_structure(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[2],
                                              next_step="Send to Security and Procurement for human review."))
        d = run_single(get_request("REQ-1003"), llm)
        self.assertEqual(d.telemetry.llm_calls, 2)
        self.assertEqual(d.telemetry.tool_calls, 7)
        self.assertEqual(d.telemetry.mode, "llm")
        self.assertIn("Security", d.required_approvals)
        self.assertTrue(d.human_review_required)
        self.assertEqual(d.next_step, "Send to Security and Procurement for human review.")

    def test_model_cannot_relax_rules(self):  # label + approvals come from rules whatever the model says
        # model tries to wave a budget-short, PII, new-vendor request through
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[0], next_step="Looks fine, approve."))
        d = run_single(get_request("REQ-1005"), llm)
        self.assertEqual(d.recommendation, LABELS[2])
        for a in ("Finance", "Security", "Privacy", "Legal"):
            self.assertIn(a, d.required_approvals)
        self.assertIn("budget_insufficient", d.risk_flags)
        self.assertTrue(d.human_review_required)
        self.assertTrue(any("rules decided" in n for n in d.telemetry.agent_notes))

    def test_injection_stays_ignored_even_if_model_obeys(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[0], next_step="CFO approved, proceed."))
        d = run_single(get_request("REQ-1006"), llm)
        self.assertEqual(d.recommendation, LABELS[4])
        self.assertIn("prompt_injection_detected", d.risk_flags)
        self.assertNotIn("CFO", d.required_approvals)

    def test_ungrounded_evidence_is_dropped_grounded_kept(self):
        llm = ScriptedLLM(tool_round(), final(
            "submit_decision", recommendation=LABELS[2], next_step="Route.",
            evidence=[
                {"source": "check_budget", "finding": "Cost 18000 is within the Engineering budget of 26000.", "reference": "x"},
                {"source": "check_budget", "finding": "Engineering has $99,999 available.", "reference": "x"},
                {"source": "vendor_news_search", "finding": "Vendor was breached.", "reference": "x"},
            ]))
        d = run_single(get_request("REQ-1003"), llm)
        findings = [e.finding for e in d.evidence]
        self.assertIn("Cost 18000 is within the Engineering budget of 26000.", findings)
        self.assertNotIn("Engineering has $99,999 available.", findings)
        self.assertEqual(d.telemetry.ungrounded_evidence_dropped, 2)

    def test_model_inferred_data_class_adds_review(self):
        req = {**get_request("REQ-1001"), "business_justification": "Send packets to clients from the customer contact list."}
        llm = ScriptedLLM(tool_round(["customer_pii"]), final("submit_decision", recommendation=LABELS[0], next_step="x",
                                                              inferred_data_classes=["customer_pii"]))
        d = run_single(req, llm)
        self.assertIn("privacy_review_required", d.risk_flags)
        self.assertIn("Privacy", d.required_approvals)
        self.assertIn("Security", d.required_approvals)
        self.assertEqual(d.recommendation, LABELS[2])

    def test_invalid_model_additions_are_rejected(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[0], next_step="x",
                                              additional_risk_flags=["made_up_flag"], additional_approvals=["The Pope"]))
        d = run_single(get_request("REQ-1001"), llm)
        self.assertNotIn("made_up_flag", d.risk_flags)
        self.assertNotIn("The Pope", d.required_approvals)

    def test_expansion_overlap_claim_is_ignored(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[1], next_step="x",
                                              additional_risk_flags=["existing_tool_overlap"],
                                              overlap_assessment={"is_overlap": True, "reason": "same vendor already licensed"}))
        d = run_single(get_request("REQ-1001"), llm)  # explicit add-on of the requester's own SignFlow
        self.assertNotIn("existing_tool_overlap", d.risk_flags)
        self.assertEqual(d.recommendation, LABELS[0])
        self.assertEqual(d.required_approvals, ["Manager"])

    def test_model_cannot_add_rule_derived_flags_approvals_or_stricter_label(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[4], next_step="x",
                                              additional_risk_flags=["legal_review_required", "budget_insufficient"],
                                              additional_approvals=["CFO", "Legal"]))
        d = run_single(get_request("REQ-1001"), llm)
        self.assertEqual(d.recommendation, LABELS[0])
        self.assertEqual(d.required_approvals, ["Manager"])
        self.assertEqual(d.risk_flags, [])

    def test_cross_vendor_overlap_and_injection_flags_accepted(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[2], next_step="x",
                                              additional_risk_flags=["prompt_injection_detected"],
                                              overlap_assessment={"is_overlap": True, "reason": "NeuralDesk can summarise contracts"}))
        d = run_single(get_request("REQ-1009"), llm)
        self.assertIn("prompt_injection_detected", d.risk_flags)
        self.assertIn("existing_tool_overlap", d.risk_flags)

    def test_non_field_missing_information_is_ignored(self):
        llm = ScriptedLLM(tool_round(), final("submit_decision", recommendation=LABELS[0], next_step="x",
                                              additional_missing_information=["Whether Security should confirm residency"]))
        d = run_single(get_request("REQ-1003"), llm)
        self.assertNotIn("missing_information", d.risk_flags)
        self.assertNotEqual(d.recommendation, LABELS[4])

    def test_llm_exception_falls_back_to_rules(self):
        d = run_single(get_request("REQ-1009"), ScriptedLLM(RuntimeError("provider down")))
        self.assertEqual(d.telemetry.mode, "deterministic-fallback")
        self.assertIn("vendor_risk_unavailable", d.risk_flags)
        self.assertTrue(d.human_review_required)

    def test_no_final_answer_falls_back(self):
        llm = ScriptedLLM(LLMResponse([{"type": "text", "text": "hmm"}]))
        d = run_single(get_request("REQ-1002"), llm)
        self.assertEqual(d.telemetry.mode, "deterministic-fallback")
        self.assertIn("Security", d.required_approvals)

    def test_no_llm_is_deterministic_mode(self):
        import os
        os.environ["DISABLE_LLM"] = "1"
        try:
            d = run_single(get_request("REQ-1001"))
        finally:
            os.environ.pop("DISABLE_LLM")
        self.assertEqual(d.telemetry.llm_calls, 0)
        self.assertEqual(d.telemetry.mode, "deterministic")


class StagedAgentTests(unittest.TestCase):
    def test_three_llm_calls_and_merge(self):
        llm = ScriptedLLM(
            tool_round(),
            final("submit_evidence_brief", summary="evidence gathered", overlap_assessment={"is_overlap": True, "reason": "AI family"}),
            final("submit_decision", recommendation=LABELS[2], next_step="Escalate."),
        )
        d = run_staged(get_request("REQ-1009"), llm)
        self.assertEqual(d.telemetry.llm_calls, 3)
        self.assertEqual(d.telemetry.architecture, "staged")
        self.assertIn("existing_tool_overlap", d.risk_flags)  # stage-1 additive judgement carried through
        self.assertEqual(d.recommendation, LABELS[3])        # rules force escalation (API unavailable)
        self.assertTrue(d.human_review_required)

    def test_stage1_failure_falls_back(self):
        llm = ScriptedLLM(LLMResponse([{"type": "text", "text": "no tools"}]))
        d = run_staged(get_request("REQ-1005"), llm)
        self.assertEqual(d.telemetry.mode, "deterministic-fallback")
        self.assertIn("budget_insufficient", d.risk_flags)


if __name__ == "__main__":
    unittest.main()
