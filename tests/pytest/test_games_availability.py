"""Regression tests for the games-availability gate.

The bug these cover: the health probe used to mutate the process-wide settings
singleton and could only ever assign False, so one unreachable-router probe
disabled every game until the process restarted.
"""

from pqn_node.api.routes.health import ComponentStatus
from pqn_node.api.routes.health import effective_availability
from pqn_node.core.config import GamesAvailability
from pqn_node.core.config import get_settings

UP = ComponentStatus(reachable=True)
DOWN = ComponentStatus(reachable=False, error="unreachable")


def test_all_games_gated_off_when_router_unreachable() -> None:
    result = effective_availability(GamesAvailability(chsh=True, qf=True, ssm=True), DOWN, UP)
    assert (result.chsh, result.qf, result.ssm) == (False, False, False)


def test_follower_unreachable_leaves_qf_enabled() -> None:
    result = effective_availability(GamesAvailability(chsh=True, qf=True, ssm=True), UP, DOWN)
    assert (result.chsh, result.qf, result.ssm) == (False, True, False)


def test_no_follower_configured_does_not_gate() -> None:
    result = effective_availability(GamesAvailability(chsh=True, qf=True, ssm=True), UP, None)
    assert (result.chsh, result.qf, result.ssm) == (True, True, True)


def test_games_re_enable_once_hardware_recovers() -> None:
    """The whole point: the gate is re-derived from config, not latched."""
    configured = GamesAvailability(chsh=True, qf=True, ssm=True)

    while_down = effective_availability(configured, DOWN, UP)
    assert not while_down.qf

    after_recovery = effective_availability(configured, UP, UP)
    assert (after_recovery.chsh, after_recovery.qf, after_recovery.ssm) == (True, True, True)


def test_config_disabled_game_is_never_enabled_by_a_healthy_probe() -> None:
    configured = GamesAvailability(chsh=False, qf=True, ssm=False)

    result = effective_availability(configured, UP, UP)

    assert result.chsh is False
    assert result.ssm is False
    assert result.qf is True


def test_gating_does_not_mutate_the_configured_object() -> None:
    configured = GamesAvailability(chsh=True, qf=True, ssm=True)

    effective_availability(configured, DOWN, DOWN)

    assert (configured.chsh, configured.qf, configured.ssm) == (True, True, True)


def test_gating_does_not_mutate_the_settings_singleton() -> None:
    """A failed probe must not poison process-wide state."""
    configured = get_settings().games_availability
    before = (configured.chsh, configured.qf, configured.ssm)

    effective_availability(configured, DOWN, DOWN)

    after = get_settings().games_availability
    assert (after.chsh, after.qf, after.ssm) == before
