---
name: verifying-langfuse-traces
description: Use when Langfuse tracing has just been added or changed and needs proof it works, or when traces are missing, empty, split into several traces, have no tokens or $0 cost, show unknown_service, lack tool calls, or when the app slows down or fails while Langfuse is down.
---

# Verifying Langfuse traces

## Overview

"I saw a trace in the UI" is not verification. Tracing works only when **every**
checklist item below passes on **real** traffic, and you have shown the user the
evidence. Report each item as PASS/FAIL with its output. Don't summarize it as "looks
good."

## Definition of done

Run one real request (not the preflight trace), then check each item:

| # | Check | How to prove it |
|---|---|---|
| 1 | Credentials and ingestion work | `python3 ../setting-up-langfuse-tracing/scripts/check_langfuse.py` passes |
| 2 | One request = **one** trace, with every model hop and tool call nested | `audit_traces.py --tree` shows one root and children. The UI shows a tree, not siblings |
| 3 | Tokens and **non-zero cost** on each generation | audit: "token usage", "cost" PASS |
| 4 | `user_id`, `session_id`, and tags on the root | audit: "user_id", "session_id" PASS |
| 5 | Tool executions appear as TOOL observations | audit: "tool calls observed" PASS |
| 6 | Real service name, not `unknown_service` | audit: "service name set" PASS |
| 7 | **Inert without keys**: unset `LANGFUSE_PUBLIC_KEY` and the test suite still passes | run the tests |
| 8 | **Survives an outage**: with Langfuse stopped (or `LANGFUSE_HOST` pointed at a dead port), a request still succeeds and latency is unchanged | time one request with and without Langfuse |
| 9 | **No cross-talk**: two concurrent requests from different users produce two separate traces | fire both at once, then run `--tree` |
| 10 | Secrets hygiene: keys are in a gitignored env file, and the UI and ClickHouse are not public | `git check-ignore <envfile>`, check exposed ports |

Items 7–9 are the ones people skip, and they're the ones that take an app down.

## Tools

```bash
# Items 2–6, graded automatically (Langfuse v4 / self-hosted; reads ClickHouse)
CLICKHOUSE_PASSWORD=… python3 scripts/audit_traces.py --service my-service
CLICKHOUSE_PASSWORD=… python3 scripts/audit_traces.py --service my-service --tree
```

On **Langfuse Cloud**, or when ClickHouse isn't reachable, verify items 2–6 in the UI.
Open the trace URL, confirm the tree shape, and check the Tokens and Cost columns plus
the User and Session fields. On pre-v4 servers `GET /api/public/traces/{id}` also works.

## No traces: diagnose in this order

| Symptom | Cause | Fix |
|---|---|---|
| Nothing ever arrives from a script or CLI | The process exits before the background exporter sends | `get_client().flush()` before exit |
| Nothing arrives, no errors | `LANGFUSE_PUBLIC_KEY` isn't set *in the process* (compose file doesn't pass it through, wrong env file) | `docker exec <c> env \| grep LANGFUSE` |
| Log shows `tracing disabled: ModuleNotFoundError: langchain` | `langfuse.langchain` imports the full `langchain` package | add `langchain>=1.0` |
| Connection refused or timeout | Wrong host: a container DNS name used from another host, a missing port, or a security group | run `check_langfuse.py` from **inside** the app container |
| 401 | Keys from another project or region (EU vs US) | re-copy the keys from the right project |
| "not available … events_only mode" | v2/v3 SDK or API against a v4 server | `langfuse>=4`. Read via UI or ClickHouse |
| Traces reach the server but the UI is empty | Worker isn't processing the queue | `docker compose logs langfuse-worker` |

## Wrong shape

| Symptom | Cause | Fix |
|---|---|---|
| Model calls are separate traces, not children | No parent span, or the callback isn't passed (`config=` missing) | Open the turn span first, then pass `config={"callbacks": [handler]}` |
| Two users' calls interleaved in one trace | Context shared across requests (global span, thread pool without context copy) | Keep spans in context vars. Use `contextvars.copy_context()` for thread hops |
| Gap between two generations | Tools run as plain functions | Wrap them with an `as_type="tool"` observation |
| Tokens present, cost $0 | Model name isn't in the price table | Project Settings → Models → add a definition |
| `RuntimeError: generator didn't stop after throw()` | `@contextmanager` yields twice on the error path | Yield exactly once. Enter the span through `ExitStack` |
| App slow or 5xx while Langfuse is down | `flush()` on the request path | Remove it. Flush only at shutdown |

## Red flags: stop and re-verify

- You checked only the preflight or test trace, not a real request
- You didn't test with Langfuse down, or with keys unset
- "Cost is $0 but that's fine"
- "Probably one trace per request"

Each of these means an item on the checklist hasn't been proven yet.
