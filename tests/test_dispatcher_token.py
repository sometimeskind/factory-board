"""The dispatcher's token: its setting, and who the server takes its bearer for.

The app, the stub issuer and the calls are test_auth.py's. Every token here is invented.
"""

from __future__ import annotations

import pytest
import test_auth
from test_auth import ACCEPT, INIT, PRIVATE, ROUTE_HEADER, served, sha256, told, whoami
from test_config import SERVER_ENV

from factory_board.auth import SESSION_HEADER
from factory_board.config import ConfigError, ServerSettings

DISPATCHER_TOKEN = "dispatcher-token-for-tests"
AS_DISPATCHER = {"Authorization": f"Bearer {DISPATCHER_TOKEN}"}
VARIABLE = "FACTORY_BOARD_DISPATCHER_TOKEN_SHA256"

# test_auth.py's fixtures, under their own names: the stub issuer, and settings that carry
# the session's and the nightly's token and not the dispatcher's.
issuer = test_auth.issuer
settings = test_auth.settings


def test_the_dispatcher_s_hash_is_read_and_is_off_when_unset() -> None:
    assert ServerSettings.from_env(SERVER_ENV).dispatcher_token_sha256 is None
    read = ServerSettings.from_env(SERVER_ENV | {VARIABLE: "d" * 64})
    assert read.dispatcher_token_sha256 == "d" * 64
    assert (read.session_token_sha256, read.nightly_token_sha256) == (None, None)
    assert read.reconciler_token_sha256 is None


@pytest.mark.parametrize("value", ["abc", "D" * 64, "d" * 63, "d" * 65, "g" * 64])
def test_a_malformed_dispatcher_hash_is_refused(value: str) -> None:
    with pytest.raises(ConfigError, match="DISPATCHER_TOKEN_SHA256 must be"):
        ServerSettings.from_env(SERVER_ENV | {VARIABLE: value})


@pytest.mark.parametrize("other", ["SESSION", "NIGHTLY", "RECONCILER"])
def test_the_dispatcher_s_hash_must_differ_from_every_other(other: str) -> None:
    same = {VARIABLE: "d" * 64, f"FACTORY_BOARD_{other}_TOKEN_SHA256": "d" * 64}
    with pytest.raises(ConfigError, match="must differ"):
        ServerSettings.from_env(SERVER_ENV | same)
    different = same | {VARIABLE: "e" * 64}
    assert ServerSettings.from_env(SERVER_ENV | different).dispatcher_token_sha256 == "e" * 64


async def test_the_dispatcher_token_is_the_dispatcher_at_the_process_tier(
    settings: ServerSettings,
) -> None:
    configured = {"dispatcher_token_sha256": sha256(DISPATCHER_TOKEN)}
    async with served(settings, **configured) as (client, _, _):
        # Whatever session it says it is.
        result = await whoami(client, PRIVATE | AS_DISPATCHER | {SESSION_HEADER: "0a1b2c3d"})
        assert told(result) == {
            "tier": "board_process",
            "identity": "process:dispatcher",
            "operator_typed": False,
        }
        # The other tokens are who they were.
        result = await whoami(client, PRIVATE | test_auth.AS_NIGHTLY)
        assert told(result)["identity"] == "process:nightly"


@pytest.mark.parametrize(
    "headers",
    [{}, {ROUTE_HEADER: "public"}, {ROUTE_HEADER: "Private"}, {"X-Other-Route": "private"}],
)
async def test_the_dispatcher_token_is_refused_off_the_private_route(
    settings: ServerSettings, headers: dict[str, str]
) -> None:
    before = test_auth.refused("dispatcher")
    configured = {"dispatcher_token_sha256": sha256(DISPATCHER_TOKEN)}
    async with served(settings, **configured) as (client, _, _):
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | headers | AS_DISPATCHER)
    assert response.status_code == 401
    assert test_auth.refused("dispatcher") > before


async def test_the_dispatcher_token_is_off_when_unset(settings: ServerSettings) -> None:
    assert settings.dispatcher_token_sha256 is None
    async with served(settings) as (client, _, _):
        response = await client.post("/mcp", json=INIT, headers=ACCEPT | PRIVATE | AS_DISPATCHER)
        assert response.status_code == 401
        # And its hash is not its token.
    configured = {"dispatcher_token_sha256": sha256(DISPATCHER_TOKEN)}
    async with served(settings, **configured) as (client, _, _):
        for wrong in (sha256(DISPATCHER_TOKEN), DISPATCHER_TOKEN + "x"):
            response = await client.post(
                "/mcp", json=INIT, headers=ACCEPT | PRIVATE | {"Authorization": f"Bearer {wrong}"}
            )
            assert response.status_code == 401, wrong
