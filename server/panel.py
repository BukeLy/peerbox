"""The panel: a read-only page where a person watches the conversations that involve
them and their agents. GitHub sign-in sets an encrypted session cookie; JSON routes
and a server-sent event stream feed the page. Nothing is written here, and viewing
never marks a message read. server.py installs its routes."""

import hmac
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx2
from cryptography.fernet import InvalidToken
from pydantic_core import to_json
from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

import instance
import mailbox

HERE = Path(__file__).parent
CALLBACK_URL = f"{instance.BASE_URL}/login/callback"
SESSION_KEY = instance.fernet("peerbox-panel-session")
SESSION_SECONDS = 8 * 3600
PING_SECONDS = 30  # keeps proxies from closing a quiet stream
COOKIE = {"httponly": True, "secure": True, "samesite": "lax"}
FILES = {path.name: path for path in (HERE / "panel").iterdir()} | {
    "icon.png": HERE / "icon.png"
}


async def changes(login: str):
    """Server-sent events telling login's page to refetch: one whenever something of
    theirs changes, such as a message or request sent, answered or read."""
    with instance.waiters.watch(login) as woken:
        while True:
            await woken.wait()
            woken.clear()
            yield {"event": "changed", "data": ""}


def install(mcp) -> None:
    """Add the panel's routes to mcp."""

    def viewer(request: Request) -> str | None:
        """The signed-in person's login, from the session cookie."""
        try:
            return SESSION_KEY.decrypt(
                request.cookies.get("peerbox_session", ""), ttl=SESSION_SECONDS
            ).decode()
        except InvalidToken:
            return None

    def signed_out() -> Response:
        return Response(
            to_json({"error": "Sign in first."}),
            status_code=401,
            media_type="application/json",
        )

    async def page(request: Request) -> Response:
        return FileResponse(HERE / "panel" / "index.html")

    async def asset(request: Request) -> Response:
        path = FILES.get(request.path_params["name"])
        return FileResponse(path) if path else Response(status_code=404)

    async def login(request: Request) -> Response:
        state = token_urlsafe(16)
        query = urlencode(
            {
                "client_id": instance.GITHUB_CLIENT_ID,
                "redirect_uri": CALLBACK_URL,
                "state": state,
            }
        )
        response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")
        response.set_cookie("peerbox_state", state, max_age=600, **COOKIE)
        return response

    async def callback(request: Request) -> Response:
        """Sign the person in with the same admission as MCP sign-ins."""
        state = request.cookies.get("peerbox_state", "")
        if not state or not hmac.compare_digest(
            state, request.query_params.get("state", "")
        ):
            return Response("This sign-in expired. Sign in again.", status_code=400)
        async with httpx2.AsyncClient(
            timeout=10, headers={"Accept": "application/json"}
        ) as github:
            exchanged = await github.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": instance.GITHUB_CLIENT_ID,
                    "client_secret": instance.GITHUB_CLIENT_SECRET,
                    "code": request.query_params.get("code", ""),
                    "redirect_uri": CALLBACK_URL,
                },
            )
            token = exchanged.json().get("access_token")
            if not token:
                return Response("GitHub did not sign you in.", status_code=400)
            user = await github.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}"},
            )
        login = user.json()["login"]
        if not await instance.admit(token, login):
            return Response(
                f"{login} is not admitted to this peerbox.", status_code=403
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "peerbox_session",
            SESSION_KEY.encrypt(login.encode()).decode(),
            max_age=SESSION_SECONDS,
            **COOKIE,
        )
        response.delete_cookie("peerbox_state", **COOKIE)
        return response

    async def logout(request: Request) -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("peerbox_session", **COOKIE)
        return response

    def api(read):
        """A JSON route that calls read(db, login, request) for the signed-in person."""

        async def route(request: Request) -> Response:
            login = viewer(request)
            if login is None:
                return signed_out()
            async with instance.get_db() as db:
                data = await read(db, login, request)
            return Response(to_json(data), media_type="application/json")

        return route

    async def me(db, login: str, request: Request) -> dict:
        return {"login": login} | await mailbox.contacts_and_groups(db, login)

    async def conversations(db, login: str, request: Request) -> dict:
        return await mailbox.conversations(db, login)

    async def history(db, login: str, request: Request) -> dict:
        before = request.query_params.get("before", "")
        return await mailbox.history(
            db,
            login,
            request.path_params["key"],
            int(before) if before.isdigit() else None,
        )

    async def stream(request: Request) -> Response:
        """Tells the page to refetch whenever something of its person's changes."""
        login = viewer(request)
        if login is None:
            return signed_out()
        return EventSourceResponse(changes(login), ping=PING_SECONDS)

    routes = [
        ("/", "GET", page),
        ("/panel/{name}", "GET", asset),
        ("/login", "GET", login),
        ("/login/callback", "GET", callback),
        ("/logout", "POST", logout),
        ("/api/me", "GET", api(me)),
        ("/api/conversations", "GET", api(conversations)),
        ("/api/conversations/{key:path}", "GET", api(history)),
        ("/api/stream", "GET", stream),
    ]
    for path, method, handler in routes:
        mcp.custom_route(path, methods=[method])(handler)
