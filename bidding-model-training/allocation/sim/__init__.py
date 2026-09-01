"""A synthetic hospital, and the honest accounting of what in it is invented.

    fabricated.py  every invented constant, hashed and sweepable
    patients.py    latent severity -> vitals the real feature layer reads
    world.py       arrivals, occupancy, and the DataSource seam
    outcomes.py    the outcome model — the largest fabrication, and it IS the objective
    dataset.py     run shifts, score outcomes, emit trainable episodes

RL_READINESS §7.7 marks all three simulator inputs as fabrication. With no live and no
historical data, all three are invented here. The response is not to hide that but to make it
enumerable, versioned and sweepable — see `fabricated.py`, and `rl/evaluate.py` for the sweep
that tests whether a trained policy fitted the fabrication rather than the structure.
"""

from allocation.sim.fabricated import DEFAULT, FabricationRegister, register
from allocation.sim.world import SimDataSource, SimWorld

__all__ = ["DEFAULT", "FabricationRegister", "register", "SimDataSource", "SimWorld"]
