"""Tests for Whobot's config file: what it reads, and what it refuses.

Whobot reads `./whobot.toml`, so each test runs in a temp directory holding one.
"""

import tomllib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pqn_whobot.config import ConfigWriteError
from pqn_whobot.config import NodeEntry
from pqn_whobot.config import WhobotSettings
from pqn_whobot.config import config_path
from pqn_whobot.config import update_config

EXAMPLE_CONFIG = """\
# Slack credentials.
slack_bot_token = "xoxb-secret"
slack_app_token = "xapp-secret"
digest_channel = "C0123456789"

schedule_timezone = "America/Chicago"
schedule_hour = 7  # morning digest
schedule_minute = 0

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


def test_how_a_node_is_measured_is_one_answer_for_the_whole_network(tmp_path: Path) -> None:
    """Not per-Node: a registry entry holds an address and nothing about measuring it.

    ``timetagger_address`` is resolved on the Node — it builds
    ``http://{timetagger_address}/timetagger/...`` — so one value serves every Node, and
    ``basis`` is a measurement choice that belongs to the Network rather than to a machine.
    """
    (tmp_path / "whobot.toml").write_text(
        'timetagger_address = "10.0.0.5:9000"\nbasis = [11.0, 33.5]\n'
        '\n[[nodes]]\napi_url = "http://node-a.invalid:9000"\n',
        encoding="utf-8",
    )

    settings = WhobotSettings()

    assert settings.timetagger_address == "10.0.0.5:9000"
    assert settings.basis == (11.0, 33.5)
    assert set(NodeEntry.model_fields) == {"api_url"}


def test_the_timetagger_default_is_the_nodes_own_host_not_whobots(tmp_path: Path) -> None:
    """The one loopback default in Whobot, and it is not a host assumption.

    Whobot never dials this address: it hands the string to a Node, which resolves it. So
    127.0.0.1 means *that Node's* host, and a fleet of Nodes each measuring with their own
    timetagger needs no per-Node configuration at all. Nothing here may assume which machine
    Whobot runs on, and this does not.
    """
    (tmp_path / "whobot.toml").write_text("", encoding="utf-8")

    settings = WhobotSettings()

    assert settings.timetagger_address == "127.0.0.1:9000"
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
        ("per_game_timeout_s = 0\n", "per_game_timeout_s"),
        ('slack_bot_tokn = "typo"\n', "slack_bot_tokn"),
        ('[[nodes]]\napi_url = "node-a.invalid:9000"\n', "api_url"),
        # `POST /chsh/` declares `basis: tuple[float, float]`, so a third angle is a typo that
        # would otherwise be found by the Node refusing an unattended run at 07:00.
        ("basis = [0.0, 22.5, 45.0]\n", "basis"),
        ("basis = [0.0]\n", "basis"),
    ],
)
def test_invalid_values_name_the_offending_key(tmp_path: Path, body: str, expected: str) -> None:
    """A typo'd or out-of-range key is an error, not a setting that silently never applies."""
    (tmp_path / "whobot.toml").write_text(body, encoding="utf-8")

    with pytest.raises(ValidationError, match=expected):
        WhobotSettings()


def test_a_removed_key_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """`per_node_timeout_s` is derived now, so a file still setting it must say so, not be ignored.

    ``extra="forbid"`` is what makes this loud: a key Whobot no longer reads would otherwise sit
    in the file looking like it was doing something.
    """
    (tmp_path / "whobot.toml").write_text("per_node_timeout_s = 900\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="per_node_timeout_s"):
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


# --------------------------------------------------------------------------------------
# Writing back. `write_config`'s own guarantees — atomic rename, no stray temp file — are
# `pqn_node`'s and are tested in `test_config_updates.py`.
# --------------------------------------------------------------------------------------


def test_a_persisted_schedule_survives_a_reload(config_file: Path) -> None:
    settings = WhobotSettings()

    update_config(settings, {"schedule_hour": 9, "schedule_minute": 30})

    reloaded = WhobotSettings()
    assert (reloaded.schedule_hour, reloaded.schedule_minute) == (9, 30)
    assert config_file.read_text(encoding="utf-8").count("schedule_hour") == 1


def test_a_write_applies_to_the_live_settings_object(config_file: Path) -> None:
    """No restart: the loop re-reads this object every tick, so it re-arms itself."""
    settings = WhobotSettings()
    assert settings.schedule_hour == 7  # noqa: PLR2004 - the value in EXAMPLE_CONFIG

    update_config(settings, {"schedule_hour": 9})

    assert settings.schedule_hour == 9  # noqa: PLR2004 - and the file agrees
    assert "schedule_hour = 9" in config_file.read_text(encoding="utf-8")


def test_the_comments_and_the_tokens_survive_a_write(config_file: Path) -> None:
    """Operators hand-write this file from a commented example, and it holds the tokens."""
    update_config(WhobotSettings(), {"schedule_hour": 9})

    written = config_file.read_text(encoding="utf-8")
    assert "# Slack credentials." in written
    assert "schedule_hour = 9  # morning digest" in written
    assert 'slack_app_token = "xapp-secret"' in written
    assert "# The Node Registry." in written
    assert [node.api_url for node in WhobotSettings().nodes] == [
        "http://node-a.invalid:9000",
        "http://node-b.invalid:9000",
    ]


def test_what_a_run_recorded_round_trips(config_file: Path) -> None:
    """A digest writes an aware instant as a TOML timestamp and reads back the same instant.

    Both keys are absent until the first run writes them, and `[[nodes]]` is last in the file —
    so a key appended in the wrong place lands inside it and takes the registry with it.
    """
    ran_at = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)

    update_config(WhobotSettings(), {"last_run_at": ran_at, "last_result": "ok — 2 of 2 Nodes reported no problems"})

    reloaded = WhobotSettings()
    assert reloaded.last_run_at == ran_at
    assert reloaded.last_result == "ok — 2 of 2 Nodes reported no problems"
    assert len(reloaded.nodes) == 2  # noqa: PLR2004 - both registry entries, still where they were

    written = config_file.read_text(encoding="utf-8")
    assert "# Slack credentials." in written
    # A quoted string would reload as a `datetime` too, so the round trip alone does not
    # prove the file is readable by anything else that parses TOML.
    assert "last_run_at = 2026-07-29T12:00:00Z" in written
    assert written.index("last_run_at") < written.index("[[nodes]]")


def test_an_unknown_key_is_refused_before_the_file_is_touched(config_file: Path) -> None:
    """A typo would write a key that `extra="forbid"` then refuses on the next start: the
    bot keeps running and cannot come back up."""  # noqa: D205, D209
    before = config_file.read_text(encoding="utf-8")

    with pytest.raises(KeyError, match="schedule_hours"):
        update_config(WhobotSettings(), {"schedule_hours": 9})

    assert config_file.read_text(encoding="utf-8") == before


MUTABLE_KEY_BELOW_THE_REGISTRY = """\
slack_bot_token = "xoxb-secret"

[[nodes]]
api_url = "http://node-a.invalid:9000"

last_result = "ok"
"""
"""A hand-edited file with `last_result` after the table, so TOML reads it as that Node's.

`tomlkit` cannot see it there, so a write adds a second one at the top and leaves this behind —
and `extra="forbid"` then refuses the file, which is what `update_config` must not allow.
"""


def test_a_write_is_rolled_back_when_the_file_does_not_load(config_file: Path) -> None:
    """The real timeline: Whobot is running, the file is hand-edited under it, then a digest writes.

    Whobot cannot mend the file, but it must not leave a *different* broken file behind, and it
    must not report a run it could not record.
    """
    settings = WhobotSettings()  # loaded while the file was still good
    config_file.write_text(MUTABLE_KEY_BELOW_THE_REGISTRY, encoding="utf-8")

    with pytest.raises(ConfigWriteError, match="does not load"):
        update_config(settings, {"last_result": "warn"})

    assert config_file.read_text(encoding="utf-8") == MUTABLE_KEY_BELOW_THE_REGISTRY
    assert list(config_file.parent.iterdir()) == [config_file], "no temp file left behind"
    # Memory and disk still agree, which is the point of writing before applying.
    assert settings.last_result is None


def test_a_rolled_back_write_names_the_likely_cause(config_file: Path) -> None:
    """Whoever reads this has to know where to look; pydantic's dump alone does not say."""
    settings = WhobotSettings()
    config_file.write_text(MUTABLE_KEY_BELOW_THE_REGISTRY, encoding="utf-8")

    with pytest.raises(ConfigWriteError, match=r"move it above"):
        update_config(settings, {"schedule_hour": 9})

    assert settings.schedule_hour == 7  # noqa: PLR2004 - the default, unchanged
