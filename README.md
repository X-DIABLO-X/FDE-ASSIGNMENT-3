# AI Procurement Request Copilot

FDE Assessment 3 - Harshit Tiwari (roll no. 24BCS10277)

An internal procurement **triage copilot**. For a software/service purchase request it gathers evidence with tools,
applies the procurement policy with deterministic code, uses an LLM only where language understanding is needed, and
hands a structured package (recommendation, evidence, approvals, missing information, risk flags, next step) to the
right **humans**. It never approves, purchases or changes budgets.

Two architectures are implemented on the same tools, rules and guardrails and evaluated on the same 26 cases:

* **A - single agent**: one tool-using agent.
* **B - staged**: Agent 1 (evidence analyst, tools) -> Agent 2 (policy reviewer, no tools).

> **Final answer: ship Architecture A (single agent).** See [Ship decision](#ship-decision) and [`docs/DECISION_MEMO.md`](docs/DECISION_MEMO.md).

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
python evals/run_comparison.py --replay                  # 26-case A-vs-B on recorded model answers (no key)
python evals/run_comparison.py --no-llm                  # same cases, rules only
python evals/run_comparison.py --label live              # same cases with your own LLM key
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

## Assumptions

See the assumptions table in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (reference date 2026-09-30, department-head resolution, region and overlap assumptions) and the known limitations at the end of this file.

## Evaluation

Reproduce: `python evals/run_comparison.py --no-llm` (rules only), `python evals/run_comparison.py --replay` (recorded model
answers, no key needed), `python evals/run_comparison.py --label live` (your own `GROQ_API_KEY`/`ANTHROPIC_API_KEY`).
Same 26 cases for every run: the 10 dataset requests, 12 synthetic edge cases (boundaries at $1,000 / $25,000, unknown vendor,
injection in request and in vendor-registry data, hidden PII, budget, duplicate tool, outage) and 4 held-out cases written
after the first live run and not tuned against. Gold answers (exact approval set, required/forbidden flags, label) were derived
by hand from `data/procurement_policy.md`. Per-case rows: `evals/results/*_per_case.csv`.

| Metric | Rules only | **A - single agent** | **B - staged (2 agents)** |
|---|---:|---:|---:|
| Cases passing **all** gold checks | 23/26 (88%) | **26/26 (100%)** | **26/26 (100%)** |
| Starter public cases (minimum checks) | 6/6 | 6/6 | 6/6 |
| LLM calls / request (by design) | 0 | 2 | 3 |
| Tool calls / request | 7 | 7 | 7 |
| Evidence items citing a tool that was called | 100% | 100% | 100% |
| Model overlap claims rejected by guardrails | - | 0 | 6 |
| Model labels overridden by rules | - | 2 (stale baseline in packet) | 0 |
| `human_review_required` always true | yes | yes | yes |

**How to read this honestly**

* The A/B columns are a **replay of recorded model answers** (Claude acting as the model; see `evals/recorded/README.md`),
  because the free-tier live quota (200k tokens/day) cannot fit a full run. They exercise the real tools, rules and guardrails.
  **Latency and token columns in replay are not measurements** and are omitted here.
* **Measured live data I do have** (Groq `qwen/qwen3.8-27b`): a 22-case single-agent run of the *first, less constrained design*
  scored **11/22**, avg **12.5 s** per request (p95 22.6 s, quota waits excluded), 2.09 LLM calls, 7.3 tool calls
  (`evals/results/v1_single_agent_live_run.log`). Its failures were the model adding flags/approvals/labels the policy did not
  support (over-escalating low-risk requests). That is why routing is now rule-derived. One live smoke run of each architecture
  on REQ-1004 (first design) used 6.2k tokens (A, 2 calls) vs 9.6k tokens (B, 3 calls). The revised design was **not** re-run live.
* The second half of that first live run (staged) was discarded: quota exhaustion made calls fall back to rules, so it was not a fair measurement.
* Rules alone fail exactly three cases (`SY-07`, `HO-3`: PII/secrets only in free text; `HO-4`: injection phrased outside the scan
  patterns). The model closes all three in both architectures.
* Scores are at the ceiling, one recording per case, from a strong model, with rules and gold written by the same author.
  They show the *design* works and that B adds no accuracy; they do not predict hidden-case performance or run-to-run variance.

## Architecture comparison and ship decision

| | A single agent | B staged |
|---|---|---|
| Accuracy on 26 cases | 26/26 | 26/26 |
| LLM calls | 2 | 3 (+50%) |
| Tokens (one live case, first design) | 6.2k | 9.6k (+55%) |
| False overlap claims to be cleaned up by code | 0 | 6 |
| Moving parts / failure points | 1 agent loop | 2 loops + hand-off schema |

### Ship decision

**Ship A.** All policy-critical outputs are deterministic code, so the second agent has nothing left to review except wording; it
added cost and extra false claims without improving a single case. Full reasoning, limits and what would change the decision:
[`docs/DECISION_MEMO.md`](docs/DECISION_MEMO.md) (<= 500 words).

## Repository map

```text
src/policy.py        deterministic rules engine          src/tools.py       7 tools + LLM tool specs
src/agents.py        architectures A and B, prompts      src/guardrails.py  monotonic merge, grounding, label from rules
src/llm.py           Groq / Anthropic / recorded replay  src/vendor_client.py  typed failure envelope + embedded mock API
app.py               Streamlit product UI                evals/             cases, runners, results, recorded answers
docs/ARCHITECTURE.md diagram, tools, assumptions, fixes  docs/DECISION_MEMO.md  500-word memo
```

## Known limitations

* Evaluation evidence is recorded-model replay plus partial live data (see above); no variance estimate; small, author-written set.
* Free-text understanding depends on the model; the rules only see structured fields.
* "Operating region", utilisation data and legal-terms nuance are not in the data (assumptions A4, A7 in `docs/ARCHITECTURE.md`).
* The vendor-risk service is a mock; production needs auth, caching and circuit breaking.
* Streamlit UI is single-user; the human decision log is a local JSONL file, not a workflow system.
