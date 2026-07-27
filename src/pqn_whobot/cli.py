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

_UNKNOWN_NAME = "(unknown)"


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


def _display_name(node: Node) -> str:
    """Name this Node for the listing, or call it unknown if it never gave one."""
    return node.name or _UNKNOWN_NAME


def _node_line(node: Node, name_width: int) -> str:
    columns = f"  {_display_name(node):<{name_width}}  {node.api_url}"
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
    name_width = max(len(_display_name(node)) for node in resolved)
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


if __name__ == "__main__":
    app()
