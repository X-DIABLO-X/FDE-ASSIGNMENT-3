# Recorded model responses (replay evaluation)

**What this is.** The live provider I had access to (Groq free tier, `qwen/qwen3.8-27b`) caps at 200k tokens/day, which
is less than one full A-vs-B run (~300-390k tokens). Instead of reporting a partial run, the model step was performed
once by **Claude (Sonnet 5.5) acting as the language model via Claude Code subagents**, and the answers were recorded
here so the whole evaluation is reproducible without any API key.

**How it was produced**

1. `make_packets.py` exports, per case and per stage, the *exact* inputs a model receives: the system prompt from
   `src/agents.py`, the (untrusted-wrapped) request, and the real tool outputs (as trimmed by `_llm_view`). For stage 2 the
   packet also contains that case's recorded stage-1 brief and the rules result computed from it.
2. A fresh subagent per packet read **only its packet** (no repo access, no gold answers, told not to explore) and wrote
   the JSON the model would submit: `A_<case>.json` (single agent), `B1_<case>.json` (stage-1 evidence brief),
   `B2_<case>.json` (stage-2 decision). 78 files = 26 cases x (1 + 2).
3. `python evals/run_comparison.py --replay` replays them through the **real** agent loops (`run_single`, `run_staged`),
   the real tools, the real rules engine and the real guardrails (`src/llm.py::RecordedLLM`).

**What it does and does not show**

* It exercises the same code path as a live model; prompts, schemas, guardrails and scoring are identical.
* The responder is a strong model and not independent of the author - treat results as an upper-ish bound for a smaller
  model (see the first live Groq run in `evals/results/`, where an earlier, less constrained design scored 11/22).
* **Latency and token columns in replay are NOT measurements** (they time local compute only). LLM-call counts (2 vs 3)
  are structural: they follow from the design (parallel tool round + answer; plus a reviewer stage).
* One recording per case: no estimate of run-to-run variance.

To test with a real model instead: set `GROQ_API_KEY` or `ANTHROPIC_API_KEY` in `.env` and run
`python evals/run_comparison.py --label live` (add `--resume` to continue after a quota stop).
