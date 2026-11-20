"""Synthetic world: segment-level parameters and per-contact seeded ground truth.

Everything here is SIMULATED. Parameters are illustrative and are not quotes or
benchmarks of any real vendor.

Generative model (shared by the simulated providers, the simulated verifier and
the Monte-Carlo oracle, so all three agree):

* contact latent ``D`` (email discoverable at all) ~ Bernoulli(d_s)
* contact latent ``CA`` (domain is catch-all)      ~ Bernoulli(ca_s)
* provider ``i`` call: if D, returns an email w.p. h_is, correct w.p. a_i;
  if not D, returns a pattern guess w.p. 0.15 * h_is (always wrong)
* verifier: CA -> catch_all; correct -> valid (0.97) / risky;
  wrong -> invalid (0.93) / risky (0.06) / valid (0.01, false positive)
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from statistics import NormalDist

from wfo.models import COUNTRIES, SENIORITIES, SIZE_BANDS, Contact, Segment, all_segments

GUESS_FACTOR = 0.15
P_VALID_IF_CORRECT = 0.97
P_INVALID_IF_WRONG = 0.93
P_RISKY_IF_WRONG = 0.06
_NORMAL = NormalDist()


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass(frozen=True, slots=True)
class SimProviderSpec:
    name: str
    cost_per_call: float
    charge_on_miss: bool
    base_hit: float
    accuracy: float
    country_mod: dict[str, float]
    size_mod: dict[str, float]
    seniority_mod: dict[str, float]
    latency_median_s: float
    latency_sigma: float
    error_rate: float
    rate_limit_rate: float
    hit_multiplier: float = 1.0
    """Sensitivity knob: scales every segment hit rate (capped at 0.99)."""


DEFAULT_SPECS: tuple[SimProviderSpec, ...] = (
    SimProviderSpec(
        "apollo",
        0.020,
        True,
        0.62,
        0.80,
        {"US": 0.6, "UK": 0.1, "DE": -1.0, "FR": -1.0, "IN": 0.2},
        {"1-50": -0.6, "51-500": 0.0, "501+": 0.5},
        {"ic": 0.0, "manager": 0.0, "exec": -0.2},
        0.35,
        0.5,
        0.02,
        0.03,
    ),
    SimProviderSpec(
        "hunter",
        0.035,
        False,
        0.50,
        0.90,
        {"US": 0.2, "UK": 0.2, "DE": 0.0, "FR": 0.0, "IN": -0.4},
        {"1-50": 0.7, "51-500": 0.2, "501+": -0.5},
        {"ic": 0.2, "manager": 0.0, "exec": -0.6},
        0.8,
        0.5,
        0.02,
        0.02,
    ),
    SimProviderSpec(
        "prospeo",
        0.040,
        True,
        0.52,
        0.88,
        {"US": 0.1, "UK": 0.0, "DE": -0.3, "FR": -0.3, "IN": 0.7},
        {"1-50": 0.0, "51-500": 0.3, "501+": 0.0},
        {"ic": 0.0, "manager": 0.3, "exec": 0.5},
        0.9,
        0.6,
        0.03,
        0.04,
    ),
    SimProviderSpec(
        "dropcontact",
        0.080,
        True,
        0.45,
        0.96,
        {"US": -0.4, "UK": 0.6, "DE": 1.4, "FR": 1.5, "IN": -0.8},
        {"1-50": 0.0, "51-500": 0.0, "501+": 0.0},
        {"ic": 0.0, "manager": 0.0, "exec": 0.0},
        2.2,
        0.4,
        0.01,
        0.01,
    ),
)

_D_COUNTRY = {"US": 0.3, "UK": 0.1, "DE": -0.2, "FR": -0.3, "IN": -0.6}
_D_SIZE = {"1-50": -0.3, "51-500": 0.0, "501+": 0.3}
_D_SEN = {"ic": -0.2, "manager": 0.1, "exec": 0.4}
_CA_SIZE = {"1-50": -0.4, "51-500": 0.0, "501+": 0.6}
_CA_COUNTRY = {"US": 0.0, "UK": 0.1, "DE": 0.3, "FR": 0.3, "IN": -0.2}

_COUNTRY_W = (0.40, 0.15, 0.15, 0.10, 0.20)
_SIZE_W = (0.45, 0.35, 0.20)
_SEN_W = (0.55, 0.30, 0.15)


@dataclass(frozen=True, slots=True)
class WorldModel:
    """Contact-level latents and segment mix. Defaults are the illustrative world."""

    discoverable_base: float = 0.82
    d_country: dict[str, float] = field(default_factory=lambda: dict(_D_COUNTRY))
    d_size: dict[str, float] = field(default_factory=lambda: dict(_D_SIZE))
    d_seniority: dict[str, float] = field(default_factory=lambda: dict(_D_SEN))
    catch_all_base: float = 0.12
    ca_size: dict[str, float] = field(default_factory=lambda: dict(_CA_SIZE))
    ca_country: dict[str, float] = field(default_factory=lambda: dict(_CA_COUNTRY))
    country_weights: tuple[float, ...] = _COUNTRY_W
    size_weights: tuple[float, ...] = _SIZE_W
    seniority_weights: tuple[float, ...] = _SEN_W


def _latent(base: float, *mods: float) -> float:
    if base <= 0.0:
        return 0.0
    if base >= 1.0:
        return 1.0
    return _sigmoid(_logit(base) + sum(mods))


DEFAULT_MODEL = WorldModel()
_FIRST = (
    "ana",
    "ben",
    "chen",
    "dana",
    "eli",
    "fatima",
    "gabriel",
    "hana",
    "ivan",
    "jules",
    "kofi",
    "lena",
    "marco",
    "nina",
    "omar",
    "priya",
    "quinn",
    "rosa",
    "sam",
    "tara",
)
_LAST = (
    "ali",
    "becker",
    "costa",
    "dubois",
    "evans",
    "fischer",
    "garcia",
    "haddad",
    "iyer",
    "jones",
    "kumar",
    "lopez",
    "martin",
    "novak",
    "okafor",
    "patel",
    "rossi",
    "smith",
)


@dataclass(frozen=True, slots=True)
class SegmentParams:
    discoverable: float
    catch_all: float
    hit: dict[str, float]
    accuracy: dict[str, float]


@dataclass(frozen=True, slots=True)
class GroundTruth:
    true_email: str
    discoverable: bool
    catch_all: bool


class SyntheticWorld:
    """Seeded world. Same seed -> identical parameters, contacts and outcomes."""

    def __init__(
        self,
        seed: int,
        specs: tuple[SimProviderSpec, ...] = DEFAULT_SPECS,
        segment_noise: float = 0.35,
        model: WorldModel = DEFAULT_MODEL,
    ) -> None:
        self.seed = seed
        self.specs = {s.name: s for s in specs}
        self.model = model
        rng = random.Random(f"world:{seed}")
        self.params: dict[str, SegmentParams] = {}
        m = model
        for seg in all_segments():
            d = _latent(
                m.discoverable_base,
                m.d_country[seg.country],
                m.d_size[seg.size],
                m.d_seniority[seg.seniority],
            )
            ca = _latent(m.catch_all_base, m.ca_size[seg.size], m.ca_country[seg.country])
            hit: dict[str, float] = {}
            for s in specs:
                z = (
                    _logit(s.base_hit)
                    + s.country_mod[seg.country]
                    + s.size_mod[seg.size]
                    + s.seniority_mod[seg.seniority]
                    + rng.gauss(0.0, segment_noise)
                )
                h = _sigmoid(z)
                if s.hit_multiplier != 1.0:
                    h = min(0.99, h * s.hit_multiplier)
                hit[s.name] = h
            self.params[seg.key] = SegmentParams(d, ca, hit, {s.name: s.accuracy for s in specs})

    # ---- deterministic uniforms (common random numbers across strategies) ----
    def uniform(self, *parts: object) -> float:
        h = hashlib.blake2b(f"{self.seed}:{':'.join(map(str, parts))}".encode(), digest_size=8)
        return (int.from_bytes(h.digest(), "big") + 0.5) / 2.0**64

    def truth(self, contact: Contact) -> GroundTruth:
        p = self.params[contact.segment.key]
        email = f"{contact.first_name}.{contact.last_name}@{contact.company_domain}".lower()
        return GroundTruth(
            true_email=email,
            discoverable=self.uniform(contact.id, "D") < p.discoverable,
            catch_all=self.uniform(contact.id, "CA") < p.catch_all,
        )

    def latency(self, provider: str, *parts: object) -> float:
        spec = self.specs[provider]
        z = _NORMAL.inv_cdf(self.uniform(provider, "lat", *parts))
        return spec.latency_median_s * math.exp(spec.latency_sigma * z)

    # ---- synthetic contacts -------------------------------------------------
    def generate_contacts(self, n: int) -> list[Contact]:
        rng = random.Random(f"contacts:{self.seed}")
        sizes = {"1-50": (2, 50), "51-500": (51, 500), "501+": (501, 20000)}
        out: list[Contact] = []
        for i in range(n):
            country = rng.choices(COUNTRIES, self.model.country_weights)[0]
            band = rng.choices(SIZE_BANDS, self.model.size_weights)[0]
            seniority = rng.choices(SENIORITIES, self.model.seniority_weights)[0]
            lo, hi = sizes[band]
            out.append(
                Contact(
                    id=f"c{self.seed}-{i}",
                    first_name=rng.choice(_FIRST),
                    last_name=rng.choice(_LAST),
                    company_domain=f"co{rng.randrange(10**6)}.{country.lower()}.example",
                    country=country,
                    employees=rng.randint(lo, hi),
                    seniority=seniority,
                )
            )
        return out

    # ---- analytic helpers ---------------------------------------------------
    def p_verified(self, segment: Segment, provider: str) -> float:
        """Marginal P(call to ``provider`` yields a VALID email) in a segment."""
        p = self.params[segment.key]
        h, a = p.hit[provider], p.accuracy[provider]
        correct = p.discoverable * h * a
        wrong = p.discoverable * h * (1 - a) + (1 - p.discoverable) * GUESS_FACTOR * h
        valid_rate = 1 - P_INVALID_IF_WRONG - P_RISKY_IF_WRONG
        return (1 - p.catch_all) * (correct * P_VALID_IF_CORRECT + wrong * valid_rate)
