"""Whobot's command line, for checking a host's config and its Nodes without Slack.

Commands read ``./whobot.toml``, so run them from the directory holding it.
"""

import asyncio
import logging
from pathlib import Path

import typer

from pqn_whobot.config import WhobotSettings
from pqn_whobot.config import config_path
from pqn_whobot.registry import Node
from pqn_whobot.registry import resolve_nodes

logging.basicConfig(level=logging.INFO)
# httpx logs every request at INFO, which buries Whobot's own output in a listing.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, help="CLI for Whobot, the PQN Network operations bot.")


@app.callback()
def main() -> None:
    """Keep subcommands addressable by name, which Typer collapses while there is only one."""


def _load() -> WhobotSettings:
    """Load ``./whobot.toml``, or exit naming the file and what is wrong with it.

    Checks the file exists first, since loading an absent one succeeds and yields defaults.
    """
    path = config_path()
    if not path.is_file():
        typer.echo(f"No {path} in {Path.cwd()}. Copy configs/whobot_example.toml there and fill it in.", err=True)
        raise typer.Exit(code=1)

    try:
        return WhobotSettings()
    # Bad syntax (TOMLDecodeError) and bad values (ValidationError) are both ValueErrors.
    except (OSError, ValueError) as e:
        typer.echo(f"Could not load {path.resolve()}:\n{e}", err=True)
        raise typer.Exit(code=1) from None


def _node_line(node: Node, name_width: int) -> str:
    columns = f"  {node.name:<{name_width}}  {node.api_url}"
    if not node.reachable:
        return f"{columns}  UNREACHABLE — {node.error}"
    latency = f"{node.latency_ms:.0f}ms" if node.latency_ms is not None else "ok"
    state = f"reachable ({latency})"
    return f"{columns}  {state} — {node.warning}" if node.warning else f"{columns}  {state}"


@app.command()
def nodes() -> None:
    """List every Node in the registry with its resolved name and reachability."""
    settings = _load()
    if not settings.nodes:
        typer.echo("No Nodes registered. Add a [[nodes]] entry with an api_url to whobot.toml.")
        raise typer.Exit(code=1)

    resolved = asyncio.run(resolve_nodes(settings))
    name_width = max(len(node.name) for node in resolved)
    unreachable = [node for node in resolved if not node.reachable]
    warned = [node for node in resolved if node.reachable and node.warning]

    typer.echo(f"{len(resolved)} Node(s) in {config_path().resolve()}:")
    for node in resolved:
        typer.echo(_node_line(node, name_width))

    if warned:
        typer.echo(f"\n{len(warned)} of {len(resolved)} reachable with warnings.")
    if unreachable:
        typer.echo(f"\n{len(unreachable)} of {len(resolved)} unreachable.")
        # Only an unreachable Node fails the command: a warning is for a human to read, and a
        # partly-deployed fleet is a normal state that shouldn't look like an outage.
        raise typer.Exit(code=1)


@app.command()
def serve() -> None:
    """Connect to Slack and stay up, answering /whobot until stopped."""
    settings = _load()

    missing = [
        name
        for name, value in (
            ("slack_bot_token", settings.slack_bot_token),
            ("slack_app_token", settings.slack_app_token),
        )
        if not value
    ]
    if missing:
        # Socket Mode needs both, and the failure without one is an opaque Slack error, so
        # it is worth naming exactly which is absent.
        typer.echo(
            f"{config_path()} is missing {' and '.join(missing)}. "
            "See configs/whobot_example.toml for where each token comes from.",
            err=True,
        )
        raise typer.Exit(code=1)

    if not settings.nodes:
        typer.echo("Warning: no Nodes registered, so every Node Action will have nothing to offer.", err=True)

    # Imported here rather than at module scope because the Slack libraries are an optional
    # extra: a Node installs neither, and `whobot nodes` must keep working without them.
    try:
        from slack_sdk.errors import SlackApiError  # noqa: PLC0415

        from pqn_whobot.whobot_slack import WhobotSlack  # noqa: PLC0415
    except ImportError as e:
        typer.echo(f"Whobot's Slack support is not installed ({e.name}).", err=True)
        typer.echo("  Install it with: uv sync --extra whobot", err=True)
        raise typer.Exit(code=1) from None

    bot = WhobotSlack(settings)

    async def run() -> None:
        # Check the tokens first: start_async retries a rejected one forever rather than
        # raising, so without this a bad token looks like a bot that started and then
        # quietly never answered.
        await bot.check_credentials()
        typer.echo(f"Connected. {len(settings.nodes)} Node(s) registered. Ctrl-C to stop.")
        await bot.serve()

    typer.echo("Whobot connecting to Slack…")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        # asyncio.run has already cancelled the loop; serve's finally block reported any
        # Action that was interrupted, so there is nothing to add but a clean exit.
        typer.echo("Stopped.")
    except SlackApiError as e:
        error = e.response.get("error", "unknown")
        typer.echo(f"Slack rejected Whobot's credentials: {error}.", err=True)
        typer.echo(
            "  slack_bot_token is the 'xoxb-' Bot User OAuth token (OAuth & Permissions).\n"
            "  slack_app_token is the 'xapp-' app-level token with connections:write (Socket Mode).",
            err=True,
        )
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app(["serve"])
