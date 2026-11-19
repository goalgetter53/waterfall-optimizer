"""Hunter.io Email Finder adapter (``GET /v2/email-finder``)."""

from __future__ import annotations

from wfo.models import Contact
from wfo.providers.base import ProviderResponse
from wfo.providers.http import HttpProvider


class HunterProvider(HttpProvider):
    name = "hunter"
    cost_per_call = 0.035
    charge_on_miss = False  # Hunter does not consume a credit when nothing is found
    base_url = "https://api.hunter.io/v2/email-finder"

    async def find_email(self, contact: Contact) -> ProviderResponse:
        body = await self._send(
            "GET",
            self.base_url,
            params={
                "domain": contact.company_domain,
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "api_key": self._api_key,
            },
        )
        data = body.get("data") or {}
        email = data.get("email") or None
        verification = data.get("verification") or {}
        status = verification.get("status") if isinstance(verification, dict) else None
        return ProviderResponse(
            email=email,
            charged=self.cost_per_call if email else 0.0,
            provider_status=status,
            raw={"score": data.get("score")},
        )
