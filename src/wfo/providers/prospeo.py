"""Prospeo Email Finder adapter (``POST /email-finder``)."""

from __future__ import annotations

from wfo.models import Contact
from wfo.providers.base import ProviderResponse
from wfo.providers.http import HttpProvider


class ProspeoProvider(HttpProvider):
    name = "prospeo"
    cost_per_call = 0.040
    charge_on_miss = False
    base_url = "https://api.prospeo.io/email-finder"

    async def find_email(self, contact: Contact) -> ProviderResponse:
        body = await self._send(
            "POST",
            self.base_url,
            headers={"X-KEY": self._api_key, "Content-Type": "application/json"},
            json={
                "first_name": contact.first_name,
                "last_name": contact.last_name,
                "company": contact.company_domain,
            },
        )
        if body.get("error"):
            return ProviderResponse(email=None, charged=0.0)
        response = body.get("response") or {}
        email_obj = response.get("email") or {}
        email: str | None
        status: str | None
        if isinstance(email_obj, str):
            email, status = email_obj, response.get("email_status")
        else:
            email, status = email_obj.get("email"), email_obj.get("email_status")
        return ProviderResponse(
            email=email or None,
            charged=self.cost_per_call if email else 0.0,
            provider_status=status,
        )
