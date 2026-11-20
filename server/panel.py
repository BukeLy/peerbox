"""The panel: a read-only page where a person watches the conversations that involve
them and their agents. GitHub sign-in sets an encrypted session cookie; JSON routes
and a server-sent event stream feed the page. Nothing is written here, and viewing
never marks a message read. server.py imports this module to register its routes
before it builds the app."""

import asyncio
import hmac
from base64 import urlsafe_b64encode
from secrets import token_urlsafe
from urllib.parse import urlencode

import httpx2
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastmcp.server.auth import AccessToken
from pydantic_core import to_json
from sse_starlette import EventSourceResponse
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse, Response

import mailbox
import server
from server import (
    BASE_URL,
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    HEARTBEAT_SECONDS,
    HERE,
    mcp,
)

SESSION_SECONDS = 8 * 3600
SESSION = Fernet(
    urlsafe_b64encode(
        HKDF(SHA256(), 32, salt=None, info=b"peerbox-panel-session").derive(
            GITHUB_CLIENT_SECRET.encode()
        )
    )
)
CALLBACK = f"{BASE_URL}/login/callback"
COOKIE = {"httponly": True, "secure": True, "samesite": "lax"}
FILES = {path.name: path for path in (HERE / "panel").iterdir()} | {
    "icon.png": HERE / "icon.png"
}


def viewer(request: Request) -> str | None:
    """The signed-in person's login, from the session cookie."""
    try:
        return SESSION.decrypt(
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


@mcp.custom_route("/", methods=["GET"])
async def page(request: Request) -> Response:
    return FileResponse(HERE / "panel" / "index.html")


@mcp.custom_route("/panel/{name}", methods=["GET"])
async def asset(request: Request) -> Response:
    path = FILES.get(request.path_params["name"])
    return FileResponse(path) if path else Response(status_code=404)


@mcp.custom_route("/login", methods=["GET"])
async def login(request: Request) -> Response:
    state = token_urlsafe(16)
    query = urlencode(
        {"client_id": GITHUB_CLIENT_ID, "redirect_uri": CALLBACK, "state": state}
    )
    response = RedirectResponse(f"https://github.com/login/oauth/authorize?{query}")
    response.set_cookie("peerbox_state", state, max_age=600, **COOKIE)
    return response


@mcp.custom_route("/login/callback", methods=["GET"])
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
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": request.query_params.get("code", ""),
                "redirect_uri": CALLBACK,
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
    if not await server.is_member(
        AccessToken(
            token=token, client_id=GITHUB_CLIENT_ID, scopes=[], claims={"login": login}
        )
    ):
        return Response(f"{login} is not admitted to this peerbox.", status_code=403)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "peerbox_session",
        SESSION.encrypt(login.encode()).decode(),
        max_age=SESSION_SECONDS,
        **COOKIE,
    )
    response.delete_cookie("peerbox_state", **COOKIE)
    return response


@mcp.custom_route("/logout", methods=["POST"])
async def logout(request: Request) -> Response:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("peerbox_session", **COOKIE)
    return response


def api(path: str):
    """A JSON route for the signed-in person: the handler gets (db, login, request)."""

    def register(handler):
        @mcp.custom_route(path, methods=["GET"])
        async def route(request: Request) -> Response:
            login = viewer(request)
            if login is None:
                return signed_out()
            async with server.pool.acquire() as db:
                data = await handler(db, login, request)
            return Response(to_json(data), media_type="application/json")

        return handler

    return register


@api("/api/me")
async def me(db, login: str, request: Request) -> dict:
    return {"login": login} | await mailbox.directory(db, login)


@api("/api/conversations")
async def conversations(db, login: str, request: Request) -> dict:
    return await mailbox.conversations(db, login)


@api("/api/conversations/{key:path}")
async def history(db, login: str, request: Request) -> dict:
    before = request.query_params.get("before", "")
    return await mailbox.history(
        db,
        login,
        request.path_params["key"],
        int(before) if before.isdigit() else None,
    )


@mcp.custom_route("/api/stream", methods=["GET"])
async def stream(request: Request) -> Response:
    """Tells the page to refetch whenever a message is sent or read."""
    if viewer(request) is None:
        return signed_out()

    async def changes():
        woken = asyncio.Event()
        server.waiters.add(woken)
        try:
            while True:
                await woken.wait()
                woken.clear()
                yield {"event": "changed", "data": ""}
        finally:
            server.waiters.discard(woken)

    return EventSourceResponse(changes(), ping=HEARTBEAT_SECONDS)
