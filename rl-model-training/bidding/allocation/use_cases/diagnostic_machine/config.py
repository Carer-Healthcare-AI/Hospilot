"""Selecting a modality's caps and budget tables.

The diagnostic caps and pools live in ``use_cases/diagnostic_machine/config/`` rather than in
``allocation/config/``, for one reason: adding eight files to the shared config directory
would move ``config_version`` — a hash of every config file — for every bed auction in the
system, and a bed audit row would then claim a version that changed because a scanner was
added. Keeping them here means the bed family's versioning is untouched.

Everything else is reused. :func:`for_modality` merges the modality tables into the loaded
:class:`~allocation.config.Config` and then calls :meth:`Config.for_resource` — the same
per-resource selection, the same version restamping, the same refusal to pair one resource's
profile with another's tables. No diagnostic copy of that logic exists.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

# Deliberately importing the loader's own file-reading and digest helpers rather than
# re-implementing them: `caps_version` is a content hash, and a second implementation of
# "hash a config file" is a second chance for two files with identical content to stamp
# different versions.
from allocation.config import Config
from allocation.config.loader import _digest, _read
from allocation.use_cases.diagnostic_machine.contracts import DiagnosticModality
from allocation.use_cases.diagnostic_machine.profiles import MODALITIES, ModalityProfile

DIAGNOSTIC_CONFIG_DIR = Path(__file__).parent / "config"


@lru_cache(maxsize=1)
def diagnostic_rules() -> Mapping[str, Any]:
    """The diagnostic pathway rule table, with its own version.

    Deliberately NOT ``config.rule("pathway")``: that is the bed table, calibrated against a
    discharge forecast, and a diagnostic run must not load bed configuration at all.
    """
    table = _read(DIAGNOSTIC_CONFIG_DIR / "pathway.yaml")
    return table


@lru_cache(maxsize=1)
def diagnostic_reward_rules() -> Mapping[str, Any]:
    """The diagnostic reward weights, with their own version.

    Never ``config.reward`` — that is the bed objective, whose terms do not exist here and
    whose two verified defects this table is written to avoid.
    """
    return _read(DIAGNOSTIC_CONFIG_DIR / "reward.yaml")


def rules_version() -> str:
    """Version stamped on diagnostic rows, so a decision can be re-derived."""
    return str(diagnostic_rules().get("version", "unversioned"))


def unsigned_diagnostic_rules() -> Mapping[str, str]:
    """Diagnostic rule tables not yet signed off, as ``{table: status}``.

    The counterpart of ``Config.unsigned``, kept separate for the same reason the tables are:
    a bed run must not report a diagnostic table's status, and vice versa.
    """
    out: dict[str, str] = {}
    for table, name in (
        (diagnostic_rules(), "diagnostic.pathway"),
        (diagnostic_reward_rules(), "diagnostic.reward"),
    ):
        status = str(table.get("status", "unknown"))
        if status != "signed_off":
            out[name] = status
    return out


@lru_cache(maxsize=1)
def _tables() -> tuple[
    Mapping[str, Mapping[str, Any]], Mapping[str, str], Mapping[str, Mapping[str, Any]]
]:
    """Every diagnostic caps and budget file, read once per process.

    Cached for the same reason ``load_config`` reads at process start: config is a governance
    artifact pinned for the run, and re-reading it per auction would let a mid-run edit change
    what an auction was scored against without changing its stamped version.
    """
    caps_paths = sorted(DIAGNOSTIC_CONFIG_DIR.glob("caps_*.yaml"))
    budget_paths = sorted(DIAGNOSTIC_CONFIG_DIR.glob("budget_*.yaml"))
    if not caps_paths or not budget_paths:
        raise FileNotFoundError(
            f"no diagnostic caps/budget tables in {DIAGNOSTIC_CONFIG_DIR}; "
            "a modality cannot be scored or funded against another's table"
        )
    return (
        {p.name: _read(p) for p in caps_paths},
        {p.name: _digest(p) for p in caps_paths},
        {p.name: _read(p) for p in budget_paths},
    )


def with_diagnostic_tables(config: Config) -> Config:
    """``config`` with the diagnostic tables added alongside the bed ones.

    Additive, never destructive: the bed caps and pools stay in ``caps_files`` and
    ``budget_files`` exactly as loaded, so a config passed through here can still run a bed
    auction. The selected table is not changed — that is :meth:`Config.for_resource`'s job.
    """
    caps_files, caps_versions, budget_files = _tables()
    return replace(
        config,
        caps_files={**config.caps_files, **caps_files},
        caps_versions={**config.caps_versions, **caps_versions},
        budget_files={**config.budget_files, **budget_files},
    )


def for_modality(config: Config, modality: DiagnosticModality | ModalityProfile) -> Config:
    """``config`` scoped to one modality's caps table and budget pool.

    Accepts either the modality or its profile; the profile is what
    :meth:`Config.for_resource` actually reads, and looking it up here means a caller cannot
    hand-build a profile that points at the wrong tables.
    """
    profile = (
        modality
        if isinstance(modality, ModalityProfile)
        else MODALITIES.get(modality)
    )
    return with_diagnostic_tables(config).for_resource(profile)
