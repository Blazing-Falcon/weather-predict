import hashlib
import json
import os
from datetime import UTC, datetime

from nowcast import http, paths
from nowcast.log import get_logger

BOUNDARIES_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IDN/ADM2/"
    "geoBoundaries-IDN-ADM2.geojson"
)
BOUNDARIES_NAME = "kabkota_full.geojson"


def download(force: bool) -> None:
    output = paths.zone("ref") / BOUNDARIES_NAME
    logger = get_logger(__name__)
    if output.exists() and not force:
        logger.info("Skipping existing %s (%d bytes)", output, output.stat().st_size)
        return

    with http.client(timeout=60.0) as client:
        response = http.get(client, BOUNDARIES_URL)
        response.raise_for_status()
    content = response.content
    collection = json.loads(content)
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("Expected a geoBoundaries GeoJSON FeatureCollection")
    features = collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("The geoBoundaries FeatureCollection has no features")

    metadata = {
        "url": BOUNDARIES_URL,
        "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    source = output.with_suffix(".source.json")
    output_temp = output.with_suffix(".geojson.tmp")
    source_temp = source.with_suffix(".json.tmp")
    target = output_temp.open("xb")
    try:
        with target:
            target.write(content)
        source_temp.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        os.replace(source_temp, source)
        os.replace(output_temp, output)
    except BaseException:
        source_temp.unlink(missing_ok=True)
        output_temp.unlink(missing_ok=True)
        raise

    logger.info("Downloaded %s (%d bytes, %d features)", output, len(content), len(features))
