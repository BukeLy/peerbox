"""Who people are: their accounts, the sign-ins that reach them, their usernames and
display names, and the addresses typed to reach them. Other modules call the names
without a leading _."""

import re
from itertools import count

from asyncpg import Connection, UniqueViolationError

from mailbox import Address, Refused, wake_known_by

# Letters, digits and single hyphens, as in GitHub's logins; matched in any case.
USERNAME = r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*"
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


def _username(login: str, n: int) -> str:
    """The nth username to try for login: login in the username format (a run of other
    characters, as in old or enterprise logins, becomes one hyphen), with -n added
    from the second on, cut to fit."""
    base = re.sub(r"[^A-Za-z0-9]+", "-", login).strip("-") or "user"
    suffix = "" if n == 1 else f"-{n}"
    return base[: MAX_USERNAME - len(suffix)].rstrip("-") + suffix


async def _account(
    db: Connection, provider: str, subject: str, login: str, name: str | None
) -> tuple[int, str]:
    """The id and username of the account that signs in as subject, made at its first
    sign-in with name as its display name and the first free one of login, login-2,
    login-3 and so on, in the username format, as its username. Keeps login, the
    provider's name for them, current."""
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
        username = _username(login, n)
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
    close matches with their GitHub logins, since anyone may take any free username."""
    if text.startswith("#"):
        return Address(group=text.removeprefix("#"))
    username, _, agent = text.partition("/")
    row = await db.fetchrow(
        "select id, username from users where lower(username) = lower($1)", username
    )
    if row is None:
        candidates = await db.fetch(
            "select username, github(id) from users "
            "where username ilike $1 or name ilike $1 order by username limit 10",
            f"%{username}%",
        )
        names = (
            ", ".join(
                f"{row['username']} (GitHub {row['github']})" for row in candidates
            )
            or "none"
        )
        raise Refused(
            f"No one on peerbox has the username '{username}'. Close matches: {names}."
        )
    return Address(row["id"], agent or None, username=row["username"])


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
    to one nobody else holds or held; one you gave up you may take back. Changing only
    its case is free."""
    async with db.transaction():
        if username is not None:
            # Locked, so two changes at once can't both pass the yearly check.
            now = await db.fetchrow(
                "select username, case when username_changed_at > now() - "
                "interval '1 year' then username_changed_at + interval '1 year' end "
                "as next_change from users where id = $1 for update",
                you.person,
            )
            renamed = username.lower() != now["username"].lower()
            if renamed and now["next_change"] is not None:
                raise Refused(
                    "A username changes once a year; yours can change again after "
                    f"{now['next_change']:%Y-%m-%d}."
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
                "update users set username = $2, username_changed_at = "
                "case when $3 then now() else username_changed_at end where id = $1",
                you.person,
                username,
                renamed,
            )
        if name is not None:
            await db.execute(
                "update users set name = $2 where id = $1", you.person, name
            )
        await wake_known_by(db, you.person)
    return await profile(db, you.person)
