from typing import Annotated

import typer

from nowcast import __version__
from nowcast.doctor import doctor
from nowcast.registry.cli import app as registry_app

app = typer.Typer(
    no_args_is_help=True,
    help="Rain nowcast for every kab/kota in Indonesia.",
)
app.command()(doctor)
app.add_typer(registry_app, name="registry")


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"nowcast {__version__}")
        raise typer.Exit()
