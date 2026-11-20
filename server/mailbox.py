"""The message rules: addresses, contacts, deliveries, groups and attachments. The MCP
tools call these, and so will the panel."""

import os
from secrets import token_hex

from asyncpg import Connection
from fastmcp.exceptions import ToolError

import bucket

MAX_ATTACHMENT = 10 * 1024 * 1024
# A self-hosted instance admits only its organisation, so everyone is a contact.
SELF_HOSTED = bool(os.environ.get("GITHUB_ORG"))
# A writer may post to a group when their address, or they as a person, is a member.
# The messages that involve login's side, them and their agents, each with its
# conversation: the address on the other side, or the group.
MINE = """
    select m.*, case
        when m.group_id is not null then '#' || m.group_id
        when m.sender = $1 and not (m.recipient = $1 and m.recipient_agent is null)
            then concat_ws('/', m.recipient, m.recipient_agent)
        else concat_ws('/', m.sender, m.sender_agent)
    end as key
    from messages m
    where m.sender = $1 or m.id in (select message_id from deliveries where recipient = $1)
"""
IN_GROUP = (
    "select exists(select from group_members where group_id = $1 and login = $2 "
    "and (agent is null or agent = $3))"
)


class Refused(ToolError):
    """A rule the caller broke; the text says which and what to do instead."""


def address(login: str, agent: str | None) -> str:
    return "/".join(filter(None, [login, agent]))


def note(row) -> dict:
    """A message as an agent reads it."""
    if row["group_id"]:
        to = f"#{row['group_id']}"
    else:
        to = address(row["recipient"], row["recipient_agent"])
    return {
        "id": row["id"],
        "from": address(row["sender"], row["sender_agent"]),
        "to": to,
        "sent_at": row["created_at"],
        "reply_to": row["reply_to"],
        "text": row["body"],
    }


async def contact(db: Connection, login: str, other: str) -> bool:
    return await db.fetchval("select is_contact($1, $2, $3)", login, other, SELF_HOSTED)


async def known(db: Connection, login: str) -> None:
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


async def own_agent(db: Connection, login: str, agent: str) -> None:
    if not await db.fetchval(
        "select exists(select from agents where owner = $1 and name = $2)",
        login,
        agent,
    ):
        raise Refused(
            f"You have no agent named '{agent}' yet. Call inbox(agent='{agent}') "
            "first so messages can reach it."
        )


async def send(
    db: Connection,
    sender: str,
    sender_agent: str | None,
    to: str,
    text: str,
    reply_to: int | None = None,
    attachments: list[int] | None = None,
) -> dict:
    if sender_agent is not None:
        await own_agent(db, sender, sender_agent)
    group = login = agent_name = None
    status = "in their inbox"
    if to.startswith("#"):
        group = to[1:]
        if not await db.fetchval(IN_GROUP, group, sender, sender_agent):
            writer = address(sender, sender_agent)
            raise Refused(f"{writer} is not in {to}. Call contacts for your groups.")
    else:
        login, _, agent_name = to.partition("/")
        agent_name = agent_name or None
        await known(db, login)
        if not await contact(db, sender, login):
            raise Refused(
                f"{login} is not your contact. Ask with add_contact; you can write "
                "once they accept."
            )
        if agent_name:
            agent = await db.fetchrow(
                "select last_seen from agents where owner = $1 and name = $2",
                login,
                agent_name,
            )
            if agent is None:
                raise Refused(
                    f"{login} has no agent named '{agent_name}'. Call contacts."
                )
            status = f"in the agent's inbox; it last checked at {agent['last_seen']}"
    files = await sendable(db, sender, attachments or [])
    async with db.transaction():
        message_id = await db.fetchval(
            "insert into messages (sender, sender_agent, recipient, recipient_agent, "
            "group_id, body, reply_to) values ($1, $2, $3, $4, $5, $6, $7) returning id",
            sender,
            sender_agent,
            login,
            agent_name,
            group,
            text,
            reply_to,
        )
        if group is None:
            await db.execute(
                "insert into deliveries (message_id, recipient, recipient_agent) "
                "values ($1, $2, $3)",
                message_id,
                login,
                agent_name,
            )
        else:
            # Every member but the writing address, so one person's agents can
            # talk to each other; agents answer only what asks them in a group,
            # which keeps them from looping.
            reached = await db.fetchval(
                "with delivered as (insert into deliveries "
                "(message_id, recipient, recipient_agent) "
                "select $1, login, agent from group_members where group_id = $2 "
                "and not (login = $3 and agent is not distinct from $4) "
                "returning 1) select count(*) from delivered",
                message_id,
                group,
                sender,
                sender_agent,
            )
            status = f"in the inboxes of {reached} members"
        await db.execute(
            "update attachments a set message_id = $1, size = f.size "
            "from unnest($2::bigint[], $3::bigint[]) as f(id, size) where a.id = f.id",
            message_id,
            [id for id, _ in files],
            [size for _, size in files],
        )
        await db.execute("notify notes")
    return {"id": message_id, "to": to, "status": status}


async def sendable(db: Connection, owner: str, ids: list[int]) -> list[tuple]:
    """The writer's unsent attachments with their sizes. An upload is checked in the
    bucket, since its size is only known once it is there."""
    rows = await db.fetch(
        "select id, key, size from attachments "
        "where id = any($1) and owner = $2 and message_id is null",
        ids,
        owner,
    )
    if len(rows) < len(set(ids)):
        raise Refused(
            "Each attachment must come from your own attach and can be sent once."
        )
    files = []
    for row in rows:
        size = row["size"]
        if size is None:
            head = await bucket.request("HEAD", row["key"])
            if head.status_code == 404:
                raise Refused(
                    f"Attachment {row['id']} is not uploaded yet: PUT the file to "
                    "its upload_url first."
                )
            head.raise_for_status()
            size = int(head.headers["content-length"])
            if size > MAX_ATTACHMENT:
                raise Refused(f"Attachment {row['id']} is over 10 MB.")
        files.append((row["id"], size))
    return files


async def inbox(db: Connection, login: str, agent: str | None, limit: int) -> list:
    """Unread deliveries to login, or to one of its agents, oldest first."""
    return await db.fetch(
        "select m.id, m.sender, m.sender_agent, m.recipient, m.recipient_agent, "
        "m.group_id, m.body, m.reply_to, m.created_at "
        "from deliveries d join messages m on m.id = d.message_id "
        "where d.recipient = $1 and d.recipient_agent is not distinct from $2 "
        "and d.read_at is null order by d.id limit $3",
        login,
        agent,
        limit,
    )


async def waiting(db: Connection, login: str, agent: str | None, after: int) -> list:
    """What an event subscriber hears of, oldest first: unread deliveries and pending
    requests to login, or to one of its agents, past position after."""
    return await db.fetch(
        "select d.id as position, 'message' as kind, m.id, m.sender, m.sender_agent, "
        "m.recipient, m.recipient_agent, m.group_id, m.body, m.reply_to, m.created_at "
        "from deliveries d join messages m on m.id = d.message_id "
        "where d.recipient = $1 and d.recipient_agent is not distinct from $2 "
        "and d.read_at is null and d.id > $3 "
        "union all select id, kind, id, asker, asker_agent, target_login, "
        "target_agent, group_id, greeting, null, created_at from requests "
        "where target_login = $1 and target_agent is not distinct from $2 "
        "and id > $3 order by position",
        login,
        agent,
        after,
    )


async def notes(db: Connection, rows: list) -> list[dict]:
    """Messages as an agent reads them, each with its attachments' download links."""
    files = await db.fetch(
        "select id, message_id, name, mime, size, key from attachments "
        "where message_id = any($1) order by id",
        [row["id"] for row in rows],
    )
    by_message: dict[int, list[dict]] = {}
    for file in files:
        by_message.setdefault(file["message_id"], []).append(
            {
                "id": file["id"],
                "name": file["name"],
                "mime": file["mime"],
                "size": file["size"],
                "url": bucket.presign("GET", file["key"]),
            }
        )
    return [note(row) | {"attachments": by_message.get(row["id"], [])} for row in rows]


async def requests(db: Connection, login: str, agent: str | None) -> list[dict]:
    rows = await db.fetch(
        "select id, kind, asker, asker_agent, group_id, greeting from requests "
        "where target_login = $1 and target_agent is not distinct from $2 order by id",
        login,
        agent,
    )
    return [
        {
            "id": row["id"],
            "kind": row["kind"],
            "from": address(row["asker"], row["asker_agent"]),
            "group": f"#{row['group_id']}" if row["group_id"] else None,
            "greeting": row["greeting"],
        }
        for row in rows
    ]


async def mark_read(
    db: Connection, login: str, ids: list[int], agent: str | None = None
) -> int:
    # Without an agent it matches the person's deliveries and all their agents',
    # so clients that never name an agent here still clear their messages.
    marked = await db.fetchval(
        "with updated as (update deliveries set read_at = now() "
        "where recipient = $1 and message_id = any($2) and read_at is null "
        "and ($3::text is null or recipient_agent = $3) "
        "returning message_id) select count(distinct message_id) from updated",
        login,
        ids,
        agent,
    )
    if marked:
        await db.execute("notify notes")  # the panel shows who handled what
    return marked


async def conversations(db: Connection, login: str) -> dict:
    """login's conversations, newest first: each with its last message, the addresses
    of login's side in it and what waits there; and what waits in each of login's
    inboxes."""
    rows = await db.fetch(
        f"with mine as ({MINE}), "
        "sides as (select x.key, concat_ws('/', d.recipient, d.recipient_agent) as side, "
        "d.read_at is null as waits, x.created_at from mine x "
        "join deliveries d on d.message_id = x.id and d.recipient = $1 "
        "union all select key, concat_ws('/', sender, sender_agent), false, created_at "
        "from mine where sender = $1) "
        "select distinct on (x.key) x.key, x.sender, x.sender_agent, x.body, "
        "x.created_at, g.name, "
        "(select array_agg(distinct side) from sides s where s.key = x.key) as sides, "
        "(select count(*) from sides s where s.key = x.key and waits) as waiting, "
        "(select min(created_at) from sides s where s.key = x.key and waits) "
        "as waiting_since "
        "from mine x left join groups g on '#' || g.id = x.key "
        "order by x.key, x.id desc",
        login,
    )
    inboxes = await db.fetch(
        "select concat_ws('/', recipient, recipient_agent) as inbox, count(*) "
        "from deliveries where recipient = $1 and read_at is null group by 1",
        login,
    )
    listed = [
        {
            "key": row["key"],
            "name": row["name"],
            "last": {
                "from": address(row["sender"], row["sender_agent"]),
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


async def history(
    db: Connection, login: str, key: str, before: int | None, limit: int = 50
) -> dict:
    """One conversation's messages before message id before, oldest first, each with
    its attachments and the state of each of its deliveries."""
    rows = await db.fetch(
        f"with mine as ({MINE}) select * from mine where key = $2 "
        "and ($3::bigint is null or id < $3) order by id desc limit $4",
        login,
        key,
        before,
        limit + 1,
    )
    shown = rows[:limit][::-1]
    deliveries = await db.fetch(
        "select message_id, recipient, recipient_agent, read_at from deliveries "
        "where message_id = any($1) order by id",
        [row["id"] for row in shown],
    )
    by_message: dict[int, list[dict]] = {}
    for delivery in deliveries:
        by_message.setdefault(delivery["message_id"], []).append(
            {
                "to": address(delivery["recipient"], delivery["recipient_agent"]),
                "read_at": delivery["read_at"],
            }
        )
    return {
        "messages": [
            message | {"deliveries": by_message.get(message["id"], [])}
            for message in await notes(db, shown)
        ],
        "has_more": len(rows) > limit,
    }


async def directory(db: Connection, login: str) -> dict:
    users = await db.fetch(
        "select login, name, aliases from users where is_contact($1, login, $2) "
        "order by login",
        login,
        SELF_HOSTED,
    )
    agents = await db.fetch(
        "select owner, name, description, last_seen from agents "
        "where is_contact($1, owner, $2) order by name",
        login,
        SELF_HOSTED,
    )
    groups = await db.fetch(
        "select g.id, g.name, concat_ws('/', g.owner, g.owner_agent) as owner, "
        "array_agg(concat_ws('/', m.login, m.agent) "
        "order by m.joined_at) as members "
        "from groups g join group_members m on m.group_id = g.id "
        "where g.id in (select group_id from group_members where login = $1) "
        "group by g.id order by g.created_at",
        login,
    )
    by_owner: dict[str, list[dict]] = {}
    for agent in agents:
        by_owner.setdefault(agent["owner"], []).append(
            {
                "name": agent["name"],
                "description": agent["description"],
                "last_seen": agent["last_seen"],
            }
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
                "address": f"#{group['id']}",
                "name": group["name"],
                "owner": group["owner"],
                "members": group["members"],
            }
            for group in groups
        ],
    }


async def create_group(
    db: Connection, owner: str, owner_agent: str | None, name: str
) -> str:
    """Create a group owned by the creating address, its first member; returns
    the group's address."""
    if owner_agent is not None:
        await own_agent(db, owner, owner_agent)
    group = token_hex(4)
    await db.execute(
        "with created as (insert into groups (id, name, owner, owner_agent) "
        "values ($1, $2, $3, $4) returning id) "
        "insert into group_members (group_id, login, agent) "
        "select id, $3, $4 from created",
        group,
        name,
        owner,
        owner_agent,
    )
    return f"#{group}"


async def invite(
    db: Connection, inviter: str, inviter_agent: str | None, group: str, member: str
) -> str:
    """Add one of the inviter's own addresses, or invite a contact's."""
    if inviter_agent is not None:
        await own_agent(db, inviter, inviter_agent)
    if not await db.fetchval(IN_GROUP, group[1:], inviter, inviter_agent):
        writer = address(inviter, inviter_agent)
        raise Refused(f"{writer} is not in {group}. Call contacts for your groups.")
    login, _, agent = member.partition("/")
    agent = agent or None
    if login == inviter:
        if agent:
            await own_agent(db, inviter, agent)
        await db.execute(
            "insert into group_members (group_id, login, agent) values ($1, $2, $3) "
            "on conflict do nothing",
            group[1:],
            login,
            agent,
        )
        return "joined"
    await known(db, login)
    if not await contact(db, inviter, login):
        raise Refused(f"{login} is not your contact. Ask with add_contact first.")
    if agent and not await db.fetchval(
        "select exists(select from agents where owner = $1 and name = $2)", login, agent
    ):
        raise Refused(f"{login} has no agent named '{agent}'. Call contacts.")
    await db.execute(
        "insert into requests (kind, asker, asker_agent, target_login, target_agent, "
        "group_id) values ('invitation', $1, $2, $3, $4, $5) on conflict do nothing",
        inviter,
        inviter_agent,
        login,
        agent,
        group[1:],
    )
    await db.execute("notify notes")
    return "invited; it joins when they accept"


async def leave(
    db: Connection, actor: str, actor_agent: str | None, group: str, member: str
) -> None:
    """Remove member from group. Any address of the member's person may, and the
    owner address may remove anyone."""
    if actor_agent is not None:
        await own_agent(db, actor, actor_agent)
    login, _, agent = member.partition("/")
    async with db.transaction():
        # Locking the group makes members who leave at once still delete it
        # when it empties.
        owner = await db.fetchval(
            "select concat_ws('/', owner, owner_agent) from groups "
            "where id = $1 for update",
            group[1:],
        )
        if login != actor and owner != address(actor, actor_agent):
            raise Refused(
                f"Only the owner of {group} can remove other people's addresses."
            )
        if not await db.fetchval(
            "delete from group_members where group_id = $1 and login = $2 "
            "and agent is not distinct from $3 returning true",
            group[1:],
            login,
            agent or None,
        ):
            raise Refused(f"{member} is not in {group}.")
        await settle(db, [group[1:]])


async def settle(db: Connection, groups: list[str]) -> None:
    """After members leave: delete the groups left empty, and hand each group whose
    owner address left to the member that joined first."""
    await db.execute(
        "delete from groups g where id = any($1) "
        "and not exists (select from group_members where group_id = g.id)",
        groups,
    )
    await db.execute(
        "update groups g set (owner, owner_agent) = (select login, agent "
        "from group_members where group_id = g.id order by joined_at limit 1) "
        "where id = any($1) and not exists (select from group_members m "
        "where m.group_id = g.id and m.login = g.owner "
        "and m.agent is not distinct from g.owner_agent)",
        groups,
    )


async def add_contact(
    db: Connection, login: str, agent: str | None, other: str, greeting: str | None
) -> str:
    if agent is not None:
        await own_agent(db, login, agent)
    await known(db, other)
    await db.execute(
        "delete from blocks where blocker = $1 and blocked = $2", login, other
    )
    if await contact(db, login, other):
        return "already your contact"
    if await db.fetchval(
        "select exists(select from blocks where blocker = $1 and blocked = $2)",
        other,
        login,
    ):
        raise Refused(f"{other} does not take contact requests from you.")
    async with db.transaction():
        if await db.fetchval(
            "delete from requests where kind = 'contact_request' and asker = $1 "
            "and target_login = $2 returning true",
            other,
            login,
        ):
            await befriend(db, login, other)
            return "now your contact: they had asked you"
        await db.execute(
            "insert into requests (kind, asker, asker_agent, target_login, greeting) "
            "values ('contact_request', $1, $2, $3, $4) on conflict do nothing",
            login,
            agent,
            other,
            greeting,
        )
        await db.execute("notify notes")
    return "asked; they become your contact when they accept"


async def befriend(db: Connection, login: str, other: str) -> None:
    await db.execute(
        "insert into contacts (a, b) values (least($1::text, $2::text), "
        "greatest($1::text, $2::text)) on conflict do nothing",
        login,
        other,
    )


async def answer(
    db: Connection, login: str, agent: str | None, request_id: int, accept: bool
) -> str:
    if agent is not None:
        await own_agent(db, login, agent)
    async with db.transaction():
        request = await db.fetchrow(
            "delete from requests where id = $1 and target_login = $2 returning *",
            request_id,
            login,
        )
        if request is None:
            raise Refused(f"No request {request_id} for you; inbox lists yours.")
        if not accept:
            return "declined"
        if request["kind"] == "contact_request":
            # Accepting is adding them, so it lifts your block too.
            await db.execute(
                "delete from blocks where blocker = $1 and blocked = $2",
                login,
                request["asker"],
            )
            await befriend(db, login, request["asker"])
            return "accepted: now your contact"
        group = request["group_id"]
        if await db.fetchval(
            "select count(distinct login) >= 50 and not bool_or(login = $2) "
            "from group_members where group_id = $1",
            group,
            login,
        ):
            raise Refused(f"#{group} already has 50 people.")
        await db.execute(
            "insert into group_members (group_id, login, agent) values ($1, $2, $3) "
            "on conflict do nothing",
            group,
            login,
            request["target_agent"],
        )
        return f"accepted: now in #{group}"


async def remove_contact(db: Connection, login: str, other: str) -> None:
    """Remove a contact and block them, so neither can write to the other."""
    await known(db, other)
    async with db.transaction():
        await db.execute(
            "delete from contacts where a = least($1::text, $2::text) "
            "and b = greatest($1::text, $2::text)",
            login,
            other,
        )
        await db.execute(
            "insert into blocks (blocker, blocked) values ($1, $2) "
            "on conflict do nothing",
            login,
            other,
        )


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
    owner: str,
    owner_agent: str | None,
    name: str,
    mime: str,
    data: bytes | None,
) -> dict:
    """Store a file in the bucket or, with no data, hand out a URL to upload it to."""
    if owner_agent is not None:
        await own_agent(db, owner, owner_agent)
    key = token_hex(16)
    if data is not None:
        (await bucket.request("PUT", key, data)).raise_for_status()
    id = await db.fetchval(
        "insert into attachments (owner, owner_agent, name, mime, size, key) "
        "values ($1, $2, $3, $4, $5, $6) returning id",
        owner,
        owner_agent,
        name,
        mime,
        None if data is None else len(data),
        key,
    )
    if data is None:
        return {"id": id, "upload_url": bucket.presign("PUT", key)}
    return {"id": id, "name": name, "mime": mime, "size": len(data)}


async def open_attachment(db: Connection, login: str, id: int) -> tuple:
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
    response = await bucket.request("GET", row["key"])
    response.raise_for_status()
    return row["name"], row["mime"], response.content


async def delete_account(db: Connection, login: str) -> list[str]:
    """Delete a person with their agents, contacts, blocks, memberships, requests,
    attachments and the messages they sent or were sent directly; groups they owned
    pass on. Returns the bucket keys of their attachments, to delete there."""
    async with db.transaction():
        keys = await db.fetch("select key from attachments where owner = $1", login)
        groups = await db.fetch(
            "delete from group_members where login = $1 returning group_id", login
        )
        await settle(db, list({row["group_id"] for row in groups}))
        await db.execute(
            "delete from messages where sender = $1 or recipient = $1", login
        )
        await db.execute("delete from deliveries where recipient = $1", login)
        await db.execute("delete from agents where owner = $1", login)
        await db.execute("delete from users where login = $1", login)
    return [row["key"] for row in keys]


async def expire(db: Connection) -> None:
    """Delete messages older than 30 days, with their deliveries and attachments, and
    attachments never sent; the bucket's own 30-day rule deletes the files."""
    await db.execute(
        "delete from messages where created_at < now() - interval '30 days'"
    )
    await db.execute(
        "delete from attachments "
        "where message_id is null and created_at < now() - interval '30 days'"
    )
