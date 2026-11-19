"""Domain types shared across the runtime, routers and simulation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

COUNTRIES: tuple[str, ...] = ("US", "UK", "DE", "FR", "IN")
SIZE_BANDS: tuple[str, ...] = ("1-50", "51-500", "501+")
SENIORITIES: tuple[str, ...] = ("ic", "manager", "exec")


def size_band(employees: int) -> str:
    if employees <= 50:
        return "1-50"
    if employees <= 500:
        return "51-500"
    return "501+"


@dataclass(frozen=True, slots=True)
class Segment:
    """Routing context: country x company-size band x seniority."""

    country: str
    size: str
    seniority: str

    @property
    def key(self) -> str:
        return f"{self.country}|{self.size}|{self.seniority}"

    @classmethod
    def from_key(cls, key: str) -> Segment:
        country, size, seniority = key.split("|")
        return cls(country, size, seniority)


def all_segments() -> list[Segment]:
    return [Segment(c, s, r) for c in COUNTRIES for s in SIZE_BANDS for r in SENIORITIES]


@dataclass(frozen=True, slots=True)
class Contact:
    id: str
    first_name: str
    last_name: str
    company_domain: str
    country: str
    employees: int
    seniority: str

    @property
    def segment(self) -> Segment:
        country = self.country.upper() if self.country.upper() in COUNTRIES else "US"
        seniority = self.seniority.lower() if self.seniority.lower() in SENIORITIES else "ic"
        return Segment(country, size_band(self.employees), seniority)

    @property
    def lookup_key(self) -> str:
        """Normalised identity used for response caching (not the row id)."""
        return (
            f"{self.first_name.strip().lower()}|{self.last_name.strip().lower()}|"
            f"{self.company_domain.strip().lower()}"
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Contact:
        return cls(
            id=str(d["id"]),
            first_name=str(d["first_name"]),
            last_name=str(d["last_name"]),
            company_domain=str(d["company_domain"]),
            country=str(d.get("country", "US")),
            employees=int(d.get("employees", 100)),
            seniority=str(d.get("seniority", "ic")),
        )


class Verification(StrEnum):
    VALID = "valid"
    RISKY = "risky"
    CATCH_ALL = "catch_all"
    INVALID = "invalid"
    UNVERIFIED = "unverified"


class StopReason(StrEnum):
    VERIFIED = "verified"
    CATCH_ALL = "catch_all"
    EXHAUSTED = "exhausted"
    ROUTER_STOP = "router_stop"
    BUDGET = "budget"


@dataclass(slots=True)
class ProviderCall:
    provider: str
    found: bool
    cost: float
    latency_s: float
    from_cache: bool = False
    verification: Verification = Verification.UNVERIFIED
    error: str | None = None


@dataclass(slots=True)
class ContactOutcome:
    contact_id: str
    segment: str
    email: str | None
    verification: Verification
    provider: str | None
    stop_reason: StopReason
    cost: float
    latency_s: float
    calls: list[ProviderCall] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.verification is Verification.VALID

    @property
    def found(self) -> bool:
        return self.email is not None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verification"] = self.verification.value
        d["stop_reason"] = self.stop_reason.value
        for c in d["calls"]:
            c["verification"] = Verification(c["verification"]).value
        return d
