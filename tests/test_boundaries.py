import hashlib
import json

import httpx
import pytest

from nowcast import http
from nowcast.registry import boundaries

CONTENT = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":null}]}'


@pytest.mark.parametrize("existing", [False, True])
def test_checksum_mismatch_does_not_publish(tmp_path, monkeypatch, existing):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(http, "client", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=CONTENT)),
    ))
    output = tmp_path / "ref" / boundaries.BOUNDARIES_NAME
    metadata = output.with_suffix(".source.json")
    if existing:
        output.parent.mkdir()
        output.write_bytes(b"previous boundaries")
        metadata.write_bytes(b"previous metadata")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        boundaries.download(force=True)

    assert boundaries.BOUNDARIES_SHA256 == (
        "947cedc058888c0c2b2628071d289956c85fe279adb369dcf1327ce2236a5602"
    )
    if existing:
        assert output.read_bytes() == b"previous boundaries"
        assert metadata.read_bytes() == b"previous metadata"
    else:
        assert not output.exists()
        assert not metadata.exists()


def test_matching_checksum_publishes_content_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(http, "client", lambda **kw: httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=CONTENT)),
    ))
    digest = hashlib.sha256(CONTENT).hexdigest()
    monkeypatch.setattr(boundaries, "BOUNDARIES_SHA256", digest)

    boundaries.download(force=False)

    output = tmp_path / "ref" / boundaries.BOUNDARIES_NAME
    assert output.read_bytes() == CONTENT
    metadata = json.loads(output.with_suffix(".source.json").read_text())
    assert metadata == {
        "url": boundaries.BOUNDARIES_URL, "bytes": len(CONTENT), "sha256": digest,
        "fetched_at": metadata["fetched_at"],
    }
    assert metadata["fetched_at"].endswith("Z")
