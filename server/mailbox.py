"""The message rules: addresses, deliveries and groups. The MCP tools call these, and
so will the panel."""

from secrets import token_hex

from asyncpg import Connection
from fastmcp.exceptions import ToolError

# A writer may post to a group when their address, or they as a person, is a member.
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
        elif not await db.fetchval(
            "select exists(select 1 from users where login = $1)", login
        ):
            candidates = await db.fetch(
                "select login from users where login ilike $1 or name ilike $1 "
                "or exists (select 1 from unnest(aliases) alias where alias ilike $1)",
                f"%{login}%",
            )
            names = ", ".join(row["login"] for row in candidates) or "none"
            raise Refused(f"No teammate with login '{login}'. Close matches: {names}.")
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
        await db.execute("notify notes")
    return {"id": message_id, "to": to, "status": status}


async def inbox(
    db: Connection,
    login: str,
    agent: str | None,
    limit: int | None = None,
    after: int = 0,
) -> list:
    """Unread deliveries to login, or to one of its agents, oldest first."""
    return await db.fetch(
        "select d.id as delivery_id, m.id, m.sender, m.sender_agent, m.recipient, "
        "m.recipient_agent, m.group_id, m.body, m.reply_to, m.created_at "
        "from deliveries d join messages m on m.id = d.message_id "
        "where d.recipient = $1 and d.recipient_agent is not distinct from $2 "
        "and d.read_at is null and d.id > $3 order by d.id limit $4",
        login,
        agent,
        after,
        limit,
    )


async def mark_read(
    db: Connection, login: str, ids: list[int], agent: str | None = None
) -> int:
    # Without an agent it matches the person's deliveries and all their agents',
    # so clients that never name an agent here still clear their messages.
    return await db.fetchval(
        "with updated as (update deliveries set read_at = now() "
        "where recipient = $1 and message_id = any($2) and read_at is null "
        "and ($3::text is null or recipient_agent = $3) "
        "returning message_id) select count(distinct message_id) from updated",
        login,
        ids,
        agent,
    )


async def directory(db: Connection, login: str) -> dict:
    users = await db.fetch("select login, name, aliases from users order by login")
    agents = await db.fetch(
        "select owner, name, description, last_seen from agents order by name"
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
) -> None:
    login, _, agent = member.partition("/")
    if login != inviter:
        raise Refused(
            f"For now you can only add your own addresses. Inviting {login} arrives "
            "in the next version of peerbox."
        )
    for own in filter(None, [inviter_agent, agent]):
        await own_agent(db, inviter, own)
    if not await db.fetchval(IN_GROUP, group[1:], inviter, inviter_agent):
        writer = address(inviter, inviter_agent)
        raise Refused(f"{writer} is not in {group}. Call contacts for your groups.")
    await db.execute(
        "insert into group_members (group_id, login, agent) values ($1, $2, $3) "
        "on conflict do nothing",
        group[1:],
        login,
        agent or None,
    )


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
        await db.execute(
            "delete from groups where id = $1 "
            "and not exists (select from group_members where group_id = $1)",
            group[1:],
        )
        await db.execute(
            "update groups g set (owner, owner_agent) = (select login, agent "
            "from group_members where group_id = g.id order by joined_at limit 1) "
            "where id = $1 and not exists (select from group_members m "
            "where m.group_id = g.id and m.login = g.owner "
            "and m.agent is not distinct from g.owner_agent)",
            group[1:],
        )


async def expire(db: Connection) -> None:
    """Delete messages older than 30 days, with their deliveries."""
    await db.execute(
        "delete from messages where created_at < now() - interval '30 days'"
    )
