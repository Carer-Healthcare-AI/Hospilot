"""Reserve price — the minimum acceptable closing bid. RL-Steps section 17.

*"ER is left competing but the resource manager can require a minimum final commitment...
Suppose the minimum acceptable closing bid based on resource scarcity is 110."*

That is the entire specification: scarcity-based, one worked value, no formula. The rule below
is monotone in occupancy — which is all "based on resource scarcity" actually says — and is
fitted to that single point::

    reserve = highest_ceiling x clamp(min + (max - min) x occ_stress)

At 100 % occupancy with a highest ceiling of 171 it gives 111, against section 17's 110.
**One data point, two parameters.** In the section 18 example it never binds: ER closes at
118.

What the reserve is *for* is clearer than what its value should be. Without one, an unopposed
bidder in a crunch can take a scarce bed for its opening bid, and the price signal that the
whole budget mechanism depends on disappears.
"""

from __future__ import annotations

from allocation.config import Config
from allocation.features.scale import clamp


def reserve_price(config: Config, highest_ceiling: float, occupancy: float) -> float:
    """Minimum bid the resource manager will accept at close."""
    cfg = config.auction["reserve"]
    if not bool(cfg.get("enabled", True)):
        return 0.0
    if highest_ceiling <= 0:
        return 0.0

    contention_cfg = config.budget["spend"]["contention"]
    onset = float(contention_cfg["occupancy_onset"])
    span = float(contention_cfg["occupancy_span"])
    occ_stress = clamp((occupancy - onset) / span)

    lo = float(cfg["min_fraction"])
    hi = float(cfg["max_fraction"])
    return highest_ceiling * clamp(lo + (hi - lo) * occ_stress, 0.0, 1.0)


def meets_reserve(bid: float, reserve: float) -> bool:
    return bid >= reserve - 1e-9
