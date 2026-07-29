"""When the Daily Digest is due. Nothing here runs one; the loop is a method on ``Whobot``."""

from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from zoneinfo import ZoneInfo

from pqn_whobot.config import WhobotSettings


@dataclass(frozen=True)
class Schedule:
    """A wall-clock time of day in one IANA timezone, and the instants it falls on.

    Days are advanced on the *local date* rather than by moving an instant, so a 07:00 digest
    stays at 07:00 across a DST boundary — where consecutive runs are 23 or 25 real hours apart.
    """

    hour: int
    minute: int
    timezone: ZoneInfo

    @classmethod
    def from_settings(cls, settings: WhobotSettings) -> "Schedule":
        """Read the configured schedule. Rebuilt per tick, which is how a change re-arms."""
        return cls(hour=settings.schedule_hour, minute=settings.schedule_minute, timezone=settings.timezone)

    def _on(self, day: date) -> datetime:
        """Return this time of day on one local day, as an aware datetime."""
        return datetime.combine(day, time(self.hour, self.minute), tzinfo=self.timezone)

    def next_after(self, instant: datetime) -> datetime:
        """Return the first scheduled run strictly after ``instant``.

        Strictly, so a just-fired run computes tomorrow rather than itself.
        """
        local_day = instant.astimezone(self.timezone).date()
        candidate = self._on(local_day)
        if candidate <= instant:
            candidate = self._on(local_day + timedelta(days=1))
        return candidate

    def previous_at_or_before(self, instant: datetime) -> datetime:
        """Return the most recent scheduled run at or before ``instant``.

        A digest is overdue exactly when its last recorded run is older than this.
        """
        local_day = instant.astimezone(self.timezone).date()
        candidate = self._on(local_day)
        if candidate > instant:
            candidate = self._on(local_day - timedelta(days=1))
        return candidate

    def __str__(self) -> str:
        """Describe the schedule as an operator states it, zone included."""
        return f"{self.hour:02d}:{self.minute:02d} {self.timezone.key}"
