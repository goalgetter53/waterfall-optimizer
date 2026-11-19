"""Dropcontact-style asynchronous batch adapter: submit, then poll for the result."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from wfo.models import Contact
from wfo.providers.base import ProviderError, ProviderResponse
from wfo.providers.http import HttpProvider


class DropcontactProvider(HttpProvider):
    name = "dropcontact"
    cost_per_call = 0.080
    charge_on_miss = True
    base_url = "https://api.dropcontact.io/batch"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        cost_per_call: float | None = None,
        poll_interval_s: float = 2.0,
        max_polls: int = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(api_key, client, cost_per_call)
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self._sleep = sleep

    async def find_email(self, contact: Contact) -> ProviderResponse:
        headers = {"X-Access-Token": self._api_key, "Content-Type": "application/json"}
        submitted = await self._send(
            "POST",
            self.base_url,
            headers=headers,
            json={
                "data": [
                    {
                        "first_name": contact.first_name,
                        "last_name": contact.last_name,
                        "website": contact.company_domain,
                    }
                ],
                "siren": False,
                "language": "en",
            },
        )
        request_id = submitted.get("request_id")
        if not request_id:
            raise ProviderError(f"{self.name}: no request_id in submit response")
        for _ in range(self.max_polls):
            await self._sleep(self.poll_interval_s)
            body = await self._send("GET", f"{self.base_url}/{request_id}", headers=headers)
            if not body.get("success", False):
                continue  # still processing
            rows = body.get("data") or []
            emails = (rows[0].get("email") if rows else None) or []
            if emails:
                first = emails[0]
                return ProviderResponse(
                    email=first.get("email"),
                    charged=self.cost_per_call,
                    provider_status=first.get("qualification"),
                )
            return ProviderResponse(email=None, charged=self.cost_per_call)
        raise ProviderError(f"{self.name}: result not ready after {self.max_polls} polls")
