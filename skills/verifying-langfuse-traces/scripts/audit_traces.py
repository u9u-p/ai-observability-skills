#!/usr/bin/env python3
"""Grade real traces against the tracing definition of done. Standard library only.

Reads Langfuse v4's ClickHouse `events_full` table directly. On v4
(events_only mode) this is the only shell-side way to inspect traces, because
GET /api/public/traces is disabled and the legacy `traces` and `observations`
tables stay empty.

  python3 audit_traces.py --service my-service              # graded checklist
  python3 audit_traces.py --tag level_01_vault --tree       # span tree per trace
  python3 audit_traces.py --since-minutes 1440              # widen the window

Env: CLICKHOUSE_PASSWORD (required), CLICKHOUSE_URL (default
http://localhost:8123), CLICKHOUSE_USER (default clickhouse). ClickHouse should be
bound to loopback, so run this on the Langfuse host or through an SSH tunnel:
  ssh -L 8123:127.0.0.1:8123 <langfuse-host>
"""

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PASS, FAIL, WARN = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m", "\033[33mWARN\033[0m"


def query(sql: str) -> list[list[str]]:
    pw = os.environ.get("CLICKHOUSE_PASSWORD")
    if not pw:
        sys.exit("set CLICKHOUSE_PASSWORD (it is in the Langfuse server's .env)")
    base = os.environ.get("CLICKHOUSE_URL", "http://localhost:8123")
    user = os.environ.get("CLICKHOUSE_USER", "clickhouse")
    url = base + "?" + urllib.parse.urlencode({"user": user, "password": pw})
    req = urllib.request.Request(url, data=(sql + " FORMAT TSV").encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode()
    except urllib.error.HTTPError as exc:
        sys.exit(f"ClickHouse rejected the query: {exc.read().decode()[:300]}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach ClickHouse at {base}: {exc.reason}")
    return [line.split("\t") for line in text.splitlines() if line]


def sq(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", help="OTEL_SERVICE_NAME to audit")
    ap.add_argument("--tag", help="only traces whose root carries this tag")
    ap.add_argument("--since-minutes", type=int, default=60)
    ap.add_argument("--tree", action="store_true", help="print span trees instead of grading")
    ap.add_argument("--limit", type=int, default=5, help="traces to print with --tree")
    args = ap.parse_args()

    where = [f"start_time > now() - INTERVAL {int(args.since_minutes)} MINUTE", "is_deleted = 0"]
    if args.service:
        where.append(f"service_name = {sq(args.service)}")
    if args.tag:
        where.append(f"has(tags, {sq(args.tag)})")
    ids = f"SELECT DISTINCT trace_id FROM events_full WHERE {' AND '.join(where)}"
    scope = f"start_time > now() - INTERVAL {int(args.since_minutes) + 60} MINUTE AND trace_id IN ({ids})"

    if args.tree:
        rows = query(f"""
            SELECT trace_id, parent_span_id = '', type, name, user_id, session_id,
                   arrayStringConcat(tool_call_names, ','), toString(usage_details['total']),
                   toString(round(total_cost, 6)), level
            FROM events_full WHERE {scope}
              AND trace_id IN (SELECT trace_id FROM ({ids}) LIMIT {int(args.limit)})
            ORDER BY trace_id, parent_span_id != '', start_time""")
        last = None
        for tid, root, typ, name, uid, sid, tools, tok, cost, level in rows:
            if tid != last:
                print(f"\ntrace {tid}")
                last = tid
            pad = "" if root == "1" else "  └─ "
            extra = f"user={uid or '∅'} session={sid or '∅'}" if root == "1" else \
                (f"tokens={tok} ${cost}" if typ == "GENERATION" else "") + (f" tool_calls=[{tools}]" if tools else "")
            flag = "  [ERROR]" if level == "ERROR" else ""
            print(f"  {pad}{typ:<10} {name:<28} {extra}{flag}")
        if not rows:
            print("no traces in window", file=sys.stderr)
            return 1
        return 0

    (r,) = query(f"""
        WITH per_trace AS (
            SELECT trace_id,
                   countIf(parent_span_id = '')                           AS roots,
                   anyIf(user_id, parent_span_id = '')                    AS uid,
                   anyIf(session_id, parent_span_id = '')                 AS sid,
                   countIf(type = 'GENERATION')                           AS gens,
                   countIf(type = 'TOOL')                                 AS tools,
                   sumIf(length(tool_call_names), type = 'GENERATION')    AS tool_reqs,
                   groupUniqArray(span_id)                                AS spans,
                   groupUniqArrayIf(parent_span_id, parent_span_id != '') AS parents
            FROM events_full WHERE {scope} GROUP BY trace_id)
        SELECT count(),
               countIf(roots = 0), countIf(roots > 1),
               countIf(length(arrayFilter(p -> NOT has(spans, p), parents)) > 0),
               countIf(uid = ''), countIf(sid = ''),
               countIf(gens = 0), countIf(tool_reqs > 0 AND tools = 0)
        FROM per_trace""")
    (traces, no_root, multi_root, orphans, no_uid, no_sid, no_gen, tool_gap) = map(int, r)

    (g,) = query(f"""
        SELECT countIf(type = 'GENERATION'),
               countIf(type = 'GENERATION' AND usage_details['total'] = 0),
               countIf(type = 'GENERATION' AND total_cost = 0 AND usage_details['total'] > 0),
               arrayStringConcat(groupUniqArrayIf(provided_model_name,
                    type = 'GENERATION' AND total_cost = 0 AND usage_details['total'] > 0), ','),
               countIf(level = 'ERROR'),
               arrayStringConcat(groupUniqArray(service_name), ','),
               arrayStringConcat(groupUniqArray(environment), ',')
        FROM events_full WHERE {scope}""")
    gens, no_usage, no_cost, unpriced, errors, services, envs = int(g[0]), int(g[1]), int(g[2]), g[3], int(g[4]), g[5], g[6]

    if traces == 0:
        print(f"{FAIL} no traces in the last {args.since_minutes} min. Run a real request first, "
              "then see the 'No traces' table in SKILL.md")
        return 1

    unpriced_hint = f"unpriced models: {unpriced}; " if unpriced else ""
    checks = [  # (passed, severity if not, what is checked, how to fix)
        (True, FAIL, f"traces found: {traces} (services={services or '∅'}, env={envs or '∅'})", ""),
        (not any(s.startswith("unknown_service") for s in services.split(",")), WARN,
         "service name set", "some spans report unknown_service; set OTEL_SERVICE_NAME"),
        (multi_root == 0, FAIL, f"one root per trace ({multi_root} split)",
         "a unit of work is emitted as sibling traces; open one parent span around it"),
        (no_root == 0 and orphans == 0, WARN, f"no orphan spans ({no_root} rootless, {orphans} missing parent)",
         "context is lost across a thread/async hop, or the parent is still in flight"),
        (no_gen == 0, WARN, f"every trace has a GENERATION ({no_gen} without)",
         "fine for refusals or cached replies; otherwise the model client is not instrumented"),
        (no_uid == 0, FAIL, f"user_id on every trace ({no_uid} missing)", "set langfuse_user_id on the root span"),
        (no_sid == 0, WARN, f"session_id on every trace ({no_sid} missing)", "set langfuse_session_id on the root span"),
        (no_usage == 0, FAIL, f"token usage on every generation ({no_usage}/{gens} zero)",
         "pass usage_details, or use an integration that reports usage"),
        (no_cost == 0, WARN, f"cost on every generation ({no_cost}/{gens} priced $0)",
         unpriced_hint + "add a model definition under Project Settings → Models"),
        (tool_gap == 0, FAIL, f"tool calls observed ({tool_gap} traces requested tools with no TOOL span)",
         "wrap tool execution in an as_type='tool' observation"),
        (errors == 0, WARN, f"no ERROR-level observations ({errors})", "open them with --tree"),
    ]
    worst = 0
    for ok, sev, what, fix in checks:
        print(f"{PASS if ok else sev}  {what}" + ("" if ok or not fix else f"\n        → {fix}"))
        if not ok and sev == FAIL:
            worst = 1
    print("\nDEFINITION OF DONE: " + ("NOT MET" if worst else "MET (review WARN lines)"))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
