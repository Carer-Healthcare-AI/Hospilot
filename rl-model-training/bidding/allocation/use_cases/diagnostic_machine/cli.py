"""The diagnostic entry point — ``python -m allocation.use_cases.diagnostic_machine``.

Separate from ``python -m allocation`` on purpose, and the separation is total: a diagnostic
run loads diagnostic configuration, a diagnostic encoder, a diagnostic reward and diagnostic
artifacts, and touches none of the bed equivalents. A bed run does the reverse. Neither
imports the other's policy or weights, which is checked statically in
``tests/test_diagnostic_boundaries.py`` rather than asserted here.

**Serving only.** The ``train`` and ``compare`` subcommands, and the baseline and training
modules behind them, are research tooling and are not shipped here. Fitting a new artifact
happens in the research tree; this package loads one and runs it.

    python -m allocation.use_cases.diagnostic_machine scenarios
    python -m allocation.use_cases.diagnostic_machine run three_way_contention
    python -m allocation.use_cases.diagnostic_machine governance
"""

from __future__ import annotations

import argparse
from pathlib import Path

from allocation.config import load_config
from allocation.use_cases.diagnostic_machine.budgets import REGIMES
from allocation.use_cases.diagnostic_machine.config import (
    for_modality,
    rules_version,
    unsigned_diagnostic_rules,
)
from allocation.use_cases.diagnostic_machine.evaluate import run_policy
from allocation.use_cases.diagnostic_machine.profiles import MODALITIES
from allocation.use_cases.diagnostic_machine.qlearn import DiagnosticQPolicy
from allocation.use_cases.diagnostic_machine.scenarios import SCENARIOS, all_scenarios
from allocation.use_cases.diagnostic_machine.serving import ServedQPolicy
from allocation.use_cases.diagnostic_machine.simulator import SimulationResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allocation.use_cases.diagnostic_machine",
        description="Diagnostic-machine capacity allocation — CT, MRI, X-ray, ultrasound.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="list the deterministic scenarios")
    sub.add_parser("governance", help="show unsigned diagnostic rule tables and assumptions")

    run = sub.add_parser("run", help="run one scenario and report its outcomes")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument("--regime", choices=REGIMES, default="normal")
    run.add_argument("--policy", type=Path, help="a trained diagnostic Q artifact")
    detail = run.add_mutually_exclusive_group()
    detail.add_argument("--explain", action="store_true", help="print every mathematical step")
    detail.add_argument(
        "--explain-json", action="store_true", help="emit the complete trace as JSON"
    )


    query = sub.add_parser(
        "query", help="resolve natural language to a validated diagnostic simulation"
    )
    query.add_argument("text", nargs="+", help="diagnostic allocation question")
    query.add_argument("--regime", choices=REGIMES, default="normal")
    query.add_argument("--json", action="store_true", help="emit the complete trace as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config()

    if args.command == "scenarios":
        print(f"{len(SCENARIOS)} deterministic scenarios\n")
        for scenario in all_scenarios():
            print(f"  {scenario.name}")
            print(f"      {scenario.description}")
            print(f"      modality {scenario.modality.value}, expects: {scenario.expects}")
        return 0

    if args.command == "governance":
        print("DIAGNOSTIC GOVERNANCE\n")
        print(f"  pathway rules  {rules_version()}")
        for table_, status in sorted(unsigned_diagnostic_rules().items()):
            print(f"  UNSIGNED  {table_:24} {status}")
        print()
        for modality in sorted(MODALITIES.all(), key=lambda p: p.modality.value):
            scoped = for_modality(config, modality.modality)
            unsigned = scoped.unsigned
            print(f"  {modality.modality.value}: caps {scoped.caps_version}")
            for name in sorted(unsigned):
                if modality.modality.value in name or name.startswith("caps."):
                    print(f"      {name:34} {unsigned[name]}")
        return 0

    served = None
    if getattr(args, "policy", None):
        weights = DiagnosticQPolicy.load(args.policy)
        print(f"loaded {args.policy}  ({weights.trained_on} transitions)")
        if weights.unfitted_heads():
            print(f"  WARNING unfitted heads held down: {weights.unfitted_heads()}")
        served = lambda scoped: ServedQPolicy(scoped, weights)  # noqa: E731

    if args.command == "run":
        scenario = SCENARIOS[args.scenario]()
        result = run_policy(config, scenario, served, regime=args.regime)
        if args.explain or args.explain_json:
            from allocation.use_cases.diagnostic_machine.explain import as_json, explain
            print(as_json(result, scenario, config) if args.explain_json else explain(result, scenario, config))
        else:
            print(_render(scenario.name, args.regime, result))
        return 0


    if args.command == "query":
        from allocation.use_cases.diagnostic_machine.explain import as_json, explain
        from allocation.use_cases.diagnostic_machine.query import resolve

        text = " ".join(args.text)
        resolution = resolve(text)
        if not resolution.runnable:
            print("DIAGNOSTIC QUERY NOT RUN")
            print(f"  query: {text}")
            print(f"  evidence: {', '.join(resolution.evidence) or 'none'}")
            for missing in resolution.missing:
                print(f"  missing: {missing}")
            print("  No clinical value was invented and no auction was opened.")
            return 2
        scenario = SCENARIOS[resolution.scenario]()
        result = run_policy(config, scenario, None, regime=args.regime)
        print(
            as_json(result, scenario, config)
            if args.json
            else (
                f"resolved scenario={resolution.scenario} confidence={resolution.confidence:.2f} "
                f"evidence={','.join(resolution.evidence)}\n\n"
                + explain(result, scenario, config)
            )
        )
        return 0


    return 1  # pragma: no cover - argparse requires a subcommand


def _render(name: str, regime: str, result: SimulationResult) -> str:
    m = result.metrics
    lines = [
        "=" * 78,
        f"{name}   regime={regime}",
        "=" * 78,
        "",
        f"  requests {m.requests}   answered {m.answered}   diverted {m.diverted}   "
        f"expired {m.expired}   abandoned {m.abandoned}   pending {m.pending}",
        f"  auctions {m.auctions}   awarded {m.awarded}   violations {m.constraint_violations}",
        "",
        f"  timeliness {m.timeliness:.3f}   impact {m.management_impact_rate:.3f}   "
        f"delay {m.average_delay_minutes:.1f} min   utilisation {m.utilisation:.3f}",
        f"  wins {m.wins}",
        f"  burn {({k: round(v, 3) for k, v in m.burn_rate.items()})}",
        "",
        "  schedule:",
    ]
    for procedure in sorted(result.procedures, key=lambda p: p.starts_at):
        lines.append(
            f"    {procedure.starts_at:%H:%M}-{procedure.ends_at:%H:%M} "
            f"{procedure.machine_id:8} {procedure.agent.value:5} "
            f"{procedure.request_id:16} waited {procedure.delay_minutes:5.1f} min, "
            f"{procedure.deadline_slack_minutes:5.1f} min to spare"
        )
    unserved = [o for o in result.outcomes if o.procedure is None]
    if unserved:
        lines.append("")
        lines.append("  unserved:")
        for outcome in unserved:
            extra = f" -> {outcome.diverted_to.value}" if outcome.diverted_to else ""
            lines.append(f"    {outcome.request_id:16} {outcome.fate}{extra}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
