"""httpx client to the RL engine's HTTP API.

Only the routes the adapter needs. mode is stamped onto every auction body from config and is
never `live` — the engine refuses `mode: live` over HTTP by design (service.py:280); first
live auctions go through its CLI with a human watching.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from rl_gateway.config import config


class AllocationClient:
    def __init__(self, cfg=config) -> None:
        self._cfg = cfg

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as c:
            r = await c.get(f"{self._cfg.base_url}{path}", headers=self._cfg.headers())
            r.raise_for_status()
            return r.json()

    async def health(self) -> dict:
        return await self._get("/health")

    async def use_cases(self) -> dict:
        """Registered profiles and their eligible bidders — the source for the §0 guard."""
        return await self._get("/use-cases")

    async def bidders_for(self, resource_type: str) -> list[str]:
        """The agents allowed to bid on a profile. Empty list if the profile is unknown
        (caller's assert_biddable will then refuse every candidate).

        /use-cases shape (verified): {"profiles": [{"resource_type": "icu_bed",
        "detail": {"bidders": "er, ot, ward", ...}}, ...]} — bidders is a comma string."""
        data = await self.use_cases()
        for p in data.get("profiles", []):
            if p.get("resource_type") == resource_type:
                # Prefer the machine-readable list; fall back to the human comma-string on
                # an older engine build that doesn't yet return it.
                if isinstance(p.get("bidders"), list):
                    return list(p["bidders"])
                raw = (p.get("detail") or {}).get("bidders", "")
                return [b.strip() for b in raw.split(",") if b.strip()]
        return []

    async def run_auction(self, body: dict) -> dict:
        """POST /auction. mode is forced from config (advisory) — never trust a caller to
        set it, and never send live over HTTP."""
        body = {**body, "mode": self._cfg.mode}
        # default=str coerces datetimes/Decimals to strings — robust whether the reader
        # returns JSON strings (Fabric) or native DB objects (direct psycopg).
        payload = json.dumps(body, default=str)
        headers = {**self._cfg.headers(), "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as c:
            r = await c.post(f"{self._cfg.base_url}/auction", content=payload, headers=headers)
            r.raise_for_status()
            return r.json()
