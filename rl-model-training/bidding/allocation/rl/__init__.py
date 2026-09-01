"""Learning a bidding policy.

    encoder.py   the frozen, versioned state vector — F-24
    policy.py    a linear Q-function over the six actions, behind `BiddingPolicy`
    train.py     cross-entropy method: sample, evaluate, refit, repeat
    evaluate.py  paired-seed comparison, four metrics, and the fabrication sweep

Ordered deliberately. RL_READINESS §5.3 ② puts the representation ladder at "parametric rules +
CEM -> tabular Q -> linear Q -> network" and this build stops at linear-Q-by-CEM. With roughly
six auctions per department per shift there is not the data for anything wider, and a network
would make the one question that matters — did it learn the structure or the fabrication? —
much harder to answer than it already is.
"""
