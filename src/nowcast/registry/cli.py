from enum import StrEnum
from typing import Annotated

import typer

from nowcast.registry.bmkg_points import sweep
from nowcast.registry.boundaries import download
from nowcast.registry.dim_area import build
from nowcast.registry.grids import fetch_mergir_grid
from nowcast.registry.membership import build as build_membership
from nowcast.registry.neighbours import build as build_neighbours

app = typer.Typer(help="Reference tables built once.")


@app.command()
def bmkg_candidates(
    limit: Annotated[int | None, typer.Option(min=0, help="Maximum kecamatan to process.")] = None,
    max_tries: Annotated[int, typer.Option(min=1, help="Villages to try per kecamatan.")] = 3,
) -> None:
    sweep(limit, max_tries)


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


@app.command()
def mergir_grid() -> None:
    """Fetch and verify the MERGIR grid coordinates."""
    fetch_mergir_grid()


class MembershipGrid(StrEnum):
    imerg = "imerg"
    mergir = "mergir"
    all = "all"


@app.command()
def membership(grid: Annotated[MembershipGrid, typer.Option()]) -> None:
    """Build area-weighted grid membership for admin areas."""
    build_membership(grid.value)


@app.command()
def neighbours() -> None:
    """Build border and nearest-neighbour links for admin areas."""
    build_neighbours()
