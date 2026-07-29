"""Tests for when the Daily Digest is due. Pure arithmetic: no clock, no config file."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pqn_whobot.config import WhobotSettings
from pqn_whobot.schedule import Schedule

CHICAGO = ZoneInfo("America/Chicago")
AT_SEVEN = Schedule(hour=7, minute=0, timezone=CHICAGO)


def test_fires_later_today_when_the_time_has_not_passed() -> None:
    fires_at = AT_SEVEN.next_after(datetime(2026, 7, 29, 6, 30, tzinfo=CHICAGO))

    assert fires_at == datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)


def test_fires_tomorrow_when_today_is_already_past() -> None:
    fires_at = AT_SEVEN.next_after(datetime(2026, 7, 29, 7, 30, tzinfo=CHICAGO))

    assert fires_at == datetime(2026, 7, 30, 7, 0, tzinfo=CHICAGO)


def test_a_run_that_has_just_fired_computes_tomorrow_not_itself() -> None:
    """Strictly-after, or the loop would fire the same scheduled time repeatedly."""
    fired_at = datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)

    assert AT_SEVEN.next_after(fired_at) == fired_at + timedelta(days=1)


def test_the_schedule_is_read_in_its_own_zone_not_the_callers() -> None:
    """12:15 UTC is 07:15 in Chicago, so the 07:00 run is already past."""
    fires_at = AT_SEVEN.next_after(datetime(2026, 7, 29, 12, 15, tzinfo=UTC))

    assert fires_at == datetime(2026, 7, 30, 7, 0, tzinfo=CHICAGO)


def _real_hours_between(first: datetime, second: datetime) -> timedelta:
    """Measure elapsed time as a clock outside the zone would.

    Subtracting two datetimes sharing a `tzinfo` answers in wall clock, so a DST assertion made
    that way reads 24 hours across every boundary and can never fail.
    """
    return second.astimezone(UTC) - first.astimezone(UTC)


def test_the_digest_stays_at_seven_across_spring_forward() -> None:
    """Chicago loses an hour on 8 March 2026, so that run is 23 real hours after the last.

    Advancing an instant instead of the local date drifts the digest to 08:00 until autumn.
    """
    first = AT_SEVEN.next_after(datetime(2026, 3, 6, 7, 30, tzinfo=CHICAGO))
    second = AT_SEVEN.next_after(first)

    assert (first.day, second.day) == (7, 8)
    assert (first.hour, second.hour) == (7, 7)
    assert _real_hours_between(first, second) == timedelta(hours=23)


def test_the_digest_stays_at_seven_across_fall_back() -> None:
    """Chicago gains an hour early on 1 November 2026, so that run is 25 real hours later."""
    first = AT_SEVEN.next_after(datetime(2026, 10, 30, 7, 30, tzinfo=CHICAGO))
    second = AT_SEVEN.next_after(first)

    assert (first.day, second.day) == (31, 1)
    assert (first.hour, second.hour) == (7, 7)
    assert _real_hours_between(first, second) == timedelta(hours=25)


def test_a_different_zone_moves_the_digest() -> None:
    eastern = Schedule(hour=7, minute=0, timezone=ZoneInfo("America/New_York"))

    # 05:30 Central is 06:30 Eastern, so the Eastern 07:00 run is half an hour away.
    fires_at = eastern.next_after(datetime(2026, 7, 29, 5, 30, tzinfo=CHICAGO))

    assert fires_at == datetime(2026, 7, 29, 6, 0, tzinfo=CHICAGO)


def test_a_minute_other_than_zero_is_respected() -> None:
    late = Schedule(hour=23, minute=45, timezone=CHICAGO)

    assert late.next_after(datetime(2026, 7, 29, 23, 44, tzinfo=CHICAGO)) == datetime(
        2026, 7, 29, 23, 45, tzinfo=CHICAGO
    )


def test_the_previous_run_is_earlier_today_once_the_time_has_passed() -> None:
    ran_at = AT_SEVEN.previous_at_or_before(datetime(2026, 7, 29, 9, 0, tzinfo=CHICAGO))

    assert ran_at == datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)


def test_the_previous_run_is_yesterday_before_todays_time() -> None:
    ran_at = AT_SEVEN.previous_at_or_before(datetime(2026, 7, 29, 6, 0, tzinfo=CHICAGO))

    assert ran_at == datetime(2026, 7, 28, 7, 0, tzinfo=CHICAGO)


def test_the_scheduled_instant_itself_counts_as_the_previous_run() -> None:
    """At-or-before, so a digest that has just run is not also reported as overdue."""
    fired_at = datetime(2026, 7, 29, 7, 0, tzinfo=CHICAGO)

    assert AT_SEVEN.previous_at_or_before(fired_at) == fired_at


def test_it_describes_itself_with_its_zone() -> None:
    """What the status Action shows an operator; the zone is never left implied."""
    assert str(AT_SEVEN) == "07:00 America/Chicago"
    assert str(Schedule(hour=0, minute=5, timezone=CHICAGO)) == "00:05 America/Chicago"


def test_it_is_built_from_the_configured_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test needing settings, so the one needing a working directory."""
    monkeypatch.chdir(tmp_path)
    settings = WhobotSettings(schedule_timezone="America/New_York", schedule_hour=6, schedule_minute=30)

    assert Schedule.from_settings(settings) == Schedule(hour=6, minute=30, timezone=ZoneInfo("America/New_York"))
