import asyncio
import json
import os
import subprocess
import time
from base64 import b64encode
from pathlib import Path

import asyncpg
import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthContext
from standardwebhooks import Webhook

os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("GITHUB_CLIENT_ID", "test")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test")
os.environ.setdefault("GITHUB_ORG", "test-org")

import server
from server import (
    SubscribeParams,
    UnsubscribeParams,
    contacts,
    inbox,
    mark_read,
    org_member,
    send,
    set_aliases,
    webhook_signature,
    whoami,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute((Path(__file__).parent.parent / "schema.sql").read_text())
    await conn.execute("truncate messages, agents, users, event_subscriptions")
    yield conn
    await conn.close()


def signed_in(login: str) -> AccessToken:
    return AccessToken(
        token=login,
        client_id="test",
        scopes=["read:user"],
        claims={"login": login, "name": login.title()},
    )


async def test_teammate_found_by_alias_receives_and_clears_message(db):
    turing, church = signed_in("turing"), signed_in("church")
    assert (await whoami(token=turing, db=db))["setup_needed"]
    await set_aliases(["Prof", "Alan"], token=turing, db=db)

    people = await contacts(token=church, db=db)
    assert {"Prof", "Alan"} <= set(
        next(p for p in people if p["login"] == "turing")["aliases"]
    )

    sent = await send("turing", "deploy done", token=church, db=db)
    unread = await inbox(token=turing, db=db)
    assert [(m["from"], m["text"]) for m in unread["messages"]] == [
        ("church", "deploy done")
    ]

    await mark_read([sent["id"]], token=turing, db=db)
    assert (await inbox(token=turing, db=db))["messages"] == []


async def test_unknown_login_error_lists_alias_matches(db):
    turing, church = signed_in("turing"), signed_in("church")
    await set_aliases(["Prof"], token=turing, db=db)
    with pytest.raises(ToolError, match="turing"):
        await send("prof", "hi", token=church, db=db)


async def test_message_to_an_agent_waits_in_that_agents_inbox(db):
    turing, church = signed_in("turing"), signed_in("church")
    await inbox(agent="review-bot", token=turing, db=db)

    sent = await send("turing/review-bot", "review the PR", token=church, db=db)
    assert sent["status"].startswith("in the agent's inbox")
    assert (await inbox(token=turing, db=db))["messages"] == []
    queued = await inbox(agent="review-bot", token=turing, db=db)
    assert [m["text"] for m in queued["messages"]] == ["review the PR"]


def test_webhook_signature_verifies_with_standard_webhooks_during_rotation():
    old, new = (f"whsec_{b64encode(os.urandom(32)).decode()}" for _ in range(2))
    body = b'{"eventId":"1"}'
    timestamp = str(int(time.time()))
    headers = {
        "webhook-id": "msg_1",
        "webhook-timestamp": timestamp,
        "webhook-signature": webhook_signature([old, new], "msg_1", timestamp, body),
    }
    for secret in (old, new):
        Webhook(secret).verify(body, headers)


@pytest.fixture
def github_token():
    token = subprocess.run(
        ["gh", "auth", "token"], check=False, capture_output=True, text=True
    ).stdout.strip()
    if not token or "PEERBOX_TEST_ORG" not in os.environ:
        pytest.skip("needs a gh login and PEERBOX_TEST_ORG, an org it belongs to")
    return token


async def test_active_org_member_is_admitted(github_token, monkeypatch):
    monkeypatch.setattr(server, "GITHUB_ORG", os.environ["PEERBOX_TEST_ORG"])
    token = AccessToken(token=github_token, client_id="test", scopes=[], claims={})
    assert await org_member(AuthContext(token=token, component=None))


async def test_sent_message_wakes_event_subscriptions(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    woken = asyncio.Event()
    server.waiters.add(woken)
    try:
        async with Client(server.mcp):
            await send("turing", "hi", token=church, db=db)
            await asyncio.wait_for(woken.wait(), timeout=5)
    finally:
        server.waiters.discard(woken)


@pytest.fixture
def webhook(monkeypatch):
    """Turing's webhook endpoint; returns the event bodies it receives."""
    deliveries = []

    async def post(url, secrets, subscription, message_id, body):
        deliveries.append(json.loads(body))
        return httpx2.Response(200, content=body)  # echoes the verification challenge

    async def signed_in_as_turing():
        return "turing"

    monkeypatch.setattr(server, "post_webhook", post)
    monkeypatch.setattr(server, "events_principal", signed_in_as_turing)
    monkeypatch.setattr(server.events, "subscriptions", {})
    monkeypatch.setattr(server.events, "pumps", {})
    return deliveries


HOOK = "https://client.example/hook"


async def subscribe_turing() -> str:
    secret = f"whsec_{b64encode(os.urandom(32)).decode()}"
    params = SubscribeParams(
        name="message.received",
        delivery={"mode": "webhook", "url": HOOK, "secret": secret},
    )
    return (await server.events.subscribe(None, params)).id


async def delivered(deliveries, count: int) -> list[str]:
    async with asyncio.timeout(5):
        while len(events := [d for d in deliveries if "eventId" in d]) < count:
            await asyncio.sleep(0.05)
    return [event["eventId"] for event in events]


async def test_subscription_keeps_delivering_after_restart(db, webhook):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    async with Client(server.mcp):
        await subscribe_turing()
    async with Client(server.mcp):  # the server after a redeploy
        await send("turing", "deploy done", token=church, db=db)
        await delivered(webhook, 1)


async def test_failed_delivery_is_retried_on_next_message(db, webhook, monkeypatch):
    deliver = server.deliver_event
    failures = [RuntimeError("database connection lost")]

    async def flaky(subscription, row):
        if failures:
            raise failures.pop()
        await deliver(subscription, row)

    monkeypatch.setattr(server, "deliver_event", flaky)
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    async with Client(server.mcp):
        await subscribe_turing()
        first = await send("turing", "one", token=church, db=db)
        second = await send("turing", "two", token=church, db=db)
        assert await delivered(webhook, 2) == [str(first["id"]), str(second["id"])]


async def test_unsubscribe_stops_a_delivery_still_retrying(db, webhook, monkeypatch):
    retrying = asyncio.Event()

    async def endpoint_down(subscription, row):
        retrying.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "deliver_event", endpoint_down)
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    async with Client(server.mcp):
        pump = server.events.pumps[await subscribe_turing()]
        await send("turing", "hi", token=church, db=db)
        await asyncio.wait_for(retrying.wait(), timeout=5)
        await server.events.unsubscribe(
            None,
            UnsubscribeParams(name="message.received", delivery={"url": HOOK}),
        )
        assert pump.cancelled()


async def test_reply_reaches_the_agent_that_wrote(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=church, db=db)
    with pytest.raises(ToolError, match="inbox"):
        await send("church", "hi", from_agent="review-bot", token=turing, db=db)

    await inbox(agent="review-bot", token=turing, db=db)
    await send("church", "hi", from_agent="review-bot", token=turing, db=db)
    await send("turing/review-bot", "thanks", token=church, db=db)
    queued = await inbox(agent="review-bot", token=turing, db=db)
    assert [m["text"] for m in queued["messages"]] == ["thanks"]


async def test_pre_sep_clients_find_message_events_in_server_discover():
    async with Client(server.mcp) as client:
        discovered = await client.session.send_discover("2026-07-28")
    assert "events" in discovered["capabilities"]
