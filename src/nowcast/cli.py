from typing import Annotated

import typer

from nowcast import __version__
from nowcast.doctor import doctor
from nowcast.registry.cli import app as registry_app
from nowcast.silver.bmkg import parse as bmkg_parse
from nowcast.sources.bmkg import tick as bmkg_tick

app = typer.Typer(
    no_args_is_help=True,
    help="Rain nowcast for every kab/kota in Indonesia.",
)
app.command()(doctor)
app.add_typer(registry_app, name="registry")
bmkg_app = typer.Typer(no_args_is_help=True, help="Archive live BMKG forecasts.")
bmkg_app.command("tick")(bmkg_tick)
bmkg_app.command("parse")(bmkg_parse)
app.add_typer(bmkg_app, name="bmkg")


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"nowcast {__version__}")
        raise typer.Exit()
