"""How Whobot names, fits, saves and draws an Angle Sweep's result.

Only Whobot uses these, so they live here rather than on the Node, which does not ship
matplotlib. Angles are polarization angles, the ones the sweep's x axis shows.
"""

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure

from pqn_node.api.routes.chsh import SweepResult
from pqn_node.constants import Polarization

# One colour per setting, so each line on a plot matches the letter under it.
POLARIZATION_COLORS: dict[Polarization, str] = {
    Polarization.H: "tab:red",
    Polarization.D: "tab:green",
    Polarization.V: "tab:purple",
    Polarization.A: "hotpink",
}

# Only the four settings the Node uses are marked, not every angle that is equivalent to one.
POLARIZATION_TICKS: dict[float, Polarization] = dict(
    sorted((angle, polarization) for polarization in Polarization if (angle := polarization.angle) is not None)
)


def polarization_name(angle: float) -> Polarization | None:
    """Name the Node's polarization at this angle — D for 45° — or None if it has none."""
    reduced = angle % 360.0
    for named, polarization in POLARIZATION_TICKS.items():
        if abs(reduced - named) < 1e-9:  # noqa: PLR2004
            return polarization
    return None


def describe_angle(angle: float) -> str:
    """Render an angle with its name when it has one: "45° (D)", or just "30°"."""
    name = polarization_name(angle)
    return f"{angle:g}°" if name is None else f"{angle:g}° ({name})"


@dataclass(frozen=True)
class FringeFit:
    """A sine fitted to a sweep: ``offset + amplitude * cos(2 * (angle - phase))``.

    The period is 180° because polarization comes round again every half turn, so a sweep
    through 0-360° holds two fringes. ``phase`` is where the coincidences peak.
    """

    offset: float
    amplitude: float
    phase: float

    @property
    def visibility(self) -> float:
        """The fitted curve's (max - min) / (max + min), which is less swayed by one noisy point."""
        return 0.0 if self.offset == 0 else self.amplitude / self.offset

    def at(self, angle: float) -> float:
        return self.offset + self.amplitude * math.cos(math.radians(2 * (angle - self.phase)))


_FIT_MIN_POINTS = 3  # offset, amplitude and phase: three unknowns


def fit_fringe(angles: list[float], coincidences: list[int]) -> FringeFit | None:
    """Least-squares fit of a 180° sine to the sweep, or None with too few points to fit.

    Written as ``c0 + c1 cos 2θ + c2 sin 2θ`` the model is linear, so one least-squares solve
    finds it: no starting guess, and nothing to fail to converge.
    """
    if len(angles) < _FIT_MIN_POINTS:
        return None
    doubled = np.radians(2 * np.asarray(angles, dtype=float))
    design = np.column_stack([np.ones_like(doubled), np.cos(doubled), np.sin(doubled)])
    (c0, c1, c2), *_ = np.linalg.lstsq(design, np.asarray(coincidences, dtype=float), rcond=None)
    phase = math.degrees(math.atan2(c2, c1)) / 2 % 180.0
    return FringeFit(offset=float(c0), amplitude=float(math.hypot(c1, c2)), phase=phase)


SWEEP_CSV_COLUMNS = (
    "measured_at",
    "leader",
    "follower",
    "follower_angle",
    "integration_time_s",
    "leader_angle",
    "coincidences",
)


def append_sweep_csv(path: Path, result: SweepResult, measured_at: datetime) -> None:
    """Add one row per point of a sweep to a CSV, writing the header first if the file is new.

    One row per point, each carrying the whole sweep's details, so every sweep ever taken lives
    in one file that is filtered by ``measured_at`` rather than split across many.
    """
    is_new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(SWEEP_CSV_COLUMNS)
        stamp = measured_at.isoformat(timespec="seconds")
        for angle, count in zip(result.angles, result.coincidences, strict=True):
            writer.writerow(
                [
                    stamp,
                    result.leader_name,
                    result.follower_name,
                    f"{result.follower_angle:g}",
                    f"{result.integration_time_s:g}",
                    f"{angle:g}",
                    count,
                ]
            )


def plot_sweep(result: SweepResult) -> bytes:
    """Render coincidences against the leader's angle as a PNG, naming which Node did what."""
    fit = fit_fringe(result.angles, result.coincidences)
    # A bare Figure, not pyplot: no global backend to pick and no figure to remember to close.
    fig = Figure(figsize=(8, 4.5))
    ax = fig.subplots()
    for angle, name in POLARIZATION_TICKS.items():
        ax.axvline(angle, color=POLARIZATION_COLORS[name], linestyle="--", linewidth=1.2, alpha=0.6, zorder=0)
    ax.plot(result.angles, result.coincidences, marker="o", linestyle="none", color="tab:blue", label="measured")
    if fit is not None:
        curve = [i / 2 for i in range(721)]
        ax.plot(curve, [fit.at(a) for a in curve], color="tab:orange", alpha=0.9, label="sine fit")
    ax.set_xlim(0, 360)
    ticks = sorted({*range(0, 361, 45), *POLARIZATION_TICKS})
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{tick:g}°\n{POLARIZATION_TICKS.get(tick, '')}" for tick in ticks])
    for tick, label in zip(ticks, ax.get_xticklabels(), strict=True):
        if tick in POLARIZATION_TICKS:
            label.set_color(POLARIZATION_COLORS[POLARIZATION_TICKS[tick]])
            label.set_fontweight("bold")
    ax.set_xlabel(f"{result.leader_name} (leader) polarization angle")
    ax.set_ylabel("Coincidences")
    ax.set_title(
        f"{result.leader_name} (leader) swept, {result.follower_name} (follower) pinned at "
        f"{describe_angle(result.follower_angle)}\n{result.integration_time_s:g}s per point"
    )
    ax.grid(visible=True, axis="y", alpha=0.3)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2, frameon=False)
    if result.coincidences:
        lines = [
            f"V = {result.visibility:.4f}",
            f"min {result.min_coincidences} / max {result.max_coincidences}",
        ]
        if fit is not None:
            lines += [f"fit V = {fit.visibility:.4f}", f"fit peak at {fit.phase:.1f}°"]
        ax.text(
            0.98,
            0.95,
            "\n".join(lines),
            transform=ax.transAxes,
            ha="right",
            va="top",
            bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8},
        )
    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=120, bbox_inches="tight")
    return buffer.getvalue()
