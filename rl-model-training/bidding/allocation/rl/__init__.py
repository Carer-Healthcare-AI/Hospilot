"""Linear reinforcement learning for the bed-allocation simulator.

``encoder`` and ``policy`` define the versioned linear action-value model. ``qlearn`` contains
the TD learner, ``safe_explore`` collects feasible exploratory transitions, and ``baseline_q``
implements the controlled BASELINE-versus-Q comparison. ``pilot`` contains the shadow and
safety wrappers used by the CLI/API.

Superseded CEM, Monte Carlo, PPO, DQN, residual-Q, and oracle-audit implementations are retained
only in the ignored local research archive; they are not part of the public runtime package.
"""
