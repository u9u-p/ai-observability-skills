#!/usr/bin/env python3
"""Preflight for Langfuse tracing. Standard library only; no SDK required.

Checks, in order, and stops at the first failure:
  1. env        LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST are set and well-formed
  2. reachable  GET  /api/public/health  answers {"status":"OK"}
  3. auth       GET  /api/public/projects  accepts the key pair
  4. ingest     POST /api/public/otel/v1/traces  takes a test trace (span + nested generation)
  5. readback   the test trace is queryable (REST API, or ClickHouse on v4 events_only)

  python3 check_langfuse.py                 # uses env vars
  python3 check_langfuse.py --env-file .env # loads KEY=VALUE lines first
  python3 check_langfuse.py --no-send       # checks 1-3 only, writes nothing

On a v4 self-host the REST read API is disabled. Set CLICKHOUSE_URL
(default http://localhost:8123), CLICKHOUSE_USER (default clickhouse) and
CLICKHOUSE_PASSWORD to verify readback from the shell; otherwise the script
prints the trace URL for you to open.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OK, FAIL, WARN = "\033[32m✔\033[0m", "\033[31m✘\033[0m", "\033[33m!\033[0m"


def die(step: str, msg: str, hint: str = "") -> None:
    print(f"{FAIL} {step}: {msg}")
    if hint:
        print(f"    hint: {hint}")
    sys.exit(1)


def load_env_file(path: str) -> None:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http(method: str, url: str, auth: str | None = None, body: bytes | None = None,
         ctype: str = "application/json", timeout: int = 10):
    req = urllib.request.Request(url, data=body, method=method)
    if auth:
        req.add_header("Authorization", "Basic " + auth)
    if body is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def attr(key: str, value) -> dict:
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, list):
        v = {"arrayValue": {"values": [{"stringValue": s} for s in value]}}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def otlp_payload(trace_id: str, marker: str) -> dict:
    now = time.time_ns()
    root_id, gen_id = secrets.token_hex(8), secrets.token_hex(8)
    root = {
        "traceId": trace_id, "spanId": root_id, "name": "langfuse-preflight", "kind": 1,
        "startTimeUnixNano": str(now - 900_000_000), "endTimeUnixNano": str(now),
        "attributes": [
            attr("langfuse.trace.name", "langfuse-preflight"),
            attr("langfuse.user.id", "preflight-user"),
            attr("langfuse.session.id", "preflight-session"),
            attr("langfuse.trace.tags", ["preflight", marker]),
            attr("langfuse.observation.input", json.dumps({"message": "ping"})),
            attr("langfuse.observation.output", json.dumps({"text": "pong"})),
        ],
    }
    gen = {
        "traceId": trace_id, "spanId": gen_id, "parentSpanId": root_id,
        "name": "preflight-generation", "kind": 1,
        "startTimeUnixNano": str(now - 800_000_000), "endTimeUnixNano": str(now - 100_000_000),
        "attributes": [
            attr("langfuse.observation.type", "generation"),
            attr("gen_ai.request.model", "gpt-4o-mini"),
            attr("gen_ai.usage.input_tokens", 12),
            attr("gen_ai.usage.output_tokens", 3),
            attr("langfuse.observation.input", json.dumps([{"role": "user", "content": "ping"}])),
            attr("langfuse.observation.output", json.dumps({"role": "assistant", "content": "pong"})),
        ],
    }
    return {"resourceSpans": [{
        "resource": {"attributes": [attr("service.name", "langfuse-preflight")]},
        "scopeSpans": [{"scope": {"name": "langfuse-preflight"}, "spans": [root, gen]}],
    }]}


def clickhouse_count(trace_id: str) -> int | None:
    pw = os.environ.get("CLICKHOUSE_PASSWORD")
    if not pw:
        return None
    base = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")
    user = os.environ.get("CLICKHOUSE_USER", "clickhouse")
    url = base + "?" + urllib.parse.urlencode({"user": user, "password": pw})
    sql = f"SELECT count() FROM events_full WHERE trace_id = '{trace_id}'"
    try:
        status, body = http("POST", url, body=sql.encode(), ctype="text/plain")
    except urllib.error.URLError as exc:
        print(f"{WARN} readback: cannot reach ClickHouse at {base}: {exc.reason}")
        return None
    if status != 200:
        print(f"{WARN} readback: ClickHouse said {status}: {body[:200]}")
        return None
    return int(body.strip() or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-file")
    ap.add_argument("--no-send", action="store_true", help="skip ingest and readback")
    ap.add_argument("--wait", type=int, default=45, help="seconds to wait for readback")
    args = ap.parse_args()
    if args.env_file:
        load_env_file(args.env_file)

    # 1. env
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = (os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
    missing = [n for n, v in [("LANGFUSE_PUBLIC_KEY", pk), ("LANGFUSE_SECRET_KEY", sk),
                              ("LANGFUSE_HOST", host)] if not v]
    if missing:
        die("env", "missing " + ", ".join(missing),
            "Cloud: https://cloud.langfuse.com (EU) or https://us.cloud.langfuse.com (US)")
    if not pk.startswith("pk-lf-") or not sk.startswith("sk-lf-"):
        die("env", "keys should look like pk-lf-… / sk-lf-…", "public and secret swapped?")
    if not host.startswith(("http://", "https://")):
        die("env", f"LANGFUSE_HOST={host!r} needs a scheme", "e.g. http://10.0.1.23:3100")
    print(f"{OK} env        host={host}  public_key={pk[:10]}…")

    # 2. reachable
    try:
        status, body = http("GET", f"{host}/api/public/health")
    except urllib.error.URLError as exc:
        die("reachable", f"{host} unreachable: {exc.reason}",
            "container DNS names (langfuse-web) only resolve on a shared Docker network; "
            "from another host use the private IP + published port, and open it in the security group")
    if status != 200:
        die("reachable", f"/api/public/health returned {status}: {body[:200]}")
    version = json.loads(body).get("version", "?")
    major = int(version.split(".")[0]) if version[:1].isdigit() else 0
    print(f"{OK} reachable  Langfuse {version}")

    # 3. auth
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    status, body = http("GET", f"{host}/api/public/projects", auth=auth)
    if status in (401, 403):
        die("auth", f"key pair rejected ({status})",
            "keys belong to a different project or region (EU vs US cloud), or were revoked")
    if status != 200:
        die("auth", f"/api/public/projects returned {status}: {body[:200]}")
    project = (json.loads(body).get("data") or [{}])[0]
    project_id = project.get("id", "")
    print(f"{OK} auth       project={project.get('name', '?')} ({project_id})")

    if args.no_send:
        print("\nPREFLIGHT PASSED (ingest not tested: --no-send)")
        return 0

    # 4. ingest
    trace_id, marker = secrets.token_hex(16), f"preflight-{int(time.time())}"
    payload = json.dumps(otlp_payload(trace_id, marker)).encode()
    status, body = http("POST", f"{host}/api/public/otel/v1/traces", auth=auth, body=payload)
    if status not in (200, 202):
        die("ingest", f"OTLP endpoint returned {status}: {body[:300]}",
            "servers older than v3 have no OTLP endpoint; upgrade Langfuse")
    print(f"{OK} ingest     OTLP accepted trace {trace_id}")
    trace_url = f"{host}/project/{project_id}/traces/{trace_id}"

    # 5. readback (ingestion is asynchronous: poll)
    deadline, events_only, found = time.time() + args.wait, False, False
    while time.time() < deadline and not found:
        time.sleep(3)
        if not events_only:
            status, body = http("GET", f"{host}/api/public/traces/{trace_id}", auth=auth)
            if status == 200:
                found = True
                break
            if "events_only" in body or "not available" in body:
                events_only = True
        if events_only:
            n = clickhouse_count(trace_id)
            if n is None:
                break
            found = n >= 2
    if found:
        print(f"{OK} readback   trace is stored with its nested generation")
    elif events_only and not os.environ.get("CLICKHOUSE_PASSWORD"):
        print(f"{WARN} readback   Langfuse v{major} events_only mode: REST read API is disabled.")
        print("             Set CLICKHOUSE_PASSWORD to check from the shell, or open the URL below.")
        print(f"\n  {trace_url}\n\nPREFLIGHT PASSED (readback: confirm in the UI)")
        return 0
    else:
        die("readback", f"trace not visible after {args.wait}s",
            "check `docker compose logs langfuse-worker`; the worker processes the ingestion queue")

    print(f"\n  {trace_url}\n\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
