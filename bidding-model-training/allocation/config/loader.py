"""Typed configuration access, with content-addressed versioning.

Two things this module exists to guarantee:

**Nothing is unversioned.** ``caps_version`` is a hash of the caps file *actually used* and
``config_version`` a hash of every config file. Both are stamped on every utility and every
budget row. Because budgets are denominated in utility points, a cap change re-derives every
budget in the system (BA8); a stored row with no version cannot be re-derived, and B.13 cap
fitting needs exactly that. Versions are computed, never hand-maintained, so they cannot
drift from the file they describe.

**Caps are per resource type.** There is one ``caps_<resource>.yaml`` per auctionable
resource, and :meth:`Config.for_resource` selects one. This matters for more than tidiness: if
every resource shared a caps file, a ward-bed auction and an ICU-bed auction would stamp
identical ``caps_version`` values and every audit row would claim a calibration it was not
scored under — which breaks B.13 cap fitting and any later re-derivation.

Every caps file is read at load time, not at auction time. Config stays pinned at process
start via ``--config-dir``: caps are governance artifacts, not inputs, and selecting between
already-loaded tables does not weaken that.

**Assumed values announce themselves.** Every rule table carries a ``status`` field. Anything
that is not ``signed_off`` is reported by :meth:`Config.unsigned`, which the auction logs at
open. An assumption nobody can see is an assumption nobody will revisit.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

CONFIG_DIR = Path(__file__).parent
RULES_DIR = CONFIG_DIR / "rules"

_SIGNED_OFF = "signed_off"


def _read(path: Path) -> Mapping[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def _digest(*paths: Path) -> str:
    """Short content hash. Order-independent across files, exact within one."""
    sha = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        sha.update(path.name.encode())
        sha.update(path.read_bytes())
    return sha.hexdigest()[:12]


#: Filename of the caps table a :class:`Config` selects before any resource is chosen.
#: :func:`~allocation.trigger.runtime.run_allocation` replaces it via :meth:`Config.for_resource`
#: as soon as the profile is known, so this is only what a caller sees if it never selects one.
DEFAULT_CAPS_FILE = "caps_icu_bed.yaml"
DEFAULT_BUDGET_FILE = "budget_icu_bed.yaml"


@dataclass(frozen=True, slots=True)
class Config:
    """The loaded configuration. Construct once per process via :func:`load_config`."""

    caps: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    budget: Mapping[str, Any]
    auction: Mapping[str, Any]
    reward: Mapping[str, Any]
    rules: Mapping[str, Mapping[str, Any]]
    caps_version: str
    config_version: str
    #: Every ``caps_*.yaml`` in the config dir, by filename. Loaded at process start; one of
    #: them is selected per auction by :meth:`for_resource`.
    caps_files: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Content hash per caps file, by filename. ``caps_version`` is whichever is selected.
    caps_versions: Mapping[str, str] = field(default_factory=dict)
    #: Every ``budget_*.yaml``, by filename. One pool per resource type (D-3).
    budget_files: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    # -- resource selection --------------------------------------------------------

    def for_resource(self, profile: Any) -> "Config":
        """This config with ``profile``'s caps and budget tables selected.

        Wires ``ResourceProfile.caps_config`` and ``budget_config``, both of which were
        declared and never read (F-A) — so every resource silently shared ICU's unfitted
        maxima and drew on one budget pool. ``caps_version`` moves with the caps table, which
        is the half that matters for audit.

        Takes the profile rather than filenames so the caller cannot pair one resource's
        profile with another's tables.

        **Already-scoped tables are left alone.** If the loaded ``caps`` or ``budget`` already
        declares this resource, it is kept rather than re-read from ``caps_files``. Otherwise
        a caller that built a Config with an adjusted table — ``replace(config, budget=...)``
        to try ``base.mode: derived``, say — would have the adjustment silently discarded here
        and would be testing the shipped file instead of theirs.
        """
        caps, caps_selected = self._table(
            profile, self.caps, self.caps_files, profile.caps_config, "caps"
        )
        budget, _ = self._table(
            profile, self.budget, self.budget_files, profile.budget_config, "budget"
        )
        return replace(
            self,
            caps=caps,
            budget=budget,
            # Only restamp when the table actually changed: an in-place caps edit keeps the
            # version of the file it was loaded from, which is the honest hash of what is on
            # disk. Editing caps in memory and expecting a new caps_version is not supported —
            # use --config-dir, which is what the governance model assumes.
            caps_version=(
                self.caps_versions[profile.caps_config] if caps_selected else self.caps_version
            ),
        )

    @staticmethod
    def _table(
        profile: Any,
        current: Mapping[str, Any],
        loaded: Mapping[str, Mapping[str, Any]],
        name: str,
        kind: str,
    ) -> tuple[Mapping[str, Any], bool]:
        """The table for this profile, and whether it was newly selected."""
        if str(current.get("resource_type", "")) == profile.resource_type.value:
            return current, False
        try:
            table = loaded[name]
        except KeyError as exc:
            raise KeyError(
                f"profile {profile.resource_type.value!r} wants {kind} file {name!r}, which "
                f"is not in the config dir. Found: {sorted(loaded)}. {kind.capitalize()} is "
                "per-resource by design — one resource scored or funded against another's "
                "table yields a plausible number valid for nothing."
            ) from exc
        declared = str(table.get("resource_type", ""))
        if declared and declared != profile.resource_type.value:
            raise ValueError(
                f"{name} declares resource_type {declared!r} but was selected by profile "
                f"{profile.resource_type.value!r}"
            )
        return table, True

    # -- component access ----------------------------------------------------------

    def cap(self, component: str) -> float:
        """Signed cap for a component. Negative for Alternative and Resource Stress."""
        try:
            return float(self.caps["components"][component]["cap"])
        except KeyError as exc:
            raise KeyError(f"no cap configured for component {component!r}") from exc

    def weights(self, component: str) -> Mapping[str, float]:
        """Factor weights for a weighted component. Empty for product components."""
        return {k: float(v) for k, v in self.caps.get("weights", {}).get(component, {}).items()}

    def threshold(self, *path: str) -> Any:
        """Fetch a threshold by path, e.g. ``threshold("urgency", "news2_max")``."""
        node: Any = self.thresholds
        for key in path:
            try:
                node = node[key]
            except (KeyError, TypeError) as exc:
                raise KeyError(f"no threshold at {'.'.join(path)!r}") from exc
        return node

    def rule(self, table: str) -> Mapping[str, Any]:
        try:
            return self.rules[table]
        except KeyError as exc:
            raise KeyError(f"no rule table {table!r} in {RULES_DIR}") from exc

    # -- governance visibility -----------------------------------------------------

    @property
    def unsigned(self) -> Mapping[str, str]:
        """Rule tables not yet signed off, as ``{table: status}``.

        Logged at auction open so no run is ever silently built on assumed clinical values.
        """
        out: dict[str, str] = {}
        for name, table in self.rules.items():
            status = str(table.get("status", "unknown"))
            if status != _SIGNED_OFF:
                out[name] = status
        # The caps table is the loudest of these. Five of the six bed types carry maxima
        # copied verbatim from the ICU table; a run against those is arithmetically valid and
        # clinically meaningless, and this is the line that says so.
        caps_status = str(self.caps.get("status", "unknown"))
        if caps_status != _SIGNED_OFF:
            out[f"caps.{self.caps.get('resource_type', 'unknown')}"] = caps_status
        # The budget pool is separate per resource type but sized from ICU's numbers on five
        # of six. A pool that is too large is inert — "bidding maximum is free" — which is a
        # silent failure of the whole constraint, so it belongs in the governance line.
        pool_status = str(self.budget.get("status", "unknown"))
        if pool_status != _SIGNED_OFF:
            out[f"budget.pool.{self.budget.get('resource_type', 'unknown')}"] = pool_status
        targets_status = str(self.budget.get("targets_status", "unknown"))
        if targets_status != _SIGNED_OFF:
            out["budget.targets"] = targets_status
        safety_status = str(self.auction.get("safety_status", "unknown"))
        if safety_status != _SIGNED_OFF:
            out["auction.safety_constraints"] = safety_status
        reward_status = str(self.reward.get("status", "unknown"))
        if reward_status != _SIGNED_OFF:
            # The reward terms ARE the objective function. A cap that is wrong distorts a
            # bid; a reward term that is wrong teaches the policy to want the wrong thing.
            out["reward.terms"] = reward_status
        return out

    def describe(self) -> str:
        lines = [
            f"caps_version   {self.caps_version}",
            f"config_version {self.config_version}",
            f"components     {len(self.caps['components'])}",
            f"rule tables    {len(self.rules)}",
        ]
        if self.unsigned:
            lines.append("UNSIGNED:")
            lines += [f"  {name:<32} {status}" for name, status in sorted(self.unsigned.items())]
        return "\n".join(lines)


def load_config(config_dir: Path | None = None) -> Config:
    """Load and version the configuration. Raises if any required file is missing."""
    base = config_dir or CONFIG_DIR
    rules_dir = base / "rules"

    core_paths = {
        "thresholds": base / "thresholds.yaml",
        "auction": base / "auction.yaml",
        "reward": base / "reward.yaml",
    }
    missing = [p for p in core_paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing config: {', '.join(str(p) for p in missing)}")

    # One caps table and one budget pool per resource type. Every one is read here, at process
    # start, so that selecting between them per auction never re-reads the filesystem — config
    # stays pinned by --config-dir.
    caps_paths = _per_resource(base, "caps_*.yaml", DEFAULT_CAPS_FILE)
    budget_paths = _per_resource(base, "budget_*.yaml", DEFAULT_BUDGET_FILE)

    caps_files = {p.name: _read(p) for p in caps_paths}
    caps_versions = {p.name: _digest(p) for p in caps_paths}
    budget_files = {p.name: _read(p) for p in budget_paths}

    default_caps = DEFAULT_CAPS_FILE if DEFAULT_CAPS_FILE in caps_files else caps_paths[0].name
    default_budget = (
        DEFAULT_BUDGET_FILE if DEFAULT_BUDGET_FILE in budget_files else budget_paths[0].name
    )

    rule_paths = sorted(rules_dir.glob("*.yaml"))
    if not rule_paths:
        raise FileNotFoundError(f"no rule tables found in {rules_dir}")

    return Config(
        caps=caps_files[default_caps],
        thresholds=_read(core_paths["thresholds"]),
        budget=budget_files[default_budget],
        auction=_read(core_paths["auction"]),
        reward=_read(core_paths["reward"]),
        rules={p.stem: _read(p) for p in rule_paths},
        caps_version=caps_versions[default_caps],
        config_version=_digest(
            *caps_paths, *budget_paths, *core_paths.values(), *rule_paths
        ),
        caps_files=caps_files,
        caps_versions=caps_versions,
        budget_files=budget_files,
    )


def _per_resource(base: Path, pattern: str, example: str) -> list[Path]:
    """Every per-resource table matching ``pattern``. Raises if there are none."""
    paths = sorted(base.glob(pattern))
    if not paths:
        raise FileNotFoundError(
            f"no {pattern} tables found in {base}. Expected one per auctionable resource, "
            f"e.g. {example}."
        )
    return paths
