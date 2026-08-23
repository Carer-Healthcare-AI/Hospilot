"""Command line entry point — run one allocation and print it step by step.

::

    python -m allocation
    python -m allocation "ER, OT, and ICU/Ward demand compete for one limited ICU bed."
    python -m allocation --json > run.json

The output is the :class:`~allocation.trigger.steps.StepTrace` rendered, each stage labelled
with the RL-Steps section that specifies it, so a run can be read next to the document.

**It runs in SIMULATION mode.** Going live needs ``--mode live``, and that flag is the only
thing between a typed sentence and a real budget decrement.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from allocation.config import load_config
from allocation.config.loader import CONFIG_DIR
from allocation.contracts import AuctionMode
from allocation.explain import explain
from allocation.ingest.fixtures import CANDIDATES, NOW, FixtureDataSource
from allocation.ingest.scenarios import ScenarioError, load_scenario
from allocation.trigger.query import UnknownUseCase, resolve_profile
from allocation.trigger.runtime import AllocationRun, run_allocation
from allocation.trigger.session import SessionResult, event_schedule, run_session, with_rounds
from allocation.trigger.steps import Step

DEFAULT_QUERY = (
    "ER, OT, and ICU/Ward demand compete for one limited ICU bed. RL learns who should "
    "receive the bed while balancing survival, throughput, waiting time, cancellations, "
    "and financial impact."
)

WIDTH = 78


def _rule(char: str = "-") -> str:
    return char * WIDTH


def render_step(step: Step, index: int) -> str:
    lines = [
        "",
        f"{index}. {step.title}",
        f"   [{step.section}]",
        _rule(),
    ]
    label_width = max((len(label) for label, _ in step.rows), default=0)
    for label, value in step.rows:
        lines.append(f"  {label:<{label_width}}  {value}")

    if step.checked:
        lines.append("")
        for name, (want, got) in step.expected.items():
            mark = "ok " if step.agrees else "XX "
            lines.append(f"  {mark}{name}: document {want}, computed {got}")

    for note in step.notes:
        lines.append(f"  ! {note}")
    return "\n".join(lines)


def render(run: AllocationRun) -> str:
    out = [
        _rule("="),
        "HOSPILOT allocation — one auction, step by step",
        _rule("="),
        f"  query   {run.query[:WIDTH - 10]}",
        f"  mode    {run.event.mode.value}"
        + ("" if run.binding else "   (nothing is held, no real budget moves)"),
    ]

    for index, step in enumerate(run.trace, start=1):
        out.append(render_step(step, index))

    result = run.outcome.result
    out += [
        "",
        _rule("="),
        f"  RESULT   {result.winner.value if result.winner else 'no allocation'}"
        + (f" wins at {result.winning_bid:.1f}" if result.winning_bid is not None else ""),
        _rule("="),
    ]

    if run.trace.checked:
        failures = run.trace.failures
        out.append(
            f"  document checks: {len(run.trace.checked) - len(failures)}"
            f"/{len(run.trace.checked)} agree"
        )
    return "\n".join(out)


def to_json(run: AllocationRun) -> str:
    result = run.outcome.result
    return json.dumps(
        {
            "query": run.query,
            "mode": run.event.mode.value,
            "binding": run.binding,
            "auction_id": result.auction_id,
            "auction_key": result.auction_key,
            "winner": result.winner.value if result.winner else None,
            "winning_bid": result.winning_bid,
            "reserve_price": result.reserve_price,
            "outcome": result.outcome,
            "rounds_run": result.rounds_run,
            "utilities": {cid: b.total for cid, b in run.utilities.items()},
            "ceilings": dict(run.ceilings),
            "budgets": {
                a.value: {
                    "total": s.budget_total,
                    "remaining": s.budget_remaining,
                    "spent": s.spent,
                    "burn_rate": s.burn_rate,
                }
                for a, s in run.outcome.budgets.items()
            },
            "caps_version": result.caps_version,
            "config_version": result.config_version,
            "unsigned_rules": dict(result.unsigned_rules),
            "reward_due_at": run.pending.due_at.isoformat(),
            "steps": [
                {
                    "key": s.key,
                    "section": s.section,
                    "title": s.title,
                    "rows": [list(r) for r in s.rows],
                    "notes": list(s.notes),
                }
                for s in run.trace
            ],
        },
        indent=2,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allocation",
        description="Run one ICU-bed allocation auction and print each framework step.",
    )
    parser.add_argument(
        "query", nargs="?", default=DEFAULT_QUERY, help="the use-case sentence"
    )
    parser.add_argument(
        "--mode",
        choices=[m.value for m in AuctionMode],
        default=AuctionMode.SIMULATION.value,
        help="live holds a bed and moves a real budget; default simulation",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print every factor, weight and intermediate value behind the run",
    )
    parser.add_argument(
        "--at",
        default=None,
        help="ISO timestamp to run at; defaults to the Appendix C fixture clock",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="PATH",
        help="YAML scenario to run instead of the Appendix C fixture "
             "(see scenarios/ward_crash.yaml)",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        metavar="PATH",
        help="alternative config directory — change caps, thresholds or budget targets "
             "without touching the shipped ones",
    )
    parser.add_argument(
        "--copy-config",
        default=None,
        metavar="PATH",
        help="copy the shipped config to PATH and exit, ready to edit and pass "
             "to --config-dir",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=None,
        metavar="N",
        help="override the profile's round count (RL-Steps uses 3)",
    )
    parser.add_argument(
        "--no-uplift",
        action="store_true",
        help="disable the B.9 ceiling uplift and fall back to D.9's Ceiling = U, for "
             "comparison against the assumed bands in rules/uplift.yaml",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=None,
        metavar="N",
        help="run N auctions as a session instead of one, carrying budgets across shifts",
    )
    parser.add_argument(
        "--every",
        default="45m",
        metavar="D",
        help="spacing between session events, e.g. 45m, 2h (default 45m)",
    )
    parser.add_argument(
        "--policy",
        default=None,
        metavar="PATH",
        help=(
            "bid with trained weights from PATH instead of the heuristic. Refuses to load "
            "weights fitted under a different encoder. Shadowed unless --live-policy is given"
        ),
    )
    parser.add_argument(
        "--live-policy",
        action="store_true",
        help=(
            "let --policy actually decide the allocation rather than shadowing it. Without "
            "this the heuristic allocates and the learned policy's choices are only recorded"
        ),
    )
    return parser


def build_policy(config, path: str, live: bool):
    """Load trained weights and put the right supervision in front of them.

    **Shadow by default, act only on an explicit flag.** ``--policy`` alone loads the weights
    and lets them decide nothing: the heuristic allocates and the learned choices are recorded
    for comparison. That is the ordering ``rl/pilot.py`` requires — a learned policy earns a
    track record before it influences an allocation, and a flag that quietly promoted it would
    make the safe path the one you have to remember.

    ``QWeights.load`` refuses weights fitted under a different encoder, so a stale artifact
    fails here rather than silently bidding on a state vector whose features have moved.
    """
    from allocation.auction.guards import (
        safety_is_declared,
        safety_is_enforced,
        safety_posture,
        safety_rules,
    )
    from allocation.policy.heuristic import HeuristicPolicy
    from allocation.rl.pilot import DivergenceMonitor, GatedPolicy, SafetyGate, ShadowPolicy
    from allocation.rl.policy import LinearQPolicy, QWeights

    if live and not safety_is_enforced(config):
        # BUILD_SPEC F-13. With no rule in force nothing structurally prevents a learned policy
        # from abandoning a critical patient — the abandonment fix binds CEM's *selection*, not
        # the action space. Shadowing under that is fine; acting under it is the configuration
        # ``rl/pilot.py`` calls "not a pilot".
        raise ValueError(
            "refusing --live-policy: auction.yaml declares safety_status "
            f"{safety_posture(config)!r} with "
            f"{len(config.auction.get('safety_constraints') or ())} constraints. A learned "
            "policy may not decide allocations while no hard constraint is enforced. Run "
            "without --live-policy to shadow it instead."
        )

    if live and not safety_is_declared(config):
        # Enforced, but by engineering judgement. Loud on every run, because the difference
        # between "a rule exists" and "a clinician approved it" is exactly what a warning-free
        # run would erase.
        rules = safety_rules(config)
        print(_rule("!"), file=sys.stderr)
        print(
            f"PROVISIONAL SAFETY RULES — {len(rules)} in force, NONE clinically approved.\n"
            "  A learned policy is deciding allocations behind constraints written by\n"
            "  engineering. auction.yaml safety_status is 'provisional', not 'signed_off'.",
            file=sys.stderr,
        )
        for constraint in rules:
            threshold = constraint.get("threshold")
            suffix = f"  (threshold {threshold})" if threshold is not None else ""
            print(f"    - {constraint['id']}{suffix}", file=sys.stderr)
        print(_rule("!"), file=sys.stderr)

    weights = QWeights.load(path)
    learned = LinearQPolicy(config, weights)
    gate = SafetyGate(config)

    if live:
        return GatedPolicy(learned, gate=gate, fallback=HeuristicPolicy(config)), weights
    return (
        ShadowPolicy(
            HeuristicPolicy(config), learned, gate=gate, monitor=DivergenceMonitor()
        ),
        weights,
    )


def _duration(text: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhd])\s*", text, re.IGNORECASE)
    if not match:
        raise ValueError(f"cannot read duration {text!r}; use forms like 45m, 2h, 1d")
    amount, unit = match.groups()
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit.lower()]: float(amount)})


def render_session(result: SessionResult) -> str:
    out = [
        _rule("="),
        f"SESSION — {len(result.runs)} auctions across {len(result.shifts)} shift(s)",
        _rule("="),
    ]

    for report in result.shifts:
        out += [
            "",
            f"  {report.shift.label}  {report.shift.start:%d %b %H:%M} - "
            f"{report.shift.end:%H:%M}   {report.auctions} auction(s)",
            _rule(),
            f"  {'agent':<6} {'opened':>9} {'spent':>9} {'recovered':>10} "
            f"{'remaining':>10} {'wins':>5} {'burn':>7}  band",
        ]
        for agent in report.burn_rate:
            out.append(
                f"  {agent.value:<6} {report.opened[agent]:9.1f} {report.spent[agent]:9.2f} "
                f"{report.recovered[agent]:10.2f} {report.closed[agent]:10.1f} "
                f"{report.wins[agent]:5d} {report.burn_rate[agent]:6.1%}  "
                f"{report.band[agent]}"
            )
        if report.exhausted:
            out.append(
                f"  ! exhausted, cannot bid for the rest of the shift: "
                f"{', '.join(a.value for a in report.exhausted)}"
            )
        if not report.healthy:
            out.append(
                "  ! not every agent is in the working band (0.70-1.10). Burn rate is the "
                "health metric for the whole mechanism — AGENT_BUDGET section 8."
            )

    shares = result.win_share
    out += [
        "",
        _rule("="),
        "  win share   "
        + "   ".join(f"{a.value} {s:.0%}" for a, s in sorted(shares.items(), key=lambda kv: kv[0].value)),
    ]
    if result.unallocated:
        out.append(f"  unallocated {result.unallocated} — no bid met the reserve")
    out.append(_rule("="))
    return "\n".join(out)


def _force_utf8() -> None:
    """Emit UTF-8 whatever the console claims, so a redirected run does not crash.

    On Windows, ``python -m allocation > run.txt`` (or any pipe) selects cp1252 rather than the
    terminal's UTF-8, and the first ``⚠`` in a report — ``runtime.py``'s uplift warning, among
    others — raises ``UnicodeEncodeError`` after the auction has already run. Capturing output
    is exactly what anybody does with this command, so the failure is on the normal path.
    ``errors="replace"`` rather than a clean encode: losing a glyph is preferable to losing the
    run, and a report that reaches the file with one substitution in it is still the report.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - detached/odd streams
                pass


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    mode = AuctionMode(args.mode)

    if args.copy_config:
        target = Path(args.copy_config)
        if target.exists():
            print(f"refusing: {target} already exists", file=sys.stderr)
            return 2
        shutil.copytree(CONFIG_DIR, target)
        print(
            f"copied config to {target}\n"
            f"  edit {target / 'budget_<resource>.yaml'} -> targets (n_win / n_req) to "
            f"change Base\n"
            f"  caps and budgets are per resource type — edit the file for the bed you are "
            f"auctioning\n"
            f"  then: python -m allocation --config-dir {target}"
        )
        return 0

    if mode.is_binding:
        # The fixture source serves three invented patients. Letting that reach a real bed
        # would be the single worst failure mode this system has.
        print(
            "refusing: --mode live with the fixture data source would hold a bed for "
            "patients that do not exist. Wire a real DataSource first.",
            file=sys.stderr,
        )
        return 2

    now = datetime.fromisoformat(args.at) if args.at else NOW

    try:
        config = load_config(Path(args.config_dir) if args.config_dir else None)
    except (OSError, KeyError, ValueError) as exc:
        print(f"cannot load config from {args.config_dir}:\n  {exc}", file=sys.stderr)
        return 1

    if args.no_uplift:
        from dataclasses import replace

        rules = {**config.rules, "uplift": {**config.rules["uplift"], "enabled": False}}
        config = replace(config, rules=rules)
        print("uplift OFF — D.9 fallback, Ceiling = U", file=sys.stderr)

    source, candidates = FixtureDataSource(), CANDIDATES
    if args.scenario:
        try:
            source, candidates, description = load_scenario(args.scenario, now)
        except ScenarioError as exc:
            print(f"cannot load scenario:\n  {exc}", file=sys.stderr)
            return 1
        print(f"scenario: {description}\n", file=sys.stderr)

    try:
        profile = resolve_profile(args.query)
    except UnknownUseCase as exc:
        print(f"cannot resolve the query:\n  {exc}", file=sys.stderr)
        return 1

    if args.rounds is not None:
        try:
            profile = with_rounds(profile, args.rounds)
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1

    policy = None
    if args.policy:
        try:
            policy, weights = build_policy(config, args.policy, args.live_policy)
        except (OSError, ValueError, KeyError) as exc:
            print(f"cannot load policy from {args.policy}:\n  {exc}", file=sys.stderr)
            return 1
        stance = (
            "ACTING — the learned policy decides this allocation"
            if args.live_policy
            else "shadowing — the heuristic decides, learned choices are recorded only"
        )
        print(
            f"policy   {policy.name}\n"
            f"         encoder {weights.encoder_version}, "
            f"fabrication {weights.fabrication_version or 'unstamped'}\n"
            f"         {stance}",
            file=sys.stderr,
        )
    elif args.live_policy:
        print("--live-policy requires --policy", file=sys.stderr)
        return 1

    if args.events is not None:
        try:
            events = event_schedule(now, args.events, _duration(args.every))
        except ValueError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        result = run_session(
            config=config, source=source, candidates=candidates, start=now,
            events=events, profile=profile, query=args.query, mode=mode, policy=policy,
        )
        print(render_session(result))
        _report_shadow(policy)
        return 0

    run = run_allocation(
        config=config,
        source=source,
        candidates=candidates,
        now=now,
        query=args.query,
        profile=profile,
        mode=mode,
        policy=policy,
    )

    if args.explain:
        print(explain(run, config))
    else:
        print(to_json(run) if args.json else render(run))
    _report_shadow(policy)
    return 0


def _report_shadow(policy) -> None:
    """Print the divergence log after a shadowed run, to stderr so --json stays parseable."""
    monitor = getattr(policy, "monitor", None)
    if monitor is None:
        blocked = getattr(policy, "blocked", None)
        if blocked:
            print(f"\ngate refusals  {len(blocked)}", file=sys.stderr)
        return
    print("\n" + _rule("="), file=sys.stderr)
    print("SHADOW — what the learned policy would have done", file=sys.stderr)
    print(_rule("="), file=sys.stderr)
    print(monitor.report(), file=sys.stderr)
    if policy.blocked:
        print(f"\ngate refusals  {len(policy.blocked)}", file=sys.stderr)
        for rule in dict.fromkeys(policy.blocked):
            print(f"  {rule}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
