from enum import Enum
from enum import StrEnum


class OperationMode(StrEnum):
    """How a Node plays its two-player Games: on entangled pairs, or only on polarization-correlated ones.

    ``entanglement`` is today's behaviour and the default; ``correlation`` is for when entanglement is low.
    """

    ENTANGLEMENT = "entanglement"
    CORRELATION = "correlation"


class CoincidenceLevel(StrEnum):
    """How many coincidences a setting gives relative to the Reference rates: high, medium or low."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class QKDAngleValuesHWP(Enum):
    """The half waveplate angles that set H, V, D and A. Waveplate angles, not polarization angles."""

    H = 0
    V = 45
    A = -22.5
    D = 22.5

    @property
    def polarization(self) -> float:
        """The polarization angle this waveplate angle sets, on the 0-360° axis."""
        return float(2 * self.value) % 360.0

    @classmethod
    def from_polarization_angle(cls, polarization_angle: float) -> "QKDAngleValuesHWP | None":
        """Return the named polarization at this polarization angle, or None when none of the four sits there.

        The inverse of ``polarization``: an Angle Sweep pins the follower at a polarization angle,
        and only the four named ones carry a Calibrated angle.
        """
        for member in cls:
            if member.polarization == polarization_angle:
                return member
        return None


class Polarization(StrEnum):
    """A polarization to pin the follower at: one of the four named ones, or a custom angle."""

    H = "H"
    D = "D"
    V = "V"
    A = "A"
    CUSTOM = "custom"

    @property
    def angle(self) -> float | None:
        """The polarization angle in degrees, or None for a custom one, which the caller supplies.

        Taken from the waveplate angles the Node's games already move to, so H, V, D and A mean
        the same four settings everywhere rather than a textbook convention.
        """
        return None if self is Polarization.CUSTOM else QKDAngleValuesHWP[self.value].polarization

    @property
    def label(self) -> str:
        """What a form shows for this choice."""
        angle = self.angle
        return "Custom" if angle is None else f"{self.value} ({angle:g}°)"


class QKDEncodingBasis(Enum):
    HV = 0
    DA = 1

    @property
    def angles(self) -> list[QKDAngleValuesHWP]:
        if self is QKDEncodingBasis.HV:
            return [QKDAngleValuesHWP.H, QKDAngleValuesHWP.V]
        if self is QKDEncodingBasis.DA:
            return [QKDAngleValuesHWP.D, QKDAngleValuesHWP.A]
        msg = f"Unknown basis: {self}"
        raise ValueError(msg)


class BasisBool(Enum):
    HV = 0
    DA = 1


# FIXME: Populate missing bell states.
class BellState(Enum):
    """Encodes."""

    Phi_plus = 0
    Psi_plus = 1
