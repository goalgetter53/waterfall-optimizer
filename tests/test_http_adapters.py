from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import make_contact
from wfo.providers.apollo import ApolloProvider
from wfo.providers.base import ProviderAuthError, ProviderError, RateLimitedError
from wfo.providers.catalog import build_live_providers
from wfo.providers.dropcontact import DropcontactProvider
from wfo.providers.hunter import HunterProvider
from wfo.providers.prospeo import ProspeoProvider

CONTACT = make_contact(1)


def client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_hunter_found_and_pay_per_hit() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "data": {
                    "email": "ada@acme1.example",
                    "score": 91,
                    "verification": {"status": "valid"},
                }
            },
        )

    p = HunterProvider("hk-test", client(httpx.MockTransport(handler)))
    r = await p.find_email(CONTACT)
    assert (r.email, r.charged, r.provider_status) == ("ada@acme1.example", 0.035, "valid")
    assert seen[0].url.params["domain"] == "acme1.example"


async def test_hunter_miss_costs_nothing() -> None:
    p = HunterProvider(
        "k",
        client(httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"email": None}}))),
    )
    r = await p.find_email(CONTACT)
    assert r.email is None
    assert r.charged == 0.0


@pytest.mark.parametrize(
    ("status", "headers", "exc"),
    [
        (429, {"Retry-After": "7"}, RateLimitedError),
        (401, {}, ProviderAuthError),
        (503, {}, ProviderError),
    ],
)
async def test_http_status_mapping(
    status: int, headers: dict[str, str], exc: type[Exception]
) -> None:
    p = ApolloProvider(
        "k", client(httpx.MockTransport(lambda r: httpx.Response(status, headers=headers, json={})))
    )
    with pytest.raises(exc) as info:
        await p.find_email(CONTACT)
    if status == 429:
        assert isinstance(info.value, RateLimitedError)
        assert info.value.retry_after == 7.0
    if status == 401:
        assert isinstance(info.value, ProviderError)
        assert not info.value.retryable


async def test_apollo_sends_key_in_header_not_body() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-Api-Key"] == "ak"
        assert "ak" not in req.content.decode()
        return httpx.Response(200, json={"person": {"email": "a@b.c", "email_status": "verified"}})

    r = await ApolloProvider("ak", client(httpx.MockTransport(handler))).find_email(CONTACT)
    assert r.email == "a@b.c"
    assert r.charged == 0.02


async def test_prospeo_parses_nested_and_error() -> None:
    ok = ProspeoProvider(
        "pk",
        client(
            httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "error": False,
                        "response": {"email": {"email": "a@b.c", "email_status": "VALID"}},
                    },
                )
            )
        ),
    )
    r = await ok.find_email(CONTACT)
    assert (r.email, r.provider_status) == ("a@b.c", "VALID")
    miss = ProspeoProvider(
        "pk",
        client(
            httpx.MockTransport(
                lambda r: httpx.Response(200, json={"error": True, "message": "NO_RESULT"})
            )
        ),
    )
    assert (await miss.find_email(CONTACT)).email is None


async def test_dropcontact_submit_then_poll() -> None:
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            body = json.loads(req.content)
            assert body["data"][0]["website"] == "acme1.example"
            return httpx.Response(200, json={"request_id": "r1", "success": True})
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(200, json={"success": False, "reason": "processing"})
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"email": [{"email": "ada@acme1.example", "qualification": "nominative@pro"}]}
                ],
            },
        )

    async def no_sleep(_: float) -> None:
        return None

    p = DropcontactProvider("dk", client(httpx.MockTransport(handler)), sleep=no_sleep)
    r = await p.find_email(CONTACT)
    assert r.email == "ada@acme1.example"
    assert polls["n"] == 3


async def test_transport_errors_are_retryable() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ProviderError) as info:
        await HunterProvider("k", client(httpx.MockTransport(boom))).find_email(CONTACT)
    assert info.value.retryable


async def test_catalog_only_builds_configured_providers_and_hides_keys() -> None:
    async with httpx.AsyncClient() as c:
        providers = build_live_providers(
            c, {"HUNTER_API_KEY": "secret-123", "WFO_HUNTER_COST": "0.01"}
        )
        assert list(providers) == ["hunter"]
        assert providers["hunter"].cost_per_call == 0.01
        assert "secret-123" not in repr(providers["hunter"])
    with pytest.raises(ValueError, match="missing API key"):
        HunterProvider("", httpx.AsyncClient())
