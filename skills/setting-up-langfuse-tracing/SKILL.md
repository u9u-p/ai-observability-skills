---
name: setting-up-langfuse-tracing
description: Use when adding LLM observability, tracing, token or cost tracking to an app that calls a model (OpenAI, Anthropic, LangChain, LangGraph, agents with tools), when someone mentions Langfuse, or when an app's prompts, tool calls or model spend are invisible to its operators.
---

# Setting up Langfuse tracing

## Overview

Tracing is done when **a real request produces one trace with every model call and
tool call nested inside it, with tokens, cost, and user/session attached**. The app
must also keep working when Langfuse is down. "The SDK is installed" is not done, and
neither is "I saw a trace once."

Work through the phases in order. Don't write instrumentation until Phase 1 has
answers. The right code depends on where Langfuse runs, which SDK version you have,
and which framework calls the model.

## Phase 1: Interview (ask before touching code)

First inspect the repo so you don't ask what you can already see:

```bash
grep -rniE "langfuse|opentelemetry|otel" --include='*.py' --include='*.ts' --include='*.js' \
  --include='*.toml' --include='requirements*.txt' --include='package.json' --include='*.env*' --include='*.yml' . | head -40
grep -rhoiE "langchain|langgraph|openai|anthropic|llama_index|litellm|vercel/ai" \
  --include='*.py' --include='*.ts' --include='package.json' --include='requirements*.txt' . | sort | uniq -c
```

Then ask the user the questions below, using `AskUserQuestion` if you have it. Skip
any question the repo already answered, and say what you found.

| # | Question | Options | Why it matters |
|---|---|---|---|
| 1 | **Do you already have Langfuse?** | Langfuse Cloud (EU / US) · self-hosted and running · none, self-host it · none, use Cloud | Decides whether Phase 2 runs at all |
| 2 | Where does the app run relative to Langfuse? | same Docker host · different host/VPC · laptop | Decides `LANGFUSE_HOST` (container DNS vs private IP vs public URL) |
| 3 | What calls the model? | LangChain/LangGraph · OpenAI SDK · Anthropic SDK · other/raw HTTP · TypeScript | Picks the integration in Phase 3 |
| 4 | Is the process long-lived or short-lived? | web server · script/CLI/cron · serverless | Decides the flush strategy |
| 5 | Can prompts contain secrets or PII? | no · yes, mask them · yes, and that's intended (e.g. a CTF) | Decides masking, and who may open the UI |

If the answer to Q1 is "I have it", also collect the **host URL** and a **project key
pair** (`pk-lf-…` / `sk-lf-…`, from Project Settings → API Keys). Never ask the user
to paste the secret key into chat. Have them put it in the env file themselves.

## Phase 2: Get a Langfuse the app can reach

| Answer to Q1 | Do this |
|---|---|
| Cloud | Host is `https://cloud.langfuse.com` (EU) or `https://us.cloud.langfuse.com` (US). Create a project and a key pair in the UI. |
| Self-hosted, running | `curl -s $HOST/api/public/health` must return `{"status":"OK","version":…}`. Note the **major version**: v4 changes how you verify. |
| None, self-host | Follow `self-hosting.md` in this directory. |

**Gate:** run `python3 scripts/check_langfuse.py` (in this skill directory) with the
env vars set. It checks reachability and credentials, sends one test trace, and reads
it back. Run it from wherever the app runs (inside the container, or on the app host),
because that's the network path that matters. Don't start Phase 3 until it prints
`ALL CHECKS PASSED`, or until it prints `PREFLIGHT PASSED (readback: confirm in the UI)`
and the user has opened the printed URL and seen the trace.

## Phase 3: Instrument

Read `instrumenting.md` in this directory and use the section for the Q3 answer. The
integration differs by framework, but every setup needs the same five pieces:

1. **Inert without keys.** With `LANGFUSE_PUBLIC_KEY` unset, the app behaves exactly as
   before, so tests and CI stay green.
2. **Never breaks the app.** SDK import or init errors are caught and logged, and tracing
   turns off.
3. **One trace per unit of work** (request, chat turn, job). Every model hop and tool
   call nests under it.
4. **Trace attributes**: `user_id` (an opaque ID, never an email address), `session_id`,
   `tags`, and a service name.
5. **Flush strategy that matches Q4.** Long-lived servers never call `flush()` on the
   request path. Scripts, CLIs, and serverless functions **must** call `flush()` before
   exit.

Put credentials in a gitignored env file. Only `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` belong in the app's environment. Keep the
Langfuse server's own database and S3 secrets out of it.

## Phase 4: Prove it

**REQUIRED SUB-SKILL:** use `verifying-langfuse-traces`. Tracing isn't done until
every item on its definition-of-done checklist passes and you've shown the user the
evidence (a trace URL or query output).

## Common mistakes

| Mistake | What happens | Fix |
|---|---|---|
| Following a v2/v3 blog post on a v4 server | `/api/public/ingestion` and `GET /api/public/traces` return "not available … events_only mode" | Use SDK ≥ 4 (OTLP). Verify in the UI or ClickHouse `events_full` |
| `flush()` on every request | Each request blocks on Langfuse, and an outage becomes app downtime | Let the background exporter send. Flush only at shutdown |
| No `flush()` in a script | The process exits before export, and zero traces arrive | Call `get_client().flush()` (or `shutdown()`) at the end |
| `langfuse.langchain` without `langchain` installed | `ModuleNotFoundError: langchain` even though you only use `langchain-core` | Add `langchain>=1.0` to requirements |
| Tools called as plain functions | Two sibling generations with a gap between them, and no tool metrics | Wrap each tool execution in an `as_type="tool"` observation |
| Calling `update_current_trace()` on SDK v4 | `AttributeError` | Set `langfuse_user_id` / `langfuse_session_id` / `langfuse_tags` as span metadata |
| No `OTEL_SERVICE_NAME` | Every service shows up as `unknown_service` | Set it per service |
| App on another host uses `http://langfuse-web:3000` | Connection refused, and traces are dropped silently | Container DNS only works on a shared Docker network. Use the host's private IP and the published port |
| Model name that isn't in the price table | Tokens show up, but cost is $0 | Add a custom model definition in Project Settings → Models |
| Langfuse UI exposed publicly | Anyone can read every prompt and completion | Keep it on a private network, VPN, or SSO |
