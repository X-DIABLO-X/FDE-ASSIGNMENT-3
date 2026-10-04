# AI Procurement Request Copilot

FDE Assessment 3 - Harshit Tiwari (roll no. 24BCS10277)

An internal procurement **triage copilot**. For a software/service purchase request it gathers evidence with tools,
applies the procurement policy with deterministic code, uses an LLM only where language understanding is needed, and
hands a structured package (recommendation, evidence, approvals, missing information, risk flags, next step) to the
right **humans**. It never approves, purchases or changes budgets.

Two architectures are implemented on the same tools, rules and guardrails and evaluated on the same 22 cases:

* **A - single agent**: one tool-using agent.
* **B - staged**: Agent 1 (evidence analyst, tools) -> Agent 2 (policy reviewer, no tools).

> Final answer: see [Ship decision](#ship-decision) and [`docs/DECISION_MEMO.md`](docs/DECISION_MEMO.md).

## Quick start (one command)

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env              # Windows: Copy-Item .env.example .env   (add GROQ_API_KEY to enable the LLM agents)
python run_local.py               # mock vendor-risk API :8001 + UI http://127.0.0.1:8501
```

Without any key the whole product still runs in **deterministic mode** (rules + templated text, 0 LLM calls).
With `GROQ_API_KEY` (default model `qwen/qwen3.8-27b`) or `ANTHROPIC_API_KEY` the two agent architectures use the LLM.
`.env` is git-ignored; only `.env.example` is committed.

```bash
python verify_setup.py                                   # preflight (starter checks)
python -m unittest discover -s tests -t .                # 31 unit tests (scripted fake LLM, no key needed)
python evals/run_public_evals.py --architecture single   # starter public runner (6 cases)
python evals/run_public_evals.py --architecture staged
python evals/run_comparison.py                           # 22-case A-vs-B evaluation -> evals/results/
python evals/run_comparison.py --no-llm                  # same, deterministic mode
```

The mock vendor-risk API starts automatically if it is not running (`AUTOSTART_MOCK_API=0` to disable).

## Product workflow

1. Reviewer picks a dataset request (or edits/creates one) and an architecture (A, B, or *compare both*).
2. The copilot gathers evidence (7 tools), runs the policy engine, and returns a `ProcurementDecision`.
3. UI shows **request details -> evidence panel -> recommendation + next step -> approvals (with named approvers),
   missing information, risk flags -> telemetry**.
4. **Human handoff**: reviewer packet download, and an audit log of the human's decision
   (`runtime/human_decisions.jsonl`, git-ignored). Nothing else is triggered.

Recommendation labels (ordered by restrictiveness): *Proceed with standard approvals* < *Review existing tool before new
purchase* < *Route for required reviews before approval* < *Escalate to Security for manual review* < *Request
clarification from requester*.

## Architecture

Diagram, tool table, failure-handling table, assumptions and starter-pack fixes:
**[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**.

Key design rule - *AI interprets, code decides, humans approve*:

* `src/policy.py` - deterministic: approval tiers, budget, 365-day review currency vs. the **policy reference date
  2026-09-30**, security/privacy/legal triggers, overlap, missing information, prompt-injection scan.
* `src/tools.py` - 7 tools (6 deterministic, 1 external API with typed failure envelope).
* `src/agents.py` - architectures A and B + prompts + structured-output tools.
* `src/guardrails.py` - **monotonic merge**: the model can only *add* flags/approvals/missing info/data classes or
  make the label stricter; it can never remove a rule-derived item. Model-written evidence is kept only if it cites a
  tool that was really called and every number in it appears in tool output.
* `src/llm.py` - Groq / Anthropic clients + generic tool-use loop; any provider error falls back to the rules result.

## Assumptions and known limitations

See the assumptions table in `docs/ARCHITECTURE.md`. Main limitations:

* Evaluation set is small (22 cases) and **I wrote both the rules and the gold answers** from the policy text. Public
  starter cases were used during development, so scores on them are not an unbiased estimate; hidden cases may differ.
  One prompt/schema change was made after seeing a live failure (see Evaluation notes) - both architectures got it.
* The LLM is stochastic; the live run is a single repeat (`--repeats` supported) on a free-tier quota.
* Free-text understanding (hidden PII in the justification) depends on the model; rules only see structured fields.
* No utilisation data, so "unused existing capacity" cannot be checked.
* Vendor-risk service is a mock; real services need auth, caching and circuit-breaking.

RESULTS_PLACEHOLDER
