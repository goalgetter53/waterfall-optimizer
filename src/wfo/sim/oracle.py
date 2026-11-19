"""Monte-Carlo oracle: best fixed provider sequence per segment under TRUE parameters.

Because a waterfall only continues after a miss, every router that conditions on
(segment, providers already tried) is equivalent to an ordered provider sequence
per segment. Enumerating all ordered subsets (65 for 4 providers, incl. "call
nobody") and scoring each on a large sample from the generative model gives the
best policy in that class: the regret baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

import numpy as np

from wfo.sim.world import (
    GUESS_FACTOR,
    P_INVALID_IF_WRONG,
    P_RISKY_IF_WRONG,
    P_VALID_IF_CORRECT,
    SyntheticWorld,
)


def ordered_subsets(items: list[str]) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = [()]
    for k in range(1, len(items) + 1):
        out.extend(permutations(items, k))
    return out


@dataclass(slots=True)
class _Draws:
    found: dict[str, np.ndarray]
    valid: dict[str, np.ndarray]
    catch_all: np.ndarray


def _draws(world: SyntheticWorld, key: str, n: int, rng: np.random.Generator) -> _Draws:
    p = world.params[key]
    disc = rng.random(n) < p.discoverable
    catch_all = rng.random(n) < p.catch_all
    # One verifier draw per distinct email string, as in SimulatedVerifier.
    valid_if_correct = rng.random(n) < P_VALID_IF_CORRECT
    valid_if_wrong = rng.random(n) >= P_INVALID_IF_WRONG + P_RISKY_IF_WRONG
    found: dict[str, np.ndarray] = {}
    valid: dict[str, np.ndarray] = {}
    for name in sorted(world.specs):
        h = p.hit[name]
        u_hit = rng.random(n)
        f = np.where(disc, u_hit < h, u_hit < GUESS_FACTOR * h)
        correct = disc & f & (rng.random(n) < p.accuracy[name])
        found[name] = f
        valid[name] = f & ~catch_all & np.where(correct, valid_if_correct, valid_if_wrong)
    return _Draws(found, valid, catch_all)


def _reward(
    world: SyntheticWorld,
    d: _Draws,
    seq: tuple[str, ...] | list[str],
    value_per_verified: float,
    verify_cost: float,
) -> float:
    n = len(d.catch_all)
    alive = np.ones(n, dtype=bool)
    reward = np.zeros(n)
    for name in seq:
        spec = world.specs[name]
        f = d.found[name]
        call_cost = np.where(f | spec.charge_on_miss, spec.cost_per_call, 0.0)
        reward -= alive * (call_cost + f * verify_cost)
        reward += (alive & d.valid[name]) * value_per_verified
        alive &= ~(d.valid[name] | (f & d.catch_all))
    return float(reward.mean())


def best_sequences(
    world: SyntheticWorld,
    value_per_verified: float,
    verify_cost: float,
    n_draws: int = 4000,
) -> tuple[dict[str, list[str]], dict[str, float]]:
    """Returns ({segment_key: best sequence}, {segment_key: expected reward/contact})."""
    rng = np.random.default_rng(world.seed + 10_007)
    seqs = ordered_subsets(sorted(world.specs))
    best: dict[str, list[str]] = {}
    best_value: dict[str, float] = {}
    for key in world.params:
        d = _draws(world, key, n_draws, rng)
        rewards = [_reward(world, d, seq, value_per_verified, verify_cost) for seq in seqs]
        i = int(np.argmax(rewards))
        best[key] = list(seqs[i])
        best_value[key] = rewards[i]
    return best, best_value


def evaluate_sequences(
    world: SyntheticWorld,
    sequences: dict[str, list[str]],
    value_per_verified: float,
    verify_cost: float,
    n_draws: int = 4000,
) -> dict[str, float]:
    """Expected reward per contact of given per-segment sequences (same draws as the oracle)."""
    rng = np.random.default_rng(world.seed + 10_007)
    out: dict[str, float] = {}
    for key in world.params:
        d = _draws(world, key, n_draws, rng)
        out[key] = _reward(world, d, sequences.get(key, []), value_per_verified, verify_cost)
    return out
