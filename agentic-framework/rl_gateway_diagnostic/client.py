"""httpx client to the engine's ``/diagnostic/*`` routes.

Reuses ``rl_gateway.config`` — one engine URL, one key, one timeout for both families. The
engine refuses ``mode: live`` over HTTP by design, and ``config.mode`` is advisory, so this
client cannot open a binding auction even if a caller asks it to.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rl_gateway.config import config


class DiagnosticEngineError(RuntimeError):
    """The engine refused the request. Carries the body so the caller can show the reason."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"diagnostic engine returned {status}: {body}")
        self.status = status
        self.body = body


class DiagnosticClient:
    def __init__(self, cfg=config) -> None:
        self._cfg = cfg

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as c:
            r = await c.get(f"{self._cfg.base_url}{path}", headers=self._cfg.headers())
            r.raise_for_status()
            return r.json()

    async def modalities(self) -> dict:
        """The four modalities and their caps/budget tables — the eligibility backstop."""
        return await self._get("/diagnostic/modalities")

    async def scenarios(self) -> dict:
        return await self._get("/diagnostic/scenarios")

    async def agents_for(self, modality: str) -> list[str]:
        """Who the ENGINE says may bid on this modality.

        The local answer is `queries.engine_agents_for`; this is the backstop, and the two
        must agree. An ineligible candidate is dropped by the engine without an error, which
        silently lowers contention and changes who wins — so a mismatch is worth failing on
        rather than discovering in a ladder that looks merely surprising.
        """
        data = await self.modalities()
        for m in data.get("modalities", []):
            if m.get("modality") == modality:
                return list(m.get("eligible_agents") or [])
        return []

    async def run_scenario(self, name: str, regime: str = "normal") -> dict:
        """The fixture path: no patient data, no key needed. Phase 1 of the rollout."""
        return await self._post({"scenario": name, "regime": regime})

    async def run_query(self, text: str, regime: str = "normal") -> dict:
        return await self._post({"query": text, "regime": regime})

    async def run_auction(self, body: dict) -> dict:
        """The inline path. ``mode`` is forced from config; never trust a caller with it."""
        return await self._post({**body, "mode": self._cfg.mode})

    async def _post(self, body: dict) -> dict:
        # default=str coerces datetimes/Decimals, whether the reader returned JSON strings
        # (Fabric) or native DB objects (direct psycopg).
        payload = json.dumps(body, default=str)
        headers = {**self._cfg.headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as c:
            r = await c.post(
                f"{self._cfg.base_url}/diagnostic/auction", content=payload, headers=headers
            )
            if r.status_code >= 400:
                # 422 carries `evidence`/`missing` or the field that was absent; 403 means no
                # key is configured on the engine. Both are answers, not transport failures,
                # and the reason has to survive to the UI.
                try:
                    detail = r.json()
                except ValueError:
                    detail = r.text
                raise DiagnosticEngineError(r.status_code, detail)
            return r.json()
