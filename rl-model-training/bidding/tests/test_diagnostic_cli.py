"""The diagnostic entry point, and the isolation between the two use cases.

The isolation tests are the point of this file. Requirement: a bed run must never load
diagnostic configuration, encoders, rewards, policies or artifacts, and a diagnostic run must
never load the bed equivalents. Checked by watching ``sys.modules`` across a real invocation
rather than by reading imports, because a lazy import inside a function is invisible to a
static check and very visible at runtime.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from allocation.use_cases.diagnostic_machine.cli import main

BED_MODULES = (
    "allocation.rl.encoder",
    "allocation.rl.qlearn",
    "allocation.reward",
    "allocation.sim",
    "allocation.pathway",
    "allocation.policy.heuristic",
)

DIAGNOSTIC_MODULES = (
    "allocation.use_cases.diagnostic_machine.encoder",
    "allocation.use_cases.diagnostic_machine.qlearn",
    "allocation.use_cases.diagnostic_machine.reward",
    "allocation.use_cases.diagnostic_machine.serving",
)


def loaded(prefixes: tuple[str, ...]) -> set[str]:
    return {
        name
        for name in sys.modules
        if any(name == p or name.startswith(p + ".") for p in prefixes)
    }


# -- the diagnostic entry point runs ---------------------------------------------------------


def test_scenarios_lists_every_scenario(capsys):
    assert main(["scenarios"]) == 0
    out = capsys.readouterr().out
    assert "three_way_contention" in out and "mri_scarcity" in out


def test_governance_reports_the_unsigned_tables(capsys):
    assert main(["governance"]) == 0
    out = capsys.readouterr().out
    assert "diagnostic.pathway" in out
    assert "diagnostic.reward" in out
    assert "unfitted" in out or "assumed" in out


def test_run_reports_a_schedule(capsys):
    assert main(["run", "three_way_contention"]) == 0
    out = capsys.readouterr().out
    assert "answered 3" in out
    assert "CT-01" in out


@pytest.mark.parametrize("regime", ["normal", "constrained", "exhausted"])
def test_run_accepts_every_regime(capsys, regime):
    assert main(["run", "three_way_contention", "--regime", regime]) == 0
    assert f"regime={regime}" in capsys.readouterr().out










# -- isolation ---------------------------------------------------------------------------------


def _modules_after(probe: str) -> list[str]:
    """Run ``probe`` in a clean interpreter and report which modules it left loaded.

    A SUBPROCESS, not ``del sys.modules[...]``. Deleting entries in-process is unreliable —
    other test modules have already imported the bed RL stack at module scope and hold live
    references, so the check reports pollution from the test session rather than from the run
    under test. A fresh interpreter is the only honest way to ask what one command loads.
    """
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, cwd=Path.cwd()
    )
    assert result.returncode == 0, result.stderr
    # The command under test prints its own report, so the answer is carried on a MARKED
    # line rather than by redirecting stdout — redirection inside the probe fights pytest's
    # capture and tells you nothing about imports.
    marked = [
        line for line in result.stdout.splitlines() if line.startswith("__MODULES__")
    ]
    assert marked, "probe produced no marker line:\n" + result.stdout[-2000:]
    return json.loads(marked[-1].removeprefix("__MODULES__"))


BED_PREFIXES = ("allocation.rl", "allocation.sim", "allocation.reward", "allocation.pathway")


def test_a_diagnostic_run_loads_no_bed_rl_or_simulator():
    """Requirement 7: never load bed encoders, rewards, policies or weights."""
    leaked = _modules_after(
        "import sys, json;"
        "from allocation.use_cases.diagnostic_machine.cli import main;"
        "main(['run', 'three_way_contention']);"
        "print('__MODULES__' + json.dumps(sorted("
        f"n for n in sys.modules if n.startswith({BED_PREFIXES!r}))))"
    )
    assert not leaked, f"a diagnostic run loaded bed modules: {leaked}"




def test_a_bed_run_loads_no_diagnostic_code():
    """Requirement 6: never import diagnostic config, encoders, rewards or policies."""
    leaked = _modules_after(
        "import sys, json, allocation.cli, allocation.trigger.runtime;"
        "import allocation.rl.policy, allocation.policy.heuristic;"
        "print('__MODULES__' + json.dumps(sorted(n for n in sys.modules "
        "if n.startswith('allocation.use_cases.diagnostic_machine'))))"
    )
    assert not leaked, f"the bed runtime imported diagnostic modules: {leaked}"


def test_a_real_bed_auction_loads_no_diagnostic_code():
    """The strongest form: an actual bed run, end to end, in a clean interpreter."""
    leaked = _modules_after(
        "import sys, json;"
        "from allocation.cli import main;"
        "main(['--json']);"
        "print('__MODULES__' + json.dumps(sorted(n for n in sys.modules "
        "if n.startswith('allocation.use_cases.diagnostic_machine'))))"
    )
    assert not leaked, f"a bed auction imported diagnostic modules: {leaked}"




def test_diagnostic_config_is_not_in_the_bed_config_dir():
    """Adding files there would move `config_version` for every bed auction."""
    from allocation.config.loader import CONFIG_DIR
    from allocation.use_cases.diagnostic_machine.config import DIAGNOSTIC_CONFIG_DIR

    assert DIAGNOSTIC_CONFIG_DIR != CONFIG_DIR
    assert not str(DIAGNOSTIC_CONFIG_DIR).startswith(str(CONFIG_DIR))

    # Every diagnostic table lives in the diagnostic directory...
    for name in ("caps_ct.yaml", "budget_ct.yaml", "pathway.yaml", "reward.yaml"):
        assert (DIAGNOSTIC_CONFIG_DIR / name).exists()

    # ...and no per-modality table has been added to the bed directory, which is what would
    # move `config_version` for every bed auction.
    from allocation.use_cases.diagnostic_machine.contracts import DiagnosticModality

    for modality in DiagnosticModality:
        assert not (CONFIG_DIR / f"caps_{modality.value}.yaml").exists()
        assert not (CONFIG_DIR / f"budget_{modality.value}.yaml").exists()


def test_the_two_reward_tables_are_different_documents():
    """`reward.yaml` exists in both trees. They must not be the same objective."""
    from allocation.config import load_config
    from allocation.use_cases.diagnostic_machine.config import diagnostic_reward_rules

    bed = load_config().reward
    diagnostic = diagnostic_reward_rules()
    assert set(diagnostic["weights"]) != set(bed.get("terms", bed).keys())
    assert "answered" in diagnostic["weights"]
    assert "budget_cost" in diagnostic["weights"], (
        "the cost term is the whole point of the diagnostic objective"
    )
