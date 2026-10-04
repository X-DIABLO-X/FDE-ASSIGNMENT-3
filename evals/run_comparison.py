"""Reproducible A-vs-B evaluation on ONE shared case set (evals/cases.json).

    python evals/run_comparison.py                    # both architectures, uses the LLM if a key is configured
    python evals/run_comparison.py --no-llm           # deterministic pipeline only (0 LLM calls)
    python evals/run_comparison.py --repeats 3        # repeat each case (LLM runs are stochastic)

Writes evals/results/<label>_per_case.csv, <label>_summary.json and <label>_summary.md.
Gold answers in cases.json were derived by hand from data/procurement_policy.md, not from system output.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from evals.run_public_evals import evaluate as public_minimum_checks  # noqa: E402
from src import data_access as da  # noqa: E402
from src.contracts import ProcurementDecision  # noqa: E402
from src.llm import get_llm  # noqa: E402
from src.solution import analyze_request  # noqa: E402
from src.vendor_client import ensure_mock_api  # noqa: E402

RESULTS = ROOT / "evals" / "results"


def _norm_set(items) -> set[str]:
    return {str(i).strip().lower() for i in items}


def score(decision: ProcurementDecision, gold: dict) -> dict[str, bool | None]:
    flags, approvals = _norm_set(decision.risk_flags), _norm_set(decision.required_approvals)
    checks: dict[str, bool | None] = {
        "label": decision.recommendation == gold["label"],
        "approvals_exact": None if "approvals_exact" not in gold else approvals == _norm_set(gold["approvals_exact"]),
        "flags_required": _norm_set(gold.get("flags_required", [])) <= flags,
        "flags_forbidden": not (_norm_set(gold.get("flags_forbidden", [])) & flags),
        "approvals_forbidden": not (_norm_set(gold.get("approvals_forbidden", [])) & approvals),
        "missing_info": None,
        "human_review": decision.human_review_required is True,
        "no_autonomous_approval": "approve" not in decision.recommendation.lower().split("before")[0]
        or "standard approvals" in decision.recommendation.lower(),
    }
    if "missing_groups" in gold:
        texts = [m.lower() for m in decision.missing_information]
        checks["missing_info"] = all(any(tok in t for t in texts for tok in group) for group in gold["missing_groups"])
    return checks


def grounding(decision: ProcurementDecision) -> tuple[int, int]:
    """(grounded, total): an evidence item is grounded if it cites a tool that was really called and a reference."""
    called = set(decision.telemetry.tool_names) if decision.telemetry else set()
    ok = 0
    for e in decision.evidence:
        cites_tool = e.source in called or e.source == "agent_judgement"
        has_ref = bool(e.reference) or e.source == "agent_judgement"
        ok += int(cites_tool and has_ref)
    return ok, len(decision.evidence)


def patched(case: dict) -> ExitStack:
    """Inject adversarial text into business data (cases with a "patch" block)."""
    stack = ExitStack()
    vendors = case.get("patch", {}).get("vendors")
    if vendors:
        original = da.load_vendors

        def load_vendors() -> pd.DataFrame:
            df = original()
            for name, changes in vendors.items():
                for col, val in changes.items():
                    df.loc[df["vendor_name"] == name, col] = val
            return df

        stack.enter_context(mock.patch.object(da, "load_vendors", load_vendors))
    return stack


def run_case(case: dict, architecture: str) -> tuple[ProcurementDecision | None, float, float, str]:
    """Returns (decision, latency_ms, throttled_ms, error). throttled_ms = time spent waiting on the provider's quota."""
    request = case.get("request") or da.get_request(case["request_id"])
    llm = get_llm()
    waited0 = getattr(llm, "throttled_s", 0.0)
    start = time.perf_counter()
    try:
        with patched(case):
            decision = analyze_request(dict(request), architecture)
        err = ""
    except Exception as exc:  # never lose a row
        decision, err = None, f"{type(exc).__name__}: {exc}"
    return decision, (time.perf_counter() - start) * 1000, (getattr(llm, "throttled_s", 0.0) - waited0) * 1000, err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--architectures", nargs="+", default=["single", "staged"], choices=["single", "staged"])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--no-llm", action="store_true", help="force deterministic-only mode")
    ap.add_argument("--cases", nargs="*", help="subset of case ids")
    ap.add_argument("--label", default=None, help="output file prefix (default: llm or deterministic)")
    ap.add_argument("--resume", action="store_true", help="continue a previous run: skip rows already in <label>_rows.jsonl")
    args = ap.parse_args()

    if args.no_llm:
        os.environ["DISABLE_LLM"] = "1"
    llm = get_llm()
    mode = f"llm:{llm.model}" if llm else "deterministic"
    label = args.label or ("llm" if llm else "deterministic")

    ensure_mock_api()
    cases = json.loads((ROOT / "evals" / "cases.json").read_text(encoding="utf-8"))
    if args.cases:
        cases = [c for c in cases if c["case_id"] in args.cases]
    public = {c["request_id"]: c["expectations"]
              for c in json.loads((ROOT / "evals" / "public_cases.json").read_text(encoding="utf-8"))}
    if llm is None:
        run_case(cases[0], "single")  # warm-up (imports, pandas); skipped for LLM runs to save quota

    RESULTS.mkdir(parents=True, exist_ok=True)
    journal = RESULTS / f"{label}_rows.jsonl"  # one JSON row per finished case: a killed run loses nothing
    rows: list[dict] = []
    if args.resume and journal.exists():
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif journal.exists():
        journal.unlink()
    done = {(r["architecture"], r["case_id"], r["repeat"]) for r in rows}
    print(f"\nComparison run - mode={mode} - {len(cases)} cases x {len(args.architectures)} architectures x {args.repeats} repeat(s)\n")
    for arch in args.architectures:
        for case in cases:
            for rep in range(args.repeats):
                if (arch, case["case_id"], rep) in done:
                    continue
                decision, ms, wait_ms, error = run_case(case, arch)
                ms_net = max(0.0, ms - wait_ms)  # latency excluding provider-quota waits
                row = {"architecture": arch, "case_id": case["case_id"], "repeat": rep, "latency_ms": round(ms_net, 1),
                       "latency_incl_quota_wait_ms": round(ms, 1), "error": error}
                if decision is None:
                    row.update(policy_pass=False, failed_checks="ERROR")
                else:
                    checks = score(decision, case["gold"])
                    failed = [k for k, v in checks.items() if v is False]
                    g_ok, g_n = grounding(decision)
                    pub = public.get(case.get("request_id")) if "patch" not in case else None
                    tel = decision.telemetry
                    row.update(
                        policy_pass=not failed, failed_checks=",".join(failed),
                        public_checks_pass=None if pub is None else not public_minimum_checks(decision, pub),
                        recommendation=decision.recommendation, approvals="|".join(decision.required_approvals),
                        flags="|".join(decision.risk_flags), llm_calls=tel.llm_calls, tool_calls=tel.tool_calls,
                        input_tokens=tel.input_tokens or 0, output_tokens=tel.output_tokens or 0,
                        grounded_evidence=g_ok, evidence_items=g_n, ungrounded_dropped=tel.ungrounded_evidence_dropped or 0,
                        mode=tel.mode, notes=" ; ".join(tel.agent_notes))
                rows.append(row)
                with journal.open("a", encoding="utf-8") as jf:
                    jf.write(json.dumps(row, default=str) + "\n")
                print(f"{'PASS' if row['policy_pass'] else 'FAIL'}  {arch:6s} {case['case_id']:8s} {ms_net:8.0f} ms  "
                      f"llm={row.get('llm_calls', '-')} tools={row.get('tool_calls', '-')}  {row['failed_checks']}")

    with (RESULTS / f"{label}_per_case.csv").open("w", newline="", encoding="utf-8") as f:
        fields = list(dict.fromkeys(k for r in rows for k in r))
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    summary = {"mode": mode, "cases": len(cases), "repeats": args.repeats, "architectures": {}}
    for arch in args.architectures:
        rs = [r for r in rows if r["architecture"] == arch]
        ok = [r for r in rs if "recommendation" in r]
        lat = sorted(r["latency_ms"] for r in rs)
        pub = [r for r in rs if r.get("public_checks_pass") is not None]
        summary["architectures"][arch] = {
            "runs": len(rs),
            "policy_pass": sum(r["policy_pass"] for r in rs),
            "policy_pass_rate": round(sum(r["policy_pass"] for r in rs) / len(rs), 3),
            "public_minimum_pass": f"{sum(bool(r['public_checks_pass']) for r in pub)}/{len(pub)}",
            "errors": sum(1 for r in rs if r["error"]),
            "avg_latency_ms": round(statistics.mean(lat), 1),
            "p95_latency_ms": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1),
            "avg_llm_calls": round(statistics.mean(r["llm_calls"] for r in ok), 2) if ok else None,
            "avg_tool_calls": round(statistics.mean(r["tool_calls"] for r in ok), 2) if ok else None,
            "avg_tokens": round(statistics.mean(r["input_tokens"] + r["output_tokens"] for r in ok)) if ok else None,
            "grounded_evidence_pct": round(100 * sum(r["grounded_evidence"] for r in ok) / max(1, sum(r["evidence_items"] for r in ok)), 1),
            "human_review_pct": 100.0 if all(score_ok(r) for r in ok) else None,
            "failures": [f"{r['case_id']}: {r['failed_checks']}" for r in rs if not r["policy_pass"]],
        }
    (RESULTS / f"{label}_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (RESULTS / f"{label}_summary.md").write_text(render_markdown(summary), encoding="utf-8")
    print("\n" + render_markdown(summary))
    print(f"Wrote {RESULTS.relative_to(ROOT)}/{label}_*.{{csv,json,md}}")


def score_ok(row: dict) -> bool:
    return "human_review" not in row.get("failed_checks", "")


def render_markdown(s: dict) -> str:
    archs = list(s["architectures"])
    names = {"single": "A - Single agent", "staged": "B - Staged (2 agents)"}
    lines = [f"**Mode:** `{s['mode']}` - {s['cases']} cases x {s['repeats']} repeat(s)", "",
             "| Metric | " + " | ".join(names[a] for a in archs) + " |", "|---|" + "---:|" * len(archs)]

    def row(label: str, key: str, fmt=lambda v: v) -> None:
        lines.append(f"| {label} | " + " | ".join(str(fmt(s['architectures'][a][key])) for a in archs) + " |")

    row("Policy/gold checks passed", "policy_pass", lambda v: v)
    lines[-1] = "| Cases passing ALL gold checks | " + " | ".join(
        f"{s['architectures'][a]['policy_pass']}/{s['architectures'][a]['runs']} ({100 * s['architectures'][a]['policy_pass_rate']:.0f}%)"
        for a in archs) + " |"
    row("Public-case minimum checks", "public_minimum_pass")
    row("Run errors", "errors")
    row("Avg latency (ms, excl. quota waits)", "avg_latency_ms")
    row("p95 latency (ms, excl. quota waits)", "p95_latency_ms")
    row("Avg LLM calls / request", "avg_llm_calls")
    row("Avg tool calls / request", "avg_tool_calls")
    row("Avg tokens / request", "avg_tokens")
    row("Evidence items citing a called tool (%)", "grounded_evidence_pct")
    lines += ["", "**Failures**", ""]
    for a in archs:
        f = s["architectures"][a]["failures"]
        lines.append(f"- {names[a]}: " + ("none" if not f else "; ".join(f)))
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
