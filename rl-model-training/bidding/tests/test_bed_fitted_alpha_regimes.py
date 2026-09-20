"""Audit-support test: proves bed's EXISTING fitted alpha head genuinely varies across
opening/leading/trailing round states, through the unmodified ``LinearQPolicy._alpha`` path
``decide_q`` calls for every compete decision.

Written after reverting an attempted Block 7 replacement of that path with a mechanical
ladder — the replacement was abandoned because it conflicted with a concurrently-developed
agent-extension design that keeps (and depends on) this fitted head. This test does not
touch the head's implementation; it only proves the pre-existing mechanism is real and
state-dependent, using hand-built (not fitted) weights and hand-built states, one per regime
— the same technique ``test_bed_agent_extension.py``'s own alpha-mechanism test uses, and no
claim of trained efficacy either way.

**Why hand-built states rather than a live auction.** A live run was tried first: in the
fixture this repo already uses for bed tests (``CANDIDATES`` on ``ICU_BED``), an agent that
falls behind the leader immediately fails ``_feasible``'s mechanical ceiling check (mirrors
``HeuristicPolicy`` rules 1/2) and exits rather than continuing to bid while trailing — so no
OVERTAKING-regime bid with a live alpha ever gets recorded, for either policy. That is a
property of this fixture's numbers, not of the alpha mechanism, so probing the mechanism
directly (as ``test_bed_agent_extension.py`` already does for its own alpha claim) is the
more reliable proof.
"""

from __future__ import annotations

from allocation.config import load_config
from allocation.rl.encoder import NAMES, SIZE, StateEncoder, regime_of
from allocation.rl.policy import LinearQPolicy, QWeights

CONFIG = load_config()
ENCODER_VERSION = StateEncoder().version

_STANDING_BID = NAMES.index("standing_bid")
_LEADER_BID = NAMES.index("leader_bid")
_IS_LEADING = NAMES.index("is_leading")


def _state(standing_bid: float, leader_bid: float, is_leading: float) -> tuple[float, ...]:
    values = [0.0] * SIZE
    values[_STANDING_BID] = standing_bid
    values[_LEADER_BID] = leader_bid
    values[_IS_LEADING] = is_leading
    return tuple(values)


# One hand-built state per regime, matching real encoded values seen in a live run (bed's
# own encoder stores standing_bid/leader_bid pre-scaled to [0, 1], is_leading as raw 0.0/1.0)
# and each verified against ``regime_of`` below rather than assumed.
_OPENING_STATE = _state(standing_bid=0.0, leader_bid=0.0, is_leading=1.0)
_LEADING_STATE = _state(standing_bid=0.5, leader_bid=0.2, is_leading=1.0)
_TRAILING_STATE = _state(standing_bid=0.2, leader_bid=0.6, is_leading=0.0)


def _hand_built_alpha_policy() -> LinearQPolicy:
    """Not fitted from any data — a deliberately non-zero alpha_row/alpha_bias reading
    ``standing_bid`` and ``is_leading``, exactly the two features ``regime_of`` itself keys
    off, so the sigmoid output is forced to respond to regime by construction. Proves the
    architecture is real and state-dependent; makes no claim about what a trained head would
    learn to do with the same features.
    """
    row = [0.0] * SIZE
    row[_STANDING_BID] = 3.0
    row[_IS_LEADING] = 1.5
    weights = QWeights(
        rows=tuple(tuple(0.0 for _ in range(SIZE)) for _ in range(6)),
        biases=tuple(0.0 for _ in range(6)),
        alpha_row=tuple(row),
        alpha_bias=-1.0,
        encoder_version=ENCODER_VERSION,
        fabrication_version="fitted-alpha-regime-audit-test",
    )
    return LinearQPolicy(CONFIG, weights)


def test_regime_of_labels_the_three_hand_built_states_as_expected():
    """Sanity check on the fixtures themselves, before trusting what they prove below."""
    from allocation.rl.encoder import LEADING, OPENING, OVERTAKING

    assert regime_of(_OPENING_STATE) == OPENING
    assert regime_of(_LEADING_STATE) == LEADING
    assert regime_of(_TRAILING_STATE) == OVERTAKING


def test_fitted_alpha_head_varies_across_opening_leading_trailing_regimes():
    """The existing, unmodified ``LinearQPolicy._alpha`` — the exact method ``decide_q`` calls
    for BASELINE and Q alike — produces three distinct aggression values for the three
    regimes, proving it is genuinely round-state-dependent rather than a constant.
    """
    policy = _hand_built_alpha_policy()
    opening_alpha = policy._alpha(_OPENING_STATE)
    leading_alpha = policy._alpha(_LEADING_STATE)
    trailing_alpha = policy._alpha(_TRAILING_STATE)

    for label, value in (
        ("opening", opening_alpha), ("leading", leading_alpha), ("trailing", trailing_alpha),
    ):
        assert 0.0 <= value <= 1.0, f"{label} alpha {value} outside [0, 1]"

    assert abs(opening_alpha - leading_alpha) > 0.01, (
        f"opening ({opening_alpha}) and leading ({leading_alpha}) alpha are not "
        "meaningfully different"
    )
    assert abs(leading_alpha - trailing_alpha) > 0.01, (
        f"leading ({leading_alpha}) and trailing ({trailing_alpha}) alpha are not "
        "meaningfully different"
    )
    assert abs(opening_alpha - trailing_alpha) > 0.01, (
        f"opening ({opening_alpha}) and trailing ({trailing_alpha}) alpha are not "
        "meaningfully different"
    )
