import logging
from pathlib import Path
from typing import Annotated

import typer

from pqn_node.core.config import config_path
from pqn_node.core.config import get_settings
from pqn_node.core.config import write_config
from pqn_node.cron_manager import describe_schedule
from pqn_node.cron_manager import get_daily_report_job
from pqn_node.cron_manager import remove_daily_report_job
from pqn_node.cron_manager import set_daily_report_schedule
from pqn_node.daily_report import run_daily_report

# TODO: check if this way of handling logging from a command line script is ok.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, help="CLI for pqn-node.")

daily_report_app = typer.Typer(no_args_is_help=True, help="Run and manage the daily health + Slack report.")
app.add_typer(daily_report_app, name="daily-report")


@app.command()
def toggle_game(
    games: Annotated[list[str], typer.Argument(help="Games to toggle: chsh, qf, ssm")],
    enable: Annotated[bool, typer.Option("--enable/--disable", help="Enable or disable the games")] = True,  # noqa: FBT002
    config: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            dir_okay=False,
            writable=True,
            help="Path to config.toml [default: the file the node loads]",
        ),
    ] = None,
) -> None:
    """
    Enable or disable one or more games in config.toml.

    Changes take effect on the next server restart (or immediately via `PUT /games/availability`
    on a running Node). Games: chsh (Verify Quantum Link), qf (Quantum Fortune), ssm (Share a Secret Message).
    """
    valid_games = {"chsh", "qf", "ssm"}
    invalid = [g for g in games if g not in valid_games]
    if invalid:
        msg = f"Game(s) must be one of: chsh, qf, ssm. Invalid: {invalid}"
        raise typer.BadParameter(msg)

    path = config if config is not None else config_path()
    write_config(path, {f"games_availability.{game}": enable for game in games})

    status = "enabled" if enable else "disabled"
    logger.info("Games %s %s in %s. Restart the server for changes to take effect.", games, status, path)


@daily_report_app.command("run")
def daily_report_run() -> None:
    """
    Run the daily health + games report and post the result to Slack.

    Reads the [daily_report] section from config.toml, probes hardware via the
    running API (`/health`), exercises each enabled game (except SSM), and posts a
    consolidated Slack digest. Exits non-zero if anything failed.
    """
    report_config = get_settings().daily_report
    if report_config is None:
        logger.error("[daily_report] section missing from config.toml")
        raise typer.Exit(code=1)

    raise typer.Exit(code=run_daily_report(report_config))


@daily_report_app.command("status")
def daily_report_status() -> None:
    """Show whether the daily report cron job is active and its schedule."""
    job = get_daily_report_job()
    if job is None:
        typer.echo("Daily report is not scheduled.")
    else:
        typer.echo(f"Daily report is active. Schedule: {describe_schedule(job)}")


_DOW_MAP = {
    "monday": "1",
    "tuesday": "2",
    "wednesday": "3",
    "thursday": "4",
    "friday": "5",
    "saturday": "6",
    "sunday": "0",
}


def _prompt_hhmm() -> tuple[int, int]:
    raw_time = typer.prompt("Time (HH:MM, 24-hour)")
    try:
        h_str, m_str = raw_time.strip().split(":")
        hour, minute = int(h_str), int(m_str)
    except ValueError:
        typer.echo("Invalid time format. Use HH:MM (e.g. 09:00).", err=True)
        raise typer.Exit(code=1)  # noqa: B904
    if not (0 <= hour <= 23 and 0 <= minute <= 59):  # noqa: PLR2004
        typer.echo("Hour must be 0-23 and minute 0-59.", err=True)
        raise typer.Exit(code=1)
    return hour, minute


def _prompt_dow() -> str:
    raw_day = typer.prompt("Day of week (monday-sunday)").strip().lower()
    if raw_day not in _DOW_MAP:
        typer.echo(f"Invalid day '{raw_day}'.", err=True)
        raise typer.Exit(code=1)
    return _DOW_MAP[raw_day]


def _prompt_dom() -> str:
    raw_dom = typer.prompt("Day of month (1-28)")
    dom_int = int(raw_dom)
    if not 1 <= dom_int <= 28:  # noqa: PLR2004
        typer.echo("Day of month must be between 1 and 28.", err=True)
        raise typer.Exit(code=1)
    return str(dom_int)


@daily_report_app.command("schedule")
def daily_report_schedule() -> None:
    """Interactively schedule the daily report cron job."""
    frequency = typer.prompt("Frequency (hourly/daily/weekly/monthly)").strip().lower()
    valid = {"hourly", "daily", "weekly", "monthly"}
    if frequency not in valid:
        typer.echo(f"Invalid frequency '{frequency}'. Choose from: {', '.join(sorted(valid))}", err=True)
        raise typer.Exit(code=1)

    minute: int
    hour: int | str = "*"
    dow = "*"
    dom = "*"

    if frequency == "hourly":
        raw_minute = typer.prompt("Minute past the hour (0-59)")
        minute = int(raw_minute)
        if not 0 <= minute <= 59:  # noqa: PLR2004
            typer.echo("Minute must be between 0 and 59.", err=True)
            raise typer.Exit(code=1)
    else:
        hour, minute = _prompt_hhmm()
        if frequency == "weekly":
            dow = _prompt_dow()
        elif frequency == "monthly":
            dom = _prompt_dom()

    try:
        set_daily_report_schedule(minute=minute, hour=hour, dow=dow, dom=dom)
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)  # noqa: B904

    job = get_daily_report_job()
    description = describe_schedule(job) if job else "unknown"
    typer.echo(f"Daily report scheduled. Schedule: {description}")


@daily_report_app.command("unschedule")
def daily_report_unschedule() -> None:
    """Remove the daily report cron job."""
    removed = remove_daily_report_job()
    if removed:
        typer.echo("Daily report unscheduled.")
    else:
        typer.echo("Daily report was not scheduled.")


if __name__ == "__main__":
    app()
