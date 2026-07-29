"""Tests for programmatic `config.toml` writes.

The bug these cover: writes used to go through `tomli_w`, which rebuilds the file
from parsed data and so deleted every comment in it. The setup docs tell operators
to paste a commented block, so the first `toggle-game` silently destroyed it.
"""

from pathlib import Path

import pytest
import tomlkit

from pqn_node.core import config
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import update_config
from pqn_node.core.config import write_config

EXAMPLE_CONFIG = """\
node_name = "example_node"  # trailing comment

# Router configuration
router_name = "router1"
timetagger = ["provider", "tagger"]  # inline array must stay inline

[games_availability]
chsh = true
qf = true  # Quantum Fortune
ssm = true
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return path


class _FakeSettings:
    """Stand-in for the real settings object, holding just what these tests write to."""

    def __init__(self) -> None:
        self.games_availability = GamesAvailability()


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> _FakeSettings:
    """Swap out the live settings singleton, so no test can apply changes to the real one."""
    fake = _FakeSettings()
    monkeypatch.setattr(config, "settings", fake)
    return fake


def test_write_preserves_comments_and_formatting(config_file: Path) -> None:
    write_config(config_file, {"games_availability.qf": False})

    written = config_file.read_text(encoding="utf-8")
    assert "# Router configuration" in written
    assert 'node_name = "example_node"  # trailing comment' in written
    assert "qf = false  # Quantum Fortune" in written
    # tomli_w exploded arrays one element per line; tomlkit leaves them as written.
    assert 'timetagger = ["provider", "tagger"]  # inline array must stay inline' in written


def test_only_the_named_key_changes(config_file: Path) -> None:
    write_config(config_file, {"games_availability.qf": False})

    document = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    assert document["games_availability"]["qf"] is False  # type: ignore[index]
    assert document["games_availability"]["chsh"] is True  # type: ignore[index]
    assert document["games_availability"]["ssm"] is True  # type: ignore[index]
    assert document["node_name"] == "example_node"


def test_availability_round_trips(config_file: Path) -> None:
    """What is written parses back into the same model the endpoint was given."""
    requested = GamesAvailability(chsh=False, qf=True, ssm=False)

    write_config(config_file, {f"games_availability.{game}": value for game, value in requested.model_dump().items()})

    written = tomlkit.parse(config_file.read_text(encoding="utf-8"))
    assert GamesAvailability.model_validate(dict(written["games_availability"])) == requested  # type: ignore[arg-type]


def test_update_config_persists_and_applies_in_place(
    config_file: Path, fake_settings: _FakeSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modules import `settings` once and hold it, so the change must land on that object."""
    monkeypatch.setattr(config, "config_path", lambda: config_file)
    held_reference = fake_settings.games_availability

    update_config({"games_availability.qf": False})

    assert "qf = false  # Quantum Fortune" in config_file.read_text(encoding="utf-8")
    assert held_reference.qf is False
    assert fake_settings.games_availability is held_reference


def test_write_config_does_not_touch_the_live_settings(config_file: Path, fake_settings: _FakeSettings) -> None:
    """The CLI writes files, possibly for a Node other than this process, so there is nothing to apply."""
    write_config(config_file, {"games_availability.qf": False})

    assert "qf = false" in config_file.read_text(encoding="utf-8")
    assert fake_settings.games_availability.qf is True


def test_missing_table_is_created(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('# just a comment\nnode_name = "n"\n', encoding="utf-8")

    write_config(path, {"games_availability.ssm": False})

    written = path.read_text(encoding="utf-8")
    assert "# just a comment" in written
    assert tomlkit.parse(written)["games_availability"]["ssm"] is False  # type: ignore[index]


def test_missing_file_is_created(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"

    write_config(path, {"games_availability.ssm": False})

    assert tomlkit.parse(path.read_text(encoding="utf-8"))["games_availability"]["ssm"] is False  # type: ignore[index]


def test_failed_write_leaves_no_partial_file_and_no_temp_files(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must leave the previous config intact, not a truncated one."""

    def explode(*_args: object, **_kwargs: object) -> None:
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", explode)

    with pytest.raises(OSError, match="disk gone"):
        write_config(config_file, {"games_availability.qf": False})

    assert config_file.read_text(encoding="utf-8") == EXAMPLE_CONFIG
    assert list(config_file.parent.iterdir()) == [config_file]
