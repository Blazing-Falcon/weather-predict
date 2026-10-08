from typing import Annotated

import typer

from nowcast import __version__
from nowcast.doctor import doctor
from nowcast.registry.cli import app as registry_app
from nowcast.silver.bmkg import parse as bmkg_parse
from nowcast.sources.bmkg import tick as bmkg_tick
from nowcast.sources.imerg import backfill as imerg_backfill
from nowcast.sources.imerg import gaps as imerg_gaps
from nowcast.sources.imerg import tick as imerg_tick

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
imerg_app = typer.Typer(no_args_is_help=True, help="Archive IMERG Early rainfall and report gaps.")
imerg_app.command("backfill")(imerg_backfill)
imerg_app.command("tick")(imerg_tick)
imerg_app.command("gaps")(imerg_gaps)
app.add_typer(imerg_app, name="imerg")


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"nowcast {__version__}")
        raise typer.Exit()
