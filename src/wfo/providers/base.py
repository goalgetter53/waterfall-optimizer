"""Provider plugin interface.

A provider looks up a work email for a contact. Adapters translate vendor HTTP
semantics into three outcomes: a response (email or no email), a retryable
error (including rate limiting), or a non-retryable error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from wfo.models import Contact


@dataclass(slots=True)
class ProviderResponse:
    email: str | None
    charged: float
    """Actual cost of this call in USD (0 for pay-per-hit misses)."""
    provider_status: str | None = None
    """Vendor-reported deliverability hint, if any (e.g. Hunter ``status``)."""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_cache(self) -> dict[str, Any]:
        return {"email": self.email, "charged": self.charged, "status": self.provider_status}

    @classmethod
    def from_cache(cls, d: dict[str, Any]) -> ProviderResponse:
        return cls(email=d.get("email"), charged=0.0, provider_status=d.get("status"))


class ProviderError(Exception):
    """Base provider failure. ``retryable`` drives the retry policy."""

    retryable: bool = True

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class RateLimitedError(ProviderError):
    """HTTP 429 or equivalent. ``retry_after`` comes from the vendor if present."""

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message, retryable=True)
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    retryable = False


@runtime_checkable
class Provider(Protocol):
    name: str
    cost_per_call: float
    """Upper bound of a single call's cost; reserved in the ledger before calling."""
    charge_on_miss: bool

    async def find_email(self, contact: Contact) -> ProviderResponse: ...
