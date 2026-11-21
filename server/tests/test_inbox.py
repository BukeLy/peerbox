import asyncio
import json
import os
import subprocess
import threading
import time
from base64 import b64encode
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import ANY
from urllib.parse import parse_qs, quote, urlsplit

import asyncpg
import httpx2
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthContext
from fastmcp.utilities.types import Image
from standardwebhooks import Webhook
from starlette.testclient import TestClient

os.environ.setdefault("BASE_URL", "http://localhost:8000")
os.environ.setdefault("GITHUB_CLIENT_ID", "test")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test")
os.environ.setdefault("GITHUB_ORG", "test-org")
# The MinIO the tests run against (see .github/workflows/test.yml).
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
os.environ.setdefault("S3_BUCKET", "peerbox")
os.environ.setdefault("S3_ACCESS_KEY_ID", "minioadmin")
os.environ.setdefault("S3_SECRET_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_REGION", "us-east-1")

import bucket
import instance
import mailbox
import panel
import server
from server import (
    ChatGPTFile,
    SubscribeParams,
    UnsubscribeParams,
    add_contact,
    admitted,
    answer,
    attach,
    contacts,
    create_group,
    delete_account,
    inbox,
    invite,
    leave,
    mark_read,
    open_attachment,
    remove_contact,
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


@pytest.fixture(scope="session")
def bucket_exists():
    """Wait for the test bucket, for the tests that store files in it: MinIO
    reports healthy a few seconds before it makes the bucket, and until then every
    write to it fails."""
    deadline = time.monotonic() + 60
    while True:
        try:
            asyncio.run(bucket.put("ready", b""))
            break
        except bucket.BucketError as error:
            if time.monotonic() > deadline:
                pytest.fail(
                    f"The bucket {bucket.BUCKET!r} at {bucket.ENDPOINT} did not appear "
                    f"within 60 s ({error}). Start MinIO as .github/CONTRIBUTING.md "
                    "shows.",
                    pytrace=False,
                )
            time.sleep(0.5)
    asyncio.run(bucket.delete("ready"))


@pytest.fixture
async def db():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    await conn.execute(SCHEMA)
    await conn.execute(
        "truncate deliveries, attachments, requests, messages, group_members, groups, "
        "contacts, blocks, memberships, agents, users, event_subscriptions"
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
    monkeypatch.setattr(mailbox, "GITHUB_ORG", os.environ["PEERBOX_TEST_ORG"])
    # The login only matters without GITHUB_ORG, which records its organisations.
    token = AccessToken(
        token=github_token, client_id="test", scopes=[], claims={"login": "member"}
    )
    assert await admitted(AuthContext(token=token, component=None))


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
        f"/user/memberships/orgs/{mailbox.GITHUB_ORG}": membership,
        f"/repos/{mailbox.GITHUB_ORG}/peerbox-plugin": plugin_repo,
    }

    def github(request):
        status, body = routes.get(request.url.path, (404, {}))
        return httpx2.Response(status, json=body)

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(github), **kwargs),
    )
    assert await instance.admit(os.urandom(8).hex(), "turing") is admitted


async def woken(change, expected: set[str], *bystanders: str) -> set[str]:
    """Which of the expected people and the bystanders the change wakes, once the
    expected ones woke. Client(server.mcp) must be open: its listener hears them."""
    with ExitStack() as stack:
        events = {
            login: stack.enter_context(instance.waiters.watch(login))
            for login in (*expected, *bystanders)
        }
        await change
        for login in expected:
            await asyncio.wait_for(events[login].wait(), timeout=5)
        await asyncio.sleep(0.1)
        return {login for login, event in events.items() if event.is_set()}


async def test_a_change_wakes_only_the_people_it_involves(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    await whoami(token=signed_in("kleene"), db=db)
    watch = instance.waiters.watch
    async with Client(server.mcp):  # its listener hears the notifications
        with watch("turing") as to, watch("church") as by, watch("kleene") as other:
            sent = await send("turing", "hi", token=church, db=db)
            for woken in (to, by):
                await asyncio.wait_for(woken.wait(), timeout=5)
                woken.clear()
            await mark_read([sent["id"]], token=turing, db=db)  # the sender sees it
            for woken in (to, by):
                await asyncio.wait_for(woken.wait(), timeout=5)
            assert not other.is_set()


async def test_groups_contacts_and_deleted_accounts_wake_whom_they_change(
    db, monkeypatch
):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=signed_in("kleene"), db=db)
    await inbox(agent="bot", token=turing, db=db)
    group = (await create_group("compilers", token=church, db=db))["group"]
    await join(db, group, church, "turing")
    revoked = httpx2.MockTransport(lambda request: httpx2.Response(204))
    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kwargs: client(transport=revoked, **kwargs)
    )
    async with Client(server.mcp):
        both = {"turing", "church"}
        joined = invite(group, "turing/bot", token=turing, db=db)
        assert await woken(joined, both, "kleene") == both
        left = leave(group, "turing/bot", token=turing, db=db)
        assert await woken(left, both, "kleene") == both
        removed = remove_contact("church", token=turing, db=db)
        assert await woken(removed, both, "kleene") == both
        # Adding church back only lifts the block: here everyone is a contact.
        added = add_contact("church", token=turing, db=db)
        assert await woken(added, both, "kleene") == both
        # Here everyone is a contact, so kleene's panel listed church too.
        everyone = {"turing", "church", "kleene"}
        assert await woken(delete_account(token=church, db=db), everyone) == everyone


async def test_marking_a_copy_read_wakes_everyone_who_sees_its_state(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=signed_in("post"), db=db)
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church", "kleene")
    sent = await send(group, "standup at ten", token=turing, db=db)
    async with Client(server.mcp):
        read = mark_read([sent["id"]], token=church, db=db)
        everyone = {"turing", "church", "kleene"}
        assert await woken(read, everyone, "post") == everyone


async def test_an_agents_first_inbox_read_wakes_its_owner(db):
    turing = signed_in("turing")
    await whoami(token=signed_in("church"), db=db)
    async with Client(server.mcp):
        registered = inbox(agent="bot", token=turing, db=db)
        assert await woken(registered, {"turing"}, "church") == {"turing"}
        checked_again = inbox(agent="bot", token=turing, db=db)
        assert await woken(checked_again, set(), "turing") == set()


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


async def subscribe_turing(agent: str | None = None) -> str:
    secret = f"whsec_{b64encode(os.urandom(32)).decode()}"
    params = SubscribeParams(
        name="message.received",
        arguments={"agent": agent},
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


async def test_an_ended_subscription_is_deleted_by_the_next_expiry(db, webhook):
    await inbox(agent="bot", token=signed_in("turing"), db=db)
    async with Client(server.mcp):
        running = await subscribe_turing(agent="bot")
        ended = await subscribe_turing()
        await server.events.unsubscribe(
            None, UnsubscribeParams(name="message.received", delivery={"url": HOOK})
        )
        # the hourly job runs after it ended, by the database's clock
        await db.execute(
            "update event_subscriptions set expires_at = now() - interval '1 minute' "
            "where id = $1",
            ended,
        )
        await mailbox.expire(db)
    assert [
        row["id"] for row in await db.fetch("select id from event_subscriptions")
    ] == [running]


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
    assert "messages to me," in server.subscribe_prompt()
    assert "my agent 'review-bot'" in server.subscribe_prompt(agent="review-bot")


async def test_pre_sep_clients_find_message_events_in_server_discover():
    async with Client(server.mcp) as client:
        discovered = await client.session.send_discover("2026-07-28")
    assert "events" in discovered["capabilities"]


async def join(db, group: str, inviter, *addresses: str, from_agent=None) -> None:
    """Invite other people's addresses to a group, and accept for each of them."""
    for address in addresses:
        member = mailbox.Address.parse(address)
        token = signed_in(member.login)
        await whoami(token=token, db=db)
        if member.agent:
            await inbox(agent=member.agent, token=token, db=db)
        await invite(group, address, from_agent=from_agent, token=inviter, db=db)
        [invitation] = [
            r
            for r in await requests_of(token, db, member.agent)
            if (r["kind"], r["group"]) == ("invitation", group)
        ]
        await answer(
            invitation["id"], True, from_agent=member.agent, token=token, db=db
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
    await join(db, group, turing, "church")
    await send(group, "before", token=turing, db=db)
    await join(db, group, turing, "kleene")
    await send(group, "after", token=turing, db=db)
    assert await texts(signed_in("kleene"), db) == ["after"]


async def test_a_members_agent_posts_to_the_group_but_outsiders_cannot(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church")
    await inbox(agent="bot", token=church, db=db)

    await send(group, "build is green", from_agent="bot", token=church, db=db)
    assert await texts(turing, db) == ["build is green"]
    with pytest.raises(ToolError, match="not in"):
        await send(group, "hi", token=signed_in("kleene"), db=db)


async def test_mark_read_clears_only_the_copy_of_the_address_that_read(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church", "church/bot", "kleene")
    sent = await send(group, "standup at ten", token=turing, db=db)

    await mark_read([sent["id"]], token=church, db=db)
    assert await texts(church, db) == []
    assert await texts(church, db, "bot") == ["standup at ten"]
    await mark_read([sent["id"]], agent="bot", token=church, db=db)
    assert await texts(church, db, "bot") == []
    assert await texts(signed_in("kleene"), db) == ["standup at ten"]


async def test_only_members_invite(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    with pytest.raises(ToolError, match="not in"):
        await invite(group, "church", token=church, db=db)


async def test_owner_address_hands_over_on_leaving(db):
    turing, church = signed_in("turing"), signed_in("church")
    await inbox(agent="bot", token=turing, db=db)
    await inbox(agent="bot", token=church, db=db)
    group = (await create_group("compilers", from_agent="bot", token=turing, db=db))[
        "group"
    ]
    [created] = (await contacts(token=turing, db=db))["groups"]
    assert (created["owner"], created["members"]) == ("turing/bot", ["turing/bot"])
    await join(db, group, turing, "church/bot", "kleene", from_agent="bot")
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
    assert await contacts(token=church, db=db) == {"people": ANY, "groups": []}
    # nobody is left and nothing was said, so the next expiry deletes the group
    await mailbox.expire(db)
    assert await db.fetchval("select count(*) from groups") == 0


async def test_joining_a_group_everyone_left_makes_you_its_owner(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=church, db=db)
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await invite(group, "church", token=turing, db=db)
    await leave(group, token=turing, db=db)
    [invitation] = await requests_of(church, db)
    await answer(invitation["id"], True, token=church, db=db)
    [joined] = (await contacts(token=church, db=db))["groups"]
    assert (joined["owner"], joined["members"]) == ("church", ["church"])


async def test_an_emptied_group_keeps_its_messages_until_they_expire(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church")
    await send(group, "plan", token=turing, db=db)
    await leave(group, token=church, db=db)
    await leave(group, token=turing, db=db)

    await mailbox.expire(db)
    with browser("church") as page:
        [talk] = page.get("/api/conversations").json()["conversations"]
        assert (talk["key"], talk["name"]) == (group, "compilers")
        [kept] = page.get(f"/api/conversations/{quote(group)}").json()["messages"]
        assert kept["text"] == "plan"
    await db.execute("update messages set created_at = now() - interval '31 days'")
    await mailbox.expire(db)
    assert await db.fetchval("select count(*) from groups") == 0


async def test_events_come_from_deliveries(db, webhook):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    group = (await create_group("compilers", token=church, db=db))["group"]
    await join(db, group, church, "turing")
    async with Client(server.mcp):
        await subscribe_turing()
        sent = await send(group, "who is on call?", token=church, db=db)
        assert await delivered(webhook, 1) == [str(sent["id"])]
    [event] = [d for d in webhook if "eventId" in d]
    assert event["data"]["to"] == group
    assert event["cursor"] == str(
        await db.fetchval("select id from deliveries where message_id = $1", sent["id"])
    )


async def test_a_reply_to_a_deleted_message_is_refused(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    question = await send("turing", "old question", token=church, db=db)
    await db.execute("update messages set created_at = now() - interval '31 days'")
    await mailbox.expire(db)
    with pytest.raises(ToolError, match="No message"):
        await send("church", "late", reply_to=question["id"], token=turing, db=db)


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


@pytest.fixture
def public(monkeypatch):
    """The public instance, where only contacts and fellow group members write."""
    monkeypatch.setattr(mailbox, "GITHUB_ORG", None)


async def requests_of(token, db, agent=None) -> list[dict]:
    return (await inbox(agent=agent, token=token, db=db))["requests"]


async def test_an_invitation_is_answered_only_as_the_address_it_invites(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=church, db=db)
    await inbox(agent="bot", token=turing, db=db)
    group = (await create_group("compilers", token=church, db=db))["group"]
    await invite(group, "turing/bot", token=church, db=db)
    assert await requests_of(turing, db) == []
    [invitation] = await requests_of(turing, db, "bot")
    with pytest.raises(ToolError, match="answered as the address it invites"):
        await answer(invitation["id"], True, token=turing, db=db)
    joined = await answer(invitation["id"], True, from_agent="bot", token=turing, db=db)
    assert joined["status"] == f"accepted: now in {group}"
    [compilers] = (await contacts(token=church, db=db))["groups"]
    assert compilers["members"] == ["church", "turing/bot"]


async def people(token, db) -> list[str]:
    return [p["login"] for p in (await contacts(token=token, db=db))["people"]]


async def test_a_stranger_writes_once_their_request_is_accepted(db, public):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    with pytest.raises(ToolError, match="not your contact"):
        await send("turing", "hi", token=church, db=db)
    assert await people(church, db) == ["church"]

    asked = await add_contact("turing", "church, from lambda", token=church, db=db)
    assert asked["status"].startswith("asked")
    [request] = await requests_of(turing, db)
    assert (request["kind"], request["from"], request["greeting"]) == (
        "contact_request",
        "church",
        "church, from lambda",
    )
    await answer(request["id"], True, token=turing, db=db)
    await send("turing", "hi", token=church, db=db)
    await send("church", "hello", token=turing, db=db)
    assert await people(church, db) == ["church", "turing"]
    assert await requests_of(turing, db) == []


async def test_a_declined_request_leaves_them_strangers(db, public):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    await add_contact("turing", token=church, db=db)
    [request] = await requests_of(turing, db)
    await answer(request["id"], False, token=turing, db=db)
    assert await requests_of(turing, db) == []
    with pytest.raises(ToolError, match="No request"):
        await answer(request["id"], True, token=turing, db=db)
    with pytest.raises(ToolError, match="not your contact"):
        await send("turing", "hi", token=church, db=db)


async def test_a_contact_request_waits_in_every_inbox_until_one_answers(
    db, webhook, public
):
    turing, church = signed_in("turing"), signed_in("church")
    await inbox(agent="bot", token=turing, db=db)
    async with Client(server.mcp):
        await subscribe_turing(agent="bot")
        await add_contact("turing", token=church, db=db)
        await delivered(webhook, 1)
    [event] = [d for d in webhook if "eventId" in d]
    assert event["data"]["kind"] == "contact_request"
    [request] = await requests_of(turing, db)
    assert await requests_of(turing, db, "bot") == [request]

    await answer(request["id"], True, from_agent="bot", token=turing, db=db)
    assert await requests_of(turing, db) == await requests_of(turing, db, "bot") == []
    with pytest.raises(ToolError, match="already answered"):
        await answer(request["id"], False, token=turing, db=db)
    await send("turing", "hi", token=church, db=db)


async def test_unanswered_requests_expire(db, public):
    turing = signed_in("turing")
    await whoami(token=turing, db=db)
    await add_contact("turing", token=signed_in("church"), db=db)
    await db.execute("update requests set created_at = now() - interval '31 days'")
    await mailbox.expire(db)
    assert await requests_of(turing, db) == []


async def test_asking_someone_who_asked_you_makes_contacts_at_once(db, public):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    await add_contact("turing", token=church, db=db)
    added = await add_contact("church", token=turing, db=db)
    assert added["status"].startswith("now your contact")
    await send("church", "hi", token=turing, db=db)


async def test_removing_a_contact_blocks_them_despite_a_shared_organisation(db, public):
    turing, church = signed_in("turing"), signed_in("church")
    await mailbox.record_memberships(db, "turing", ["acme"])
    await mailbox.record_memberships(db, "church", ["acme"])
    await send("turing", "hi", token=church, db=db)
    added = await add_contact("turing", token=church, db=db)
    assert added["status"] == "already your contact"

    await remove_contact("church", token=turing, db=db)
    for writer, to in ((church, "turing"), (turing, "church")):
        with pytest.raises(ToolError, match="not your contact"):
            await send(to, "hi", token=writer, db=db)
    with pytest.raises(ToolError, match="does not take"):
        await add_contact("turing", token=church, db=db)
    # adding them again lifts the block, so the organisation counts again
    await add_contact("church", token=turing, db=db)
    await send("turing", "back", token=church, db=db)


async def test_an_invited_contact_joins_on_accepting(db, public):
    turing, church = signed_in("turing"), signed_in("church")
    await mailbox.record_memberships(db, "turing", ["acme"])
    await mailbox.record_memberships(db, "church", ["acme"])
    await whoami(token=signed_in("kleene"), db=db)
    await inbox(agent="bot", token=church, db=db)
    group = (await create_group("compilers", token=turing, db=db))["group"]
    with pytest.raises(ToolError, match="not your contact"):
        await invite(group, "kleene", token=turing, db=db)

    invited = await invite(group, "church/bot", token=turing, db=db)
    assert invited["status"].startswith("invited")
    assert await requests_of(church, db) == []
    [invitation] = await requests_of(church, db, "bot")
    assert (invitation["kind"], invitation["group"]) == ("invitation", group)
    await answer(invitation["id"], True, from_agent="bot", token=church, db=db)
    [joined] = (await contacts(token=church, db=db))["groups"]
    assert joined["members"] == ["turing", "church/bot"]


async def test_an_invitation_cannot_take_a_group_past_50_people(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=church, db=db)
    group = (await create_group("everyone", token=turing, db=db))["group"]
    await join(db, group, turing, *(f"person{i}" for i in range(49)))
    await invite(group, "church", token=turing, db=db)
    [invitation] = await requests_of(church, db)
    with pytest.raises(ToolError, match="50 people"):
        await answer(invitation["id"], True, token=church, db=db)


async def test_deleting_an_account_deletes_its_data_and_hands_over_its_groups(
    db, monkeypatch
):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=church, db=db)
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church")
    await send(group, "bye", token=turing, db=db)
    await send("church", "to church", token=turing, db=db)
    await send("turing", "to turing", token=church, db=db)
    await attach(name="notes.txt", token=turing, db=db)
    key = await db.fetchval("select key from attachments")
    calls = []

    def outside(request):
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "api.github.com":
            assert json.loads(request.content) == {"access_token": "turing"}
        return httpx2.Response(204)

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(outside), **kwargs),
    )
    assert await delete_account(token=turing, db=db) == {
        "deleted": "turing",
        "signed_out": True,
    }
    # The grant goes first, so a failing bucket can't leave it in place.
    assert calls == [
        ("DELETE", "api.github.com", "/applications/test/grant"),
        ("DELETE", "localhost", f"/peerbox/{key}"),
    ]
    [kept] = (await contacts(token=church, db=db))["groups"]
    assert (kept["owner"], kept["members"]) == ("church", ["church"])
    assert await db.fetchval("select count(*) from messages") == 0
    assert await people(church, db) == ["church"]


async def test_deleting_an_account_wakes_who_saw_its_copies_in_groups_it_left(
    db, monkeypatch, public
):
    # church shares an organisation with turing only: kleene and post are no
    # contacts of church's.
    await mailbox.record_memberships(db, "turing", ["lambda", "recursion"])
    await mailbox.record_memberships(db, "church", ["lambda"])
    for login in ("kleene", "post"):
        await mailbox.record_memberships(db, login, ["recursion"])
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("logic", token=turing, db=db))["group"]
    await join(db, group, turing, "church", "kleene", "post")
    await send(group, "proof attached", token=signed_in("kleene"), db=db)
    await leave(group, token=church, db=db)
    revoked = httpx2.MockTransport(lambda request: httpx2.Response(204))
    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kwargs: client(transport=revoked, **kwargs)
    )
    async with Client(server.mcp):
        # kleene sent a copy church held; post and turing see that copy's state.
        everyone = {"turing", "kleene", "post"}
        assert await woken(delete_account(token=church, db=db), everyone) == everyone


async def test_anyone_is_admitted_publicly_with_their_installed_organisations(
    db, monkeypatch, public
):
    def org(login):
        return {"account": {"login": login, "type": "Organization"}}

    second = "https://api.github.com/user/installations?per_page=100&page=2"
    pages = {
        None: ([org("acme"), {"account": {"login": "turing", "type": "User"}}], second),
        "2": ([org("widgets"), org("gadgets"), org("sso")], None),
    }
    states = {"acme": "active", "widgets": "active", "gadgets": "pending"}

    def github(request):
        if request.url.path == "/user/installations":
            installations, after = pages[request.url.params.get("page")]
            links = {"Link": f'<{after}>; rel="next"'} if after else {}
            return httpx2.Response(
                200, json={"installations": installations}, headers=links
            )
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "sso":  # enforces SAML, and the token has no session for it
            return httpx2.Response(403, json={})
        return httpx2.Response(200, json={"state": states[name]})

    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx2.MockTransport(github), **kwargs),
    )
    async with Client(server.mcp):  # opens the database pool
        assert await instance.admit(os.urandom(8).hex(), "turing")
    orgs = await db.fetch(
        "select org from memberships where login = 'turing' order by org"
    )
    assert [row["org"] for row in orgs] == ["acme", "widgets"]


async def test_a_github_outage_keeps_the_recorded_organisations(
    db, monkeypatch, public
):
    await mailbox.record_memberships(db, "turing", ["acme"])
    down = httpx2.MockTransport(lambda request: httpx2.Response(503))
    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2, "AsyncClient", lambda **kwargs: client(transport=down, **kwargs)
    )
    with pytest.raises(httpx2.HTTPStatusError):
        await instance.admit(os.urandom(8).hex(), "turing")
    assert await db.fetchval("select org from memberships") == "acme"


@pytest.fixture
def web(monkeypatch):
    """A web server for fetched files, its local address let through as public."""
    served: dict[str, tuple[str, bytes]] = {}

    class Files(BaseHTTPRequestHandler):
        def do_GET(self):
            kind, body = served[self.path]
            if kind == "redirect":
                self.send_response(302)
                self.send_header("Location", body.decode())
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Files)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async def unchecked(url):
        return url, {}, {}

    monkeypatch.setattr(server, "pinned", unchecked)
    yield served, f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(64)


@pytest.mark.usefixtures("bucket_exists")
async def test_a_file_from_chatgpt_reaches_the_recipient_as_an_image(db, web):
    served, site = web
    served["/files/f1"] = ("application/octet-stream", PNG)
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    shared = ChatGPTFile(
        download_url=f"{site}/files/f1",
        file_id="f1",
        mime_type="image/png",
        file_name="chart.png",
    )
    attached = await attach(file=shared, token=church, db=db)
    assert (attached["name"], attached["mime"], attached["size"]) == (
        "chart.png",
        "image/png",
        len(PNG),
    )
    await send("turing", "the chart", attachments=[attached["id"]], token=church, db=db)

    [message] = (await inbox(token=turing, db=db))["messages"]
    [file] = message["attachments"]
    assert (file["name"], file["size"]) == ("chart.png", len(PNG))
    async with httpx2.AsyncClient() as download:
        assert (await download.get(file["url"])).content == PNG
    image = await open_attachment(file["id"], token=turing, db=db)
    assert isinstance(image, Image) and image.data == PNG
    with pytest.raises(ToolError, match="No attachment"):
        await open_attachment(file["id"], token=signed_in("kleene"), db=db)


@pytest.mark.usefixtures("bucket_exists")
async def test_a_file_at_a_url_is_fetched_through_redirects_and_read_as_text(db, web):
    served, site = web
    served["/notes.txt"] = ("text/plain; charset=utf-8", b"step 1\n")
    served["/latest"] = ("redirect", b"/notes.txt")
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    attached = await attach(url=f"{site}/latest", token=church, db=db)
    assert (attached["name"], attached["mime"]) == ("latest", "text/plain")
    await send("turing", "notes", attachments=[attached["id"]], token=church, db=db)
    opened = await open_attachment(attached["id"], token=turing, db=db)
    assert opened.resource.text == "step 1\n"


async def test_files_over_10_mb_are_refused(db, web):
    served, site = web
    served["/huge"] = ("application/octet-stream", b"x" * (10 * 1024 * 1024 + 1))
    church = signed_in("church")
    with pytest.raises(ToolError, match="over 10 MB"):
        await attach(url=f"{site}/huge", token=church, db=db)


@pytest.mark.usefixtures("bucket_exists")
async def test_an_upload_is_checked_when_sent(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    upload = await attach(name="report.bin", token=church, db=db)
    with pytest.raises(ToolError, match="not uploaded"):
        await send("turing", "report", attachments=[upload["id"]], token=church, db=db)
    async with httpx2.AsyncClient() as client:
        too_big = b"x" * (10 * 1024 * 1024 + 1)
        (await client.put(upload["upload_url"], content=too_big)).raise_for_status()
        with pytest.raises(ToolError, match="over 10 MB"):
            await send(
                "turing", "report", attachments=[upload["id"]], token=church, db=db
            )
        (await client.put(upload["upload_url"], content=b"ok")).raise_for_status()
    await send("turing", "report", attachments=[upload["id"]], token=church, db=db)
    [message] = (await inbox(token=turing, db=db))["messages"]
    assert [(f["name"], f["size"]) for f in message["attachments"]] == [
        ("report.bin", 2)
    ]
    with pytest.raises(ToolError, match="sent once"):
        await send("turing", "again", attachments=[upload["id"]], token=church, db=db)


async def test_an_attachment_another_send_claimed_meanwhile_is_refused(db, monkeypatch):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    upload = await attach(name="plan.txt", token=church, db=db)

    async def checked_before_either_sent(db, owner, ids):
        return [(upload["id"], 6)]

    monkeypatch.setattr(mailbox, "_sendable", checked_before_either_sent)
    await send("turing", "one", attachments=[upload["id"]], token=church, db=db)
    with pytest.raises(ToolError, match="sent once"):
        await send("turing", "two", attachments=[upload["id"]], token=church, db=db)


async def test_only_public_https_urls_are_fetched(db):
    with pytest.raises(ToolError, match="non-public"):
        await attach(url="https://127.0.0.1/secret", token=signed_in("church"), db=db)


async def test_attachments_are_off_without_a_bucket(db, monkeypatch):
    fetched = []

    async def fetch(url):
        fetched.append(url)

    monkeypatch.setattr(bucket, "ENABLED", False)
    monkeypatch.setattr(server, "fetch_public", fetch)
    church = signed_in("church")
    with pytest.raises(ToolError, match="off"):
        await attach(token=church, db=db)
    with pytest.raises(ToolError, match="off"):
        await attach(url="https://example.com/plan.pdf", token=church, db=db)
    assert fetched == []


async def test_requests_wake_a_subscribed_webhook(db, webhook, public):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    group = (await create_group("compilers", token=church, db=db))["group"]
    async with Client(server.mcp):
        await subscribe_turing()
        await add_contact("turing", token=church, db=db)
        await delivered(webhook, 1)
        [request] = await requests_of(turing, db)
        await answer(request["id"], True, token=turing, db=db)
        await invite(group, "turing", token=church, db=db)
        await delivered(webhook, 2)
    events = [d for d in webhook if "eventId" in d]
    assert [
        (e["data"]["kind"], e["data"]["from"], e["data"]["to"]) for e in events
    ] == [
        ("contact_request", "church", "turing"),
        ("invitation", "church", group),
    ]
    assert events[0]["eventId"] == events[0]["cursor"] == str(request["id"])


def session(login: str) -> str:
    return panel.SESSION_KEY.encrypt(login.encode()).decode()


async def asgi(scope, receive, send):
    # TestClient takes granian's proxy-header wrapper for an ASGI 2 app.
    await server.app(scope, receive, send)


def browser(login: str | None = None) -> TestClient:
    """The panel in a browser, signed in as login."""
    client = TestClient(asgi, base_url="https://localhost")
    if login:
        client.cookies.set("peerbox_session", session(login))
    return client


async def test_signing_in_to_the_panel_sets_a_session(db, monkeypatch):
    replies = {
        "/login/oauth/access_token": {"access_token": os.urandom(8).hex()},
        "/user": {"login": "turing"},
        f"/user/memberships/orgs/{mailbox.GITHUB_ORG}": {"state": "active"},
    }
    client = httpx2.AsyncClient
    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(200, json=replies[request.url.path])
            ),
            **kwargs,
        ),
    )
    with browser() as page:
        signin = page.get("/login", follow_redirects=False)
        state = parse_qs(urlsplit(signin.headers["location"]).query)["state"][0]
        done = page.get(
            "/login/callback",
            params={"code": "c", "state": state},
            follow_redirects=False,
        )
        assert done.status_code == 303
        assert "HttpOnly" in done.headers["set-cookie"]
        assert page.get("/api/me").json()["login"] == "turing"


def rpc(page: TestClient, method: str, params: dict | None = None, **headers):
    """A JSON-RPC request to /mcp, from a client that hasn't signed in."""
    return page.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"Accept": "application/json, text/event-stream", **headers},
    )


def result(response) -> dict:
    data = [line for line in response.text.splitlines() if line.startswith("data:")]
    return json.loads(data[0][5:] if data else response.text)["result"]


def test_anyone_sees_what_peerbox_offers_and_signs_in_to_use_it():
    hello = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "a directory", "version": "1"},
    }
    with browser() as page:
        assert result(rpc(page, "initialize", hello))["serverInfo"]["name"] == "peerbox"
        assert rpc(page, "ping").status_code == 200
        tools = {tool["name"] for tool in result(rpc(page, "tools/list"))["tools"]}
        assert {"whoami", "send", "inbox", "create_group", "attach"} <= tools
        call = {"name": "whoami", "arguments": {}}
        for refused in (
            rpc(page, "tools/call", call),
            rpc(page, "prompts/list"),
            rpc(page, "tools/list", Authorization="Bearer not-a-token"),
        ):
            assert refused.status_code == 401
            assert "resource_metadata=" in refused.headers["www-authenticate"]


def test_each_signed_out_address_has_its_own_rate_limit(monkeypatch):
    limiter = next(m for m in server.mcp.middleware if hasattr(m, "max_requests"))
    monkeypatch.setattr(limiter, "max_requests", 2)

    def listed(page, forwarded_for: str) -> bool:
        reply = rpc(page, "tools/list", **{"X-Forwarded-For": forwarded_for})
        return "Rate limit exceeded" not in reply.text

    with browser() as page:
        assert listed(page, "203.0.113.1") and listed(page, "203.0.113.1")
        assert not listed(page, "203.0.113.1")
        # a forged first entry still counts against the address the proxy added
        assert not listed(page, "198.51.100.7, 203.0.113.1")
        assert listed(page, "203.0.113.2")


def test_the_panel_api_needs_a_session():
    with browser() as page:
        assert page.get("/").status_code == 200
        assert page.get("/panel/panel.js").status_code == 200
        for path in ("/api/me", "/api/conversations", "/api/conversations/x"):
            assert page.get(path).status_code == 401
        assert page.get("/api/stream").status_code == 401


@pytest.mark.usefixtures("bucket_exists")
async def test_the_panel_shows_each_delivery_without_marking_it_read(db):
    turing, church = signed_in("turing"), signed_in("church")
    await inbox(agent="bot", token=turing, db=db)
    upload = await attach(name="plan.txt", token=church, db=db)
    in_an_hour = datetime.now(UTC) + timedelta(seconds=bucket.URL_SECONDS)
    assert abs(upload["upload_url_expires_at"] - in_an_hour) < timedelta(seconds=5)
    async with httpx2.AsyncClient() as client:
        await client.put(upload["upload_url"], content=b"step 1")
    asked = await send(
        "turing/bot", "review the plan", attachments=[upload["id"]], token=church, db=db
    )
    await send(
        "church", "on it", reply_to=asked["id"], from_agent="bot", token=turing, db=db
    )
    await mark_read([asked["id"]], agent="bot", token=turing, db=db)
    await send("turing", "lunch?", token=church, db=db)

    with browser("turing") as page:
        listed = page.get("/api/conversations").json()
        [talk] = listed["conversations"]
        assert (talk["key"], talk["sides"], talk["waiting"]) == (
            "church",
            ["turing", "turing/bot"],
            1,
        )
        messages = page.get("/api/conversations/church").json()["messages"]
    shown = [
        (m["text"], [(d["to"], d["read_at"] is not None) for d in m["deliveries"]])
        for m in messages
    ]
    assert shown == [
        ("review the plan", [("turing/bot", True)]),
        ("on it", [("church", False)]),
        ("lunch?", [("turing", False)]),
    ]
    async with httpx2.AsyncClient() as client:
        file = messages[0]["attachments"][0]
        assert (await client.get(file["url"])).content == b"step 1"
    expires = datetime.fromisoformat(file["url_expires_at"])
    assert abs(expires - in_an_hour) < timedelta(seconds=5)
    with browser("church") as page:
        keys = [
            c["key"] for c in page.get("/api/conversations").json()["conversations"]
        ]
    assert keys == ["turing", "turing/bot"]
    assert await texts(turing, db) == ["lunch?"]
    assert await texts(church, db) == ["on it"]


async def test_two_of_your_agents_talk_in_one_conversation(db):
    turing = signed_in("turing")
    for agent in ("claude-code", "codex"):
        await inbox(agent=agent, token=turing, db=db)
    await send(
        "turing/codex", "run the suite", from_agent="claude-code", token=turing, db=db
    )
    await send(
        "turing/claude-code", "412 passed", from_agent="codex", token=turing, db=db
    )
    with browser("turing") as page:
        [talk] = page.get("/api/conversations").json()["conversations"]
        assert (talk["key"], talk["sides"]) == (
            "turing/codex",
            ["turing/claude-code", "turing/codex"],
        )
        messages = page.get("/api/conversations/turing/codex").json()["messages"]
    assert [m["text"] for m in messages] == ["run the suite", "412 passed"]


async def test_a_group_conversation_shows_every_members_messages(db):
    turing, church = signed_in("turing"), signed_in("church")
    group = (await create_group("compilers", token=turing, db=db))["group"]
    await join(db, group, turing, "church", "kleene")
    await send(group, "standup at ten", token=turing, db=db)
    agreed = await send(group, "agreed", token=church, db=db)

    with browser("kleene") as page:
        [talk] = page.get("/api/conversations").json()["conversations"]
        assert (talk["key"], talk["name"], talk["waiting"]) == (group, "compilers", 2)
        messages = page.get(f"/api/conversations/{quote(group)}").json()["messages"]
        earlier = page.get(
            f"/api/conversations/{quote(group)}?before={agreed['id']}"
        ).json()["messages"]
    assert [(m["from"], m["text"]) for m in messages] == [
        ("turing", "standup at ten"),
        ("church", "agreed"),
    ]
    assert [d["to"] for d in messages[0]["deliveries"]] == ["church", "kleene"]
    assert [m["text"] for m in earlier] == ["standup at ten"]


async def test_the_stream_says_when_something_changed(db):
    turing, church = signed_in("turing"), signed_in("church")
    await whoami(token=turing, db=db)
    async with Client(server.mcp):  # its listener hears what is sent
        stream = panel.changes("turing")
        changed = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.1)  # the stream starts listening
        await send("turing", "hi", token=church, db=db)
        assert await asyncio.wait_for(changed, 5) == {"event": "changed", "data": ""}
        await stream.aclose()
