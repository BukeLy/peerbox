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
    alice, bob = signed_in("alice"), signed_in("bob")
    assert (await whoami(token=alice, db=db))["setup_needed"]
    await set_aliases(["小黄", "Jay"], token=alice, db=db)

    people = await contacts(token=bob, db=db)
    assert {"小黄", "Jay"} <= set(
        next(p for p in people if p["login"] == "alice")["aliases"]
    )

    sent = await send("alice", "deploy done", token=bob, db=db)
    unread = await inbox(token=alice, db=db)
    assert [(m["from"], m["text"]) for m in unread["messages"]] == [
        ("bob", "deploy done")
    ]

    await mark_read([sent["id"]], token=alice, db=db)
    assert (await inbox(token=alice, db=db))["messages"] == []


async def test_unknown_login_error_lists_alias_matches(db):
    alice, bob = signed_in("alice"), signed_in("bob")
    await set_aliases(["Jay"], token=alice, db=db)
    with pytest.raises(ToolError, match="alice"):
        await send("jay", "hi", token=bob, db=db)


async def test_message_to_an_agent_waits_in_that_agents_inbox(db):
    alice, bob = signed_in("alice"), signed_in("bob")
    await inbox(agent="review-bot", token=alice, db=db)

    sent = await send("alice/review-bot", "review the PR", token=bob, db=db)
    assert sent["status"].startswith("in the agent's inbox")
    assert (await inbox(token=alice, db=db))["messages"] == []
    queued = await inbox(agent="review-bot", token=alice, db=db)
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
    alice, bob = signed_in("alice"), signed_in("bob")
    await whoami(token=alice, db=db)
    woken = asyncio.Event()
    server.waiters.add(woken)
    try:
        async with Client(server.mcp):
            await send("alice", "hi", token=bob, db=db)
            await asyncio.wait_for(woken.wait(), timeout=5)
    finally:
        server.waiters.discard(woken)


@pytest.fixture
def webhook(monkeypatch):
    """Alice's webhook endpoint; returns the event bodies it receives."""
    deliveries = []

    async def post(url, secrets, subscription, message_id, body):
        deliveries.append(json.loads(body))
        return httpx2.Response(200, content=body)  # echoes the verification challenge

    async def signed_in_as_alice():
        return "alice"

    monkeypatch.setattr(server, "post_webhook", post)
    monkeypatch.setattr(server, "events_principal", signed_in_as_alice)
    monkeypatch.setattr(server.events, "subscriptions", {})
    monkeypatch.setattr(server.events, "pumps", {})
    return deliveries


HOOK = "https://chatgpt.example/hook"


async def subscribe_alice() -> str:
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
    alice, bob = signed_in("alice"), signed_in("bob")
    await whoami(token=alice, db=db)
    async with Client(server.mcp):
        await subscribe_alice()
    async with Client(server.mcp):  # the server after a redeploy
        await send("alice", "deploy done", token=bob, db=db)
        await delivered(webhook, 1)


async def test_failed_delivery_is_retried_on_next_message(db, webhook, monkeypatch):
    deliver = server.deliver_event
    failures = [RuntimeError("database connection lost")]

    async def flaky(subscription, row):
        if failures:
            raise failures.pop()
        await deliver(subscription, row)

    monkeypatch.setattr(server, "deliver_event", flaky)
    alice, bob = signed_in("alice"), signed_in("bob")
    await whoami(token=alice, db=db)
    async with Client(server.mcp):
        await subscribe_alice()
        first = await send("alice", "one", token=bob, db=db)
        second = await send("alice", "two", token=bob, db=db)
        assert await delivered(webhook, 2) == [str(first["id"]), str(second["id"])]


async def test_unsubscribe_stops_a_delivery_still_retrying(db, webhook, monkeypatch):
    retrying = asyncio.Event()

    async def endpoint_down(subscription, row):
        retrying.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server, "deliver_event", endpoint_down)
    alice, bob = signed_in("alice"), signed_in("bob")
    await whoami(token=alice, db=db)
    async with Client(server.mcp):
        pump = server.events.pumps[await subscribe_alice()]
        await send("alice", "hi", token=bob, db=db)
        await asyncio.wait_for(retrying.wait(), timeout=5)
        await server.events.unsubscribe(
            None,
            UnsubscribeParams(name="message.received", delivery={"url": HOOK}),
        )
        assert pump.cancelled()


async def test_reply_reaches_the_agent_that_wrote(db):
    alice, bob = signed_in("alice"), signed_in("bob")
    await whoami(token=bob, db=db)
    with pytest.raises(ToolError, match="inbox"):
        await send("bob", "hi", from_agent="review-bot", token=alice, db=db)

    await inbox(agent="review-bot", token=alice, db=db)
    await send("bob", "hi", from_agent="review-bot", token=alice, db=db)
    await send("alice/review-bot", "thanks", token=bob, db=db)
    queued = await inbox(agent="review-bot", token=alice, db=db)
    assert [m["text"] for m in queued["messages"]] == ["thanks"]


async def test_chatgpt_finds_message_events_in_server_discover():
    async with Client(server.mcp) as client:
        discovered = await client.session.send_discover("2026-07-28")
    assert "events" in discovered["capabilities"]
