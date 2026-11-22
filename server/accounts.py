"""Who people are: their accounts, the sign-ins that reach them, their usernames and
display names, and the addresses typed to reach them. The MCP tools and the panel call
the names without a leading _."""

from itertools import count

from asyncpg import Connection, UniqueViolationError

from mailbox import Address, Refused, wake_circle

MAX_USERNAME = 39  # characters, as in GitHub's logins


def github_claims(user: dict) -> dict:
    """The claims an MCP sign-in carries, from GitHub's /user: sub, the numeric id that
    names the person for good; login; and name. The panel signs in with them too, so
    both find the same account."""
    return {"sub": str(user["id"]), "login": user["login"], "name": user.get("name")}


async def sign_in(db: Connection, claims: dict) -> Address:
    """The address of the person these GitHub claims name. Their first sign-in makes
    their account."""
    person, username = await _account(
        db, "github", claims["sub"], claims["login"], claims.get("name")
    )
    return Address(person, username=username)


def _numbered(login: str, n: int) -> str:
    """login with -n added, cut so the whole fits in a username."""
    suffix = f"-{n}"
    return login[: MAX_USERNAME - len(suffix)].rstrip("-") + suffix


async def _account(
    db: Connection, provider: str, subject: str, login: str, name: str | None
) -> tuple[int, str]:
    """The id and username of the account that signs in as subject, made at its first
    sign-in with name as its display name and the first free one of login, login-2,
    login-3 and so on as its username. Keeps login, the provider's name for them,
    current."""
    for n in count(1):
        row = await db.fetchrow(
            "with refreshed as (update identities set login = $3 "
            "where (provider, subject) = ($1, $2) and login <> $3) "
            "select u.id, u.username from identities i join users u on u.id = i.user_id "
            "where (i.provider, i.subject) = ($1, $2)",
            provider,
            subject,
            login,
        )
        if row is not None:
            return row["id"], row["username"]
        username = login if n == 1 else _numbered(login, n)
        try:
            async with db.transaction():
                person = await db.fetchval(
                    "insert into users (username, name) values ($1, $2) returning id",
                    username,
                    name,
                )
                await db.execute(
                    "insert into usernames (username, user_id) values ($1, $2)",
                    username,
                    person,
                )
                await db.execute(
                    "insert into identities (provider, subject, user_id, login) "
                    "values ($1, $2, $3, $4)",
                    provider,
                    subject,
                    person,
                    login,
                )
            return person, username
        except UniqueViolationError:
            # The username is someone's, or a sign-in at the same time made this
            # account; the next round finds which.
            continue


async def resolve(db: Connection, text: str) -> Address:
    """The address text names: a group's `#id`, or a username in any case, with
    `/agent` for one of their agents. Refuses a username nobody holds, naming the
    close matches."""
    if text.startswith("#"):
        return Address(group=text.removeprefix("#"))
    username, _, agent = text.partition("/")
    row = await db.fetchrow(
        "select id, username from users where lower(username) = lower($1)", username
    )
    if row is None:
        candidates = await db.fetch(
            "select username from users where username ilike $1 or name ilike $1 "
            "order by username limit 10",
            f"%{username}%",
        )
        names = ", ".join(row["username"] for row in candidates) or "none"
        raise Refused(
            f"No one on peerbox has the username '{username}'. Close matches: {names}."
        )
    return Address(row["id"], agent or None, username=row["username"])


async def address(db: Connection, person: int, agent: str | None = None) -> Address:
    """The address of an account, or of one of its agents, under its username now."""
    username = await db.fetchval("select username from users where id = $1", person)
    return Address(person, agent, username=username)


async def profile(db: Connection, person: int) -> dict | None:
    """How a person shows: username, display name and GitHub login; None once their
    account is deleted."""
    row = await db.fetchrow(
        "select username, name, github(id) from users where id = $1", person
    )
    return row and dict(row)


async def set_profile(
    db: Connection, you: Address, username: str | None, name: str | None
) -> dict:
    """Change your username, display name or both. A username changes once a year,
    to one nobody else holds or held; one you gave up you may take back."""
    async with db.transaction():
        if username is not None and username != you.username:
            # Locked, so two changes at once can't both pass the yearly check.
            next_change = await db.fetchval(
                "select case when username_changed_at > now() - interval '1 year' "
                "then username_changed_at + interval '1 year' end "
                "from users where id = $1 for update",
                you.person,
            )
            if next_change is not None:
                raise Refused(
                    "A username changes once a year; yours can change again after "
                    f"{next_change:%Y-%m-%d}."
                )
            # Yours already, it takes the case given now; anyone else's, nothing.
            if not await db.fetchval(
                "insert into usernames (username, user_id) values ($1, $2) "
                "on conflict ((lower(username))) do update "
                "set username = excluded.username "
                "where usernames.user_id = excluded.user_id returning true",
                username,
                you.person,
            ):
                raise Refused(f"The username '{username}' is taken. Choose another.")
            await db.execute(
                "update users set username = $2, username_changed_at = now() "
                "where id = $1",
                you.person,
                username,
            )
        if name is not None:
            await db.execute(
                "update users set name = $2 where id = $1", you.person, name
            )
        await wake_circle(db, you.person)
    return await profile(db, you.person)
