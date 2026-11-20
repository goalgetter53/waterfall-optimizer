"""Calibrated world: provider prices and hit rates taken from public sources.

The calibration file (``configs/real_calibrated.json``) is written by
``scripts/derive_real_calibration.py``. Every number in it is a ``{"value", "source"}``
leaf whose ``source`` is either a key of the file's ``sources`` table (URL + date
accessed) or the literal ``"ASSUMPTION"`` with a non-empty ``note``. This module
validates that contract and turns the file into ``SimProviderSpec`` + ``WorldModel``.

Outcomes are still SIMULATED: the calibration only sets the parameters of the
same generative model the illustrative world uses (see ``wfo.sim.world``).
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from wfo.models import COUNTRIES, SENIORITIES, SIZE_BANDS
from wfo.sim.world import (
    GUESS_FACTOR,
    P_INVALID_IF_WRONG,
    P_RISKY_IF_WRONG,
    P_VALID_IF_CORRECT,
    SimProviderSpec,
    WorldModel,
)

ASSUMPTION = "ASSUMPTION"
P_VALID_IF_WRONG = 1.0 - P_INVALID_IF_WRONG - P_RISKY_IF_WRONG


class CalibrationError(ValueError):
    """The calibration file is malformed or a number lacks a source."""


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Moment matching (used by the derivation script and by the tests)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProviderMoments:
    found_rate: float
    """P(provider returns any email) on the benchmark sample."""
    valid_rate: float
    """P(provider returns an email the benchmark's verifiers rate valid)."""


def solve_provider(m: ProviderMoments, discoverable: float) -> tuple[float, float]:
    """(hit, accuracy) of the world model that reproduce ``m`` exactly (catch-all = 0).

    found = h * (d + g * (1 - d));  valid = pc * x + pw * (found - x),  x = d * h * a.
    """
    d, g = discoverable, GUESS_FACTOR
    h = m.found_rate / (d + g * (1.0 - d))
    x = (m.valid_rate - P_VALID_IF_WRONG * m.found_rate) / (P_VALID_IF_CORRECT - P_VALID_IF_WRONG)
    return h, x / (d * h)


def implied_union_valid(hits: Mapping[str, float], accs: Mapping[str, float], d: float) -> float:
    """P(at least one provider returns a valid email), providers independent given D."""
    miss_d = miss_nd = 1.0
    for name, h in hits.items():
        a = accs[name]
        miss_d *= 1.0 - (h * a * P_VALID_IF_CORRECT + h * (1.0 - a) * P_VALID_IF_WRONG)
        miss_nd *= 1.0 - GUESS_FACTOR * h * P_VALID_IF_WRONG
    return d * (1.0 - miss_d) + (1.0 - d) * (1.0 - miss_nd)


def solve_discoverable(moments: Mapping[str, ProviderMoments], union_valid: float) -> float:
    """Bisection on d so the model's union-of-providers valid rate matches the benchmark.

    The lower bound keeps every solved accuracy <= 1 and every hit < 1."""

    def feasible(d: float) -> bool:
        for m in moments.values():
            h, a = solve_provider(m, d)
            if not (0.0 < h < 1.0 and 0.0 < a <= 1.0):
                return False
        return True

    lo, hi = 0.01, 0.999
    while not feasible(lo) and lo < hi:
        lo += 0.001
    if not feasible(lo):
        raise CalibrationError("no feasible discoverable rate for these moments")

    def union(d: float) -> float:
        sol = {k: solve_provider(m, d) for k, m in moments.items()}
        return implied_union_valid(
            {k: s[0] for k, s in sol.items()}, {k: s[1] for k, s in sol.items()}, d
        )

    # union(d) increases with d (hits shrink but the shared latent grows faster).
    if union(lo) >= union_valid:
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        if union(mid) < union_valid:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------
def _leaves(node: Any, path: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(node, dict):
        if "value" in node:
            yield path, node
            return
        for k, v in node.items():
            if k in ("sources", "notes"):
                continue
            yield from _leaves(v, f"{path}.{k}" if path else k)


def _num(leaf: Mapping[str, Any], where: str) -> float:
    v = leaf["value"]
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise CalibrationError(f"{where}: value must be a number")
    return float(v)


def _bool(leaf: Mapping[str, Any], where: str) -> bool:
    v = leaf["value"]
    if not isinstance(v, bool):
        raise CalibrationError(f"{where}: value must be a boolean")
    return v


def validate(raw: Mapping[str, Any]) -> None:
    """Every value leaf must cite a known source or be an explicit assumption."""
    sources = raw.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise CalibrationError("missing 'sources' table")
    for key, src in sources.items():
        if not isinstance(src, dict) or not src.get("url") or not src.get("accessed"):
            raise CalibrationError(f"source {key!r} needs 'url' and 'accessed'")
    n = 0
    for where, leaf in _leaves(raw):
        n += 1
        src = leaf.get("source")
        if src == ASSUMPTION:
            if not str(leaf.get("note", "")).strip():
                raise CalibrationError(f"{where}: assumptions need a 'note'")
        elif src not in sources:
            raise CalibrationError(f"{where}: unknown source {src!r}")
    if n == 0:
        raise CalibrationError("no values found")
    for section in ("providers", "world"):
        if section not in raw:
            raise CalibrationError(f"missing section {section!r}")
    for name, p in raw["providers"].items():
        for k in ("price_usd", "charge_on_miss", "base_hit", "accuracy", "country_mod"):
            if k not in p:
                raise CalibrationError(f"providers.{name}: missing {k!r}")
        if _num(p["price_usd"], f"{name}.price_usd") <= 0:
            raise CalibrationError(f"providers.{name}.price_usd must be > 0")
        for k in ("base_hit", "accuracy"):
            if not 0.0 < _num(p[k], f"{name}.{k}") <= 1.0:
                raise CalibrationError(f"providers.{name}.{k} must be in (0, 1]")
        if set(p["country_mod"]) != set(COUNTRIES):
            raise CalibrationError(f"providers.{name}.country_mod must cover {COUNTRIES}")
    w = raw["world"]
    for k, dims in (
        ("country_weights", COUNTRIES),
        ("size_weights", SIZE_BANDS),
        ("seniority_weights", SENIORITIES),
    ):
        if set(w[k]) != set(dims):
            raise CalibrationError(f"world.{k} must cover {dims}")
        total = sum(_num(v, f"world.{k}") for v in w[k].values())
        if abs(total - 1.0) > 1e-6:
            raise CalibrationError(f"world.{k} must sum to 1 (got {total})")
    if not 0.0 < _num(w["discoverable"], "world.discoverable") < 1.0:
        raise CalibrationError("world.discoverable must be in (0, 1)")


@dataclass(frozen=True, slots=True)
class Calibration:
    specs: tuple[SimProviderSpec, ...]
    model: WorldModel
    segment_noise: float
    verify_cost: float
    as_of: str
    raw: dict[str, Any]

    def with_overrides(
        self,
        hit_multipliers: Mapping[str, float] | None = None,
        segment_noise: float | None = None,
        country_effects: bool = True,
    ) -> Calibration:
        """Sensitivity variants: scale hit rates, change heterogeneity, drop country effects."""
        mult = dict(hit_multipliers or {})
        specs = tuple(
            replace(
                s,
                hit_multiplier=mult.get(s.name, 1.0),
                country_mod=s.country_mod if country_effects else dict.fromkeys(s.country_mod, 0.0),
            )
            for s in self.specs
        )
        return replace(
            self,
            specs=specs,
            segment_noise=self.segment_noise if segment_noise is None else segment_noise,
        )


def _mods(node: Mapping[str, Any], dims: tuple[str, ...], where: str) -> dict[str, float]:
    return {k: _num(node[k], f"{where}.{k}") for k in dims}


def from_dict(raw: dict[str, Any]) -> Calibration:
    validate(raw)
    specs: list[SimProviderSpec] = []
    for name in sorted(raw["providers"]):
        p = raw["providers"][name]
        specs.append(
            SimProviderSpec(
                name=name,
                cost_per_call=_num(p["price_usd"], f"{name}.price_usd"),
                charge_on_miss=_bool(p["charge_on_miss"], f"{name}.charge_on_miss"),
                base_hit=_num(p["base_hit"], f"{name}.base_hit"),
                accuracy=_num(p["accuracy"], f"{name}.accuracy"),
                country_mod=_mods(p["country_mod"], COUNTRIES, f"{name}.country_mod"),
                size_mod=_mods(p["size_mod"], SIZE_BANDS, f"{name}.size_mod"),
                seniority_mod=_mods(p["seniority_mod"], SENIORITIES, f"{name}.seniority_mod"),
                latency_median_s=_num(p["latency_median_s"], f"{name}.latency_median_s"),
                latency_sigma=_num(p["latency_sigma"], f"{name}.latency_sigma"),
                error_rate=_num(p["error_rate"], f"{name}.error_rate"),
                rate_limit_rate=_num(p["rate_limit_rate"], f"{name}.rate_limit_rate"),
            )
        )
    w = raw["world"]
    zero_c = dict.fromkeys(COUNTRIES, 0.0)
    zero_s = dict.fromkeys(SIZE_BANDS, 0.0)
    zero_r = dict.fromkeys(SENIORITIES, 0.0)
    model = WorldModel(
        discoverable_base=_num(w["discoverable"], "world.discoverable"),
        d_country=zero_c,
        d_size=zero_s,
        d_seniority=zero_r,
        catch_all_base=_num(w["catch_all"], "world.catch_all"),
        ca_size=zero_s,
        ca_country=zero_c,
        country_weights=tuple(_num(w["country_weights"][c], "w") for c in COUNTRIES),
        size_weights=tuple(_num(w["size_weights"][s], "w") for s in SIZE_BANDS),
        seniority_weights=tuple(_num(w["seniority_weights"][r], "w") for r in SENIORITIES),
    )
    return Calibration(
        specs=tuple(specs),
        model=model,
        segment_noise=_num(w["segment_noise"], "world.segment_noise"),
        verify_cost=_num(w["verify_cost_usd"], "world.verify_cost_usd"),
        as_of=str(raw.get("as_of", "")),
        raw=raw,
    )


def load(path: str | Path) -> Calibration:
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise CalibrationError("calibration file must be a JSON object")
    return from_dict(raw)


def logit_mod(rate: float, reference: float) -> float:
    """Additive logit modifier that moves ``reference`` to ``rate``."""
    return _logit(rate) - _logit(reference)
