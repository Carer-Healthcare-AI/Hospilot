"""Headless per-agent smoke test.

Calls each agent's primary read/entry activity directly (no Temporal worker, no
graph, no LLM) against the running Redis / Hasura / Fabric, and reports which
agents raise. This exercises each agent's data path in isolation so a broken
repo query / cache read / import surfaces as a clear per-agent FAIL.

This file lives OUTSIDE the repo (throwaway tooling). Run it inside the stack by
copying it into the worker container (which has the app code + env + network):

    DC="sudo docker compose -f deployments/docker-compose.fabric.yml \
        -f deployments/docker-compose.agentic-framework.yml"
    $DC cp /path/to/smoke_agents.py worker:/app/smoke_agents.py
    $DC exec -T worker python /app/smoke_agents.py

Options (append to the exec command):
    --org <org_id>   route tenant reads to this org (default: "" = Carer default)
    --json           emit machine-readable JSON instead of the table

Exit code is non-zero if any agent failed, so it doubles as a CI check.

NOTE: only non-mutating read entrypoints are invoked (e.g. find_available_beds,
get_icu_census) -- never the create_*_approval / confirm_* / reserve_* paths, so
this is safe to run against a live stack without locking beds or raising
approvals.
"""
import argparse
import asyncio
import importlib
import time
import traceback
import uuid
from datetime import datetime, timezone

# (label, module, function, call-style)
#   "session" -> fn(session_id)
#   "appt"    -> fn(session_id, ta_results={}, ctx={})   (task-style agent)
AGENTS = [
    ("ambulance",   "agents.ambulance.activities",   "get_available_ambulances",     "session"),
    ("appointment", "agents.appointment.activities",  "ta_appt_find_available_slots", "appt"),
    ("bed",         "agents.bed.activities",          "find_available_beds",          "session"),
    ("billing",     "agents.billing.activities",      "detect_claim_discrepancies",   "session"),
    ("discharge",   "agents.discharge.activities",    "get_discharge_candidates",     "session"),
    ("er",          "agents.er.activities",           "get_er_visits",                "session"),
    ("housekeeping","agents.housekeeping.activities", "get_vacated_beds",             "session"),
    ("icu",         "agents.icu.activities",          "get_icu_census",               "session"),
    ("lab",         "agents.lab.activities",          "get_lab_tat_status",           "session"),
    ("ot",          "agents.ot.activities",           "get_ot_census",                "session"),
    ("pharmacy",    "agents.pharmacy.activities",     "get_discharge_ready_patients", "session"),
    ("revenue",     "agents.revenue.activities",      "identify_revenue_leakage",     "session"),
    ("staff",       "agents.staff.activities",        "get_ward_workload",            "session"),
]


def _build_report(session_id, org, when, results, failed) -> str:
    """Markdown report: summary line, per-agent table, then failure tracebacks."""
    passed = len(results) - len(failed)
    lines = [
        "# Per-agent smoke test report",
        "",
        f"- **When:** {when}",
        f"- **Session:** `{session_id}`",
        f"- **Org:** {org or '(default)'}",
        f"- **Result:** {passed}/{len(results)} OK"
        + (f" — **{len(failed)} FAILED**: {', '.join(r['agent'] for r in failed)}" if failed else ""),
        "- **LLM used:** no (read/data-path entrypoints only)",
        "",
        "| Agent | Entrypoint | Status | ms | Detail |",
        "|---|---|---|---:|---|",
    ]
    for r in results:
        status = "OK ✅" if r["status"] == "OK" else "FAIL ❌"
        detail = r["detail"].replace("|", "\\|")
        lines.append(f"| {r['agent']} | `{r['entrypoint']}` | {status} | {r['ms']} | {detail} |")
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines += [f"### {r['agent']}.{r['entrypoint']}", "", "```",
                      r["traceback"].rstrip(), "```", ""]
    lines.append("")
    return "\n".join(lines)


def _summarize(result) -> str:
    """One-line shape of a successful result, so 'ran but empty' is visible."""
    if isinstance(result, list):
        return f"list[{len(result)}]"
    if isinstance(result, dict):
        keys = ", ".join(list(result.keys())[:6])
        more = "…" if len(result) > 6 else ""
        return f"dict{{{keys}{more}}}"
    text = str(result)
    return text if len(text) <= 60 else text[:57] + "…"


async def _run_one(label, module, func, style, session_id):
    started = time.monotonic()
    try:
        mod = importlib.import_module(module)
        fn = getattr(mod, func)
        if style == "appt":
            result = await fn(session_id, {}, {})
        else:
            result = await fn(session_id)
        return {
            "agent": label, "entrypoint": f"{func}", "status": "OK",
            "detail": _summarize(result),
            "ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 -- the whole point is to catch any failure
        return {
            "agent": label, "entrypoint": f"{func}", "status": "FAIL",
            "detail": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "ms": round((time.monotonic() - started) * 1000),
        }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Per-agent headless smoke test")
    parser.add_argument("--org", default="", help="org_id for tenant reads (default: Carer default)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--report", default="/tmp/agent_smoke_report.md",
                        help="path to write the Markdown report (default: /tmp/agent_smoke_report.md)")
    args = parser.parse_args()

    session_id = f"smoke-{uuid.uuid4()}"
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Mirror app startup: agents read Redis via a module-global client that must be
    # initialised first (run_worker.py does `await init_redis()`). Also disable the
    # Kafka bus in-process -- broadcast() would otherwise spin up a producer we don't
    # need for a read-only smoke test (and leak an "Unclosed AIOKafkaProducer").
    from config import settings
    settings.kafka_enabled = False
    from cache.redis import init_redis, close_redis
    await init_redis()

    # Route tenant (Hasura) reads made inside the activities to the chosen org.
    from workflows.graph.exec_context import set_exec_ctx
    set_exec_ctx(session_id, "smoke", args.org)

    # Run sequentially -- one agent at a time, as requested, so a hang or crash is
    # attributable and logs interleave cleanly.
    results = []
    try:
        for label, module, func, style in AGENTS:
            results.append(await _run_one(label, module, func, style, session_id))
    finally:
        await close_redis()

    failed = [r for r in results if r["status"] == "FAIL"]

    # Always write the Markdown report.
    report = _build_report(session_id, args.org, when, results, failed)
    try:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(report)
        report_note = f"report written to {args.report}"
    except OSError as exc:
        report_note = f"could not write report to {args.report}: {exc}"

    if args.json:
        import json
        print(json.dumps({"session_id": session_id, "org": args.org or "(default)",
                          "results": results, "failed": len(failed),
                          "report": args.report}, indent=2))
        print(report_note)
        return 1 if failed else 0

    # Table
    w_agent = max(len(r["agent"]) for r in results)
    w_entry = max(len(r["entrypoint"]) for r in results)
    print(f"\nPer-agent smoke test   session={session_id}   org={args.org or '(default)'}\n")
    header = f"{'AGENT':<{w_agent}}  {'ENTRYPOINT':<{w_entry}}  STATUS  {'ms':>6}  DETAIL"
    print(header)
    print("-" * len(header))
    for r in results:
        mark = "OK " if r["status"] == "OK" else "XXX"
        print(f"{r['agent']:<{w_agent}}  {r['entrypoint']:<{w_entry}}  "
              f"{mark} {r['status']:<4}  {r['ms']:>6}  {r['detail']}")

    if failed:
        print("\n-- Failures (traceback) -----------------------------------------")
        for r in failed:
            print(f"\n### {r['agent']}.{r['entrypoint']}")
            print(r["traceback"].rstrip())

    passed = len(results) - len(failed)
    print(f"\n{passed}/{len(results)} agents OK"
          + (f"   |   {len(failed)} FAILED: {', '.join(r['agent'] for r in failed)}" if failed else ""))
    print(report_note)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
