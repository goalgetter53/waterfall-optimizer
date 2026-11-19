"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


def _f(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    return default if raw in (None, "") else float(raw)


@dataclass
class Settings:
    db_path: str = ".wfo/wfo.db"
    mode: str = "simulated"  # "simulated" | "live"
    budget_usd: float | None = None
    value_per_verified: float = 0.50
    max_spend_per_contact: float = 0.30
    concurrency: int = 8
    visibility_timeout_s: float = 60.0
    webhook_secret: str = ""
    sim_seed: int = 7
    sim_latency_scale: float = 0.01
    rate_per_s: float = 10.0
    burst: float = 20.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        budget_raw = env.get("WFO_BUDGET_USD")
        return cls(
            db_path=env.get("WFO_DB_PATH", cls.db_path),
            mode=env.get("WFO_MODE", cls.mode),
            budget_usd=float(budget_raw) if budget_raw else None,
            value_per_verified=_f(env, "WFO_VALUE_PER_VERIFIED", cls.value_per_verified),
            max_spend_per_contact=_f(env, "WFO_MAX_SPEND_PER_CONTACT", cls.max_spend_per_contact),
            concurrency=int(_f(env, "WFO_CONCURRENCY", cls.concurrency)),
            visibility_timeout_s=_f(env, "WFO_VISIBILITY_TIMEOUT_S", cls.visibility_timeout_s),
            webhook_secret=env.get("WFO_WEBHOOK_SECRET", ""),
            sim_seed=int(_f(env, "WFO_SIM_SEED", cls.sim_seed)),
            sim_latency_scale=_f(env, "WFO_SIM_LATENCY_SCALE", cls.sim_latency_scale),
            rate_per_s=_f(env, "WFO_RATE_PER_S", cls.rate_per_s),
            burst=_f(env, "WFO_BURST", cls.burst),
        )
