"""Apollo.io People Match adapter (``POST /api/v1/people/match``)."""

from __future__ import annotations

from wfo.models import Contact
from wfo.providers.base import ProviderResponse
from wfo.providers.http import HttpProvider


class ApolloProvider(HttpProvider):
    name = "apollo"
    cost_per_call = 0.020
    charge_on_miss = True
    base_url = "https://api.apollo.io/api/v1/people/match"

    async def find_email(self, contact: Contact) -> ProviderResponse:
        body = await self._send(
            "POST",
            self.base_url,
            headers={"X-Api-Key": self._api_key, "Cache-Control": "no-cache"},
            json={
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "domain": contact.company_domain,
                "reveal_personal_emails": False,
            },
        )
        person = body.get("person") or {}
        email = person.get("email") or None
        return ProviderResponse(
            email=email,
            charged=self.cost_per_call,
            provider_status=person.get("email_status"),
        )
