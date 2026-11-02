import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi import Path as PathParam
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nowcast import config, paths
from nowcast.api import queries

app = FastAPI(title="Nowcast API")
app.add_middleware(GZipMiddleware, compresslevel=4)
LIVE_CACHE = "public, max-age=60"
STATIC_CACHE = "public, max-age=86400"
CACHE_TTL = 60
_cache = {}
_cache_lock = Lock()


def archive():
    with queries.DB.cursor() as connection:
        yield queries.Archive(paths.data_root(), connection)


Archive = Annotated[queries.Archive, Depends(archive)]
AreaId = Annotated[int, PathParam(ge=0, lt=queries.AREA_COUNT)]
Lead = Annotated[int, Query(ge=1, le=3)]
Layer = Literal[
    "obs", "cold", "prob1", "prob2", "prob3", "bmkg", "bmkg_code", "bmkg_step", "bmkg_age", "disagree"
]


def cached(query, data, now):
    with _cache_lock:
        key = query, data.root
        result, expires = _cache.get(key, (None, 0))
        if monotonic() >= expires:
            result = query(data, now)
            _cache[key] = (result, monotonic() + CACHE_TTL)
        return result


@app.get("/api/meta", summary="Get source freshness, map assets and the area catalog")
def meta(response: Response, data: Archive):
    response.headers["Cache-Control"] = LIVE_CACHE
    return cached(queries.meta, data, datetime.now(UTC))


@app.get("/api/frames", summary="Get map frames for one layer")
def frames(
    data: Archive,
    layer: Layer = "prob3",
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
):
    now = datetime.now(UTC)
    first = from_ if from_ is not None else now - timedelta(hours=48)
    last = to if to is not None else now + timedelta(hours=3)
    if first.utcoffset() is None or last.utcoffset() is None:
        raise HTTPException(422, "Use timestamps with a UTC offset.")
    first, last = first.astimezone(UTC), last.astimezone(UTC)
    if not timedelta(0) <= last - first <= timedelta(days=31):
        raise HTTPException(422, "The frame window must be in order and span at most 31 days.")
    cache = STATIC_CACHE if last < now - timedelta(hours=6) else LIVE_CACHE
    return JSONResponse(data.frames(layer, first, last), headers={"Cache-Control": cache})


def area_metadata(data, area_id):
    areas = data.areas(area_id)
    if not areas and (data.root / queries.AREAS).is_file():
        raise HTTPException(404, "Area not found.")
    return areas[0] if areas else None


@app.get("/api/areas/{area_id}", summary="Get an area's recent weather, scores and climatology")
def area_detail(response: Response, area_id: AreaId, data: Archive):
    response.headers["Cache-Control"] = LIVE_CACHE
    return queries.area_detail(data, area_id, area_metadata(data, area_id), datetime.now(UTC))


@app.get("/api/alerts", summary="Get model or BMKG rain alerts for the latest model issue")
def alerts(
    response: Response,
    data: Archive,
    level: Annotated[
        str, Query(description="Minimum level: Waspada, Siaga, Awas, or 1–3.")
    ] = "Waspada",
    island: str | None = None,
    province: str | None = None,
    threshold: Annotated[int, Query(ge=0, le=100)] = 50,
    source: Literal["model", "bmkg"] = "model",
):
    response.headers["Cache-Control"] = LIVE_CACHE
    rank = {"waspada": 1, "siaga": 2, "awas": 3, "1": 1, "2": 2, "3": 3}.get(level.casefold())
    if source == "model" and rank is None:
        raise HTTPException(422, "Choose Waspada, Siaga or Awas.")
    return queries.alerts(data, threshold, rank, island, province, source)


@app.get("/api/scores", summary="Get daily forecast scores by scope and lead")
def scores(
    response: Response,
    data: Archive,
    scope: Literal["national", "island", "province", "area"] | None = None,
    id: str | None = None,
    lead: Lead | None = None,
    days: Annotated[int, Query(ge=1)] = 30,
):
    response.headers["Cache-Control"] = LIVE_CACHE
    return data.scores(datetime.now(UTC), days, scope, id, lead)


@app.get("/api/model", summary="Get model cards, version history and live verification")
def model(response: Response, data: Archive):
    response.headers["Cache-Control"] = LIVE_CACHE
    return queries.model(data, datetime.now(UTC))


@app.get("/api/history/{area_id}", summary="Get climatology, rainfall totals and wet-season timing")
def history(response: Response, area_id: AreaId, data: Archive):
    response.headers["Cache-Control"] = LIVE_CACHE
    return queries.history(data, area_id, area_metadata(data, area_id))


@app.get("/api/pipeline", summary="Get job health, latency, storage and recent Dagster runs")
def pipeline(response: Response, data: Archive):
    response.headers["Cache-Control"] = LIVE_CACHE
    return cached(queries.pipeline, data, datetime.now(UTC))


def static_file(path: Path, media_type: str):
    if not path.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": STATIC_CACHE})


def tile(layer, timestamp, suffix, media_type):
    if not re.fullmatch(r"[0-9]{8}T[0-9]{4}Z", timestamp):
        raise HTTPException(404, "Tile not found.")
    return static_file(
        paths.data_root() / "gold" / "tiles" / layer / (timestamp + suffix), media_type
    )


@app.get("/tiles/rain/{slot}.png", summary="Get an IMERG rain tile")
def rain_tile(slot: str):
    return tile("rain", slot, ".png", "image/png")


@app.get("/tiles/cloud/{scan}.webp", summary="Get a Himawari cloud tile")
def cloud_tile(scan: str):
    return tile("cloud", scan, ".webp", "image/webp")


@app.get("/geo/kabkota.geojson", summary="Get Indonesian kabupaten and kota boundaries")
def kabkota():
    return static_file(config.CONFIG_DIR / "geo" / "kabkota.geojson", "application/geo+json")


@app.get("/geo/neighbours.geojson", summary="Get neighbouring land boundaries")
def neighbours():
    return static_file(config.CONFIG_DIR / "geo" / "neighbours.geojson", "application/geo+json")


app.mount("/", StaticFiles(directory=config.REPO_ROOT / "web", html=True), name="web")
