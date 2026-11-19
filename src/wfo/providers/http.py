"""Shared HTTP plumbing for live provider adapters.

API keys are read from the environment by ``catalog.build_live_providers``; they
are never logged or persisted. Endpoint shapes follow each vendor's public docs
at the time of writing - verify against current docs before production use.
"""

from __future__ import annotations

from typing import Any

import httpx

from wfo.models import Contact
from wfo.providers.base import (
    ProviderAuthError,
    ProviderError,
    ProviderResponse,
    RateLimitedError,
)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class HttpProvider:
    """Base adapter: maps HTTP status codes onto the provider error taxonomy."""

    name: str = "http"
    cost_per_call: float = 0.0
    charge_on_miss: bool = True

    def __init__(
        self, api_key: str, client: httpx.AsyncClient, cost_per_call: float | None = None
    ) -> None:
        if not api_key:
            raise ValueError(f"{self.name}: missing API key")
        self._api_key = api_key
        self._client = client
        if cost_per_call is not None:
            self.cost_per_call = cost_per_call

    async def find_email(self, contact: Contact) -> ProviderResponse:
        raise NotImplementedError

    def __repr__(self) -> str:  # never leak the key
        return f"{type(self).__name__}(name={self.name!r})"

    async def _send(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            resp = await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name}: timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderError(f"{self.name}: transport error {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise RateLimitedError(f"{self.name}: 429", retry_after=_retry_after(resp))
        if resp.status_code in (401, 403):
            raise ProviderAuthError(f"{self.name}: auth failed ({resp.status_code})")
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 500:
            raise ProviderError(f"{self.name}: upstream {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: client error {resp.status_code}", retryable=False)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name}: invalid JSON") from exc
        return body if isinstance(body, dict) else {}
