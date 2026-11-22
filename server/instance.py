"""What the MCP server and the panel share: this instance's address and GitHub App,
its database, who it admits, and the wake-ups that tell a person's open panels and
event subscriptions that something of theirs changed."""

import asyncio
import os
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager, contextmanager
from hashlib import sha256
from pathlib import Path

import httpx2
from asyncpg import Connection, Pool, connect, create_pool
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastmcp.server.lifespan import lifespan
from fastmcp.utilities.logging import get_logger

import mailbox

HERE = Path(__file__).parent
logger = get_logger("peerbox")
BASE_URL = os.environ["BASE_URL"]
DATABASE_URL = os.environ["DATABASE_URL"]
GITHUB_CLIENT_ID = os.environ["GITHUB_CLIENT_ID"]
GITHUB_CLIENT_SECRET = os.environ["GITHUB_CLIENT_SECRET"]
ADMISSION_SECONDS = 600

pool: Pool  # opened by the database lifespan


def get_db() -> AbstractAsyncContextManager[Connection]:
    """A connection from the pool, for `async with` or a tool's Depends."""
    return pool.acquire()


class Waiters:
    """The panel streams and event pumps waiting to hear that something of a person's
    changed, so a change wakes only the people it involves."""

    def __init__(self) -> None:
        self.by_person: dict[int, set[asyncio.Event]] = {}

    @contextmanager
    def watch(self, person: int) -> Iterator[asyncio.Event]:
        """An event that is set each time something of this person's changes."""
        woken = asyncio.Event()
        self.by_person.setdefault(person, set()).add(woken)
        try:
            yield woken
        finally:
            watching = self.by_person[person]
            watching.discard(woken)
            if not watching:
                del self.by_person[person]

    def wake(self, connection, pid, channel, person: str) -> None:
        """The listener's callback: the notification names one person's account."""
        for woken in self.by_person.get(int(person), ()):
            woken.set()


waiters = Waiters()


def fernet(purpose: str) -> Fernet:
    """A key for one purpose, such as encrypting what this server stores, derived from
    the GitHub App's client secret."""
    return Fernet(
        urlsafe_b64encode(
            HKDF(SHA256(), 32, salt=None, info=purpose.encode()).derive(
                GITHUB_CLIENT_SECRET.encode()
            )
        )
    )


async def _expire_hourly() -> None:
    while True:
        try:
            async with get_db() as db:
                await mailbox.expire(db)
        except Exception:
            logger.exception("Deleting old messages failed")
        await asyncio.sleep(3600)


@lifespan
async def database(server):
    global pool
    # At most 7 connections an instance: 4 here, the listener and the OAuth store's 2,
    # so the server fits small Postgres plans.
    async with create_pool(DATABASE_URL, min_size=1, max_size=4) as pool:
        async with get_db() as db:
            version = db.get_server_version()
        if version.major < 15:  # schema.sql uses `unique nulls not distinct`
            raise RuntimeError(
                f"peerbox needs Postgres 15 or newer (found {version.major}."
                f"{version.minor})"
            )
        # Every statement is idempotent, so it runs on every start and creates
        # what an empty database lacks.
        await pool.execute((HERE / "schema.sql").read_text())
        listener = await connect(DATABASE_URL)
        expiry = asyncio.create_task(_expire_hourly())
        try:
            await listener.add_listener(mailbox.CHANNEL, waiters.wake)
            yield {}
        finally:
            expiry.cancel()
            await listener.close()


_admitted_until: dict[str, float] = {}  # a token's hash: when it is rechecked


async def admit(token: str, claims: dict) -> bool:
    """Whether this instance admits the holder of this GitHub token, rechecked every
    10 minutes; claims are GitHub's sub, login and name for them. With GITHUB_ORG
    set, it admits that organisation's active members and its guests. Without it,
    it admits anyone, and each check records the organisations that installed
    peerbox where they are an active member, since their members are each other's
    contacts; a GitHub failure there raises rather than recording none."""
    key = sha256(token.encode()).hexdigest()
    if _admitted_until.get(key, 0) > time.monotonic():
        return True
    async with httpx2.AsyncClient(
        base_url="https://api.github.com",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    ) as github:
        if mailbox.GITHUB_ORG is None:
            await _record_organisations(github, claims)
            admitted = True
        else:
            admitted = await _member_or_guest(github, mailbox.GITHUB_ORG)
    if admitted:
        now = time.monotonic()
        for stale in [held for held, until in _admitted_until.items() if until <= now]:
            del _admitted_until[stale]
        _admitted_until[key] = now + ADMISSION_SECONDS
    return admitted


async def _member_or_guest(github: httpx2.AsyncClient, org: str) -> bool:
    membership = await github.get(f"/user/memberships/orgs/{org}")
    if membership.status_code == 200 and membership.json().get("state") == "active":
        return True
    # A guest is a collaborator on the org's plugin repo. Only a private repo
    # counts: anyone on GitHub can read a public one.
    repo = await github.get(f"/repos/{org}/peerbox-plugin")
    return repo.status_code == 200 and repo.json().get("private") is True


async def _record_organisations(github: httpx2.AsyncClient, claims: dict) -> None:
    orgs = []
    page = "/user/installations?per_page=100"
    while page:
        installed = (await github.get(page)).raise_for_status()
        for installation in installed.json().get("installations", []):
            account = installation["account"]
            if account["type"] == "Organization":
                membership = await github.get(
                    f"/user/memberships/orgs/{account['login']}"
                )
                # 404: not a member. 403: the org enforces SAML and this token has
                # no session for it. Neither counts; any other failure raises.
                if membership.status_code in (403, 404):
                    continue
                if membership.raise_for_status().json().get("state") == "active":
                    orgs.append(account["login"])
        page = installed.links.get("next", {}).get("url")
    async with get_db() as db:
        you = await mailbox.caller(db, claims)
        await mailbox.record_memberships(db, you.person, orgs)
