"""The shared Angle Sweep helpers: named polarizations, visibility, and the plot."""

import csv
import math
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from pqn_node.api.routes.chsh import SweepResult
from pqn_node.constants import Polarization
from pqn_node.constants import QKDAngleValuesHWP
from pqn_whobot.sweep_report import POLARIZATION_TICKS
from pqn_whobot.sweep_report import append_sweep_csv
from pqn_whobot.sweep_report import describe_angle
from pqn_whobot.sweep_report import fit_fringe
from pqn_whobot.sweep_report import plot_sweep
from pqn_whobot.sweep_report import polarization_name


def sweep_of(coincidences: list[int]) -> SweepResult:
    return SweepResult(
        leader_name="alice",
        follower_name="bob",
        follower_angle=45.0,
        step_deg=90.0,
        integration_time_s=1.0,
        angles=[i * 90.0 for i in range(len(coincidences))],
        coincidences=coincidences,
    )


def test_the_named_polarizations_are_the_nodes_own_waveplate_settings_doubled() -> None:
    """H, V, D and A come from the angles the Node's games already use, not a separate table."""
    for polarization in Polarization:
        if polarization is Polarization.CUSTOM:
            assert polarization.angle is None
        else:
            assert polarization.angle == (2 * QKDAngleValuesHWP[polarization.value].value) % 360


def test_only_the_nodes_four_settings_are_marked() -> None:
    assert POLARIZATION_TICKS == {0.0: "H", 45.0: "D", 90.0: "V", 315.0: "A"}


@pytest.mark.parametrize(("angle", "name"), [(0.0, "H"), (45.0, "D"), (90.0, "V"), (315.0, "A"), (-45.0, "A")])
def test_the_nodes_settings_are_named(angle: float, name: str) -> None:
    assert polarization_name(angle) is Polarization(name)


@pytest.mark.parametrize("angle", [30.0, 135.0, 225.0, 270.0])
def test_an_equivalent_angle_is_not_named(angle: float) -> None:
    assert polarization_name(angle) is None


def test_an_angle_is_described_with_its_name_only_when_it_has_one() -> None:
    assert describe_angle(45.0) == "45° (D)"
    assert describe_angle(37.5) == "37.5°"


def test_visibility_is_max_minus_min_over_max_plus_min() -> None:
    result = sweep_of([100, 10, 100, 10])

    assert result.visibility == pytest.approx(90 / 110)
    assert (result.min_coincidences, result.max_coincidences) == (10, 100)


@pytest.mark.parametrize("counts", [[0, 0, 0], []])
def test_visibility_with_nothing_counted_is_zero(counts: list[int]) -> None:
    assert sweep_of(counts).visibility == 0.0


def test_the_plot_is_a_png() -> None:
    assert plot_sweep(sweep_of([100, 10, 100, 10])).startswith(b"\x89PNG\r\n\x1a\n")


SAMPLE = sweep_of([100, 10]).model_copy(update={"integration_time_s": 2.0})


def test_a_sweep_is_appended_one_row_per_point_under_one_header(tmp_path: Path) -> None:
    path = tmp_path / "sweeps.csv"
    append_sweep_csv(path, SAMPLE, datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    append_sweep_csv(path, SAMPLE, datetime(2026, 10, 15, 13, 0, tzinfo=UTC))

    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    assert len(rows) == 2 * len(SAMPLE.angles), "a second sweep adds rows, not a second header"
    assert rows[0] == {
        "measured_at": "2026-10-15T12:00:00+00:00",
        "leader": "alice",
        "follower": "bob",
        "follower_angle": "45",
        "integration_time_s": "2",
        "leader_angle": "0",
        "coincidences": "100",
    }
    assert rows[-1]["measured_at"] == "2026-10-15T13:00:00+00:00"


def test_the_fit_recovers_a_clean_fringe() -> None:
    angles = [i * 10.0 for i in range(36)]
    counts = [round(500 + 400 * math.cos(math.radians(2 * (a - 45)))) for a in angles]

    fit = fit_fringe(angles, counts)

    assert fit is not None
    assert fit.offset == pytest.approx(500, abs=1)
    assert fit.amplitude == pytest.approx(400, abs=1)
    assert fit.phase == pytest.approx(45, abs=0.5)
    assert fit.visibility == pytest.approx(0.8, abs=0.01)


def test_too_few_points_are_not_fitted() -> None:
    assert fit_fringe([0.0, 90.0], [100, 10]) is None
