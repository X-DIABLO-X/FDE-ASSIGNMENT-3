# Architecture decision memo

**Decision: ship Architecture A (single agent) on top of the deterministic policy engine and guardrails. Do not ship the staged 2-agent variant.**

**What was compared.** Both architectures share the same 7 tools, rules engine, guardrails and output schema. A: one tool-using agent. B: an evidence agent, then a policy-reviewer agent. Same 26 cases (10 dataset requests, 12 synthetic edge cases, 4 held-out), gold answers derived by hand from the policy.

**Evidence**

| | Rules only | A single | B staged |
|---|---:|---:|---:|
| Cases passing all gold checks | 23/26 | **26/26** | **26/26** |
| Public starter cases | 6/6 | 6/6 | 6/6 |
| LLM calls / request (by design) | 0 | 2 | 3 |
| Model-claimed overlaps the guardrails had to reject | - | 0 | 6 |
| Evidence items citing a called tool | 100% | 100% | 100% |

* Rules alone fail exactly the three cases that need language understanding (PII or secrets mentioned only in free text; an injection phrased outside the scan patterns). A model closes all three, in A and in B.
* B adds nothing on accuracy but costs 50% more LLM calls. In one live run on a public case, B used 1.5x A's tokens (9.6k vs 6.2k). B also generated six false "existing tool overlap" claims that code had to reject; A generated none.
* An earlier design that let the model add approvals, flags and labels freely scored only 11/22 in a live run (over-escalating low-risk requests). Moving routing decisions into code fixed that; this is the main reliability lesson, and it applies to both architectures.

**Why A.** Every policy-critical output (thresholds, approvals, review triggers, label, human review) is deterministic code, so a second agent has nothing left to review except wording. A's measured live latency (first design, 22 cases) was about 12 s per request (p95 23 s, excluding quota waits) with 2.1 LLM calls on average.

**Limits of this evidence (be sceptical)**

* The 26/26 scores come from recorded answers produced by Claude acting as the model, replayed through the real code. They are not independent of the author, ceiling-limited, and one sample per case. Latency and token figures in replay are not measurements.
* I wrote the rules and the gold answers; the 4 held-out cases were written after the first live run but before the design was narrowed. Hidden cases may differ.
* The only live runs were on a free-tier quota: a 22-case single-agent run of the first design and two live smoke runs of the revised one.

**What would change my mind.** If hidden or production data showed the single agent missing free-text risks that a dedicated reviewer catches (measured with repeated live runs on a larger set), a reviewer stage restricted to *adding* checks would be justified. Until then, extra orchestration is cost without benefit.

**Human controls kept in either case.** The copilot only recommends; `human_review_required` is always true; Security/Privacy/Legal approvals can never be removed by the model; tool failures and conflicting vendor data escalate to Security instead of being resolved silently.
