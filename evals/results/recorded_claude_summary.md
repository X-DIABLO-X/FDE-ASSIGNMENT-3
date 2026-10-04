**Mode:** `llm:recorded` - 26 cases x 1 repeat(s)

| Metric | A - Single agent | B - Staged (2 agents) |
|---|---:|---:|
| Cases passing ALL gold checks | 26/26 (100%) | 26/26 (100%) |
| Public-case minimum checks | 6/6 | 6/6 |
| Run errors | 0 | 0 |
| Avg latency (ms, excl. quota waits) | 192.8 | 252.0 |
| p95 latency (ms, excl. quota waits) | 480.7 | 674.5 |
| Avg LLM calls / request | 2 | 3 |
| Avg tool calls / request | 7 | 7 |
| Avg tokens / request | 0 | 0 |
| Evidence items citing a called tool (%) | 100.0 | 100.0 |

**Failures**

- A - Single agent: none
- B - Staged (2 agents): none
