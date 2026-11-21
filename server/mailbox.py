"""The message rules, as SQL: addresses, contacts and requests, deliveries, groups,
attachments and stored event subscriptions. The MCP tools and the panel call the names
without a leading _."""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from secrets import token_hex
from typing import Self

from asyncpg import Connection
from fastmcp.exceptions import ToolError

import bucket

MAX_ATTACHMENT = 10 * 1024 * 1024
TOO_LARGE = f"The file is over {MAX_ATTACHMENT // 2**20} MB."
_SENT_ONCE = "Each attachment must come from your own attach and can be sent once."
# Set: the instance admits only this organisation's members and its guests, and
# makes everyone it admits everyone's contact. Unset: the public instance's rules,
# whoever hosts it, where anyone with a GitHub account may join.
GITHUB_ORG = os.environ.get("GITHUB_ORG") or None
# The Postgres channel that names a person when something of theirs changed: a
# message or request to or from them, a read, a contact or a group they are in.
CHANNEL = "inboxes"

_HISTORY_PAGE = 50  # messages the panel loads at a time
_MAX_GROUP_PEOPLE = 50
_MAX_FILE_NAME = 200  # characters; a longer name is cut
# The messages that involve login or any of their agents, each with its
# conversation's key: the group's address, or the address on the other side;
# between two of login's own addresses, the one that sorts last, so both
# directions are one conversation.
_MINE = """
    select m.*, case
        when m.group_id is not null then group_address(m.group_id)
        when m.sender = $1 and m.recipient = $1 then greatest(
            address(m.sender, m.sender_agent), address(m.recipient, m.recipient_agent)
        )
        when m.sender = $1 then address(m.recipient, m.recipient_agent)
        else address(m.sender, m.sender_agent)
    end as key
    from messages m
    where m.sender = $1 or m.id in (select message_id from deliveries where recipient = $1)
"""
# A writer may post to a group when their address, or they as a person, is a member.
_IN_GROUP = (
    "select exists(select from group_members where group_id = $1 and login = $2 "
    "and (agent is null or agent = $3))"
)
# The unread deliveries to address ($1, $2), each at its event position.
_UNREAD = (
    "select d.id as position, 'message' as kind, m.id, m.sender, m.sender_agent, "
    "m.recipient, m.recipient_agent, m.group_id, m.body, m.reply_to, m.created_at "
    "from deliveries d join messages m on m.id = d.message_id "
    "where d.recipient = $1 and d.recipient_agent is not distinct from $2 "
    "and d.read_at is null"
)
# The requests address ($1, $2) sees and answers: a contact request asks the
# person, so it shows in all their inboxes; an invitation is for one address.
_REQUESTS_FOR = (
    "target_login = $1 "
    "and (kind = 'contact_request' or target_agent is not distinct from $2)"
)
# A contact pair ($1, $2), stored once with the lesser login first.
_PAIR = "least($1::text, $2::text), greatest($1::text, $2::text)"


class Refused(ToolError):
    """A rule the caller broke; the text says which and what to do instead."""


@dataclass(frozen=True)
class Address:
    """Where a message goes or comes from: a person's login, `login/agent` for one of
    their agents, or `#id` for a group."""

    login: str | None = None
    agent: str | None = None
    group: str | None = None

    @classmethod
    def parse(cls, text: str) -> Self:
        if text.startswith("#"):
            return cls(group=text.removeprefix("#"))
        login, _, agent = text.partition("/")
        return cls(login, agent or None)

    def __str__(self) -> str:
        if self.group is not None:
            return f"#{self.group}"
        return f"{self.login}/{self.agent}" if self.agent else str(self.login)


def _message(row) -> dict:
    """A message as an agent reads it."""
    return {
        "id": row["id"],
        "from": str(Address(row["sender"], row["sender_agent"])),
        "to": str(Address(row["recipient"], row["recipient_agent"], row["group_id"])),
        "sent_at": row["created_at"],
        "reply_to": row["reply_to"],
        "text": row["body"],
    }


def _lists_by(rows, key: str, shape) -> dict:
    """rows shaped by shape, in lists under each row's value of key."""
    lists: dict = {}
    for row in rows:
        lists.setdefault(row[key], []).append(shape(row))
    return lists


async def _wake(db: Connection, logins: list[str]) -> None:
    """Tell these people's panels and event subscriptions that something of theirs
    changed; inside a transaction, once it commits."""
    await db.execute(
        f"select pg_notify('{CHANNEL}', login) from unnest($1::text[]) as login",
        list(set(logins)),
    )


async def _wake_group(db: Connection, group_id: str, *logins: str) -> None:
    """Wake the group's members, and these people, when who is in it changed."""
    members = await db.fetch(
        "select login from group_members where group_id = $1", group_id
    )
    await _wake(db, [*logins, *(row["login"] for row in members)])


def _everyone_is_a_contact() -> bool:
    return GITHUB_ORG is not None


async def _is_contact(db: Connection, login: str, other: str) -> bool:
    return await db.fetchval(
        "select is_contact($1, $2, $3)", login, other, _everyone_is_a_contact()
    )


async def _require_person(db: Connection, login: str) -> None:
    """Refuse a login no one on peerbox has, naming the close matches."""
    if not await db.fetchval(
        "select exists(select 1 from users where login = $1)", login
    ):
        candidates = await db.fetch(
            "select login from users where login ilike $1 or name ilike $1 "
            "or exists (select 1 from unnest(aliases) alias where alias ilike $1)",
            f"%{login}%",
        )
        names = ", ".join(row["login"] for row in candidates) or "none"
        raise Refused(
            f"No one on peerbox has the login '{login}'. Close matches: {names}."
        )


async def _require_own_agent(db: Connection, login: str, agent: str) -> None:
    if not await db.fetchval(
        "select exists(select from agents where owner = $1 and name = $2)",
        login,
        agent,
    ):
        raise Refused(
            f"You have no agent named '{agent}' yet. Call inbox(agent='{agent}') "
            "first so messages can reach it."
        )


async def _require_reachable(
    db: Connection, writer: Address, to: Address
) -> datetime | None:
    """Refuse unless writer may write to, or invite, `to`: a group writer is in, or a
    contact or one of their agents. Returns when that agent last read its inbox."""
    if to.group is not None:
        if not await db.fetchval(_IN_GROUP, to.group, writer.login, writer.agent):
            raise Refused(f"{writer} is not in {to}. Call contacts for your groups.")
        return None
    await _require_person(db, to.login)
    if not await _is_contact(db, writer.login, to.login):
        raise Refused(
            f"{to.login} is not your contact. Ask with add_contact; once they accept, "
            "you can write to them and invite them."
        )
    if to.agent is None:
        return None
    agent = await db.fetchrow(
        "select last_seen from agents where owner = $1 and name = $2",
        to.login,
        to.agent,
    )
    if agent is None:
        raise Refused(f"{to.login} has no agent named '{to.agent}'. Call contacts.")
    return agent["last_seen"]


async def caller(
    db: Connection, login: str, name: str | None, agent: str | None = None
) -> Address:
    """The address a signed-in person acts as: them, or one of their agents, which
    must have read its inbox once. Records the person under the name GitHub gives."""
    await db.execute(
        "insert into users (login, name) values ($1, $2) "
        "on conflict (login) do update set name = excluded.name",
        login,
        name,
    )
    if agent is not None:
        await _require_own_agent(db, login, agent)
    return Address(login, agent)


async def person(db: Connection, login: str) -> dict:
    row = await db.fetchrow("select name, aliases from users where login = $1", login)
    return {"login": login, "name": row["name"], "aliases": row["aliases"]}


async def set_aliases(db: Connection, login: str, aliases: list[str]) -> list[str]:
    """Replace the names others can find login by; returns them, trimmed and sorted."""
    cleaned = sorted({alias.strip() for alias in aliases} - {""})
    await db.execute("update users set aliases = $2 where login = $1", login, cleaned)
    return cleaned


async def send(
    db: Connection,
    writer: Address,
    to: Address,
    text: str,
    reply_to: int | None = None,
    attachments: list[int] | None = None,
) -> dict:
    last_seen = await _require_reachable(db, writer, to)
    if reply_to is not None and not await db.fetchval(
        "select exists(select from messages where id = $1)", reply_to
    ):
        raise Refused(
            f"No message {reply_to} to reply to: messages are deleted after 30 days. "
            "Send without reply_to."
        )
    files = await _sendable(db, writer.login, attachments or [])
    async with db.transaction():
        message_id = await db.fetchval(
            "insert into messages (sender, sender_agent, recipient, recipient_agent, "
            "group_id, body, reply_to) values ($1, $2, $3, $4, $5, $6, $7) returning id",
            writer.login,
            writer.agent,
            to.login,
            to.agent,
            to.group,
            text,
            reply_to,
        )
        if to.group is None:
            await db.execute(
                "insert into deliveries (message_id, recipient, recipient_agent) "
                "values ($1, $2, $3)",
                message_id,
                to.login,
                to.agent,
            )
            recipients = [to.login]
            status = "in their inbox"
            if to.agent:
                status = f"in the agent's inbox; it last checked at {last_seen}"
        else:
            # Every member but the writing address, so one person's agents can
            # talk to each other; agents answer only what asks them in a group,
            # which keeps them from looping.
            delivered = await db.fetch(
                "insert into deliveries (message_id, recipient, recipient_agent) "
                "select $1, login, agent from group_members where group_id = $2 "
                "and not (login = $3 and agent is not distinct from $4) "
                "returning recipient",
                message_id,
                to.group,
                writer.login,
                writer.agent,
            )
            recipients = [row["recipient"] for row in delivered]
            status = f"in the inboxes of {len(delivered)} members"
        # Rechecked here: a send running at the same time may have claimed one.
        claimed = await db.fetch(
            "update attachments a set message_id = $1, size = f.size "
            "from unnest($2::bigint[], $3::bigint[]) as f(id, size) "
            "where a.id = f.id and a.message_id is null returning a.id",
            message_id,
            [id for id, _ in files],
            [size for _, size in files],
        )
        if len(claimed) < len(files):
            raise Refused(_SENT_ONCE)
        await _wake(db, [writer.login, *recipients])
    return {"id": message_id, "to": str(to), "status": status}


async def _sendable(db: Connection, owner: str, ids: list[int]) -> list[tuple]:
    """The writer's unsent attachments with their sizes. An upload is checked in the
    bucket, since its size is only known once it is there."""
    rows = await db.fetch(
        "select id, key, size from attachments "
        "where id = any($1) and owner = $2 and message_id is null",
        ids,
        owner,
    )
    if len(rows) < len(set(ids)):
        raise Refused(_SENT_ONCE)
    files = []
    for row in rows:
        size = row["size"]
        if size is None:
            size = await bucket.size(row["key"])
            if size is None:
                raise Refused(
                    f"Attachment {row['id']} is not uploaded yet: PUT the file to "
                    "its upload_url first."
                )
            if size > MAX_ATTACHMENT:
                raise Refused(f"Attachment {row['id']}: {TOO_LARGE}")
        files.append((row["id"], size))
    return files


async def inbox(db: Connection, reader: Address, limit: int) -> dict:
    """The reader's unread messages, oldest first, and the requests waiting for them.
    Reading as an agent registers it, or records that it checked again."""
    if reader.agent is not None:
        if await db.fetchval(
            "insert into agents (owner, name, last_seen) values ($1, $2, now()) "
            "on conflict (owner, name) do nothing returning true",
            reader.login,
            reader.agent,
        ):
            await _wake(db, [reader.login])  # their panel gains its inbox
        else:
            await db.execute(
                "update agents set last_seen = now() where owner = $1 and name = $2",
                reader.login,
                reader.agent,
            )
    rows = await db.fetch(
        f"{_UNREAD} order by position limit $3",
        reader.login,
        reader.agent,
        limit + 1,
    )
    return {
        "messages": await _messages(db, rows[:limit]),
        "has_more": len(rows) > limit,
        "requests": await _requests(db, reader),
    }


async def waiting(db: Connection, reader: Address, after: int) -> list[dict]:
    """What an event subscriber hears of past position after, oldest first: the
    reader's unread messages and the requests it sees, each with its kind and
    position."""
    rows = await db.fetch(
        f"{_UNREAD} and d.id > $3 union all select id, kind, id, asker, asker_agent, "
        "target_login, target_agent, group_id, greeting, null, created_at "
        f"from requests where {_REQUESTS_FOR} and id > $3 order by position",
        reader.login,
        reader.agent,
        after,
    )
    return [
        {"position": row["position"], "kind": row["kind"]} | _message(row)
        for row in rows
    ]


async def start_position(
    db: Connection, cursor: int | None, max_age_ms: int | None
) -> tuple[int, bool]:
    """Where an event subscriber starts hearing: now, or its cursor raised past what
    is older than max_age_ms; and whether that skipped anything."""
    if cursor is None:
        head = await db.fetchval(
            "select coalesce(greatest((select max(id) from deliveries), "
            "(select max(id) from requests)), 0)"
        )
        return head, False
    floor = 0
    if max_age_ms is not None:
        floor = await db.fetchval(
            "select coalesce(max(d.id), 0) from deliveries d "
            "join messages m on m.id = d.message_id "
            "where m.created_at < now() - make_interval(secs => $1)",
            max_age_ms / 1000,
        )
    return max(cursor, floor), floor > cursor


async def _messages(db: Connection, rows: list) -> list[dict]:
    """Messages as an agent reads them, each with its attachments' download links."""
    files = await db.fetch(
        "select id, message_id, name, mime, size, key from attachments "
        "where message_id = any($1) order by id",
        [row["id"] for row in rows],
    )

    def attachment(file) -> dict:
        url, expires_at = bucket.download_url(file["key"])
        return {
            "id": file["id"],
            "name": file["name"],
            "mime": file["mime"],
            "size": file["size"],
            "url": url,
            "url_expires_at": expires_at,
        }

    by_message = _lists_by(files, "message_id", attachment)
    return [
        _message(row) | {"attachments": by_message.get(row["id"], [])} for row in rows
    ]


async def _requests(db: Connection, reader: Address) -> list[dict]:
    """The requests waiting for the reader, oldest first."""
    rows = await db.fetch(
        "select id, kind, asker, asker_agent, group_id, greeting from requests "
        f"where {_REQUESTS_FOR} order by id",
        reader.login,
        reader.agent,
    )
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "from": str(Address(row["asker"], row["asker_agent"])),
            "group": str(Address(group=row["group_id"])) if row["group_id"] else None,
            "greeting": row["greeting"],
        }
        for row in rows
    ]


async def mark_read(db: Connection, reader: Address, ids: list[int]) -> int:
    """Mark the reader's own copies of these messages read; each copy belongs to
    exactly one address."""
    marked = await db.fetchrow(
        "with marked as (update deliveries set read_at = now() "
        "where recipient = $1 and recipient_agent is not distinct from $2 "
        "and message_id = any($3) and read_at is null returning message_id) "
        "select (select count(*) from marked) as count, array("
        "select sender from messages where id in (select message_id from marked) "
        "union select recipient from deliveries "
        "where message_id in (select message_id from marked)) as shown_to",
        reader.login,
        reader.agent,
        ids,
    )
    if marked["count"]:
        await _wake(db, marked["shown_to"])  # the sender and every recipient see it
    return marked["count"]


async def conversations(db: Connection, login: str) -> dict:
    """login's conversations, newest first: each with its last message, the addresses
    of login's side in it and what waits there; and what waits in each of login's
    inboxes."""
    rows = await db.fetch(
        f"with mine as ({_MINE}), "
        "sides as (select x.key, address(d.recipient, d.recipient_agent) as side, "
        "d.read_at is null as waits, x.created_at from mine x "
        "join deliveries d on d.message_id = x.id and d.recipient = $1 "
        "union all select key, address(sender, sender_agent), false, created_at "
        "from mine where sender = $1) "
        "select distinct on (x.key) x.key, x.sender, x.sender_agent, x.body, "
        "x.created_at, g.name, "
        "(select array_agg(distinct side) from sides s where s.key = x.key) as sides, "
        "(select count(*) from sides s where s.key = x.key and waits) as waiting, "
        "(select min(created_at) from sides s where s.key = x.key and waits) "
        "as waiting_since "
        "from mine x left join groups g on group_address(g.id) = x.key "
        "order by x.key, x.id desc",
        login,
    )
    inboxes = await db.fetch(
        "select address(recipient, recipient_agent) as inbox, count(*) "
        "from deliveries where recipient = $1 and read_at is null group by 1",
        login,
    )
    listed = [
        {
            "key": row["key"],
            "name": row["name"],
            "last": {
                "from": str(Address(row["sender"], row["sender_agent"])),
                "text": row["body"],
                "sent_at": row["created_at"],
            },
            "sides": row["sides"],
            "waiting": row["waiting"],
            "waiting_since": row["waiting_since"],
        }
        for row in rows
    ]
    return {
        "conversations": sorted(
            listed, key=lambda c: c["last"]["sent_at"], reverse=True
        ),
        "waiting": {row["inbox"]: row["count"] for row in inboxes},
    }


async def history(db: Connection, login: str, key: str, before: int | None) -> dict:
    """One conversation's messages before message id before, oldest first, each with
    its attachments and the state of each of its deliveries."""
    rows = await db.fetch(
        f"with mine as ({_MINE}) select * from mine where key = $2 "
        "and ($3::bigint is null or id < $3) order by id desc limit $4",
        login,
        key,
        before,
        _HISTORY_PAGE + 1,
    )
    shown = rows[:_HISTORY_PAGE][::-1]
    deliveries = await db.fetch(
        "select message_id, address(recipient, recipient_agent) as to, read_at "
        "from deliveries where message_id = any($1) order by id",
        [row["id"] for row in shown],
    )
    by_message = _lists_by(
        deliveries,
        "message_id",
        lambda delivery: {"to": delivery["to"], "read_at": delivery["read_at"]},
    )
    return {
        "messages": [
            entry | {"deliveries": by_message.get(entry["id"], [])}
            for entry in await _messages(db, shown)
        ],
        "has_more": len(rows) > _HISTORY_PAGE,
    }


async def contacts_and_groups(db: Connection, login: str) -> dict:
    """login's contacts with their agents, and the groups login is in with their
    members."""
    everyone = _everyone_is_a_contact()
    users = await db.fetch(
        "select login, name, aliases from users where is_contact($1, login, $2) "
        "order by login",
        login,
        everyone,
    )
    agents = await db.fetch(
        "select owner, name, description, last_seen from agents "
        "where is_contact($1, owner, $2) order by name",
        login,
        everyone,
    )
    groups = await db.fetch(
        "select g.id, g.name, address(g.owner, g.owner_agent) as owner, "
        "array_agg(address(m.login, m.agent) order by m.joined_at) as members "
        "from groups g join group_members m on m.group_id = g.id "
        "where g.id in (select group_id from group_members where login = $1) "
        "group by g.id order by g.created_at",
        login,
    )
    by_owner = _lists_by(
        agents,
        "owner",
        lambda agent: {
            "name": agent["name"],
            "description": agent["description"],
            "last_seen": agent["last_seen"],
        },
    )
    return {
        "people": [
            {
                "login": user["login"],
                "name": user["name"],
                "aliases": user["aliases"],
                "agents": by_owner.get(user["login"], []),
            }
            for user in users
        ],
        "groups": [
            {
                "address": str(Address(group=group["id"])),
                "name": group["name"],
                "owner": group["owner"],
                "members": group["members"],
            }
            for group in groups
        ],
    }


async def create_group(db: Connection, owner: Address, name: str) -> Address:
    """Create a group owned by the creating address, its first member."""
    group = Address(group=token_hex(4))
    await db.execute(
        "with created as (insert into groups (id, name, owner, owner_agent) "
        "values ($1, $2, $3, $4) returning id) "
        "insert into group_members (group_id, login, agent) "
        "select id, $3, $4 from created",
        group.group,
        name,
        owner.login,
        owner.agent,
    )
    return group


async def _join(db: Connection, group: Address, member: Address) -> None:
    await db.execute(
        "insert into group_members (group_id, login, agent) values ($1, $2, $3) "
        "on conflict do nothing",
        group.group,
        member.login,
        member.agent,
    )


async def invite(
    db: Connection, inviter: Address, group: Address, member: Address
) -> str:
    """Add one of the inviter's own addresses, or invite a contact's."""
    await _require_reachable(db, inviter, group)
    if member.login == inviter.login:
        if member.agent:
            await _require_own_agent(db, inviter.login, member.agent)
        async with db.transaction():
            await _join(db, group, member)
            await _wake_group(db, group.group)
        return "joined"
    await _require_reachable(db, inviter, member)
    async with db.transaction():
        await db.execute(
            "insert into requests (kind, asker, asker_agent, target_login, "
            "target_agent, group_id) values ('invitation', $1, $2, $3, $4, $5) "
            "on conflict do nothing",
            inviter.login,
            inviter.agent,
            member.login,
            member.agent,
            group.group,
        )
        await _wake(db, [inviter.login, member.login])
    return "invited; it joins when they accept"


async def leave(
    db: Connection, actor: Address, group: Address, member: Address
) -> None:
    """Remove member from group. Any address of the member's person may, and the
    owner address may remove anyone. The group keeps its messages when nobody is
    left; expire deletes it once they are gone."""
    async with db.transaction():
        # Locking the group makes members who leave at once still hand its
        # ownership to someone who stays.
        owner = await db.fetchval(
            "select address(owner, owner_agent) from groups where id = $1 for update",
            group.group,
        )
        if member.login != actor.login and owner != str(actor):
            raise Refused(
                f"Only the owner of {group} can remove other people's addresses."
            )
        if not await db.fetchval(
            "delete from group_members where group_id = $1 and login = $2 "
            "and agent is not distinct from $3 returning true",
            group.group,
            member.login,
            member.agent,
        ):
            raise Refused(f"{member} is not in {group}.")
        await _pass_on_ownership(db, [group.group])
        await _wake_group(db, group.group, member.login)


async def _pass_on_ownership(db: Connection, group_ids: list[str]) -> None:
    """Hand each group whose owner address left to the member that joined first. A
    group nobody is left in has no owner."""
    await db.execute(
        "update groups g set (owner, owner_agent) = (select login, agent "
        "from group_members where group_id = g.id order by joined_at limit 1) "
        "where id = any($1) and not exists (select from group_members m "
        "where m.group_id = g.id and m.login = g.owner "
        "and m.agent is not distinct from g.owner_agent)",
        group_ids,
    )


async def _unblock(db: Connection, blocker: str, blocked: str) -> bool:
    """Lift blocker's block on blocked; whether there was one."""
    return bool(
        await db.fetchval(
            "delete from blocks where blocker = $1 and blocked = $2 returning true",
            blocker,
            blocked,
        )
    )


async def _make_contacts(db: Connection, login: str, other: str) -> None:
    await db.execute(
        f"insert into contacts (a, b) values ({_PAIR}) on conflict do nothing",
        login,
        other,
    )


async def add_contact(
    db: Connection, asker: Address, other: str, greeting: str | None
) -> str:
    await _require_person(db, other)
    unblocked = await _unblock(db, asker.login, other)
    if await _is_contact(db, asker.login, other):
        if unblocked:
            await _wake(db, [asker.login, other])  # they see each other again
        return "already your contact"
    if await db.fetchval(
        "select exists(select from blocks where blocker = $1 and blocked = $2)",
        other,
        asker.login,
    ):
        raise Refused(f"{other} does not take contact requests from you.")
    async with db.transaction():
        await _wake(db, [asker.login, other])
        if await db.fetchval(
            "delete from requests where kind = 'contact_request' and asker = $1 "
            "and target_login = $2 returning true",
            other,
            asker.login,
        ):
            await _make_contacts(db, asker.login, other)
            return "now your contact: they had asked you"
        await db.execute(
            "insert into requests (kind, asker, asker_agent, target_login, greeting) "
            "values ('contact_request', $1, $2, $3, $4) on conflict do nothing",
            asker.login,
            asker.agent,
            other,
            greeting,
        )
    return "asked; they become your contact when they accept"


async def answer(db: Connection, you: Address, request_id: int, accept: bool) -> str:
    """Accept or decline a request for you. A contact request asks the person, so the
    first of their addresses to answer settles it; an invitation is answered by the
    address it invites."""
    async with db.transaction():
        request = await db.fetchrow(
            f"delete from requests where id = $3 and {_REQUESTS_FOR} returning *",
            you.login,
            you.agent,
            request_id,
        )
        if request is None:
            raise Refused(
                f"No request {request_id} waits for {you}. An invitation is answered "
                "as the address it invites, and a contact request that one of your "
                "addresses already answered stays answered; inbox lists the requests "
                "still waiting."
            )
        await _wake(db, [you.login, request["asker"]])
        if not accept:
            return "declined"
        if request["kind"] == "contact_request":
            # Accepting is adding them, so it lifts your block too.
            await _unblock(db, you.login, request["asker"])
            await _make_contacts(db, you.login, request["asker"])
            return "accepted: now your contact"
        group = Address(group=request["group_id"])
        if await db.fetchval(
            "select count(distinct login) >= $3 and not bool_or(login = $2) "
            "from group_members where group_id = $1",
            group.group,
            you.login,
            _MAX_GROUP_PEOPLE,
        ):
            raise Refused(f"{group} already has {_MAX_GROUP_PEOPLE} people.")
        await _join(db, group, you)
        await _pass_on_ownership(db, [group.group])  # if everyone else had left
        await _wake_group(db, group.group)
        return f"accepted: now in {group}"


async def remove_contact(db: Connection, login: str, other: str) -> None:
    """Remove a contact and block them, so neither can write to the other."""
    await _require_person(db, other)
    async with db.transaction():
        await db.execute(f"delete from contacts where (a, b) = ({_PAIR})", login, other)
        await db.execute(
            "insert into blocks (blocker, blocked) values ($1, $2) "
            "on conflict do nothing",
            login,
            other,
        )
        await _wake(db, [login, other])


async def record_memberships(db: Connection, login: str, orgs: list[str]) -> None:
    async with db.transaction():
        await db.execute(
            "insert into users (login) values ($1) on conflict do nothing", login
        )
        await db.execute("delete from memberships where login = $1", login)
        await db.executemany(
            "insert into memberships (login, org) values ($1, $2)",
            [(login, org) for org in orgs],
        )


async def attach(
    db: Connection,
    owner: Address,
    content: Callable[[], Awaitable[tuple[str, str, bytes | None]]],
) -> dict:
    """Store a file in the bucket or, with no data, hand out a URL to upload it to.
    `content` gives the file's name, type and data; it runs only once attachments
    are known to be on, so a server without them fetches nothing."""
    if not bucket.ENABLED:
        raise Refused("Attachments are off on this server.")
    name, mime, data = await content()
    name = name[:_MAX_FILE_NAME]
    if data is not None and len(data) > MAX_ATTACHMENT:
        raise Refused(TOO_LARGE)
    key = token_hex(16)
    if data is not None:
        await bucket.put(key, data)
    id = await db.fetchval(
        "insert into attachments (owner, owner_agent, name, mime, size, key) "
        "values ($1, $2, $3, $4, $5, $6) returning id",
        owner.login,
        owner.agent,
        name,
        mime,
        None if data is None else len(data),
        key,
    )
    if data is None:
        url, expires_at = bucket.upload_url(key)
        return {"id": id, "upload_url": url, "upload_url_expires_at": expires_at}
    return {"id": id, "name": name, "mime": mime, "size": len(data)}


async def open_attachment(
    db: Connection, login: str, id: int
) -> tuple[str, str, bytes]:
    """An attachment's name, type and bytes, for its owner or a recipient."""
    row = await db.fetchrow(
        "select name, mime, key from attachments a where id = $1 and (owner = $2 "
        "or exists (select from deliveries "
        "where message_id = a.message_id and recipient = $2))",
        id,
        login,
    )
    if row is None:
        raise Refused(f"No attachment {id} that you sent or received.")
    return row["name"], row["mime"], await bucket.get(row["key"])


async def delete_account(db: Connection, login: str) -> None:
    """Delete a person and what is theirs: their agents, contacts, blocks,
    organisation memberships, the requests they sent or received, every message they
    or their agents sent, every message sent directly to them or their agents, their
    copies of group messages, and their attachments, here and in the bucket. Groups
    they owned pass on. Everyone whose panel showed any of it is woken."""
    async with db.transaction():
        # The sender and every recipient of each message that loses a copy below,
        # in the groups the person already left too.
        affected = await db.fetch(
            "with gone as (select id, sender from messages "
            "where sender = $1 or recipient = $1 "
            "or id in (select message_id from deliveries where recipient = $1)) "
            "select sender as login from gone "
            "union select recipient from deliveries "
            "where message_id in (select id from gone) "
            "union select login from users where is_contact($1, login, $2) "
            "union select login from group_members where group_id in "
            "(select group_id from group_members where login = $1) "
            "union select asker from requests where target_login = $1 "
            "union select target_login from requests where asker = $1",
            login,
            _everyone_is_a_contact(),
        )
        await _wake(db, [row["login"] for row in affected])
        keys = await db.fetch("select key from attachments where owner = $1", login)
        groups = await db.fetch(
            "delete from group_members where login = $1 returning group_id", login
        )
        await _pass_on_ownership(db, list({row["group_id"] for row in groups}))
        await db.execute(
            "delete from messages where sender = $1 or recipient = $1", login
        )
        await db.execute("delete from deliveries where recipient = $1", login)
        await db.execute("delete from agents where owner = $1", login)
        # Contacts, blocks, memberships, requests and attachments go with the user.
        await db.execute("delete from users where login = $1", login)
    for row in keys:
        await bucket.delete(row["key"])


async def save_subscription(
    db: Connection, id: str, expires_at: datetime, state: bytes
) -> None:
    """Store an event subscription, encrypted by the caller, so a restarted server
    resumes its deliveries."""
    await db.execute(
        "insert into event_subscriptions (id, expires_at, state) "
        "values ($1, $2, $3) on conflict (id) do update "
        "set expires_at = excluded.expires_at, state = excluded.state",
        id,
        expires_at,
        state,
    )


async def subscriptions(db: Connection) -> list[bytes]:
    """The stored states of the event subscriptions that have not ended."""
    rows = await db.fetch(
        "select state from event_subscriptions where expires_at > now()"
    )
    return [row["state"] for row in rows]


async def expire(db: Connection) -> None:
    """Delete what is over 30 days old: messages, with their deliveries and
    attachments; attachments never sent; and requests never answered. Then delete
    the groups left with no members and no messages, and the event subscriptions
    that ended. The bucket's own 30-day rule deletes the files."""
    await db.execute(
        "delete from messages where created_at < now() - interval '30 days'"
    )
    await db.execute(
        "delete from attachments "
        "where message_id is null and created_at < now() - interval '30 days'"
    )
    await db.execute(
        "delete from requests where created_at < now() - interval '30 days'"
    )
    await db.execute(
        "delete from groups g "
        "where not exists (select from group_members where group_id = g.id) "
        "and not exists (select from messages where group_id = g.id)"
    )
    await db.execute("delete from event_subscriptions where expires_at <= now()")
