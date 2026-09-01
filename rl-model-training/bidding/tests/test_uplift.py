"""Ceiling uplift — the B.9 interim. RL-Steps section 8.2, D.9.

The uplift is the only unsigned table that can **reallocate a bed** rather than distort a
score, so these tests are as much about it staying off and staying bounded as about it working.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from allocation.contracts import AgentKind, VitalsReading
from allocation.ingest import fixtures as fx
from allocation.trigger.runtime import run_allocation
from allocation.utility.ceiling import ceiling_for
from allocation.utility.uplift import enabled, estimate


@pytest.fixture
def uplift_on(config):
    """The shipped default. Named explicitly so the tests below read unambiguously."""
    return config


@pytest.fixture
def uplift_off(config):
    """D.9's fallback, ``Ceiling = U`` — what ``--no-uplift`` produces."""
    return replace(
        config, rules={**config.rules, "uplift": {**config.rules["uplift"], "enabled": False}}
    )


def _run(cfg, patients=None):
    return run_allocation(
        config=cfg,
        source=fx.FixtureDataSource(patients=patients or fx.PATIENT_DATA),
        candidates=fx.CANDIDATES,
        now=fx.NOW,
        query="one limited ICU bed",
    )


def _est(cfg, cid="ER-Patient-A"):
    return estimate(cfg, fx.PATIENT_DATA[cid], fx.NOW, 4.0)


# -- it is on, as a declared assumption ---------------------------------------------------


def test_uplift_is_enabled_as_an_assumption(config):
    """On by default, at the same standing as every other unsigned rule table.

    ``icu_benefit.yaml`` and ``units.yaml`` are unsigned and active. Leaving this one off
    while those run would mean silently not implementing D.9 at all, which is the larger
    deviation — RL-Steps is explicit that the ceiling sits above utility.
    """
    assert enabled(config)
    assert _est(config).value > 0.0


def test_it_is_reported_as_unsigned(config):
    """A table that can change the winner must never be invisible at load."""
    assert "uplift" in config.unsigned


def test_no_uplift_falls_back_to_ceiling_equals_utility(uplift_off):
    """``--no-uplift`` must reproduce D.9's fallback exactly, for comparison."""
    assert not enabled(uplift_off)
    run = _run(uplift_off)
    for cid, breakdown in run.utilities.items():
        assert run.ceilings[cid] == pytest.approx(breakdown.total)


# -- when on ------------------------------------------------------------------------------


def test_a_deteriorating_patient_gets_a_ceiling_above_its_utility(uplift_on):
    """RL-Steps' Ward case: values the bed at 86, willing to bid 105.

    Without an uplift that behaviour cannot occur at all — an agent can only ever fight for
    who a patient is, never for who they are about to become.
    """
    run = _run(uplift_on)
    assert run.ceilings["ER-Patient-A"] > run.utilities["ER-Patient-A"].total


def test_a_stable_patient_gets_no_uplift(uplift_on):
    """OT's patient is anaesthetised and stable. A flat NEWS2 must not buy headroom."""
    run = _run(uplift_on)
    assert run.ceilings["OT-Patient-B"] == pytest.approx(run.utilities["OT-Patient-B"].total)


def test_bids_may_now_exceed_utility_but_never_the_ceiling(uplift_on):
    """The invariant that actually matters, restated under a raised ceiling."""
    run = _run(uplift_on)
    exceeded = False
    for round_state in run.outcome.result.rounds:
        for bid in round_state.bids:
            assert bid.amount <= bid.ceiling + 1e-9
            exceeded = exceeded or bid.amount > bid.utility
    assert exceeded, "with uplift on, some bid should pass its own utility"


# -- conservatism -------------------------------------------------------------------------


def test_an_improving_patient_never_gets_a_ceiling_below_utility(uplift_on):
    """A ceiling under U is not defined — the agent already values the bed at U today."""
    improving = fx.ER_VITALS[:1] + (
        replace(fx.ER_VITALS[-1], spo2=99, respiratory_rate=14, gcs=15, pulse=70,
                bp_systolic=126, temperature=36.8, is_critical=False, on_oxygen=False),
    )
    patients = dict(fx.PATIENT_DATA)
    patients["ER-Patient-A"] = replace(patients["ER-Patient-A"], vitals=improving)

    run = _run(uplift_on, patients)
    assert run.ceilings["ER-Patient-A"] == pytest.approx(
        run.utilities["ER-Patient-A"].total
    )


def test_uplift_is_capped(uplift_on):
    """Even a catastrophic trajectory cannot more than half again the willingness to pay."""
    catastrophic = fx.WARD_VITALS + (
        VitalsReading(fx.NOW - timedelta(minutes=1), temperature=40.0, pulse=150,
                      bp_systolic=70, spo2=78, respiratory_rate=38, gcs=8, on_oxygen=True),
    )
    patients = dict(fx.PATIENT_DATA)
    patients["Ward-Patient-C"] = replace(patients["Ward-Patient-C"], vitals=catastrophic)

    est = estimate(uplift_on, patients["Ward-Patient-C"], fx.NOW, 4.0)
    assert est.value <= float(uplift_on.rule("uplift")["max_uplift"])


def test_one_reading_is_not_a_trend(uplift_on):
    """Guessing "stable" about a patient nobody measured twice is the absent-factor mistake."""
    patients = dict(fx.PATIENT_DATA)
    patients["ER-Patient-A"] = replace(patients["ER-Patient-A"], vitals=fx.ER_VITALS[:1])

    est = estimate(uplift_on, patients["ER-Patient-A"], fx.NOW, 4.0)
    assert est.value == 0.0
    assert "fewer than" in est.note


class _FakeBreakdown:
    total = 100.0


def test_ceiling_for_rejects_a_negative_uplift():
    """Belt and braces on the D.9 formula itself, below the band table."""
    with pytest.raises(ValueError, match="non-negative"):
        ceiling_for(_FakeBreakdown(), -0.1)


# -- what turning it on actually changes --------------------------------------------------


def test_the_uplift_changes_bids_not_the_utility_ranking(uplift_on, uplift_off):
    """The uplift is *supposed* to change how hard agents fight, not who deserves the bed.

    Utility is untouched by it — only the ceiling moves — so the ordering that decides the
    allocation is identical either way. Bands that reordered the field would be doing
    something the framework does not ask for.
    """
    off, on = _run(uplift_off), _run(uplift_on)

    def order(run):
        return sorted(run.utilities, key=lambda c: -run.utilities[c].total)

    assert order(off) == order(on)
    for cid in on.utilities:
        assert on.utilities[cid].total == pytest.approx(off.utilities[cid].total)
    assert on.outcome.result.winning_bid >= off.outcome.result.winning_bid