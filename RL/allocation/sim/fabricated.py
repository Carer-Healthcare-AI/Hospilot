"""Every invented constant in the simulator, in one place, hashed.

The system already has this discipline for config: ``Config.unsigned`` reports each rule table
still on assumed values, and ``caps_version`` hashes the caps file actually used, so no auction
is silently built on numbers nobody signed. The simulator needs the same and needs it more,
because a simulator's constants are not merely unsigned — **they have no source at all.**

RL_READINESS §7.7 lists three simulator inputs and marks all three as fabrication:

    arrival process           fittable from `visits` / `ipd_admissions`   — NOT AVAILABLE HERE
    deterioration trajectory  fittable from `vitals`                      — NOT AVAILABLE HERE
    outcome model             "nothing available — this is B.10"          — never available

The first two would normally be fitted. This build has no live and no historical data, so all
three are invented, and the honest response is not to hide that but to make it enumerable,
versioned, and sweepable:

* **Enumerable** — :func:`register` returns every constant with its rationale and, where it can
  be reasoned about, the *direction* of the error it introduces.
* **Versioned** — :attr:`FabricationRegister.version` hashes the values, exactly as
  ``caps_version`` hashes the caps file. **A policy is only valid for the fabrication it trained
  against**, the same way it is only valid for its state encoding (F-24). A trained policy
  carries this hash; a mismatch at load time is an error, not a warning.
* **Sweepable** — :meth:`FabricationRegister.perturbed` returns a register with one constant
  moved, which is the whole basis of the falsification test in ``rl/evaluate.py``: if a learned
  policy's behaviour changes materially when an outcome-model constant is nudged, it learned the
  fabrication rather than the structure, and its results mean nothing outside this simulator.

The register is deliberately not a YAML file. Config is loaded from YAML because clinicians and
governance need to read and sign it; nobody is ever going to sign these, and putting them beside
the signed tables would blur exactly the line this module exists to draw.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class Fabrication:
    """One invented constant, with what is known about how wrong it is."""

    name: str
    value: float
    unit: str
    rationale: str
    #: Which way the modelled world differs from a real one, when that can be reasoned about.
    #: Empty when it genuinely cannot — an empty string here is more useful than a guess,
    #: because a stated direction gets used to argue about results.
    error_direction: str = ""
    #: ``outcome`` constants are the dangerous ones: whatever they encode becomes the objective
    #: the policy optimises. ``dynamics`` and ``arrivals`` shape the state distribution instead,
    #: which is a weaker and more forgiving kind of wrong.
    kind: str = "dynamics"


@dataclass(frozen=True, slots=True)
class FabricationRegister:
    """The full set, hashed. Stamped on every episode and every trained policy."""

    items: tuple[Fabrication, ...]

    @property
    def version(self) -> str:
        """Short content hash over names and values. Same shape as ``caps_version``."""
        payload = json.dumps(
            {f.name: f.value for f in sorted(self.items, key=lambda x: x.name)},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def __getitem__(self, name: str) -> float:
        for item in self.items:
            if item.name == name:
                return item.value
        raise KeyError(
            f"no fabricated constant named {name!r}. Every simulator number must be declared "
            f"in sim/fabricated.py — an undeclared one is invisible to the sweep that is "
            f"supposed to detect a policy fitting it. Declared: {sorted(f.name for f in self.items)}"
        )

    def of_kind(self, kind: str) -> tuple[Fabrication, ...]:
        return tuple(f for f in self.items if f.kind == kind)

    @property
    def outcome_constants(self) -> tuple[Fabrication, ...]:
        """The ones that *are* the objective. Sweep these before believing any result."""
        return self.of_kind("outcome")

    def perturbed(self, name: str, factor: float) -> "FabricationRegister":
        """A copy with one constant scaled. The basis of the falsification sweep.

        Returns a register with a **different version**, so an episode generated under it can
        never be pooled with the baseline's by accident.
        """
        items = tuple(
            replace(f, value=f.value * factor) if f.name == name else f for f in self.items
        )
        if items == self.items:
            raise KeyError(f"nothing named {name!r} to perturb")
        return FabricationRegister(items=items)

    def describe(self) -> str:
        lines = [
            f"fabrication_version {self.version}",
            f"constants           {len(self.items)} "
            f"({len(self.outcome_constants)} of them ARE the objective)",
            "",
        ]
        for kind in ("outcome", "dynamics", "arrivals"):
            group = self.of_kind(kind)
            if not group:
                continue
            lines.append(f"  [{kind}]")
            for item in sorted(group, key=lambda f: f.name):
                lines.append(f"    {item.name:<34} {item.value:>8.3f} {item.unit}")
                lines.append(f"      {item.rationale}")
                if item.error_direction:
                    lines.append(f"      known bias: {item.error_direction}")
            lines.append("")
        return "\n".join(lines)

    def as_dict(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            "constants": {f.name: f.value for f in self.items},
        }


# ---------------------------------------------------------------------------------------
# The register itself
# ---------------------------------------------------------------------------------------

_ITEMS: tuple[Fabrication, ...] = (
    # --- arrivals ---------------------------------------------------------------------
    Fabrication(
        name="arrival.bed_release_per_hour",
        value=0.55,
        unit="releases/h",
        kind="arrivals",
        rationale=(
            "Bed releases in a 20-bed ICU. Chosen so a 12-hour run produces ~6-7 auctions, "
            "which is the per-department request count budget/targets already assumes "
            "(n_req 6/3/4). Nothing measures it."
        ),
        error_direction=(
            "real releases cluster on ward rounds and shift changes, so a Poisson stream "
            "under-states the queueing that bursts produce"
        ),
    ),
    Fabrication(
        name="arrival.candidate_per_hour",
        value=1.30,
        unit="patients/h",
        kind="arrivals",
        rationale=(
            "New ICU-eligible patients across all three departments. Set above the release "
            "rate on purpose: demand must exceed supply or the auction never contends and "
            "every policy looks identical."
        ),
        error_direction="",
    ),
    Fabrication(
        name="arrival.er_share",
        value=0.50,
        unit="fraction",
        kind="arrivals",
        rationale="Department mix ER/OT/Ward = 0.50/0.20/0.30, matching targets n_req 6/3/4.",
        error_direction="",
    ),
    Fabrication(
        name="arrival.ot_share",
        value=0.20,
        unit="fraction",
        kind="arrivals",
        rationale="See arrival.er_share.",
        error_direction="",
    ),
    # --- deterioration dynamics -------------------------------------------------------
    Fabrication(
        name="severity.initial_mean",
        value=0.45,
        unit="latent [0,1]",
        kind="dynamics",
        rationale=(
            "Mean latent severity at arrival. Drives NEWS2 and every vital, so it sets the "
            "utility scale the whole auction is denominated in."
        ),
        error_direction="",
    ),
    Fabrication(
        name="severity.initial_sd",
        value=0.18,
        unit="latent",
        kind="dynamics",
        rationale=(
            "Spread at arrival. Too narrow and every patient is interchangeable, which makes "
            "ranking respect unmeasurable; too wide and the winner is obvious every time and "
            "there is nothing to learn."
        ),
        error_direction="",
    ),
    Fabrication(
        name="severity.drift_per_hour",
        value=0.055,
        unit="latent/h",
        kind="dynamics",
        rationale=(
            "Deterioration while waiting without ICU care. This is what makes waiting costly "
            "and therefore what makes Q(Win Now) ever preferable to Q(Continue)."
        ),
        error_direction=(
            "a single constant rate treats all patients as deteriorating alike; real "
            "trajectories are heavy-tailed, so this under-states the worst cases"
        ),
    ),
    Fabrication(
        name="severity.drift_sd_per_hour",
        value=0.030,
        unit="latent/h",
        kind="dynamics",
        rationale="Per-patient variation in deterioration speed. Without it the future is deterministic and waiting carries no risk.",
        error_direction="",
    ),
    Fabrication(
        name="severity.icu_recovery_per_hour",
        value=0.130,
        unit="latent/h",
        kind="dynamics",
        rationale=(
            "Improvement once in ICU. The ratio of this to the drift rate IS the value of an "
            "ICU bed in this world — the single most consequential dynamics number here."
        ),
        error_direction="",
    ),
    Fabrication(
        name="severity.alternative_recovery_per_hour",
        value=0.045,
        unit="latent/h",
        kind="dynamics",
        rationale=(
            "Improvement in a lesser unit (HDU/PACU). Set between drift and ICU recovery so an "
            "alternative genuinely helps but is genuinely worse — if it matched ICU, "
            "Q(Withdraw + Alternative) would dominate trivially and the auction would be moot."
        ),
        error_direction="",
    ),
    Fabrication(
        name="severity.critical_threshold",
        value=0.80,
        unit="latent",
        kind="dynamics",
        rationale="Latent severity above which a patient is deteriorating in the reward's sense.",
        error_direction="",
    ),
    # --- the outcome model — these ARE the objective -----------------------------------
    Fabrication(
        name="outcome.mortality_base",
        value=0.020,
        unit="probability",
        kind="outcome",
        rationale=(
            "P(death within the 4 h horizon) for a patient at zero latent severity. The reward "
            "term this feeds is worth +30/-60 and sets the SIGN of every episode (F-01)."
        ),
        error_direction=(
            "no hospital data supports any value; a real ICU's short-horizon mortality is "
            "strongly case-mix dependent and this collapses all of it into one intercept"
        ),
    ),
    Fabrication(
        name="outcome.mortality_severity_slope",
        value=0.240,
        unit="probability/latent",
        kind="outcome",
        rationale=(
            "How fast mortality risk rises with latent severity. **This single number is the "
            "closest thing this simulator has to a clinical claim, and it is invented.** It "
            "decides how much the policy should pay to avoid a death, hence the entire "
            "risk-appetite of anything trained here."
        ),
        error_direction="linear in severity, which no survival model is",
    ),
    Fabrication(
        name="outcome.stabilised_threshold",
        value=0.55,
        unit="latent",
        kind="outcome",
        rationale="Latent severity below which `patient_stabilised` (+40) is observed true.",
        error_direction="",
    ),
    Fabrication(
        name="outcome.escalation_threshold",
        value=0.78,
        unit="latent",
        kind="outcome",
        rationale=(
            "Latent severity at which `emergency_escalation` (-20) fires for an unplaced "
            "patient — intubation or pressor escalation in the reward's language."
        ),
        error_direction="",
    ),
    Fabrication(
        name="outcome.boarding_relief_probability",
        value=0.70,
        unit="probability",
        kind="outcome",
        rationale="P(`boarding_reduced` +15) when an ED patient is placed. Operational, not clinical.",
        error_direction="",
    ),
    Fabrication(
        name="outcome.second_bed_probability",
        value=0.30,
        unit="probability",
        kind="outcome",
        rationale=(
            "P(`second_bed_opened` +15) inside the horizon. Deliberately NOT tied to the "
            "next-release forecast the policy sees — a policy that could read its own reward "
            "signal off its own observation would be learning the simulator's plumbing."
        ),
        error_direction="",
    ),
    Fabrication(
        name="outcome.cancellation_probability",
        value=0.55,
        unit="probability",
        kind="outcome",
        rationale=(
            "P(theatre case cancelled) when OT loses and has no alternative. Drives "
            "`surgery_not_cancelled` (+20) and OT's whole reason to bid."
        ),
        error_direction="",
    ),
)

#: The register every simulator run uses unless one is passed explicitly.
DEFAULT = FabricationRegister(items=_ITEMS)


def register(overrides: Mapping[str, float] | None = None) -> FabricationRegister:
    """The default register, optionally with named constants replaced.

    Overrides are for sweeps and calibration, and they change the version — so an episode
    generated under an override can never be pooled with a baseline episode by accident.
    """
    if not overrides:
        return DEFAULT
    known = {f.name for f in _ITEMS}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise KeyError(
            f"unknown fabricated constants {unknown}. Declare them in sim/fabricated.py so "
            "the sweep can see them."
        )
    return FabricationRegister(
        items=tuple(
            replace(f, value=float(overrides[f.name])) if f.name in overrides else f
            for f in _ITEMS
        )
    )
