"""Loading and serving a bed-style diagnostic Q artifact.

``qlearn.DiagnosticQPolicy.load`` deliberately refuses these files — different ``kind``,
different encoder version — and it is right to. A 29-column policy served against the 25-column
encoder is not degraded, it is undefined. This module is the sibling loader, with the same
interlocks applied to the extended layout.

**Serving reuses the exact class the validation ran.**
``research_model.ResearchServedPolicy`` is what produced every number in
``results/diagnostic_machine/bedstyle_q.json``, so serving through it is the only way to
guarantee that live behaviour is the behaviour that was measured. In particular it argmaxes over
the LADDER-REACHABLE set, which is what the model was trained under; the raw feasible set would
present it with compete actions the ladder overrides, at ~21% of decisions.

It builds decisions through ``oracle_fork.build_forced_decision`` rather than through
``DiagnosticHeuristicPolicy``. That is not a limitation here — the fork builds a full plan for
every exit — and it means the served policy has no heuristic dependency at all.

**This module does not decide whether the policy is allowed to act.** ``api/service.py``'s
``safety_is_enforced`` startup gate and the shadow/live flag are unchanged and still govern.
"""

from __future__ import annotations

import json
from pathlib import Path

from allocation.config import Config
from allocation.use_cases.diagnostic_machine.masks import ACTIONS, action_space_version
from allocation.use_cases.diagnostic_machine.research_encoder import (
    RESEARCH_FEATURES,
    LadderParams,
    research_encoder_version,
)
from allocation.use_cases.diagnostic_machine.research_model import (
    BedStyleQLearner,
    ResearchServedPolicy,
)

KIND = "diagnostic_bedstyle_q_linear"


class IncompatibleArtifact(ValueError):
    """The file is not a bed-style diagnostic Q artifact this build can serve."""


def load(path: str | Path, config: Config) -> BedStyleQLearner:
    """Read weights, refusing anything this build cannot serve *meaningfully*.

    Four checks, each of which catches a distinct way a file can run and mean nothing:

    ``kind``              a foreign policy (a plain ``DiagnosticQPolicy``, a tree, a baseline)
                          would load into these fields and emit plausible numbers.
    ``encoder_version``   hashes the column list AND the ladder constants that shape
                          ``ladder_alpha_logit``; a change to either changes what the weights
                          refer to without changing a single name.
    ``action_space``      a Q-vector is only interpretable against the ordering it was fitted on.
    shape                 row count and width, so a truncated or hand-edited file fails here
                          rather than at the first argmax.
    """
    path = Path(path)
    body = json.loads(path.read_text(encoding="utf-8"))

    if body.get("kind") != KIND:
        raise IncompatibleArtifact(
            f"{path.name} is not a bed-style diagnostic Q artifact "
            f"(kind={body.get('kind')!r}, expected {KIND!r}); a foreign policy loaded here "
            "would run and mean nothing"
        )

    expected_encoder = research_encoder_version(LadderParams.from_config(config))
    if str(body.get("encoder_version")) != expected_encoder:
        raise IncompatibleArtifact(
            f"{path.name} was fitted under encoder {body.get('encoder_version')!r}, this build "
            f"encodes as {expected_encoder!r}. The feature list or the ladder constants that "
            "shape ladder_alpha_logit have changed, so the weights refer to different "
            "quantities. Refit rather than reinterpret."
        )

    if str(body.get("action_space_version")) != action_space_version():
        raise IncompatibleArtifact(
            f"{path.name} was fitted under a different action ordering; a Q-vector is only "
            "interpretable against the ordering it was trained on."
        )

    rows = [list(map(float, r)) for r in body["rows"]]
    bias = [float(b) for b in body["bias"]]
    if len(rows) != len(ACTIONS) or len(bias) != len(ACTIONS):
        raise IncompatibleArtifact(
            f"{path.name} carries {len(rows)} weight rows and {len(bias)} biases; "
            f"the action space has {len(ACTIONS)} entries"
        )
    for index, row in enumerate(rows):
        if len(row) != len(RESEARCH_FEATURES):
            raise IncompatibleArtifact(
                f"{path.name} row {index} has {len(row)} weights, the encoder emits "
                f"{len(RESEARCH_FEATURES)}"
            )

    learner = BedStyleQLearner(
        encoder_version=str(body["encoder_version"]),
        rows=rows,
        bias=bias,
        fit_counts=[int(c) for c in body.get("fit_counts", [0] * len(ACTIONS))],
        gamma=float(body.get("gamma", 0.99)),
        reward_scale=float(body.get("reward_scale", 1.0)),
        huber_delta=float(body.get("huber_delta", 1.0)),
        double_q=bool(body.get("double_q", True)),
        discount_horizon_minutes=float(body.get("discount_horizon_minutes", 60.0)),
    )
    unfitted = learner.unfitted_heads()
    if unfitted:
        raise IncompatibleArtifact(
            f"{path.name} has heads that never received a gradient ({', '.join(unfitted)}). "
            "An unfitted row scores exactly 0.0 in every state and wins the argmax whenever "
            "every fitted action scores negative — which is the sickest states."
        )
    return learner


def serve(config: Config, learner: BedStyleQLearner) -> ResearchServedPolicy:
    """The policy behind the auction's ``decide`` seam — the same class validation ran."""
    return ResearchServedPolicy(
        config, LadderParams.from_config(config), learner.q_values, name="diagnostic_bedstyle_q"
    )


def load_and_serve(path: str | Path, config: Config) -> ResearchServedPolicy:
    return serve(config, load(path, config))


__all__ = ["KIND", "IncompatibleArtifact", "load", "load_and_serve", "serve"]
