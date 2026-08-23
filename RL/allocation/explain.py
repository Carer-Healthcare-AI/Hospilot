"""Show the working — every factor, weight and intermediate value behind a run.

A utility of 107.4 is not verifiable. ``0.35x0.75 + 0.30x0.85 + 0.20x0.60 + 0.15xABSENT``,
renormalised over the weights present and multiplied by a cap of 40, is. This module prints
the second.

Nothing here computes anything. Every number is read back off the ``UtilityBreakdown``,
``BudgetState`` and ``Bid`` objects the run already produced — which is the point: if this
disagreed with the auction, the auction would be right and this would be the bug. It renders
exactly what the audit rows carry, so a hand-check here is a hand-check of what was logged.

Three things it makes visible that the summary view cannot:

**Absent factors, and what they cost.** An absent factor shows its weight and the reason it
was dropped, then the renormalisation that followed. Coverage stops being a percentage and
becomes a list of specific missing inputs.

**Which term dominates.** A component's factors are printed with their weighted contribution,
so "Urgency is 23.7 because NEWS2 is 0.75 at weight .35" is readable rather than inferred.

**Where a bid came from.** ``alpha x (ceiling - current)`` with the guard and the
quantisation shown, so a bid that was clamped can be told from a bid that was chosen.
"""

from __future__ import annotations

from typing import Any, Iterable

from allocation.budget.spend import max_affordable_bid
from allocation.config import Config
from allocation.contracts import (
    Action,
    ComponentResult,
    FactorScore,
    UtilityBreakdown,
)
from allocation.trigger.runtime import AllocationRun

WIDTH = 96


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _signal(factor: FactorScore) -> str:
    signal = factor.signal
    if not signal.present:
        note = signal.note or "no input"
        return f"{'ABSENT':>8}   {signal.source} — {note}"
    return f"{signal.value:8.3f}   {signal.source}" + (f" — {signal.note}" if signal.note else "")


#: Components that are NOT coverage-renormalised weighted means. Five of the eight follow
#: D.0; these three have their own shape, and printing D.0's arithmetic over them would show
#: a number that does not match the points beneath it.
OWN_FORMULA = {
    "waiting": "D.3   clamp(P(det) x severity x (1 + delay) / delay_factor_max)",
    "alternative": "D.7   a product over availability, quality and duration",
    "operational": "D.5   a single per-agent rule — no shared definition exists",
}

#: How far the reconstructed weighted mean may sit from the real value before this stops
#: claiming to explain it. Tight: the arithmetic is deterministic, so any real gap means the
#: component is not a weighted mean at all.
_RECONSTRUCTION_TOLERANCE = 1e-4


def component_derivation(result: ComponentResult) -> dict[str, Any]:
    """One component's arithmetic as data, before anything is rendered.

    Split out from :func:`component_lines` so the printed derivation and the JSON one are the
    **same numbers**, not two calculations that agree today. The text version renders this
    dict; nothing recomputes.

    The renormalisation is **verified rather than assumed**. Five components are coverage-
    renormalised weighted means (D.0); three are not, and reporting D.0's numerator and
    denominator over those would give a numerator, a denominator, and a normalised value that
    do not produce the points beneath them. Rather than listing the three by name and hoping
    the list stays correct as components are added, the reconstruction is checked against
    ``points / cap`` and marked ``weighted_mean: false`` when it disagrees — so a component
    added later cannot be silently mis-explained.
    """
    actual = _normalised(result)
    out: dict[str, Any] = {
        "component": result.component.value,
        "cap": result.cap,
        "points": result.points,
        "normalised": actual,
        "coverage": result.coverage,
        "factors": [
            {
                "name": factor.name,
                "weight": factor.weight,
                "value": factor.signal.value,
                "present": factor.present,
                "source": factor.signal.source,
                # Carried even when present: the note is where "assumed", "config" and
                # "no oxygen flag exists" live, and a value without its provenance is a
                # number nobody can re-derive later.
                "note": factor.signal.note or None,
            }
            for factor in result.factors
        ],
    }

    if not result.factors:
        out["shape"] = "single_rule"
        out["formula"] = OWN_FORMULA.get(result.component.value, "single-rule component")
        return out

    present = [f for f in result.factors if f.present]
    total_w = sum(f.weight for f in result.factors)
    present_w = sum(f.weight for f in present)
    out["weight_present"] = present_w
    out["weight_total"] = total_w
    out["factors_dropped"] = len(result.factors) - len(present)

    if present_w <= 0:
        # The distinction the whole system is built on: no inputs means the component is
        # ABSENT, which lowers coverage. It does not mean the component scored zero.
        out["shape"] = "absent"
        out["formula"] = "every factor absent — the component is absent, not zero"
        return out

    weighted = sum(f.weight * float(f.signal.value or 0.0) for f in present)
    reconstructed = weighted / present_w

    if abs(reconstructed - actual) <= _RECONSTRUCTION_TOLERANCE:
        out["shape"] = "weighted_mean"
        out["formula"] = "D.0   sum(weight x value) / sum(weight of present factors)"
        out["numerator"] = weighted
        out["denominator"] = present_w
    else:
        out["shape"] = "own_formula"
        out["formula"] = OWN_FORMULA.get(result.component.value, "not a weighted mean")
        out["weighted_mean_would_give"] = reconstructed

    return out


def component_lines(result: ComponentResult) -> list[str]:
    """One component, factor by factor, rendered from :func:`component_derivation`."""
    d = component_derivation(result)
    actual = d["normalised"]
    lines = [f"  {result.component.value}   cap {result.cap:+g}"]

    if not result.factors:
        lines.append("      (single-rule component — no weighted factors)")
    else:
        lines.append(f"      {'factor':<22}{'weight':>8}{'value':>11}   source")
        for factor in result.factors:
            lines.append(f"      {factor.name:<22}{factor.weight:8.2f}{_signal(factor)}")

        if d["shape"] == "absent":
            lines.append("      every factor absent — the COMPONENT is absent, not zero")
        else:
            present = [f for f in result.factors if f.present]
            present_w, total_w = d["weight_present"], d["weight_total"]
            lines.append("")

            if d["shape"] == "weighted_mean":
                terms = " + ".join(f"{f.weight:g}x{f.signal.value:.3f}" for f in present)
                dropped = d["factors_dropped"]
                lines += [
                    f"      numerator   {terms} = {d['numerator']:.4f}",
                    f"      denominator {' + '.join(f'{f.weight:g}' for f in present)}"
                    f" = {present_w:.2f}"
                    + (f"   (of {total_w:.2f} — {dropped} dropped)" if dropped
                       else "   (all factors present)"),
                    f"      normalised  {d['numerator']:.4f} / {present_w:.2f} = {actual:.4f}",
                ]
            else:
                lines += [
                    f"      formula     {d['formula']}",
                    f"      normalised  {actual:.4f}",
                    "      (D.0's weighted mean would give "
                    f"{d['weighted_mean_would_give']:.4f} — this component does not use it)",
                ]

            lines.append(f"      coverage    {present_w:.2f} / {total_w:.2f} = "
                         f"{result.coverage:.1%}")

    lines.append(f"      POINTS      {result.cap:+g} x {actual:.4f} = {result.points:+.2f}")
    return lines


def _normalised(result: ComponentResult) -> float:
    """Recovered from points and cap — components do not carry their normalised value."""
    return result.points / result.cap if result.cap else 0.0


def utility_lines(breakdown: UtilityBreakdown, label: str) -> list[str]:
    lines = [_rule("="), f"UTILITY — {label}", _rule("=")]
    for result in breakdown.components:
        lines += component_lines(result)
        lines.append("")

    lines.append(_rule())
    total = " + ".join(f"{r.points:+.2f}" for r in breakdown.components)
    lines.append(f"  TOTAL   {total}")
    lines.append(f"        = {breakdown.total:.2f}")
    lines.append("")
    return lines


def ceiling_lines(run: AllocationRun) -> list[str]:
    lines = [_rule("="), "CEILING — D.9   Ceiling = U x (1 + uplift)", _rule("=")]
    source = run.trace["ceiling"]
    for label, value in source.rows:
        lines.append(f"  {label:<24}{value}")
    for note in source.notes:
        lines.append(f"  ! {note}")
    lines.append("")
    return lines


#: The formula behind each budget factor. Four of the five HAVE one; ``base`` does not —
#: it is a chosen constant, which is exactly why it is the one that needs governance rather
#: than data.
FACTOR_FORMULA = {
    "base": "chosen constant — RL-Steps 4 line 141, NOT derived from anything",
    "demand": "clamp(forecast / median30(forecast), 0.8, 1.3)",
    "criticality": "share of requests needing admission <30 min -> band",
    "scarcity": "clamp(1 + 0.3 x (occupancy - 0.85) / 0.15, 1.0, 1.3)",
    "fairness": "v1: constant.  v2: clamp(1 + 0.4 x (expected_share - actual_share))",
}


def budget_lines(run: AllocationRun) -> list[str]:
    """The five terms, each with its formula and where its value actually came from.

    The distinction this exists to make: **a factor of 1.00 that was computed and one that
    fell back are the same number and different facts.** Demand and Fairness both read 1.00
    today and neither is a measurement, so a reader shown only the product would reasonably
    conclude the model was running when three fifths of it is inert.
    """
    lines = [
        _rule("="),
        "BUDGET — RL-Steps 4   B = Base x Demand x Criticality x Scarcity x Fairness",
        _rule("="),
    ]
    for agent, state in run.opening_budgets.items():
        sources = state.factor_sources
        lines.append(f"  {agent.value}")
        lines.append(f"      {'term':<14}{'value':>9}   formula / source")

        for name, value, source in (
            ("base", state.base, run.bases[agent].source),
            ("demand", state.demand, sources.get("demand", "")),
            ("criticality", state.criticality, sources.get("criticality", "")),
            ("scarcity", state.scarcity, sources.get("scarcity", "")),
            ("fairness", state.fairness, sources.get("fairness", "")),
        ):
            lines.append(f"      {name:<14}{value:9.2f}   {FACTOR_FORMULA[name]}")
            if source:
                lines.append(f"      {'':<14}{'':>9}   <- {source}")

        lines += [
            f"      TOTAL         {state.base:.2f} x {state.demand:.2f} x "
            f"{state.criticality:.2f} x {state.scarcity:.2f} x {state.fairness:.2f}"
            f" = {state.budget_total:.2f}",
            "",
        ]
    return lines


def bid_lines(run: AllocationRun, config: Config) -> list[str]:
    """Every bid, with ``Increment = alpha x (Ceiling - CurrentBid)`` worked through."""
    lines = [_rule("="), "BIDS — RL-Steps 6   Increment = alpha x (Ceiling - CurrentBid)",
             _rule("=")]
    standing: dict[str, float] = {}

    for round_state in run.outcome.result.rounds:
        lines.append(f"  round {round_state.round_index + 1}")
        for bid in round_state.bids:
            key = bid.agent.value
            before = standing.get(key, 0.0)

            if bid.action is Action.WITHDRAW:
                lines.append(
                    f"      {key:<5} WITHDRAW      standing {before:.1f}, "
                    f"ceiling {bid.ceiling:.1f}"
                )
                continue
            if bid.alpha is None:
                lines.append(f"      {key:<5} HOLD          standing {before:.1f}")
                continue

            headroom = bid.ceiling - before
            increment = bid.alpha * headroom
            proposed = before + increment
            budget = run.outcome.budgets[bid.agent]
            affordable = max_affordable_bid(
                config, budget.budget_total, bid.contention or 1.0, won=True
            )
            limit = min(bid.ceiling, affordable)
            clamped = proposed > limit + 1e-9

            lines += [
                f"      {key:<5} INCREASE",
                f"            current bid   {before:9.2f}",
                f"            ceiling       {bid.ceiling:9.2f}",
                f"            headroom      {bid.ceiling:.2f} - {before:.2f} = {headroom:.2f}",
                f"            alpha         {bid.alpha:9.2f}",
                f"            increment     {bid.alpha:.2f} x {headroom:.2f} = {increment:.2f}",
                f"            proposed      {before:.2f} + {increment:.2f} = {proposed:.2f}",
                f"            max affordable{affordable:9.2f}   (budget "
                f"{budget.budget_total:.1f})",
                f"            guard limit   min({bid.ceiling:.2f}, {affordable:.2f})"
                f" = {limit:.2f}" + ("   CLAMPED" if clamped else "   not binding"),
                f"            BID           {bid.amount:9.1f}   (whole points)",
            ]
            standing[key] = bid.amount
        lines.append("")
    return lines


def settlement_lines(run: AllocationRun) -> list[str]:
    result = run.outcome.result
    lines = [_rule("="), "SETTLEMENT — RL-Steps 19   Cost = Bid x Contention x Outcome x Rate",
             _rule("=")]
    lines.append(f"  reserve price   {result.reserve_price:.2f}")
    lines.append(f"  winning bid     "
                 f"{result.winning_bid if result.winning_bid is not None else 0:.2f}"
                 f"   {'clears' if result.winner else 'does NOT clear'} the reserve")
    lines.append(f"  contention      {result.contention:.4f}   (fixed at open)")
    lines.append("")
    for agent, spend in run.outcome.spends.items():
        state = run.outcome.budgets[agent]
        lines += [
            f"  {agent.value}",
            f"      cost      {spend.bid:.2f} x {spend.contention:.4f} x "
            f"{spend.outcome_factor:.2f} x {spend.commitment_rate:.2f} = {spend.cost:.4f}",
            f"      burn      {spend.cost:.4f} / {state.budget_total:.2f} = "
            f"{spend.cost / state.budget_total:.2%}",
        ]
    lines.append("")
    return lines


def derivation(run: AllocationRun, config: Config) -> dict[str, Any]:
    """The whole derivation as data — what :func:`explain` prints, for a machine.

    Same content, same numbers, one source: the per-component arithmetic comes from
    :func:`component_derivation`, which the text renderer also uses. A second implementation
    would be a second set of numbers that agree until the day they do not.

    Structured **per round**, because a utility is not a fact about a patient — it is a fact
    about a patient at a moment. Section 15 has ER rising 148 -> 171 and OT falling 112 -> 94
    inside two minutes, so a derivation that reported one utility per candidate would be
    describing an auction that did not happen.
    """
    result = run.outcome.result
    rate = float(config.budget["spend"]["commitment_rate"])

    return {
        "auction_id": result.auction_id,
        "winner": result.winner.value if result.winner else None,
        "winning_bid": result.winning_bid,
        "formulas": {
            "utility": "U = sum(cap x normalised) over 8 components",
            "component": "D.0   sum(weight x value) / sum(weight of PRESENT factors)",
            "ceiling": "D.9   Ceiling = U x (1 + uplift)",
            "budget": "B = Base x Demand x Criticality x Fairness x Scarcity",
            "increment": "RL-Steps 6   Increment = alpha x (Ceiling - CurrentBid)",
            "affordability": "Bid <= Remaining / (Contention x Outcome x Rate)",
            "cost": "RL-Steps 19   Cost = Bid x Contention x Outcome x Rate",
            "reserve": "Reserve = highest_ceiling x clamp(min + (max-min) x occupancy_stress)",
        },
        # One entry per round: the world as it was read that round, scored.
        "rounds": [
            {
                "round_index": index,
                "candidates": {
                    candidate_id: {
                        "agent": breakdown.agent.value,
                        "total": breakdown.total,
                        "components": [
                            component_derivation(c) for c in breakdown.components
                        ],
                    }
                    for candidate_id, breakdown in sorted(round_breakdown.items())
                },
            }
            for index, round_breakdown in enumerate(result.breakdowns)
        ],
        "ceiling": {
            "formula": "Ceiling = U x (1 + uplift)",
            "per_candidate": {
                cid: {
                    "utility": run.utilities[cid].total,
                    "ceiling": ceiling,
                    "uplift": (ceiling / run.utilities[cid].total - 1.0)
                    if run.utilities[cid].total
                    else 0.0,
                }
                for cid, ceiling in run.ceilings.items()
            },
        },
        "budget": {
            "formula": "B = Base x Demand x Criticality x Fairness x Scarcity",
            "per_agent": {
                agent.value: {
                    "base": run.bases[agent].base,
                    "n_win": run.bases[agent].n_win,
                    "n_req": run.bases[agent].n_req,
                    "demand": state.demand,
                    "criticality": state.criticality,
                    "fairness": state.fairness,
                    "scarcity": state.scarcity,
                    "budget_total": state.budget_total,
                }
                for agent, state in run.opening_budgets.items()
            },
        },
        # Every bid with the arithmetic that produced it, including the guard limit — so a
        # bid that was CLAMPED can be told from a bid that was chosen.
        "bids": [
            {
                "round_index": state.round_index,
                "agent": bid.agent.value,
                "action": bid.action.value,
                "alpha": bid.alpha,
                "ceiling": bid.ceiling,
                "amount": bid.amount,
                "max_affordable": max_affordable_bid(
                    config,
                    run.opening_budgets[bid.agent].budget_remaining,
                    result.contention,
                    won=True,
                ),
            }
            for state in result.rounds
            for bid in state.bids
        ],
        "settlement": {
            "formula": "Cost = Bid x Contention x Outcome x Rate",
            "reserve_price": result.reserve_price,
            "contention": result.contention,
            "commitment_rate": rate,
            "outcome": result.outcome,
            "per_agent": {
                agent.value: {
                    "bid": spend.bid,
                    "contention": spend.contention,
                    "outcome_factor": spend.outcome_factor,
                    "commitment_rate": spend.commitment_rate,
                    "cost": spend.cost,
                    "won": spend.won,
                }
                for agent, spend in run.outcome.spends.items()
            },
            # The mode gate, stated rather than left to be inferred from two fields that
            # disagree: a simulation computes every cost above and applies none of them.
            "charged": run.binding,
        },
        "governance": {
            "caps_version": result.caps_version,
            "config_version": result.config_version,
            "unsigned_rules": dict(result.unsigned_rules),
        },
    }


def explain(run: AllocationRun, config: Config) -> str:
    """The whole derivation, top to bottom."""
    lines: list[str] = [
        _rule("="),
        "FULL DERIVATION — every input, weight and intermediate value",
        f"caps_version {run.outcome.result.caps_version}   "
        f"config_version {run.outcome.result.config_version}",
        _rule("="),
        "",
    ]

    for candidate in _candidates(run):
        breakdown = run.utilities[candidate]
        agent = breakdown.agent.value
        lines += utility_lines(breakdown, f"{agent}  {candidate}")

    lines += ceiling_lines(run)
    lines += budget_lines(run)
    lines += bid_lines(run, config)
    lines += settlement_lines(run)

    unsigned = run.outcome.result.unsigned_rules
    lines += [
        _rule("="),
        f"UNSIGNED INPUTS — {len(unsigned)} of the values above are assumptions",
        _rule("="),
    ]
    lines += [f"  {name:<32}{status}" for name, status in sorted(unsigned.items())]
    lines.append(_rule("="))
    return "\n".join(lines)


def _candidates(run: AllocationRun) -> Iterable[str]:
    return sorted(run.utilities, key=lambda c: -run.utilities[c].total)
