"""The message rules, as SQL: addresses, contacts and requests, remarks, deliveries,
groups, attachments and stored event subscriptions. Other modules call the names
without a leading _."""

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from secrets import token_hex

from asyncpg import Connection, UniqueViolationError
from fastmcp.exceptions import ToolError

import bucket

MAX_ATTACHMENT = 10 * 1024 * 1024
TOO_LARGE = f"The file is over {MAX_ATTACHMENT // 2**20} MB."
_SENT_ONCE = "Each attachment must come from your own attach and can be sent once."
# Set: the instance admits only this organisation's members and its guests, and
# makes everyone it admits everyone's contact. Unset: the public instance's rules,
# whoever hosts it, where anyone with a GitHub account may join.
GITHUB_ORG = os.environ.get("GITHUB_ORG") or None
# The Postgres channel that names a person, by account id, when something of theirs
# changed: a message or request to or from them, a read, a contact or a group they
# are in.
CHANNEL = "inboxes"

_HISTORY_PAGE = 50  # messages the panel loads at a time
_MAX_GROUP_PEOPLE = 50
_MAX_FILE_NAME = 200  # characters; a longer name is cut
# The messages that involve person $1 or any of their agents, each with its
# conversation's key: the group's address, or the address on the other side;
# between two of their own addresses, the one that sorts last, so both directions
# are one conversation.
_MINE = """
    select m.*, address(m.sender, m.sender_agent) as "from",
    coalesce(group_address(m.group_id), address(m.recipient, m.recipient_agent))
        as "to",
    case
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
    "select exists(select from group_members where group_id = $1 and person = $2 "
    "and (agent is null or agent = $3))"
)
# The unread deliveries to address ($1, $2), each at its event position.
_UNREAD = (
    "select d.id as position, 'message' as kind, m.id, "
    'address(m.sender, m.sender_agent) as "from", '
    "coalesce(group_address(m.group_id), address(m.recipient, m.recipient_agent)) "
    'as "to", m.body, m.reply_to, m.created_at '
    "from deliveries d join messages m on m.id = d.message_id "
    "where d.recipient = $1 and d.recipient_agent is not distinct from $2 "
    "and d.read_at is null"
)
# The requests address ($1, $2) sees and answers: a contact request asks the
# person, so it shows in all their inboxes; an invitation is for one address.
_REQUESTS_FOR = (
    "target = $1 and (kind = 'contact_request' or target_agent is not distinct from $2)"
)
# A contact pair ($1, $2), stored once with the lesser id first.
_PAIR = "least($1::bigint, $2::bigint), greatest($1::bigint, $2::bigint)"
# Person $1's circle: their contacts and the members of their groups, themselves
# included; $2 is whether everyone is a contact. It is mutual: everyone in it has $1
# in theirs.
_CIRCLE = (
    "select id as person from users where is_contact($1, id, $2) "
    "union select person from group_members where group_id in "
    "(select group_id from group_members where person = $1)"
)
# Everyone whose contacts list person $1: their circle, and anyone who gave them a
# remark.
_KNOWN_BY = f"{_CIRCLE} union select owner from remarks where person = $1"


class Refused(ToolError):
    """A rule the caller broke; the text says which and what to do instead."""


@dataclass(frozen=True)
class Address:
    """Where a message goes or comes from: a person, `username/agent` for one of their
    agents, or `#id` for a group. A person is their account id, shown by username."""

    person: int | None = None
    agent: str | None = None
    group: str | None = None
    username: str | None = field(default=None, compare=False)  # as shown, not who

    def __str__(self) -> str:
        if self.group is not None:
            return f"#{self.group}"
        return f"{self.username}/{self.agent}" if self.agent else str(self.username)


def _message(row) -> dict:
    """A message as an agent reads it."""
    return {
        "id": row["id"],
        "from": row["from"],
        "to": row["to"],
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


async def _wake(db: Connection, people: list[int]) -> None:
    """Tell these people's panels and event subscriptions that something of theirs
    changed; inside a transaction, once it commits."""
    await db.execute(
        f"select pg_notify('{CHANNEL}', person::text) "
        "from unnest($1::bigint[]) as person",
        list(set(people)),
    )


async def wake_known_by(db: Connection, person: int) -> None:
    """Wake everyone whose contacts list person, when how they show changed."""
    known_by = await db.fetch(_KNOWN_BY, person, _everyone_is_a_contact())
    await _wake(db, [row["person"] for row in known_by])


async def _wake_group(db: Connection, group_id: str, *people: int) -> None:
    """Wake the group's members, and these people, when who is in it changed."""
    members = await db.fetch(
        "select person from group_members where group_id = $1", group_id
    )
    await _wake(db, [*people, *(row["person"] for row in members)])


def _everyone_is_a_contact() -> bool:
    return GITHUB_ORG is not None


async def _is_contact(db: Connection, person: int, other: int) -> bool:
    return await db.fetchval(
        "select is_contact($1, $2, $3)", person, other, _everyone_is_a_contact()
    )


async def as_agent(db: Connection, you: Address, agent: str | None) -> Address:
    """You, or with agent, that agent of yours, which must have read its inbox once."""
    if agent is None:
        return you
    await _require_own_agent(db, you.person, agent)
    return replace(you, agent=agent)


async def _require_own_agent(db: Connection, person: int, agent: str) -> None:
    if not await db.fetchval(
        "select exists(select from agents where owner = $1 and name = $2)",
        person,
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
        if not await db.fetchval(_IN_GROUP, to.group, writer.person, writer.agent):
            raise Refused(f"{writer} is not in {to}. Call contacts for your groups.")
        return None
    if not await _is_contact(db, writer.person, to.person):
        raise Refused(
            f"{to.username} is not your contact. Ask with add_contact; once they "
            "accept, you can write to them and invite them."
        )
    if to.agent is None:
        return None
    agent = await db.fetchrow(
        "select last_seen from agents where owner = $1 and name = $2",
        to.person,
        to.agent,
    )
    if agent is None:
        raise Refused(f"{to.username} has no agent named '{to.agent}'. Call contacts.")
    return agent["last_seen"]


async def set_remark(
    db: Connection, owner: int, other: Address, remark: str
) -> str | None:
    """Give other a remark only owner sees, or with an empty one, clear theirs.
    Returns the remark kept, trimmed."""
    remark = remark.strip() or None
    if remark is None:
        await db.execute(
            "delete from remarks where owner = $1 and person = $2", owner, other.person
        )
    else:
        try:
            await db.execute(
                "insert into remarks (owner, person, remark) values ($1, $2, $3) "
                "on conflict (owner, person) do update set remark = excluded.remark",
                owner,
                other.person,
                remark,
            )
        except UniqueViolationError:
            named = await db.fetchval(
                "select address(person, null) from remarks "
                "where owner = $1 and lower(remark) = lower($2)",
                owner,
                remark,
            )
            raise Refused(
                f"You already call {named} '{remark}'. Give {other} another remark, "
                f"or change {named}'s first."
            ) from None
    await _wake(db, [owner])
    return remark


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
    files = await _sendable(db, writer.person, attachments or [])
    async with db.transaction():
        message_id = await db.fetchval(
            "insert into messages (sender, sender_agent, recipient, recipient_agent, "
            "group_id, body, reply_to) values ($1, $2, $3, $4, $5, $6, $7) returning id",
            writer.person,
            writer.agent,
            to.person,
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
                to.person,
                to.agent,
            )
            recipients = [to.person]
            status = "in their inbox"
            if to.agent:
                status = f"in the agent's inbox; it last checked at {last_seen}"
        else:
            # Every member but the writing address, so one person's agents can
            # talk to each other; agents answer only what asks them in a group,
            # which keeps them from looping.
            delivered = await db.fetch(
                "insert into deliveries (message_id, recipient, recipient_agent) "
                "select $1, person, agent from group_members where group_id = $2 "
                "and not (person = $3 and agent is not distinct from $4) "
                "returning recipient",
                message_id,
                to.group,
                writer.person,
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
        await _wake(db, [writer.person, *recipients])
    return {"id": message_id, "to": str(to), "status": status}


async def _sendable(db: Connection, owner: int, ids: list[int]) -> list[tuple]:
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
            reader.person,
            reader.agent,
        ):
            await _wake(db, [reader.person])  # their panel gains its inbox
        else:
            await db.execute(
                "update agents set last_seen = now() where owner = $1 and name = $2",
                reader.person,
                reader.agent,
            )
    rows = await db.fetch(
        f"{_UNREAD} order by position limit $3",
        reader.person,
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
        f"{_UNREAD} and d.id > $3 union all select id, kind, id, "
        "address(asker, asker_agent), "
        "coalesce(group_address(group_id), address(target, target_agent)), "
        f"greeting, null, created_at from requests where {_REQUESTS_FOR} and id > $3 "
        "order by position",
        reader.person,
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
    """The requests waiting for the reader, oldest first, each with the GitHub login
    of whoever asked."""
    return [
        dict(row)
        for row in await db.fetch(
            'select id, kind, address(asker, asker_agent) as "from", '
            'github(asker), group_address(group_id) as "group", greeting '
            f"from requests where {_REQUESTS_FOR} order by id",
            reader.person,
            reader.agent,
        )
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
        reader.person,
        reader.agent,
        ids,
    )
    if marked["count"]:
        await _wake(db, marked["shown_to"])  # the sender and every recipient see it
    return marked["count"]


async def conversations(db: Connection, person: int) -> dict:
    """person's conversations, newest first: each with its last message, the
    addresses of their side in it and what waits there."""
    rows = await db.fetch(
        f"with mine as ({_MINE}), "
        "sides as (select x.key, address(d.recipient, d.recipient_agent) as side, "
        "d.read_at is null as waits, x.created_at from mine x "
        "join deliveries d on d.message_id = x.id and d.recipient = $1 "
        "union all select key, address(sender, sender_agent), false, created_at "
        "from mine where sender = $1) "
        'select distinct on (x.key) x.key, x."from", x.body, x.created_at, g.name, '
        "(select array_agg(distinct side) from sides s where s.key = x.key) as sides, "
        "(select count(*) from sides s where s.key = x.key and waits) as waiting, "
        "(select min(created_at) from sides s where s.key = x.key and waits) "
        "as waiting_since "
        "from mine x left join groups g on group_address(g.id) = x.key "
        "order by x.key, x.id desc",
        person,
    )
    listed = [
        {
            "key": row["key"],
            "name": row["name"],
            "last": {
                "from": row["from"],
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
    }


async def history(db: Connection, person: int, key: str, before: int | None) -> dict:
    """One conversation's messages before message id before, oldest first, each with
    its attachments and the state of each of its deliveries."""
    rows = await db.fetch(
        f"with mine as ({_MINE}) select * from mine where key = $2 "
        "and ($3::bigint is null or id < $3) order by id desc limit $4",
        person,
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


async def contacts_and_groups(db: Connection, person: int) -> dict:
    """The people person knows, and the groups person is in with their members. The
    people are person's circle and anyone they gave a remark, each with how they
    show, person's remark for them, whether they are a contact, and a contact's
    agents."""
    everyone = _everyone_is_a_contact()
    people = await db.fetch(
        f"with known as ({_CIRCLE} union select person from remarks where owner = $1) "
        "select u.id, u.username, u.name, github(u.id), r.remark, "
        "is_contact($1, u.id, $2) as contact "
        "from users u join known k on k.person = u.id "
        "left join remarks r on (r.owner, r.person) = ($1, u.id) "
        "order by lower(u.username)",
        person,
        everyone,
    )
    agents = await db.fetch(
        "select owner, name, description, last_seen from agents "
        "where is_contact($1, owner, $2) order by name",
        person,
        everyone,
    )
    groups = await db.fetch(
        "select g.id, g.name, address(g.owner, g.owner_agent) as owner, "
        "array_agg(address(m.person, m.agent) order by m.joined_at) as members "
        "from groups g join group_members m on m.group_id = g.id "
        "where g.id in (select group_id from group_members where person = $1) "
        "group by g.id order by g.created_at",
        person,
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
                "username": known["username"],
                "name": known["name"],
                "github": known["github"],
                "remark": known["remark"],
                "contact": known["contact"],
                "agents": by_owner.get(known["id"], []),
            }
            for known in people
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
        "insert into group_members (group_id, person, agent) "
        "select id, $3, $4 from created",
        group.group,
        name,
        owner.person,
        owner.agent,
    )
    return group


async def _join(db: Connection, group: Address, member: Address) -> None:
    await db.execute(
        "insert into group_members (group_id, person, agent) values ($1, $2, $3) "
        "on conflict do nothing",
        group.group,
        member.person,
        member.agent,
    )


async def invite(
    db: Connection, inviter: Address, group: Address, member: Address
) -> str:
    """Add one of the inviter's own addresses, or invite a contact's."""
    await _require_reachable(db, inviter, group)
    if member.person == inviter.person:
        if member.agent:
            await _require_own_agent(db, inviter.person, member.agent)
        async with db.transaction():
            await _join(db, group, member)
            await _wake_group(db, group.group)
        return "joined"
    await _require_reachable(db, inviter, member)
    async with db.transaction():
        await db.execute(
            "insert into requests (kind, asker, asker_agent, target, target_agent, "
            "group_id) values ('invitation', $1, $2, $3, $4, $5) "
            "on conflict do nothing",
            inviter.person,
            inviter.agent,
            member.person,
            member.agent,
            group.group,
        )
        await _wake(db, [inviter.person, member.person])
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
        if member.person != actor.person and owner != str(actor):
            raise Refused(
                f"Only the owner of {group} can remove other people's addresses."
            )
        if not await db.fetchval(
            "delete from group_members where group_id = $1 and person = $2 "
            "and agent is not distinct from $3 returning true",
            group.group,
            member.person,
            member.agent,
        ):
            raise Refused(f"{member} is not in {group}.")
        await _pass_on_ownership(db, [group.group])
        await _wake_group(db, group.group, member.person)


async def _pass_on_ownership(db: Connection, group_ids: list[str]) -> None:
    """Hand each group whose owner address left to the member that joined first. A
    group nobody is left in has no owner."""
    await db.execute(
        "update groups g set (owner, owner_agent) = (select person, agent "
        "from group_members where group_id = g.id order by joined_at limit 1) "
        "where id = any($1) and not exists (select from group_members m "
        "where m.group_id = g.id and m.person = g.owner "
        "and m.agent is not distinct from g.owner_agent)",
        group_ids,
    )


async def _unblock(db: Connection, blocker: int, blocked: int) -> bool:
    """Lift blocker's block on blocked; whether there was one."""
    return bool(
        await db.fetchval(
            "delete from blocks where blocker = $1 and blocked = $2 returning true",
            blocker,
            blocked,
        )
    )


async def _make_contacts(db: Connection, person: int, other: int) -> None:
    await db.execute(
        f"insert into contacts (a, b) values ({_PAIR}) on conflict do nothing",
        person,
        other,
    )


async def add_contact(
    db: Connection, asker: Address, other: Address, greeting: str | None
) -> dict:
    """Ask other to become asker's contact: their username and GitHub login, since
    anyone may take any free username, and what became of the request."""
    return {
        "username": str(other),
        "github": await db.fetchval("select github($1)", other.person),
        "status": await _ask(db, asker, other, greeting),
    }


async def _ask(
    db: Connection, asker: Address, other: Address, greeting: str | None
) -> str:
    unblocked = await _unblock(db, asker.person, other.person)
    if await _is_contact(db, asker.person, other.person):
        if unblocked:
            await _wake(db, [asker.person, other.person])  # they see each other again
        return "already your contact"
    if await db.fetchval(
        "select exists(select from blocks where blocker = $1 and blocked = $2)",
        other.person,
        asker.person,
    ):
        raise Refused(f"{other} does not take contact requests from you.")
    async with db.transaction():
        await _wake(db, [asker.person, other.person])
        if await db.fetchval(
            "delete from requests where kind = 'contact_request' and asker = $1 "
            "and target = $2 returning true",
            other.person,
            asker.person,
        ):
            await _make_contacts(db, asker.person, other.person)
            return "now your contact: they had asked you"
        await db.execute(
            "insert into requests (kind, asker, asker_agent, target, greeting) "
            "values ('contact_request', $1, $2, $3, $4) on conflict do nothing",
            asker.person,
            asker.agent,
            other.person,
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
            you.person,
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
        await _wake(db, [you.person, request["asker"]])
        if not accept:
            return "declined"
        if request["kind"] == "contact_request":
            # Accepting is adding them, so it lifts your block too.
            await _unblock(db, you.person, request["asker"])
            await _make_contacts(db, you.person, request["asker"])
            return "accepted: now your contact"
        group = Address(group=request["group_id"])
        if await db.fetchval(
            "select count(distinct person) >= $3 and not bool_or(person = $2) "
            "from group_members where group_id = $1",
            group.group,
            you.person,
            _MAX_GROUP_PEOPLE,
        ):
            raise Refused(f"{group} already has {_MAX_GROUP_PEOPLE} people.")
        await _join(db, group, you)
        await _pass_on_ownership(db, [group.group])  # if everyone else had left
        await _wake_group(db, group.group)
        return f"accepted: now in {group}"


async def remove_contact(db: Connection, person: int, other: Address) -> None:
    """Remove a contact and block them, so neither can write to the other."""
    async with db.transaction():
        await db.execute(
            f"delete from contacts where (a, b) = ({_PAIR})", person, other.person
        )
        await db.execute(
            "insert into blocks (blocker, blocked) values ($1, $2) "
            "on conflict do nothing",
            person,
            other.person,
        )
        await _wake(db, [person, other.person])


async def record_memberships(db: Connection, person: int, orgs: list[str]) -> None:
    async with db.transaction():
        await db.execute("delete from memberships where person = $1", person)
        await db.executemany(
            "insert into memberships (person, org) values ($1, $2)",
            [(person, org) for org in orgs],
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
        owner.person,
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
    db: Connection, person: int, id: int
) -> tuple[str, str, bytes]:
    """An attachment's name, type and bytes, for its owner or a recipient."""
    row = await db.fetchrow(
        "select name, mime, key from attachments a where id = $1 and (owner = $2 "
        "or exists (select from deliveries "
        "where message_id = a.message_id and recipient = $2))",
        id,
        person,
    )
    if row is None:
        raise Refused(f"No attachment {id} that you sent or received.")
    return row["name"], row["mime"], await bucket.get(row["key"])


async def delete_account(db: Connection, person: int) -> None:
    """Delete a person and what is theirs: their sign-ins and usernames, agents,
    contacts, blocks, remarks, organisation memberships, the requests they sent or
    received, every message they or their agents sent, every message sent directly
    to them or their agents, their copies of group messages, and their attachments,
    here and in the bucket. Groups they owned pass on. Everyone whose panel showed
    any of it is woken."""
    async with db.transaction():
        # The sender and every recipient of each message that loses a copy below,
        # in the groups the person already left too.
        affected = await db.fetch(
            "with gone as (select id, sender from messages "
            "where sender = $1 or recipient = $1 "
            "or id in (select message_id from deliveries where recipient = $1)) "
            "select sender as person from gone "
            "union select recipient from deliveries "
            f"where message_id in (select id from gone) union {_KNOWN_BY} "
            "union select asker from requests where target = $1 "
            "union select target from requests where asker = $1",
            person,
            _everyone_is_a_contact(),
        )
        await _wake(db, [row["person"] for row in affected])
        keys = await db.fetch("select key from attachments where owner = $1", person)
        groups = await db.fetch(
            "delete from group_members where person = $1 returning group_id", person
        )
        await _pass_on_ownership(db, list({row["group_id"] for row in groups}))
        await db.execute(
            "delete from messages where sender = $1 or recipient = $1", person
        )
        await db.execute("delete from deliveries where recipient = $1", person)
        await db.execute("delete from agents where owner = $1", person)
        # Sign-ins, usernames, contacts, blocks, remarks, memberships, requests and
        # attachments go with the user.
        await db.execute("delete from users where id = $1", person)
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
