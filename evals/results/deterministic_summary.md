**Mode:** `deterministic` - 26 cases x 1 repeat(s)

| Metric | A - Single agent | B - Staged (2 agents) |
|---|---:|---:|
| Cases passing ALL gold checks | 23/26 (88%) | 23/26 (88%) |
| Public-case minimum checks | 6/6 | 6/6 |
| Run errors | 0 | 0 |
| Avg latency (ms, excl. quota waits) | 205.4 | 206.7 |
| p95 latency (ms, excl. quota waits) | 556.2 | 417.3 |
| Avg LLM calls / request | 0 | 0 |
| Avg tool calls / request | 7 | 7 |
| Avg tokens / request | 0 | 0 |
| Evidence items citing a called tool (%) | 100.0 | 100.0 |

**Failures**

- A - Single agent: SY-07: label,approvals_exact,flags_required; HO-3: label,approvals_exact,flags_required; HO-4: flags_required
- B - Staged (2 agents): SY-07: label,approvals_exact,flags_required; HO-3: label,approvals_exact,flags_required; HO-4: flags_required
