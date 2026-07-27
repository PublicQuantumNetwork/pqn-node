"""Tests for Whobot's config file: what it reads, and what it refuses.

Whobot reads `./whobot.toml`, so each test runs in a temp directory holding one.
"""

import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pqn_whobot.config import WhobotSettings
from pqn_whobot.config import config_path

EXAMPLE_CONFIG = """\
# Slack credentials.
slack_bot_token = "xoxb-secret"
slack_app_token = "xapp-secret"
digest_channel = "C0123456789"

schedule_timezone = "America/Chicago"
schedule_hour = 7  # morning digest
schedule_minute = 0

per_node_timeout_s = 900
per_game_timeout_s = 600

# The Node Registry.
[[nodes]]
api_url = "http://node-a.invalid:9000"

[[nodes]]
api_url = "http://node-b.invalid:9000/"
"""


@pytest.fixture(autouse=True)
def _in_a_temp_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "whobot.toml"
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return path


@pytest.mark.usefixtures("config_file")
def test_reads_tokens_schedule_and_registry() -> None:
    settings = WhobotSettings()

    assert settings.slack_bot_token == "xoxb-secret"  # noqa: S105 - a fake token, not a credential
    assert settings.digest_channel == "C0123456789"
    assert (settings.schedule_hour, settings.schedule_minute) == (7, 0)
    assert [node.api_url for node in settings.nodes] == [
        "http://node-a.invalid:9000",
        # Trailing slash normalised away, so it can be joined with a path unconditionally.
        "http://node-b.invalid:9000",
    ]


def test_the_config_is_found_relative_to_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The path mechanism: a fixed filename, resolved against the working directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "whobot.toml").write_text("schedule_hour = 9\n", encoding="utf-8")

    assert config_path() == Path("whobot.toml")
    assert WhobotSettings().schedule_hour != 9  # noqa: PLR2004 - cwd is tmp_path, which holds no config

    monkeypatch.chdir(elsewhere)
    assert WhobotSettings().schedule_hour == 9  # noqa: PLR2004 - the value written above


def test_a_missing_file_reads_as_defaults() -> None:
    """An absent file is "no values" to pydantic-settings, so loading one yields defaults.

    Startup has to check for the file itself; see `test_whobot_cli.py`.
    """
    assert not config_path().exists()

    assert WhobotSettings().nodes == []


def test_a_node_is_added_by_editing_the_file(config_file: Path) -> None:
    """A new Node is config, not code."""
    before = len(WhobotSettings().nodes)

    with config_file.open("a", encoding="utf-8") as f:
        f.write('\n[[nodes]]\napi_url = "http://node-c.invalid:9000"\n')

    after = WhobotSettings().nodes
    assert len(after) == before + 1
    assert after[-1].api_url == "http://node-c.invalid:9000"


@pytest.mark.usefixtures("config_file")
def test_schedule_timezone_is_a_real_zone() -> None:
    # America/Chicago is CST/CDT — a fixed -6 would drift for half the year.
    assert WhobotSettings().timezone.key == "America/Chicago"


def test_defaults_are_host_agnostic() -> None:
    """No default may point at the machine Whobot happens to run on."""
    settings = WhobotSettings()

    assert settings.nodes == []
    assert settings.schedule_timezone == "America/Chicago"
    assert "localhost" not in settings.model_dump_json()


def test_unknown_timezone_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "whobot.toml").write_text('schedule_timezone = "Mars/Olympus_Mons"\n', encoding="utf-8")

    with pytest.raises(ValidationError, match="not a known IANA timezone"):
        WhobotSettings()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("schedule_hour = 24\n", "schedule_hour"),
        ("schedule_minute = -1\n", "schedule_minute"),
        ("per_node_timeout_s = 0\n", "per_node_timeout_s"),
        ('slack_bot_tokn = "typo"\n', "slack_bot_tokn"),
        ('[[nodes]]\napi_url = "node-a.invalid:9000"\n', "api_url"),
    ],
)
def test_invalid_values_name_the_offending_key(tmp_path: Path, body: str, expected: str) -> None:
    """A typo'd or out-of-range key is an error, not a setting that silently never applies."""
    (tmp_path / "whobot.toml").write_text(body, encoding="utf-8")

    with pytest.raises(ValidationError, match=expected):
        WhobotSettings()


def test_malformed_toml_is_a_parse_error(tmp_path: Path) -> None:
    (tmp_path / "whobot.toml").write_text("schedule_hour = \n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        WhobotSettings()


def test_the_mutable_fields_round_trip_from_the_file(tmp_path: Path) -> None:
    """A TOML timestamp reads back as an aware `datetime`, in its own offset."""
    (tmp_path / "whobot.toml").write_text(
        'last_run_at = 2026-01-02T07:00:00-06:00\nlast_result = "ok"\n',
        encoding="utf-8",
    )

    settings = WhobotSettings()

    assert settings.last_result == "ok"
    assert settings.last_run_at is not None
    assert settings.last_run_at.utcoffset() == timedelta(hours=-6)
