"""Raw rows in, normalised signals in ``[0, 1]`` out.

This layer knows clinical scoring rules but nothing about caps, weights, budgets or bidding.
It never multiplies by a cap and never decides a component's value — that belongs to
``utility/``.

The one rule that runs through everything here: **an input that is missing returns ``None``,
which the component turns into an absent :class:`~allocation.contracts.Signal`.** Never 0.0.
"""

from allocation.features import labs, news2, scale, timeseries

__all__ = ["labs", "news2", "scale", "timeseries"]
