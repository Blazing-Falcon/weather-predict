import io
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

import h5py
import httpx
import numpy as np
from numpy.typing import NDArray

from nowcast import config, http
from nowcast.log import get_logger
from nowcast.storage import write_json

EARTH_RADIUS_KM = 6371.0072
MERGIR_PATH = config.CONFIG_DIR / "geo" / "mergir_grid.json"


def _edges(centres: NDArray[np.float64]) -> NDArray[np.float64]:
    steps = np.diff(centres)
    return np.r_[centres[0] - steps[0] / 2, centres[:-1] + steps / 2, centres[-1] + steps[-1] / 2]


@dataclass(frozen=True)
class Grid:
    name: str
    lon: NDArray[np.float64]
    lat: NDArray[np.float64]
    width: int = field(init=False)
    height: int = field(init=False)
    lon_edges: NDArray[np.float64] = field(init=False)
    lat_edges: NDArray[np.float64] = field(init=False)
    cell_km2: NDArray[np.float64] = field(init=False)

    def __post_init__(self) -> None:
        lon = np.array(self.lon, dtype=np.float64)
        lat = np.array(self.lat, dtype=np.float64)
        lon_edges = _edges(lon)
        lat_edges = _edges(lat)
        # Use the mean width because rounded MERGIR longitudes have slightly different steps.
        lon_step = (lon_edges[-1] - lon_edges[0]) / len(lon)
        cell_km2 = (
            EARTH_RADIUS_KM**2 * np.deg2rad(lon_step) * np.diff(np.sin(np.deg2rad(lat_edges)))
        )
        for name, values in (
            ("lon", lon),
            ("lat", lat),
            ("lon_edges", lon_edges),
            ("lat_edges", lat_edges),
            ("cell_km2", cell_km2),
        ):
            values.setflags(write=False)
            object.__setattr__(self, name, values)
        object.__setattr__(self, "width", len(lon))
        object.__setattr__(self, "height", len(lat))


def imerg_grid() -> Grid:
    source = config.settings()["imerg"]
    west, east = source["lon_index"]
    south, north = source["lat_index"]
    lon = (2 * (np.arange(west, east + 1) - 1800) + 1) / 20
    lat = (2 * (np.arange(south, north + 1) - 900) + 1) / 20
    return Grid("imerg", lon, lat)


def mergir_grid() -> Grid:
    data = json.loads(MERGIR_PATH.read_text(encoding="utf-8"))
    return Grid("mergir", np.array(data["lon"]), np.array(data["lat"]))


def fetch_mergir_grid() -> None:
    settings = config.settings()
    source = settings["mergir"]
    with http.client(timeout=30) as client:
        response = http.get(
            client,
            settings["cmr"]["granules"],
            params={
                "collection_concept_id": source["collection"],
                "sort_key": "-start_date",
                "page_size": 1,
            },
        ).raise_for_status()
    entries = response.json()["feed"]["entry"]
    if not entries:
        raise ValueError("CMR returned no MERGIR granules")

    title = entries[0]["title"]
    constraints = []
    for axis in ("lat", "lon"):
        first, last = source[f"{axis}_index"]
        constraints.append(f"/{axis}[{first}:1:{last}]")
    constraint = quote(";".join(constraints), safe="/:;")
    url = f"{source['opendap']}{quote(title, safe='')}.dap.nc4?dap4.ce={constraint}"
    try:
        with http.client(auth=True, timeout=30) as client:
            content = client.get(url).raise_for_status().content
    except httpx.HTTPStatusError as error:
        raise RuntimeError(
            f"MERGIR DAP4 request returned HTTP {error.response.status_code}"
        ) from None
    except httpx.TransportError as error:
        raise RuntimeError(f"MERGIR DAP4 request failed ({type(error).__name__})") from None

    data = {"source": title, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds")}
    logger = get_logger(__name__)
    logger.info("MERGIR source %s", title)
    with h5py.File(io.BytesIO(content), "r") as file:
        for axis, endpoints in (("lat", (-11.4797, 6.4948)), ("lon", (94.0198, 141.9665))):
            values = np.round(np.asarray(file[axis], dtype=np.float64), 6)
            first, last = source[f"{axis}_index"]
            if values.shape != (last - first + 1,):
                raise ValueError(f"Unexpected MERGIR {axis} shape {values.shape}")
            steps = np.diff(values)
            logger.info(
                "MERGIR %s (%d centres) %.6f to %.6f, step min %.6f, max %.6f degrees",
                axis,
                len(values),
                values[0],
                values[-1],
                steps.min(),
                steps.max(),
            )
            if not (steps > 0).all() or tuple(values[[0, -1]].round(4)) != endpoints:
                raise ValueError(
                    f"Unexpected MERGIR {axis} coordinates, expected endpoints {endpoints}"
                )
            data[axis] = values.tolist()

    write_json(MERGIR_PATH, data, allow_nan=False)
    logger.info("Wrote %s", MERGIR_PATH)
