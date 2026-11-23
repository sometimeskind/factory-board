"""Who is calling, and at which tier.

Three ways in, and the tier belongs to the token:

- **The operator** signs in through FastMCP's OIDCProxy in front of Pocket ID (claude.ai).
  A user in the operator group is `human:<username>` at the operator tier.
- **The session token** (interactive sessions on pilot) is `session:<id>` at the agent
  tier. The ID comes from a request header and is asserted, not proven: every session
  holds the same token.
- **The nightly token** is `process:nightly` at the process tier.

A static token is accepted only when its SHA-256 matches the configured hash *and* the
route header (set by the private HTTPRoute, overwriting the client's) is exactly `private`.

All of it is checked again on every tool call, not only at the HTTP layer. Nothing here
reads a tool's input: the caller, and whether the operator typed into its run, come from
the token and the request.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token, get_http_request

from factory_board import metrics
from factory_board.config import ServerSettings
from factory_board.store import Caller, Tier

log = logging.getLogger(__name__)

SESSION = "session"
NIGHTLY = "nightly"

# The header an interactive session names itself with.
SESSION_HEADER = "X-Factory-Board-Session"

NIGHTLY_IDENTITY = "process:nightly"

# The part of an identity after the colon, as the database accepts it (set_pr_head_sha).
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


@dataclass(frozen=True)
class Identity:
    """The authenticated caller, and what the server knows about its run."""

    caller: Caller
    # The operator typed into the run that is calling: true for an interactive session.
    operator_typed: bool


class StaticTokens(TokenVerifier):
    """The session token and the nightly token, on the private route only."""

    def __init__(
        self,
        sha256_by_name: Mapping[str, str],
        route_header: str,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(required_scopes=required_scopes)
        self._sha256_by_name = {name: sha.lower() for name, sha in sha256_by_name.items()}
        self._route_header = route_header

    def _named(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        found: str | None = None
        # Every hash is compared, so the time taken does not say which one matched.
        for name, sha256 in self._sha256_by_name.items():
            if hmac.compare_digest(digest, sha256):
                found = name
        return found

    def _on_private_route(self) -> bool:
        try:
            request = get_http_request()
        except RuntimeError:
            return False
        # Exactly one value, exactly "private". A client-supplied duplicate never passes.
        return request.headers.getlist(self._route_header) == ["private"]

    def accepts(self, token: str) -> str | None:
        """The token's name (`session` or `nightly`), or None when it is not accepted."""
        name = self._named(token)
        if name is None:
            return None
        if not self._on_private_route():
            metrics.STATIC_TOKEN_REFUSED.labels(name).inc()
            log.warning(
                "the %s token was presented without %s: private; refused",
                name,
                self._route_header,
            )
            return None
        return name

    async def verify_token(self, token: str) -> AccessToken | None:
        name = self.accepts(token)
        if name is None:
            return None
        return AccessToken(
            token=token,
            client_id=name,
            # The HTTP layer enforces the server's required scopes on every token.
            scopes=list(self.required_scopes),
            expires_at=None,
            claims={"sub": name},
        )


def postgres_store(pool: Any) -> Any:
    """The login proxy's store: the oauth_proxy_kv table, which a migration creates."""
    from key_value.aio.stores.postgresql import PostgreSQLStore

    return PostgreSQLStore(pool=pool, table_name="oauth_proxy_kv", auto_create=False)


def build_auth(settings: ServerSettings, key_value: Any) -> tuple[Any, StaticTokens | None]:
    """OIDCProxy in front of Pocket ID, its state Fernet-encrypted in `key_value`
    (postgres_store in production), plus the static tokens that are configured.
    Returns (auth, static verifier)."""
    from cryptography.fernet import Fernet
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.oidc_proxy import OIDCProxy
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    proxy = OIDCProxy(
        config_url=settings.oidc_config_url,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        base_url=settings.base_url,
        # Pocket ID's id_token carries the groups and the username the tools check.
        verify_id_token=True,
        required_scopes=["openid", "profile", "email", "groups"],
        # Matched by parsed scheme, host and port, never as a string glob.
        allowed_client_redirect_uris=list(settings.allowed_redirects),
        # The client's `resource` names this server, whose tokens the proxy issues itself.
        # Pocket ID only logs the user in, and rejects a resource that isn't one of its APIs.
        forward_resource=False,
        jwt_signing_key=settings.jwt_signing_key,
        client_storage=FernetEncryptionWrapper(
            key_value=key_value,
            fernet=Fernet(settings.storage_key),
            # A rotated key reads old rows as missing: the operator reconnects, nothing crashes.
            raise_on_decryption_error=False,
        ),
    )
    hashes = {
        name: sha256
        for name, sha256 in (
            (SESSION, settings.session_token_sha256),
            (NIGHTLY, settings.nightly_token_sha256),
        )
        if sha256 is not None
    }
    if not hashes:
        return proxy, None
    static = StaticTokens(hashes, settings.route_header, proxy.required_scopes)
    return MultiAuth(server=proxy, verifiers={"static": static}), static


def _session_identity() -> Identity:
    try:
        values = get_http_request().headers.getlist(SESSION_HEADER)
    except RuntimeError:
        values = []
    if len(values) != 1 or not _ID.fullmatch(values[0]):
        raise ToolError(
            f"the session token needs one {SESSION_HEADER} header: the session's ID, as "
            "letters, digits, '.', '_' or '-'"
        )
    return Identity(Caller(Tier.AGENT, f"session:{values[0]}"), operator_typed=True)


def identity_resolver(operator_group: str, static: StaticTokens | None) -> Callable[[], Identity]:
    """The per-call identity check every tool runs before it touches the store."""

    def resolve() -> Identity:
        token = get_access_token()
        if token is None:
            raise ToolError("not signed in")
        if static is not None:
            name = static.accepts(token.token)
            if name == SESSION:
                return _session_identity()
            if name == NIGHTLY:
                return Identity(Caller(Tier.PROCESS, NIGHTLY_IDENTITY), operator_typed=False)
        claims = token.claims or {}
        groups = claims.get("groups")
        if not isinstance(groups, list) or operator_group not in groups:
            raise ToolError(f"your account is not in the {operator_group!r} group")
        username = claims.get("preferred_username")
        if not isinstance(username, str) or not _ID.fullmatch(username):
            raise ToolError("the sign-in carries no usable username")
        return Identity(Caller(Tier.OPERATOR, f"human:{username}"), operator_typed=True)

    return resolve
