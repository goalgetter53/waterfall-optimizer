"""Email verification: makes "verified" distinct from "found".

``SimulatedVerifier`` models an SMTP-style verifier against the synthetic world's
ground truth. ``ProviderStatusVerifier`` is the live-mode default: it maps the
deliverability status vendors return alongside the email. A dedicated verifier
(ZeroBounce, NeverBounce, ...) plugs into the same ``Verifier`` protocol.
"""

from __future__ import annotations

from typing import Protocol

from wfo.clock import Clock
from wfo.models import Contact, Verification
from wfo.sim.world import P_INVALID_IF_WRONG, P_RISKY_IF_WRONG, P_VALID_IF_CORRECT, SyntheticWorld


class Verifier(Protocol):
    cost_per_check: float

    async def verify(
        self, contact: Contact, email: str, provider_status: str | None
    ) -> Verification: ...


class SimulatedVerifier:
    def __init__(
        self,
        world: SyntheticWorld,
        clock: Clock,
        cost_per_check: float = 0.003,
        latency_s: float = 0.15,
        latency_scale: float = 1.0,
    ) -> None:
        self.world = world
        self.clock = clock
        self.cost_per_check = cost_per_check
        self.latency_s = latency_s * latency_scale
        self.checks = 0

    async def verify(
        self, contact: Contact, email: str, provider_status: str | None
    ) -> Verification:
        self.checks += 1
        await self.clock.sleep(self.latency_s)
        truth = self.world.truth(contact)
        if truth.catch_all:
            return Verification.CATCH_ALL
        u = self.world.uniform(contact.id, "verify", email)
        if email == truth.true_email:
            return Verification.VALID if u < P_VALID_IF_CORRECT else Verification.RISKY
        if u < P_INVALID_IF_WRONG:
            return Verification.INVALID
        if u < P_INVALID_IF_WRONG + P_RISKY_IF_WRONG:
            return Verification.RISKY
        return Verification.VALID


_STATUS_MAP = {
    "valid": Verification.VALID,
    "deliverable": Verification.VALID,
    "verified": Verification.VALID,
    "accept_all": Verification.CATCH_ALL,
    "catch_all": Verification.CATCH_ALL,
    "catch-all": Verification.CATCH_ALL,
    "risky": Verification.RISKY,
    "unknown": Verification.RISKY,
    "invalid": Verification.INVALID,
    "undeliverable": Verification.INVALID,
}


class ProviderStatusVerifier:
    """Zero-cost verifier that trusts the vendor's own deliverability status."""

    cost_per_check = 0.0

    async def verify(
        self, contact: Contact, email: str, provider_status: str | None
    ) -> Verification:
        if provider_status is None:
            return Verification.RISKY
        return _STATUS_MAP.get(provider_status.strip().lower(), Verification.RISKY)
