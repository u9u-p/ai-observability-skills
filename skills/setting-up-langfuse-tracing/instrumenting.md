# Instrumenting an app for Langfuse

All examples target the **Python SDK v4** (`langfuse>=4`), which exports over
OpenTelemetry. Environment:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com   # or http://<self-host>:3100
LANGFUSE_TRACING_ENVIRONMENT=production     # filters dev and prod apart in the UI
OTEL_SERVICE_NAME=my-service                # otherwise "unknown_service"
```

## The tracing block (framework-agnostic core)

Copy this into the app once. Every other section builds on it.

```python
import contextlib, logging, os

logger = logging.getLogger(__name__)
SERVICE = os.environ.get("OTEL_SERVICE_NAME", "my-service")


def _tracing():
    # Inert without keys: CI and local tests behave exactly as before.
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return None
    try:
        from langfuse import get_client
        return get_client()          # connects lazily; boot order doesn't matter
    except Exception as exc:         # a broken tracer must never break the app
        logger.warning("tracing disabled: %s: %s", type(exc).__name__, exc)
        return None


LANGFUSE = _tracing()


@contextlib.contextmanager
def trace_turn(user_id: str, session_id: str, user_input):
    """One trace per unit of work. Every model and tool call nests inside it."""
    if LANGFUSE is None:
        yield None
        return
    with contextlib.ExitStack() as stack:
        try:
            span = stack.enter_context(
                LANGFUSE.start_as_current_observation(as_type="span", name=SERVICE))
            span.update(input=user_input, metadata={
                # v4 removed update_current_trace(); these keys set trace attributes.
                "langfuse_user_id": user_id,          # opaque ID, never an email
                "langfuse_session_id": session_id,
                "langfuse_tags": [SERVICE],
            })
        except Exception as exc:
            logger.warning("trace start failed: %s", exc)
            span = None
        yield span                                    # yield exactly ONCE (see below)


def record_tool(name: str, args: dict, fn):
    """Run a tool inside a TOOL observation so it appears in the tree with timing."""
    if LANGFUSE is None:
        return fn(**args)
    with LANGFUSE.start_as_current_observation(as_type="tool", name=name, input=args) as obs:
        try:
            result = fn(**args)
        except Exception as exc:
            obs.update(level="ERROR", status_message=str(exc))
            raise
        obs.update(output=result)
        return result
```

Why `ExitStack` and a single `yield`: a `@contextmanager` generator that has a second
`yield` in an `except` branch raises
`RuntimeError: generator didn't stop after throw()` whenever the body fails, and that
error hides the real one.

Request handler:

```python
with trace_turn(user.id, conversation.id, {"message": text}) as span:
    reply = run_agent(text)
    if span: span.update(output={"text": reply})
```

## LangChain / LangGraph

```python
from langfuse.langchain import CallbackHandler   # also needs `langchain>=1.0` installed
HANDLER = CallbackHandler() if LANGFUSE else None

def trace_config():
    return {"callbacks": [HANDLER]} if HANDLER else {}

await llm.ainvoke(messages, config=trace_config())     # or graph.ainvoke(state, config=...)
```

One shared handler is fine. Each call nests under whichever span is current in its own
context. If your code dispatches tools itself (`fn(**call["args"])`) rather than
through a LangChain `ToolNode`, wrap each call with `record_tool`.

## OpenAI SDK

```python
from langfuse.openai import openai   # drop-in replacement; the rest of the code is unchanged
client = openai.OpenAI()
client.chat.completions.create(model="gpt-4o-mini", messages=[...])
```

Inside `trace_turn`, each completion becomes a GENERATION with usage and cost.

## Anthropic SDK (or any raw client)

```python
# Guard the decorator too: an unguarded @observe with no keys logs
# "Authentication error ... Client will be disabled" on every call.
if LANGFUSE:
    from langfuse import observe
else:
    def observe(**_):
        return lambda fn: fn

@observe(as_type="generation", name="anthropic.messages")
def call_claude(messages, model="claude-sonnet-5"):
    resp = anthropic_client.messages.create(model=model, max_tokens=1024, messages=messages)
    if LANGFUSE:
        LANGFUSE.update_current_generation(
            model=model, input=messages, output=resp.content[0].text,
            usage_details={"input": resp.usage.input_tokens, "output": resp.usage.output_tokens},
        )
    return resp
```

If the model name isn't in Langfuse's price table, cost reads $0. Add it under
Project Settings → Models.

## TypeScript / Node

```ts
// instrumentation.ts, loaded before anything else
import { NodeSDK } from "@opentelemetry/sdk-node";
import { LangfuseSpanProcessor } from "@langfuse/otel";
export const sdk = new NodeSDK({ spanProcessors: [new LangfuseSpanProcessor()] });
sdk.start();

// handler
import { startActiveObservation } from "@langfuse/tracing";
await startActiveObservation("chat-turn", async (span) => {
  span.update({ input: { message } });
  const reply = await runAgent(message);
  span.update({ output: { reply } });
});
```

In serverless code, `await sdk.shutdown()` (or `forceFlush()`) before the handler
returns.

## Flush strategy

| Process | Rule |
|---|---|
| Web server / worker | Never call `flush()` on the request path. At shutdown, call `LANGFUSE.shutdown()` |
| Script / CLI / cron | `LANGFUSE.flush()` before exit, or you get zero traces |
| Serverless | Flush at the end of each invocation. The runtime freezes the process afterwards |

## Masking (if Q5 = "mask it")

```python
from langfuse import Langfuse
import re
def mask(data, **_):
    return re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED]", data) if isinstance(data, str) else data
Langfuse(mask=mask)   # construct once at startup; get_client() then returns this instance
```

If the raw bytes are the point (security testing, a CTF), don't mask. Instead, treat
the Langfuse UI as operator-only and write that decision down.
