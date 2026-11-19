"""Builds provider sets for live (HTTP) and simulated modes."""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from wfo.providers.apollo import ApolloProvider
from wfo.providers.base import Provider
from wfo.providers.dropcontact import DropcontactProvider
from wfo.providers.http import HttpProvider
from wfo.providers.hunter import HunterProvider
from wfo.providers.prospeo import ProspeoProvider

LIVE_ADAPTERS: dict[str, tuple[type[HttpProvider], str]] = {
    "apollo": (ApolloProvider, "APOLLO_API_KEY"),
    "hunter": (HunterProvider, "HUNTER_API_KEY"),
    "prospeo": (ProspeoProvider, "PROSPEO_API_KEY"),
    "dropcontact": (DropcontactProvider, "DROPCONTACT_API_KEY"),
}


def build_live_providers(
    client: httpx.AsyncClient, env: Mapping[str, str] | None = None
) -> dict[str, Provider]:
    """Instantiate an adapter for every provider whose API key is set."""
    env = os.environ if env is None else env
    out: dict[str, Provider] = {}
    for name, (cls, key_var) in LIVE_ADAPTERS.items():
        key = env.get(key_var, "")
        if not key:
            continue
        cost_raw = env.get(f"WFO_{name.upper()}_COST")
        out[name] = cls(key, client, float(cost_raw) if cost_raw else None)
    return out
