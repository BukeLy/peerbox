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

import mailbox
import server
from server import (
    SubscribeParams,
    UnsubscribeParams,
    contacts,
    create_group,
    inbox,
    invite,
    leave,
    mark_read,
    org_member,
    send,
    set_aliases,
    webhook_signature,
    whoami,
)

pytestmark = pytest.mark.anyio
SCHEMA = (Path(__file__).parent.parent / "schema.sql").read_text()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute(SCHEMA)
    await conn.execute(
        "truncate deliveries, messages, group_members, groups, agents, users, "
        "event_subscriptions"
    )
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

    people = (await contacts(token=church, db=db))["people"]
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


@pytest.mark.parametrize(
    ("membership", "plugin_repo", "admitted"),
    [
        ((200, {"state": "active"}), (404, {}), True),  # a member
        ((404, {}), (200, {"private": True}), True),  # a guest on the plugin repo
        ((404, {}), (200, {"private": False}), False),  # anyone reads a public repo
        ((404, {}), (404, {}), False),  # a stranger
    ],
)
async def test_members_and_plugin_repo_guests_are_admitted(
    monkeypatch, membership, plugin_repo, admitted
):
    routes = {
        f"/user/memberships/orgs/{server.GITHUB_ORG}": membership,
        f"/repos/{server.GITHUB_ORG}/peerbox-plugin": plugin_repo,
    }

    def github(request):
        status, body = routes.get(request.url.path, (404, {}))
        return httpx2.Response(status, json=body)

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        server.httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(github), **kwargs),
    )
    token = AccessToken(
        token=os.urandom(8).hex(), client_id="test", scopes=[], claims={}
    )
    assert await server.is_member(token) is admitted


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


def test_reply_argument_keeps_the_ids_of_subscriptions_made_without_it():
    before = UnsubscribeParams(name="message.received", delivery={"url": HOOK})
    after = UnsubscribeParams(
        name="message.received", arguments={"reply": False}, delivery={"url": HOOK}
    )
    assert server.subscription_id("turing", before) == server.subscription_id(
        "turing", after
    )


def test_subscribe_prompt_names_the_inbox_to_watch():
    assert "notes to me," in server.subscribe_prompt()
    assert "my agent 'review-bot'" in server.subscribe_prompt(agent="review-bot")


async def test_pre_sep_clients_find_message_events_in_server_discover():
    async with Client(server.mcp) as client:
        discovered = await client.session.send_discover("2026-07-28")
    assert "events" in discovered["capabilities"]


async def join(db, group: str, *addresses: str) -> None:
    """Add other people's addresses to a group, as an approved invitation will."""
    for address in addresses:
        login, _, agent = address.partition("/")
        await db.execute(
            "insert into users (login) values ($1) on conflict do nothing", login
        )
        await db.execute(
            "insert into group_members (group_id, login, agent) values ($1, $2, $3)",
            group[1:],
            login,
            agent or None,
        )


async def texts(token, db, agent=None) -> list[str]:
    return [
        m["text"] for m in (await inbox(agent=agent, token=token, db=db))["messages"]
    ]


async def own_agents_group(token, db, creator: str, *others: str) -> str:
    """A group of one person's agents, as when they make their models discuss."""
    for agent in (creator, *others):
        await inbox(agent=agent, token=token, db=db)
    group = (await create_group("models", from_agent=creator, token=token, db=db))[
        "group"
    ]
    for agent in others:
        await invite(group, f"bukely/{agent}", from_agent=creator, token=token, db=db)
    return group


async def test_group_message_reaches_every_member_but_the_writing_address(db):
    bukely = signed_in("bukely")
    group = await own_agents_group(bukely, db, "grok", "gpt", "claude")

    await send(
        group, "which cache is faster?", from_agent="claude", token=bukely, db=db
    )
    assert await texts(bukely, db, "claude") == []
    for agent in ("grok", "gpt"):
        [message] = (await inbox(agent=agent, token=bukely, db=db))["messages"]
        assert (message["from"], message["to"]) == ("bukely/claude", group)


async def test_an_agent_marks_read_only_its_own_copy(db):
    bukely = signed_in("bukely")
    group = await own_agents_group(bukely, db, "claude", "grok", "gpt")
    sent = await send(group, "benchmark done", from_agent="claude", token=bukely, db=db)

    await mark_read([sent["id"]], agent="grok", token=bukely, db=db)
    assert await texts(bukely, db, "grok") == []
    assert await texts(bukely, db, "gpt") == ["benchmark done"]


async def test_late_joiner_sees_only_later_group_messages(db):
    turing = signed_in("turing")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, "church")
    await send(group, "before", token=turing, db=db)
    await join(db, group, "kleene")
    await send(group, "after", token=turing, db=db)
    assert await texts(signed_in("kleene"), db) == ["after"]


async def test_a_members_agent_posts_to_the_group_but_outsiders_cannot(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, "church")
    await inbox(agent="bot", token=church, db=db)

    await send(group, "build is green", from_agent="bot", token=church, db=db)
    assert await texts(turing, db) == ["build is green"]
    with pytest.raises(ToolError, match="not in"):
        await send(group, "hi", token=signed_in("kleene"), db=db)


async def test_mark_read_clears_only_the_readers_copies(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, "church", "church/bot", "kleene")
    sent = await send(group, "standup at ten", token=turing, db=db)

    await mark_read([sent["id"]], token=church, db=db)
    assert await texts(church, db) == await texts(church, db, "bot") == []
    assert await texts(signed_in("kleene"), db) == ["standup at ten"]


async def test_only_your_own_addresses_join_until_the_next_version(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    with pytest.raises(ToolError, match="next version"):
        await invite(group, "church", token=turing, db=db)
    with pytest.raises(ToolError, match="not in"):
        await invite(group, "church", token=church, db=db)


async def test_owner_address_hands_over_on_leaving_and_the_last_one_out_deletes(db):
    turing, church = signed_in("turing"), signed_in("church")
    await inbox(agent="bot", token=turing, db=db)
    await inbox(agent="bot", token=church, db=db)
    group = (await create_group("compilers", from_agent="bot", token=turing, db=db))[
        "group"
    ]
    [created] = (await contacts(token=turing, db=db))["groups"]
    assert (created["owner"], created["members"]) == ("turing/bot", ["turing/bot"])
    await join(db, group, "church/bot")
    await join(db, group, "kleene")
    with pytest.raises(ToolError, match="owner"):
        await leave(group, "kleene", from_agent="bot", token=church, db=db)

    await leave(group, from_agent="bot", token=turing, db=db)
    [joined] = (await contacts(token=church, db=db))["groups"]
    assert (joined["owner"], joined["members"]) == (
        "church/bot",
        ["church/bot", "kleene"],
    )
    await leave(group, "kleene", from_agent="bot", token=church, db=db)
    # church, writing as no agent, removes its own agent's address
    await leave(group, "church/bot", token=church, db=db)
    assert await db.fetchval("select count(*) from groups") == 0


async def test_migration_numbers_old_messages_deliveries_with_their_ids(db):
    await db.execute("insert into users (login) values ('turing'), ('church')")
    # as written by a version without deliveries
    old = await db.fetch(
        "insert into messages (sender, recipient, recipient_agent, body, read_at) "
        "values ('church', 'turing', null, 'unread', null), "
        "('church', 'turing', 'bot', 'read', now()) returning id"
    )
    await db.execute(SCHEMA)
    await db.execute(SCHEMA)

    rows = await db.fetch(
        "select id, message_id, read_at is not null from deliveries order by id"
    )
    assert [tuple(row) for row in rows] == [
        (old[0]["id"], old[0]["id"], False),
        (old[1]["id"], old[1]["id"], True),
    ]
    await send("turing", "new", token=signed_in("church"), db=db)
    assert await texts(signed_in("turing"), db) == ["unread", "new"]


async def test_events_come_from_deliveries(db, webhook):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    group = (await create_group("compilers", token=church, db=db))["group"]
    await join(db, group, "turing")
    async with Client(server.mcp):
        await subscribe_turing()
        sent = await send(group, "who is on call?", token=church, db=db)
        assert await delivered(webhook, 1) == [str(sent["id"])]
    [event] = [d for d in webhook if "eventId" in d]
    assert event["data"]["to"] == group
    assert event["cursor"] == str(
        await db.fetchval("select id from deliveries where message_id = $1", sent["id"])
    )


async def test_expiry_deletes_old_messages_and_keeps_replies_to_them(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    question = await send("turing", "old question", token=church, db=db)
    answer = await send(
        "church", "old answer", reply_to=question["id"], token=turing, db=db
    )
    await send("turing", "thanks", reply_to=answer["id"], token=church, db=db)
    await db.execute(
        "update messages set created_at = now() - interval '31 days' "
        "where id = any($1)",
        [question["id"], answer["id"]],
    )

    await mailbox.expire(db)
    [thanks] = (await inbox(token=turing, db=db))["messages"]
    assert (thanks["text"], thanks["reply_to"]) == ("thanks", None)
    assert await db.fetchval("select count(*) from deliveries") == 1
