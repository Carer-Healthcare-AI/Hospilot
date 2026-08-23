"""Forecast reads for the hospital block, wrapping util.forecast_client.

forecast() never raises — it returns None when the service is unconfigured/unreachable/slow —
so a missing forecast becomes a None field and the engine falls back to its own factor. Each
successful read should also be appended to allocation.forecast_history so the 30-day Demand
denominator accumulates (see record_forecast hook — wired with persist.py once T1 is applied).
"""

from __future__ import annotations

from typing import Any

from util.forecast_client import forecast


# Per-unit demand endpoint; default to /{unit}/demand for units without a specific path.
DEMAND_PATHS = {
    "icu": "/icu/demand",
    "ward": "/ward/demand",
    "ed": "/er/demand",
}
DISCHARGE_PATH = "/discharge/volume"

# Response keys that may carry the scalar we want, in priority order (service shapes vary).
_VALUE_KEYS = ("value", "forecast", "predicted", "predicted_demand", "expected", "count")


def _num(resp: Any, *keys: str) -> float | None:
    if not isinstance(resp, dict):
        return None
    for k in (*keys, *_VALUE_KEYS):
        v = resp.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


class Forecaster:
    """Async forecast reads for one tenant. `record` is an optional callable
    (endpoint, scope, horizon, value, payload, raw) used to log to forecast_history."""

    def __init__(self, record: Any = None) -> None:
        self._record = record

    async def _read(self, path: str, scope: str, value_keys: tuple[str, ...]) -> float | None:
        payload = {"scope": scope, "horizon": "4h"}
        resp = await forecast(path, payload)
        value = _num(resp, *value_keys)
        if value is not None and self._record is not None:
            await self._record(path, scope, "4h", value, payload, resp)
        return value

    async def demand_4h(self, unit: str) -> float | None:
        path = DEMAND_PATHS.get(unit, f"/{unit}/demand")
        return await self._read(path, unit, ("predicted_demand", "demand"))

    async def discharges_4h(self, unit: str) -> float | None:
        return await self._read(DISCHARGE_PATH, unit, ("expected_discharges", "discharges", "volume"))
