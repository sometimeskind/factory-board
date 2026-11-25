"""Settings, read from the environment once at start-up."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# A source checkout keeps migrations/ beside the package. The image installs the package
# into a venv, so its Dockerfile sets FACTORY_BOARD_MIGRATIONS_DIR instead.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class ConfigError(Exception):
    """A setting is missing or unusable."""


SERVE_URL_VAR = "FACTORY_BOARD_DATABASE_URL"
MIGRATE_URL_VAR = "FACTORY_BOARD_MIGRATE_DATABASE_URL"


@dataclass(frozen=True)
class Settings:
    """The two database URLs are for two roles, and a process needs only its own.

    The server connects as board_app, which can do nothing until it takes a tier. The
    migrations connect as the role that owns the tables. Neither URL is read at start-up:
    ask for the one the command needs, and it fails by name if it is missing.
    """

    database_url: str
    migrate_database_url: str
    migrations_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        migrations_dir = env.get("FACTORY_BOARD_MIGRATIONS_DIR", "")
        return cls(
            database_url=env.get(SERVE_URL_VAR, ""),
            migrate_database_url=env.get(MIGRATE_URL_VAR, ""),
            migrations_dir=Path(migrations_dir) if migrations_dir else DEFAULT_MIGRATIONS_DIR,
        )

    def serve_url(self) -> str:
        """The URL the store connects with, as board_app."""
        if not self.database_url:
            raise ConfigError(f"{SERVE_URL_VAR} is not set")
        return self.database_url

    def migrate_url(self) -> str:
        """The URL the migrations run with, as the tables' owner."""
        if not self.migrate_database_url:
            raise ConfigError(f"{MIGRATE_URL_VAR} is not set")
        return self.migrate_database_url


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_IDENTITY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_HEADER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")

CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@dataclass(frozen=True)
class ServerSettings:
    """What the server needs to know who is calling. All of it is required to serve.

    The operator signs in through Pocket ID and must be in `operator_group`. The static
    tokens are configured as SHA-256 hashes only, and are accepted only on a request whose
    `route_header` says `private`: the private HTTPRoute sets that header, overwriting
    whatever the client sent, and the public one sets `public`.
    """

    base_url: str
    oidc_config_url: str
    oidc_client_id: str
    oidc_client_secret: str
    jwt_signing_key: str
    storage_key: str
    operator_group: str
    allowed_redirects: tuple[str, ...]
    route_header: str
    # The operator's Pocket ID username: a new item for the operator is held by
    # `human:<operator>`, whoever wrote it.
    operator: str
    # Interactive sessions on pilot share one token; the nightly, the reconciler and the
    # dispatcher have one each.
    session_token_sha256: str | None = None
    nightly_token_sha256: str | None = None
    reconciler_token_sha256: str | None = None
    dispatcher_token_sha256: str | None = None
    # "" binds every address of both families (uvicorn makes "::" IPv6-only).
    host: str = ""
    port: int = 8000
    metrics_port: int = 9090

    def __post_init__(self) -> None:
        if not _IDENTITY_NAME.fullmatch(self.operator):
            raise ConfigError("FACTORY_BOARD_OPERATOR is not a username")
        hashes = (
            ("FACTORY_BOARD_SESSION_TOKEN_SHA256", self.session_token_sha256),
            ("FACTORY_BOARD_NIGHTLY_TOKEN_SHA256", self.nightly_token_sha256),
            ("FACTORY_BOARD_RECONCILER_TOKEN_SHA256", self.reconciler_token_sha256),
            ("FACTORY_BOARD_DISPATCHER_TOKEN_SHA256", self.dispatcher_token_sha256),
        )
        seen: dict[str, str] = {}
        for name, value in hashes:
            if value is None:
                continue
            if not _SHA256_HEX.fullmatch(value):
                raise ConfigError(f"{name} must be 64 lower-case hex characters")
            # One token for two identities would give each the other's tools.
            if value in seen:
                raise ConfigError(f"{seen[value]} and {name} must differ")
            seen[value] = name
        if not _HEADER_NAME.fullmatch(self.route_header):
            raise ConfigError("FACTORY_BOARD_ROUTE_HEADER is not a header name")
        if not self.allowed_redirects:
            raise ConfigError("FACTORY_BOARD_ALLOWED_REDIRECTS names no redirect URI")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServerSettings:
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is not set")
            return value

        def optional(name: str) -> str | None:
            return env.get(name, "").strip() or None

        def port(name: str, default: int) -> int:
            try:
                return int(env.get(name, "").strip() or default)
            except ValueError:
                raise ConfigError(f"{name} is not a number") from None

        redirects = env.get("FACTORY_BOARD_ALLOWED_REDIRECTS", CLAUDE_REDIRECT)
        return cls(
            base_url=required("FACTORY_BOARD_BASE_URL"),
            oidc_config_url=required("FACTORY_BOARD_OIDC_CONFIG_URL"),
            oidc_client_id=required("FACTORY_BOARD_OIDC_CLIENT_ID"),
            oidc_client_secret=required("FACTORY_BOARD_OIDC_CLIENT_SECRET"),
            jwt_signing_key=required("FACTORY_BOARD_JWT_SIGNING_KEY"),
            storage_key=required("FACTORY_BOARD_STORAGE_KEY"),
            operator_group=env.get("FACTORY_BOARD_OPERATOR_GROUP", "").strip()
            or "factory-operators",
            allowed_redirects=tuple(uri.strip() for uri in redirects.split(",") if uri.strip()),
            route_header=required("FACTORY_BOARD_ROUTE_HEADER"),
            operator=required("FACTORY_BOARD_OPERATOR"),
            port=port("FACTORY_BOARD_PORT", 8000),
            metrics_port=port("FACTORY_BOARD_METRICS_PORT", 9090),
            session_token_sha256=optional("FACTORY_BOARD_SESSION_TOKEN_SHA256"),
            nightly_token_sha256=optional("FACTORY_BOARD_NIGHTLY_TOKEN_SHA256"),
            reconciler_token_sha256=optional("FACTORY_BOARD_RECONCILER_TOKEN_SHA256"),
            dispatcher_token_sha256=optional("FACTORY_BOARD_DISPATCHER_TOKEN_SHA256"),
        )
