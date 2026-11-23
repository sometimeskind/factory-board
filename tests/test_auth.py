"""Who the server takes a caller for: the real OIDCProxy and static tokens over HTTP against
a FastMCP app, and the per-call check on its own.

Every token, name and URL here is invented.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import asyncpg  # pyright: ignore[reportMissingTypeStubs]
import httpx
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken
from helpers import OPERATOR, as_tier
from key_value.aio.stores.memory import MemoryStore
from prometheus_client import REGISTRY
from psycopg.conninfo import conninfo_to_dict

from factory_board import auth as auth_module
from factory_board.auth import (
    SESSION_HEADER,
    Identity,
    build_auth,
    identity_resolver,
    postgres_store,
)
from factory_board.config import CLAUDE_REDIRECT, ServerSettings
from factory_board.store import Tier

BASE = "https://board.example.test"
ROUTE_HEADER = "X-Board-Route"
SESSION_TOKEN = "session-token-for-tests"
NIGHTLY_TOKEN = "nightly-token-for-tests"
GROUP = "factory-operators"
ALLOWED = (CLAUDE_REDIRECT, "http://localhost:*")

PRIVATE = {ROUTE_HEADER: "private"}
AS_SESSION = {"Authorization": f"Bearer {SESSION_TOKEN}"}
AS_NIGHTLY = {"Authorization": f"Bearer {NIGHTLY_TOKEN}"}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def issuer() -> Iterator[str]:
    """A stub Pocket ID: the discovery document and an empty key set."""
    holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = holder["url"]
            documents: dict[str, dict[str, Any]] = {
                "/.well-known/openid-configuration": {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/api/oidc/token",
                    "jwks_uri": f"{base}/.well-known/jwks.json",
                    "response_types_supported": ["code"],
                    "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                },
                "/.well-known/jwks.json": {"keys": []},
            }
            body = json.dumps(documents.get(self.path, {})).encode()
            self.send_response(200 if self.path in documents else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    holder["url"] = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield holder["url"]
    server.shutdown()


@pytest.fixture
def settings(issuer: str) -> ServerSettings:
    return ServerSettings(
        base_url=BASE,
        oidc_config_url=f"{issuer}/.well-known/openid-configuration",
        oidc_client_id="factory-board",
        oidc_client_secret="s3cret",
        jwt_signing_key="jwt-key",
        storage_key=Fernet.generate_key().decode(),
        operator_group=GROUP,
        allowed_redirects=ALLOWED,
        route_header=ROUTE_HEADER,
        session_token_sha256=sha256(SESSION_TOKEN),
        nightly_token_sha256=sha256(NIGHTLY_TOKEN),
    )


def describe(identity: Identity) -> dict[str, Any]:
    return {
        "tier": identity.caller.tier.value,
        "identity": identity.caller.identity,
        "operator_typed": identity.operator_typed,
    }


@asynccontextmanager
async def served(
    settings: ServerSettings, **overrides: Any
) -> AsyncGenerator[tuple[httpx.AsyncClient, MemoryStore, Any]]:
    """An app with one tool, `whoami`, over ASGI. A context manager in the test's own task,
    not a fixture: the app's lifespan can't be exited from a fixture's teardown task."""
    settings = replace(settings, **overrides)
    key_value = MemoryStore()
    auth, static = build_auth(settings, key_value)
    resolve = identity_resolver(settings.operator_group, static)
    mcp = FastMCP("test", auth=auth)

    @mcp.tool
    def whoami() -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        return describe(resolve())

    asgi = mcp.http_app()
    async with (
        asgi.router.lifespan_context(asgi),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url=BASE) as client,
    ):
        yield client, key_value, auth


def rpc_result(response: httpx.Response) -> dict[str, Any]:
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


async def whoami(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """Open an MCP session with these headers and call the tool; the tool's result."""
    response = await client.post("/mcp", json=INIT, headers=ACCEPT | headers)
    assert response.status_code == 200, response.text
    session = {"mcp-session-id": response.headers["mcp-session-id"]} | ACCEPT | headers
    await client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
    )
    response = await client.post(
        "/mcp",
        headers=session,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
    )
    assert response.status_code == 200, response.text
    return rpc_result(response)["result"]


def told(result: dict[str, Any]) -> dict[str, Any]:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


def refused(token: str) -> float:
    return (
        REGISTRY.get_sample_value("factory_board_static_token_refused_total", {"token": token})
        or 0.0
    )


# --- The static tokens over HTTP -------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {ROUTE_HEADER: "public"},
        {ROUTE_HEADER: "Private"},
        {ROUTE_HEADER: "private "},
        {ROUTE_HEADER: "private,private"},
        {"X-Other-Route": "private"},
    ],
)
@pytest.mark.parametrize(("name", "token"), [("session", AS_SESSION), ("nightly", AS_NIGHTLY)])
async def test_a_static_token_is_refused_off_the_private_route(
    settings: ServerSettings, headers: dict[str, str], name: str, token: dict[str, str]
) -> None:
    before = refused(name)
    async with served(settings) as (client, _, _):
        response = await client.post(
            "/mcp", json=INIT, headers=ACCEPT | headers | token | {SESSION_HEADER: "0a1b2c3d"}
        )
    assert response.status_code == 401
    assert refused(name) > before


async def test_a_static_token_is_refused_with_a_duplicated_route_header(
    settings: ServerSettings,
) -> None:
    headers = [
        *AS_SESSION.items(),
        (ROUTE_HEADER, "public"),
        (ROUTE_HEADER, "private"),
        *ACCEPT.items(),
    ]
    async with served(settings) as (client, _, _):
        response = await client.post("/mcp", json=INIT, headers=headers)
    assert response.status_code == 401


async def test_a_wrong_token_is_refused_on_the_private_route(settings: ServerSettings) -> None:
    wrong = [
        SESSION_TOKEN + "x",
        SESSION_TOKEN.upper(),
        "a" * 64,
        # The configured hash is not the token.
        sha256(SESSION_TOKEN),
        sha256(NIGHTLY_TOKEN),
    ]
    async with served(settings) as (client, _, _):
        for token in wrong:
            response = await client.post(
                "/mcp", json=INIT, headers=ACCEPT | PRIVATE | {"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401, token
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE)
        assert response.status_code == 401


async def test_the_session_token_is_a_session_at_the_agent_tier(settings: ServerSettings) -> None:
    async with served(settings) as (client, _, _):
        result = await whoami(client, PRIVATE | AS_SESSION | {SESSION_HEADER: "0a1b2c3d"})
    assert told(result) == {
        "tier": "board_agent",
        "identity": "session:0a1b2c3d",
        "operator_typed": True,
    }


@pytest.mark.parametrize(
    "session_id",
    [None, "", "has space", "colon:inside", "-leading-dash", "x" * 101],
)
async def test_the_session_token_needs_a_usable_session_id(
    settings: ServerSettings, session_id: str | None
) -> None:
    headers = PRIVATE | AS_SESSION
    if session_id is not None:
        headers = headers | {SESSION_HEADER: session_id}
    async with served(settings) as (client, _, _):
        result = await whoami(client, headers)
    assert result.get("isError"), result
    assert SESSION_HEADER in result["content"][0]["text"]


async def test_the_nightly_token_is_the_nightly_whatever_it_says(settings: ServerSettings) -> None:
    async with served(settings) as (client, _, _):
        result = await whoami(client, PRIVATE | AS_NIGHTLY | {SESSION_HEADER: "0a1b2c3d"})
    assert told(result) == {
        "tier": "board_process",
        "identity": "process:nightly",
        "operator_typed": False,
    }


async def test_an_unconfigured_token_is_off(settings: ServerSettings) -> None:
    async with served(settings, nightly_token_sha256=None) as (client, _, _):
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE | AS_NIGHTLY)
        assert response.status_code == 401
        result = await whoami(client, PRIVATE | AS_SESSION | {SESSION_HEADER: "0a1b2c3d"})
        assert told(result)["identity"] == "session:0a1b2c3d"

    async with served(settings, nightly_token_sha256=None, session_token_sha256=None) as (
        client,
        _,
        auth,
    ):
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE | AS_SESSION)
        assert response.status_code == 401
        assert type(auth).__name__ == "OIDCProxy"


# --- The login proxy -------------------------------------------------------------------

REGISTRATION: dict[str, Any] = {
    "client_name": "x",
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}


async def test_registration_admits_only_the_allowed_redirects_and_stores_encrypted(
    settings: ServerSettings,
) -> None:
    async with served(settings) as (client, key_value, _):
        metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
        assert "groups" in metadata["scopes_supported"]
        for bad in [
            "http://localhost.evil.example/cb",
            "http://localhost@evil.example/cb",
            "https://evil.example/cb",
            "https://claude.ai.evil.example/api/mcp/auth_callback",
            "http://claude.ai/api/mcp/auth_callback",
        ]:
            response = await client.post("/register", json=REGISTRATION | {"redirect_uris": [bad]})
            assert response.status_code == 400, (bad, response.text)
        for good in [CLAUDE_REDIRECT, "http://localhost:33418/callback"]:
            response = await client.post("/register", json=REGISTRATION | {"redirect_uris": [good]})
            assert response.status_code == 201, (good, response.text)

    # What the store holds, as the database would (MemoryStore keeps each entry's JSON).
    cache: Any = key_value._cache  # pyright: ignore[reportPrivateUsage]
    stored = [
        entry.json_str for collection in cache.values() for entry in collection._cache.values()
    ]
    assert stored, "the client registrations were not stored"
    assert not any("localhost:33418" in text or CLAUDE_REDIRECT in text for text in stored)


async def test_pocket_id_does_not_get_the_resource(settings: ServerSettings) -> None:
    async with served(settings, session_token_sha256=None, nightly_token_sha256=None) as (
        _,
        _,
        auth,
    ):
        # Pocket ID rejects a resource that isn't one of its APIs (a private FastMCP method:
        # the consent page sits between /authorize and the redirect).
        upstream = auth._build_upstream_authorize_url(
            "txn", {"resource": f"{BASE}/mcp", "scopes": ["openid", "groups"]}
        )
    query = httpx.URL(upstream).params
    assert "resource" not in query
    assert query["scope"] == "openid groups"


# --- The per-call check, on its own ----------------------------------------------------


def signed_in(monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any] | None) -> None:
    """Make the per-call check see a proxy-issued token with these claims, or no token."""
    token = (
        None
        if claims is None
        else AccessToken(token="issued-by-the-proxy", client_id="c", scopes=[], claims=claims)
    )
    monkeypatch.setattr(auth_module, "get_access_token", lambda: token)


def test_an_operator_in_the_group_is_a_human_at_the_operator_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed_in(monkeypatch, {"sub": "u-1", "preferred_username": "olive", "groups": [GROUP, "x"]})
    identity = identity_resolver(GROUP, None)()
    assert identity.caller.tier is Tier.OPERATOR
    assert identity.caller.identity == "human:olive"
    assert identity.operator_typed


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        (None, "not signed in"),
        ({"preferred_username": "olive"}, "not in the"),
        ({"preferred_username": "olive", "groups": []}, "not in the"),
        ({"preferred_username": "olive", "groups": ["factory-operators-2"]}, "not in the"),
        # A string that contains the group's name is not a list that holds it.
        ({"preferred_username": "olive", "groups": "factory-operators"}, "not in the"),
        ({"groups": [GROUP]}, "no usable username"),
        ({"preferred_username": "", "groups": [GROUP]}, "no usable username"),
        ({"preferred_username": "olive oyl", "groups": [GROUP]}, "no usable username"),
        ({"preferred_username": "process:nightly", "groups": [GROUP]}, "no usable username"),
        ({"preferred_username": 7, "groups": [GROUP]}, "no usable username"),
    ],
)
def test_a_sign_in_without_the_group_or_a_username_is_refused(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any] | None, message: str
) -> None:
    signed_in(monkeypatch, claims)
    with pytest.raises(ToolError, match=message):
        identity_resolver(GROUP, None)()


def test_a_static_name_in_a_proxy_token_gives_no_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the verifier's own answer names a static token; claims never do."""
    signed_in(monkeypatch, {"sub": "nightly", "client_id": "nightly"})
    with pytest.raises(ToolError, match="not in the"):
        identity_resolver(GROUP, None)()


# --- The proxy's table -----------------------------------------------------------------


async def test_the_proxy_store_works_as_board_app_and_nothing_else_does(
    migrated: None, app_url: str
) -> None:
    # asyncpg takes a URL or keywords, not the conninfo string the fixtures build.
    info = conninfo_to_dict(app_url)
    untyped: Any = asyncpg
    pool: Any = await untyped.create_pool(
        host=info["host"],
        port=info["port"],
        user=info["user"],
        password=info["password"],
        database=info["dbname"],
        min_size=1,
        max_size=1,
    )
    try:
        key_value = postgres_store(pool)
        await key_value.put(key="k", value={"v": 1}, collection="clients")
        assert await key_value.get(key="k", collection="clients") == {"v": 1}
        assert await key_value.delete(key="k", collection="clients")
        assert await key_value.get(key="k", collection="clients") is None
        # board_app still reads no item as itself.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.fetch("SELECT * FROM items")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await pool.execute("CREATE TABLE mine (id int)")
    finally:
        await pool.close()

    # No tier reads the proxy's rows.
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        as_tier(app_url, OPERATOR) as conn,
    ):
        conn.execute("SELECT * FROM oauth_proxy_kv")
