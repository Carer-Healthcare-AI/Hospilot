"""A diagnostic Q artifact records what it was fitted to MAXIMISE.

Loading refuses anything else.

The encoder and action-space hashes catch a *layout* mismatch. They cannot catch an *objective*
mismatch, and that is the harder failure: weights fitted against the old fixed-per-decision
discount are perfectly layout-compatible with this code. They would load, run, and serve in
silence — having been trained under an objective that pays for keeping a request bidding and for
going quiet on one it has given up on. ``training_objective_version`` closes that.

The mirror of ``test_critical_care_artifact_compat.py``, which exists for the same reason and was
written after the same defect was found in the critical-care family.
"""

from __future__ import annotations

import json

import pytest

from allocation.use_cases.diagnostic_machine.encoder import FEATURES, encoder_version
from allocation.use_cases.diagnostic_machine.masks import ACTIONS, action_space_version
from allocation.use_cases.diagnostic_machine.qlearn import (
    RETIRED_OBJECTIVES,
    TRAINING_OBJECTIVE_VERSION,
    DiagnosticQPolicy,
    IncompatibleObjective,
)


def _fitted() -> DiagnosticQPolicy:
    """A policy carrying the metadata ``fit`` would have stamped, without running a fit."""
    policy = DiagnosticQPolicy()
    policy.fit_counts = [50] * len(ACTIONS)
    policy.gamma = 0.9
    policy.discount_horizon_minutes = 60.0
    policy.trained_on = 1234
    return policy


def _write(path, **overrides):
    """A saved artifact with fields overridden, for the refusal cases."""
    _fitted().save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key, value in overrides.items():
        if value is _DELETE:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class _Delete:
    pass


_DELETE = _Delete()


def test_a_fresh_policy_declares_the_current_objective():
    assert DiagnosticQPolicy().training_objective_version == TRAINING_OBJECTIVE_VERSION


def test_the_retired_objective_is_named_and_explained():
    """A retired objective must say what was wrong with it, or the refusal is unactionable."""
    assert "diagnostic_td_index_v0" in RETIRED_OBJECTIVES
    reason = RETIRED_OBJECTIVES["diagnostic_td_index_v0"]
    assert "decision index" in reason
    assert TRAINING_OBJECTIVE_VERSION not in RETIRED_OBJECTIVES


def test_fit_records_the_discount_it_actually_used(tmp_path):
    """The recorded gamma and horizon come from the call, not from the reader's defaults."""
    from allocation.contracts import AgentKind
    from allocation.use_cases.diagnostic_machine.config import rules_version
    from allocation.use_cases.diagnostic_machine.dataset import (
        DiagnosticDataset,
        DiagnosticTransition,
    )

    rows = tuple(
        DiagnosticTransition(
            request_id=f"r{i}", agent=AgentKind.ER, step=0,
            state=tuple(0.1 for _ in FEATURES), action=i % len(ACTIONS),
            action_mask=tuple(True for _ in ACTIONS), reward=-10.0,
            next_state=None, next_mask=None, done=True, fate="expired",
            elapsed_minutes=30.0,
        )
        for i in range(12)
    )
    dataset = DiagnosticDataset(
        transitions=rows, encoder_version=encoder_version(),
        action_space_version=action_space_version(), reward_version="test",
        rules_version=rules_version(),
    )
    policy = DiagnosticQPolicy().fit(
        dataset, epochs=1, lr=0.001, gamma=0.8, discount_horizon_minutes=45.0
    )
    assert policy.gamma == 0.8
    assert policy.discount_horizon_minutes == 45.0
    assert policy.training_objective_version == TRAINING_OBJECTIVE_VERSION


def test_save_then_load_round_trips_the_training_metadata(tmp_path):
    path = tmp_path / "q.json"
    _fitted().save(path)
    loaded = DiagnosticQPolicy.load(path)
    assert loaded.training_objective_version == TRAINING_OBJECTIVE_VERSION
    assert loaded.gamma == 0.9
    assert loaded.discount_horizon_minutes == 60.0


def test_the_saved_file_actually_carries_the_fields(tmp_path):
    path = tmp_path / "q.json"
    _fitted().save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["training_objective_version"] == TRAINING_OBJECTIVE_VERSION
    assert data["gamma"] == 0.9
    assert data["discount_horizon_minutes"] == 60.0


def test_a_legacy_artifact_with_no_objective_is_refused(tmp_path):
    """Every artifact written before the fix predates the clinical-time objective.

    Silence is the dangerous case: such weights match the encoder and the action space exactly.
    """
    path = _write(tmp_path / "q.json", training_objective_version=_DELETE)
    with pytest.raises(IncompatibleObjective, match="records no training_objective_version"):
        DiagnosticQPolicy.load(path)


def test_a_retired_objective_is_refused_by_name(tmp_path):
    path = _write(tmp_path / "q.json", training_objective_version="diagnostic_td_index_v0")
    with pytest.raises(IncompatibleObjective, match="retired objective"):
        DiagnosticQPolicy.load(path)


def test_an_unknown_future_objective_is_refused(tmp_path):
    """Forward-incompatible too: an objective this code does not serve must not be served."""
    path = _write(tmp_path / "q.json", training_objective_version="diagnostic_td_something_v9")
    with pytest.raises(IncompatibleObjective, match="fitted to maximise"):
        DiagnosticQPolicy.load(path)


def test_a_claimed_clock_objective_with_no_horizon_is_refused(tmp_path):
    """A zero horizon is what a decision-index fit leaves behind, whatever the label claims."""
    path = _write(tmp_path / "q.json", discount_horizon_minutes=0.0)
    with pytest.raises(IncompatibleObjective, match="discount_horizon_minutes"):
        DiagnosticQPolicy.load(path)


def test_a_foreign_artifact_is_still_refused_by_kind(tmp_path):
    """A critical-care or bed artifact loaded here would run and mean nothing."""
    path = _write(tmp_path / "q.json", kind="critical_care_q_linear")
    with pytest.raises(ValueError, match="not a diagnostic Q artifact"):
        DiagnosticQPolicy.load(path)


def test_an_encoder_mismatch_is_still_refused(tmp_path):
    path = _write(tmp_path / "q.json", encoder_version="deadbeefcafe")
    with pytest.raises(ValueError, match="fitted under encoder"):
        DiagnosticQPolicy.load(path)


def test_the_report_states_the_objective_and_the_discount():
    """"The model trained" is not evidence of what it trained to maximise."""
    report = _fitted().report()
    assert TRAINING_OBJECTIVE_VERSION in report
    assert "horizon" in report
    assert "60 min" in report
