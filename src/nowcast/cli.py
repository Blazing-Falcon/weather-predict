from typing import Annotated

import typer

from nowcast import __version__

app = typer.Typer(
    no_args_is_help=True,
    help="Rain nowcast for every kab/kota in Indonesia.",
)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"nowcast {__version__}")
        raise typer.Exit()
