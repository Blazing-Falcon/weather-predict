from typing import Annotated

import typer

from nowcast.registry.bmkg_points import sweep
from nowcast.registry.boundaries import download
from nowcast.registry.dim_area import build

app = typer.Typer(help="Reference tables built once.")


@app.command()
def bmkg_candidates(
    limit: Annotated[int | None, typer.Option(min=0, help="Maximum kecamatan to process.")] = None,
) -> None:
    sweep(limit)


@app.command()
def boundaries(
    force: Annotated[bool, typer.Option(help="Replace the existing download.")] = False,
) -> None:
    """Download the full-resolution boundaries."""
    download(force)


@app.command()
def dim_area() -> None:
    """Build the area dimension from the full-resolution boundaries."""
    build()
