"""Tests for `whobot nodes`.

Broken input must produce an explanation and a non-zero exit rather than a traceback, and an
unreachable Node must show in the output, not only in the exit code.

The command reads `./whobot.toml`, so each test runs in a temp directory.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pqn_whobot import cli
from pqn_whobot.registry import Node

runner = CliRunner()

CONFIG = """\
[[nodes]]
api_url = "http://node-a.invalid:9000"

[[nodes]]
api_url = "http://offline.invalid:9000"
"""

ALIVE = Node(api_url="http://node-a.invalid:9000", name="uiuc-public-left", reachable=True, latency_ms=12.3)
DEAD = Node(api_url="http://offline.invalid:9000", reachable=False, error="ConnectError: refused")
OUTDATED = Node(
    api_url="http://node-b.invalid:9000",
    reachable=True,
    warning="no node_name in /node/config; the Node is running older code — update it",
    latency_ms=8.0,
)


@pytest.fixture(autouse=True)
def _in_a_clean_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "whobot.toml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *nodes: Node) -> None:
    async def fake_resolve_nodes(_settings: object) -> list[Node]:
        return list(nodes)

    monkeypatch.setattr(cli, "resolve_nodes", fake_resolve_nodes)


def test_lists_every_node_with_its_name_and_reachability(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config_file.exists()  # the command takes no path; this is the file it will find
    _resolve_to(monkeypatch, ALIVE, DEAD)

    result = runner.invoke(cli.app, ["nodes"])

    assert "uiuc-public-left" in result.output
    assert "reachable (12ms)" in result.output
    assert "UNREACHABLE — ConnectError: refused" in result.output
    # The unreachable row names no Node, rather than printing its address in both columns.
    assert "(unknown)" in result.output
    assert result.output.count(DEAD.api_url) == 1
    # A Node that can't be reached makes the command fail, so a cron/monitor notices.
    assert result.exit_code == 1


@pytest.mark.usefixtures("config_file")
def test_a_reachable_node_with_a_warning_is_shown_but_does_not_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partly-deployed fleet is a normal state, so it must not read as an outage."""
    _resolve_to(monkeypatch, ALIVE, OUTDATED)

    result = runner.invoke(cli.app, ["nodes"])

    assert "reachable (8ms) — no node_name" in result.output
    assert "UNREACHABLE" not in result.output
    assert "1 of 2 reachable with warnings." in result.output
    assert result.exit_code == 0


@pytest.mark.usefixtures("config_file")
def test_all_reachable_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, ALIVE)

    result = runner.invoke(cli.app, ["nodes"])

    assert result.exit_code == 0
    assert "UNREACHABLE" not in result.output


def test_the_output_names_the_file_it_read(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing says which config file it read."""
    _resolve_to(monkeypatch, ALIVE)

    result = runner.invoke(cli.app, ["nodes"])

    assert str(config_file.resolve()) in result.output


def test_a_missing_config_is_reported_not_treated_as_defaults(tmp_path: Path) -> None:
    """Loading an absent file succeeds and yields defaults, so the CLI checks for it."""
    result = runner.invoke(cli.app, ["nodes"])

    assert result.exit_code == 1
    assert "whobot_example.toml" in result.output
    assert str(tmp_path) in result.output  # says which directory it looked in
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_malformed_toml_explains_itself_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / "whobot.toml").write_text("schedule_hour = \n", encoding="utf-8")

    result = runner.invoke(cli.app, ["nodes"])

    assert result.exit_code == 1
    assert str((tmp_path / "whobot.toml").resolve()) in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_invalid_value_explains_itself_instead_of_raising(tmp_path: Path) -> None:
    """The message names both the file and the offending key."""
    (tmp_path / "whobot.toml").write_text("schedule_hour = 99\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["nodes"])

    assert result.exit_code == 1
    assert str((tmp_path / "whobot.toml").resolve()) in result.output
    assert "schedule_hour" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_empty_registry_says_how_to_add_a_node(tmp_path: Path) -> None:
    (tmp_path / "whobot.toml").write_text("schedule_hour = 7\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["nodes"])

    assert result.exit_code == 1
    assert "[[nodes]]" in result.output
