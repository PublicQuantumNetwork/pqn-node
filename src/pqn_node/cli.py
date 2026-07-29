import logging
from pathlib import Path
from typing import Annotated

import typer

from pqn_node.core.config import config_path
from pqn_node.core.config import write_config

# TODO: check if this way of handling logging from a command line script is ok.
logging.basicConfig(level=logging.INFO)

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, help="CLI for pqn-node.")


@app.callback()
def main() -> None:
    """
    Keep Typer in subcommand mode.

    With a single registered command and no callback, Typer collapses the app and
    `pqn-node toggle-game chsh` becomes `pqn-node chsh`. This existed implicitly while the
    `daily-report` subcommands were here; it has to be explicit now that they are gone.
    """


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


if __name__ == "__main__":
    app()
