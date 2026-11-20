from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from wfo.sim.calibration import (
    ASSUMPTION,
    CalibrationError,
    ProviderMoments,
    from_dict,
    implied_union_valid,
    load,
    solve_discoverable,
    solve_provider,
    validate,
)
from wfo.sim.harness import SimConfig, build_world, run_one
from wfo.sim.world import GUESS_FACTOR, SyntheticWorld

CONFIG = Path(__file__).resolve().parents[1] / "configs" / "real_calibrated.json"


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CONFIG.read_text())
    return data


def test_calibrated_config_loads_and_validates() -> None:
    cal = load(CONFIG)
    assert {s.name for s in cal.specs} == {"apollo", "dropcontact", "hunter", "prospeo"}
    for s in cal.specs:
        assert 0 < s.cost_per_call < 1
        assert 0 < s.base_hit < 1
        assert 0 < s.accuracy <= 1
        assert s.charge_on_miss is False  # all four publish pay-on-found credit rules
    assert 0 < cal.model.discoverable_base < 1
    world = SyntheticWorld(1, cal.specs, cal.segment_noise, cal.model)
    assert len(world.params) == 45


def test_every_number_cites_a_source_or_is_an_explicit_assumption() -> None:
    raw = _raw()
    for key, src in raw["sources"].items():
        assert src["url"].startswith("https://"), key
        assert src["accessed"] == raw["as_of"]
    prices = {p: v["price_usd"] for p, v in raw["providers"].items()}
    for name, leaf in prices.items():
        assert leaf["source"] != ASSUMPTION, f"{name} price must come from a real source"

    bad = copy.deepcopy(raw)
    bad["providers"]["hunter"]["price_usd"]["source"] = "S_nowhere"
    with pytest.raises(CalibrationError, match="unknown source"):
        validate(bad)
    bad = copy.deepcopy(raw)
    bad["world"]["catch_all"].pop("note")
    with pytest.raises(CalibrationError, match="need a 'note'"):
        validate(bad)
    bad = copy.deepcopy(raw)
    bad["world"]["size_weights"]["1-50"]["value"] = 0.9
    with pytest.raises(CalibrationError, match="sum to 1"):
        from_dict(bad)


def test_solved_parameters_reproduce_the_benchmark_moments() -> None:
    raw = _raw()
    d = raw["world"]["discoverable"]["value"]
    hits, accs = {}, {}
    for name, p in raw["providers"].items():
        obs = raw["observed"][name]
        m = ProviderMoments(obs["found_rate"]["value"], obs["valid_rate"]["value"])
        h, a = solve_provider(m, d)
        assert h == pytest.approx(p["base_hit"]["value"], abs=1e-3)
        assert a == pytest.approx(p["accuracy"]["value"], abs=1e-3)
        found = h * (d + GUESS_FACTOR * (1 - d))
        assert found == pytest.approx(m.found_rate, abs=1e-9)
        hits[name], accs[name] = h, a
    union = raw["observed"]["union4_valid_rate"]["value"]
    assert implied_union_valid(hits, accs, d) == pytest.approx(union, abs=2e-3)


def test_solve_discoverable_round_trips() -> None:
    moments = {"a": ProviderMoments(0.7, 0.6), "b": ProviderMoments(0.5, 0.45)}
    d = solve_discoverable(moments, 0.75)
    sol = {k: solve_provider(m, d) for k, m in moments.items()}
    got = implied_union_valid(
        {k: s[0] for k, s in sol.items()}, {k: s[1] for k, s in sol.items()}, d
    )
    assert got == pytest.approx(0.75, abs=1e-6)


def test_sensitivity_overrides_and_harness_wiring() -> None:
    cfg = SimConfig(
        n_contacts=120, seeds=(1,), world_config=str(CONFIG), hit_multipliers={"apollo": 0.7}
    )
    world, verify_cost = build_world(1, cfg)
    base, _ = build_world(1, SimConfig(world_config=str(CONFIG)))
    assert verify_cost == 0.0
    for key in world.params:
        assert world.params[key].hit["apollo"] == pytest.approx(
            base.params[key].hit["apollo"] * 0.7
        )
        assert world.params[key].hit["hunter"] == base.params[key].hit["hunter"]
    annual, _ = build_world(1, SimConfig(world_config=str(CONFIG), price_set="annual"))
    assert annual.specs["hunter"].cost_per_call < base.specs["hunter"].cost_per_call
    run = run_one("static", 1, cfg)
    assert run.n == 120
    assert run.verified > 0
