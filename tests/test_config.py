"""Settings from the environment, and the migrate command that reads them."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from factory_board.__main__ import main
from factory_board.config import DEFAULT_MIGRATIONS_DIR, ConfigError, Settings


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
