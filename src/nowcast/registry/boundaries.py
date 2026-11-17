import hashlib
import json

from nowcast import http, paths
from nowcast.log import get_logger
from nowcast.storage import atomic_write, write_json
from nowcast.timeslots import utc_now

BOUNDARIES_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen/IDN/ADM2/"
    "geoBoundaries-IDN-ADM2.geojson"
)
BOUNDARIES_NAME = "kabkota_full.geojson"
BOUNDARIES_SHA256 = "947cedc058888c0c2b2628071d289956c85fe279adb369dcf1327ce2236a5602"


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
    digest = hashlib.sha256(content).hexdigest()
    if digest != BOUNDARIES_SHA256:
        raise ValueError(f"geoBoundaries sha256 mismatch: expected {BOUNDARIES_SHA256}, found {digest}")
    collection = json.loads(content)
    if not isinstance(collection, dict) or collection.get("type") != "FeatureCollection":
        raise ValueError("Expected a geoBoundaries GeoJSON FeatureCollection")
    features = collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("The geoBoundaries FeatureCollection has no features")

    metadata = {
        "url": BOUNDARIES_URL,
        "bytes": len(content),
        "sha256": digest,
        "fetched_at": utc_now(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    source = output.with_suffix(".source.json")
    with atomic_write(output) as file:
        file.write(content)
        write_json(source, metadata)

    logger.info("Downloaded %s (%d bytes, %d features)", output, len(content), len(features))
