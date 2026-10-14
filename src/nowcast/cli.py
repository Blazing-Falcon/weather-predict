from typing import Annotated

import typer

from nowcast import __version__
from nowcast.doctor import doctor
from nowcast.features import build, climatology
from nowcast.gold import frames, gauge_check, tiles
from nowcast.model import score, scores_daily, verify
from nowcast.model.train import train
from nowcast.registry.cli import app as registry_app
from nowcast.retention import disk, retention
from nowcast.silver.bmkg import parse as bmkg_parse
from nowcast.sources import gauges
from nowcast.sources.bmkg import tick as bmkg_tick
from nowcast.sources.himawari import tick as himawari_tick
from nowcast.sources.imerg import backfill as imerg_backfill
from nowcast.sources.imerg import gaps as imerg_gaps
from nowcast.sources.imerg import tick as imerg_tick
from nowcast.sources.mergir import backfill as mergir_backfill
from nowcast.sources.mergir import gaps as mergir_gaps

app = typer.Typer(
    no_args_is_help=True,
    help="Rain nowcast for every kab/kota in Indonesia.",
)
app.command()(doctor)
app.command()(retention)
app.command()(disk)
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
mergir_app = typer.Typer(
    no_args_is_help=True, help="Archive MERGIR cloud features and report gaps."
)
mergir_app.command("backfill")(mergir_backfill)
mergir_app.command("gaps")(mergir_gaps)
app.add_typer(mergir_app, name="mergir")
himawari_app = typer.Typer(no_args_is_help=True, help="Archive live Himawari cloud features.")
himawari_app.command("tick")(himawari_tick)
app.add_typer(himawari_app, name="himawari")
gauges_app = typer.Typer(no_args_is_help=True, help="Archive historical and live rain gauges.")
gauges_app.command()(gauges.stations)
gauges_app.command()(gauges.ghcnh)
gauges_app.command()(gauges.metar)
app.add_typer(gauges_app, name="gauges")
features_app = typer.Typer(no_args_is_help=True, help="Build issue-time features and climatology.")
features_app.command("build")(build.build)
features_app.command("climatology")(climatology.climatology)
app.add_typer(features_app, name="features")
model_app = typer.Typer(no_args_is_help=True, help="Train and evaluate nowcast models.")
model_app.command("train")(train)
app.add_typer(model_app, name="model")
live_app = typer.Typer(no_args_is_help=True, help="Issue and verify live nowcast forecasts.")
live_app.command()(score.score)
live_app.command()(verify.verify)
live_app.command()(scores_daily.daily)
app.add_typer(live_app, name="live")
gold_app = typer.Typer(no_args_is_help=True, help="Build map layers and check rain gauges.")
gold_app.command()(frames.frames)
gold_app.command("tiles")(tiles.backfill)
gold_app.command("gauge-check")(gauge_check.gauge_check)
app.add_typer(gold_app, name="gold")


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(f"nowcast {__version__}")
        raise typer.Exit()


@app.command()
def api(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Bind port.")] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Reload on source changes.")] = False,
) -> None:
    """Serve the read-only Nowcast API."""
    import uvicorn

    uvicorn.run("nowcast.api.app:app", host=host, port=port, reload=reload)
