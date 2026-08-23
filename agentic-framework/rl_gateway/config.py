"""Configuration for the RL engine adapter.

Self-contained (reads os.environ) to keep the blast radius off the central config.settings;
move these onto settings later if the gateway becomes first-class.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


# §0 of BACKEND_HANDOVER. Our departments -> the engine's AgentKind enum (er|ot|ward|icu),
# now sourced from cases.json `departments` (single source of truth). Re-exported here so
# existing importers (mapping.py) keep working.
#
# ICU maps to `ward` (Option A, decision F-12 PENDING with the medical director): `icu` is
# declared in the enum but NOT eligible on icu_bed / hdu_bed profiles, and an ineligible
# candidate is SILENTLY DROPPED (runner.py:152) — no error, contention falls, a different
# department wins. Until F-12 resolves, ICU-originated demand bids as the medical bidder,
# whose budget/criticality/targets are already fitted. mapping.assert_biddable() enforces the
# guard rail on every POST so a mis-map cannot silently drop.
from rl_gateway.queries import AGENT_MAP  # noqa: E402,F401  (re-export; single source: cases.json)


@dataclass(frozen=True)
class GatewayConfig:
    # The RL engine's HTTP service (the OTHER server in the 2-server split). Set ALLOCATION_URL
    # to wherever RL runs — same host different port, or a separate host. mode: live is refused
    # over HTTP by design, so this is advisory only.
    base_url: str = field(default_factory=lambda: os.getenv("ALLOCATION_URL", "http://localhost:8901"))
    api_key: str | None = field(default_factory=lambda: os.getenv("ALLOCATION_API_KEY", "").strip() or None)
    # Only advisory is exercised in phase 1; kept explicit so nothing defaults to live.
    mode: str = field(default_factory=lambda: os.getenv("ALLOCATION_MODE", "advisory"))
    timeout_seconds: float = 30.0

    def headers(self) -> dict[str, str]:
        # candidates[] carry patient data and require the key (service.py:470, 403 otherwise).
        return {"X-API-Key": self.api_key} if self.api_key else {}


config = GatewayConfig()
