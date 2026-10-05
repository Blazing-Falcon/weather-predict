from typing import Annotated

import typer

from nowcast.registry.bmkg_points import sweep

app = typer.Typer(help="Reference tables built once.")


@app.command()
def bmkg_candidates(
    limit: Annotated[int | None, typer.Option(min=0, help="Maximum kecamatan to process.")] = None,
) -> None:
    sweep(limit)
