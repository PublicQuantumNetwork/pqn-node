"""The Angle Sweep's result, shared by the Node, which runs the sweep, and Whobot, which asks for one.

Angles here are polarization angles, the ones the sweep's x axis shows. The half waveplate
is moved to half of them, which ``move_to_polarization`` takes care of.
"""

from pydantic import BaseModel


class SweepResult(BaseModel):
    leader_name: str
    follower_name: str
    follower_angle: float
    step_deg: float
    integration_time_s: float
    angles: list[float]
    coincidences: list[int]

    @property
    def min_coincidences(self) -> int:
        return min(self.coincidences, default=0)

    @property
    def max_coincidences(self) -> int:
        return max(self.coincidences, default=0)

    @property
    def visibility(self) -> float:
        """Fringe visibility, (max - min) / (max + min). Zero when nothing was counted."""
        low, high = self.min_coincidences, self.max_coincidences
        return 0.0 if high + low == 0 else (high - low) / (high + low)
