"""Settings from the environment, and the migrate command that reads them."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from factory_board.__main__ import main
from factory_board.config import DEFAULT_MIGRATIONS_DIR, ConfigError, ServerSettings, Settings


def test_each_url_is_required_only_when_asked_for() -> None:
    settings = Settings.from_env({})
    with pytest.raises(ConfigError, match="FACTORY_BOARD_DATABASE_URL is not set"):
        settings.serve_url()
    with pytest.raises(ConfigError, match="FACTORY_BOARD_MIGRATE_DATABASE_URL is not set"):
        settings.migrate_url()


def test_the_two_urls_are_separate() -> None:
    settings = Settings.from_env(
        {
            "FACTORY_BOARD_DATABASE_URL": "postgresql://board_app@example/board",
            "FACTORY_BOARD_MIGRATE_DATABASE_URL": "postgresql://board_owner@example/board",
        }
    )
    assert settings.serve_url() == "postgresql://board_app@example/board"
    assert settings.migrate_url() == "postgresql://board_owner@example/board"


def test_migrate_does_not_need_the_serve_url() -> None:
    settings = Settings.from_env(
        {"FACTORY_BOARD_MIGRATE_DATABASE_URL": "postgresql://board_owner@example/board"}
    )
    assert settings.migrate_url() == "postgresql://board_owner@example/board"
    with pytest.raises(ConfigError):
        settings.serve_url()


def test_migrations_dir_defaults_to_the_checkout() -> None:
    settings = Settings.from_env({})
    assert settings.migrations_dir == DEFAULT_MIGRATIONS_DIR
    assert (settings.migrations_dir / "README.md").is_file()


def test_migrations_dir_can_be_set() -> None:
    settings = Settings.from_env({"FACTORY_BOARD_MIGRATIONS_DIR": "/somewhere/else"})
    assert settings.migrations_dir == Path("/somewhere/else")


def test_migrate_command(
    database_url: str,
    conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "0001_widgets.sql").write_text("CREATE TABLE widgets (id integer);")
    monkeypatch.setenv("FACTORY_BOARD_MIGRATE_DATABASE_URL", database_url)
    monkeypatch.setenv("FACTORY_BOARD_MIGRATIONS_DIR", str(tmp_path))

    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == "applied 0001_widgets\n"
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out == "nothing to apply\n"
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)


def test_migrate_command_reports_a_refusal(
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "0002_widgets.sql").write_text("SELECT 1;")
    monkeypatch.setenv("FACTORY_BOARD_MIGRATE_DATABASE_URL", database_url)
    monkeypatch.setenv("FACTORY_BOARD_MIGRATIONS_DIR", str(tmp_path))

    assert main(["migrate"]) == 1
    assert "gap in the migrations" in capsys.readouterr().err


def test_migrate_command_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The serve URL is no substitute: the migrations need the owner's.
    monkeypatch.setenv("FACTORY_BOARD_DATABASE_URL", "postgresql://board_app@example/board")
    monkeypatch.delenv("FACTORY_BOARD_MIGRATE_DATABASE_URL", raising=False)
    assert main(["migrate"]) == 1
    assert "FACTORY_BOARD_MIGRATE_DATABASE_URL is not set" in capsys.readouterr().err


SERVER_ENV = {
    "FACTORY_BOARD_BASE_URL": "https://board.example.test",
    "FACTORY_BOARD_OIDC_CONFIG_URL": "https://id.example.test/.well-known/openid-configuration",
    "FACTORY_BOARD_OIDC_CLIENT_ID": "factory-board",
    "FACTORY_BOARD_OIDC_CLIENT_SECRET": "s3cret",
    "FACTORY_BOARD_JWT_SIGNING_KEY": "jwt-key",
    "FACTORY_BOARD_STORAGE_KEY": "storage-key",
    "FACTORY_BOARD_ROUTE_HEADER": "X-Board-Route",
}


def test_the_server_settings_have_two_defaults() -> None:
    settings = ServerSettings.from_env(SERVER_ENV)
    assert settings.operator_group == "factory-operators"
    assert settings.allowed_redirects == ("https://claude.ai/api/mcp/auth_callback",)
    assert settings.session_token_sha256 is None
    assert settings.nightly_token_sha256 is None


@pytest.mark.parametrize("name", sorted(SERVER_ENV))
def test_each_server_setting_is_required(name: str) -> None:
    env = {key: value for key, value in SERVER_ENV.items() if key != name}
    with pytest.raises(ConfigError, match=f"{name} is not set"):
        ServerSettings.from_env(env)
    with pytest.raises(ConfigError, match=f"{name} is not set"):
        ServerSettings.from_env(env | {name: "  "})


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"FACTORY_BOARD_SESSION_TOKEN_SHA256": "A" * 64}, "SESSION_TOKEN_SHA256 must be"),
        ({"FACTORY_BOARD_NIGHTLY_TOKEN_SHA256": "abc"}, "NIGHTLY_TOKEN_SHA256 must be"),
        (
            {
                "FACTORY_BOARD_SESSION_TOKEN_SHA256": "a" * 64,
                "FACTORY_BOARD_NIGHTLY_TOKEN_SHA256": "a" * 64,
            },
            "must differ",
        ),
        ({"FACTORY_BOARD_ROUTE_HEADER": "X Board Route"}, "not a header name"),
        ({"FACTORY_BOARD_ALLOWED_REDIRECTS": " , "}, "names no redirect URI"),
    ],
)
def test_an_unusable_server_setting_is_refused(extra: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        ServerSettings.from_env(SERVER_ENV | extra)


def test_the_token_hashes_and_redirects_are_read() -> None:
    settings = ServerSettings.from_env(
        SERVER_ENV
        | {
            "FACTORY_BOARD_SESSION_TOKEN_SHA256": "a" * 64,
            "FACTORY_BOARD_NIGHTLY_TOKEN_SHA256": "b" * 64,
            "FACTORY_BOARD_OPERATOR_GROUP": "board-people",
            "FACTORY_BOARD_ALLOWED_REDIRECTS": "https://one.example/cb, http://localhost:*",
        }
    )
    assert settings.session_token_sha256 == "a" * 64
    assert settings.nightly_token_sha256 == "b" * 64
    assert settings.operator_group == "board-people"
    assert settings.allowed_redirects == ("https://one.example/cb", "http://localhost:*")
