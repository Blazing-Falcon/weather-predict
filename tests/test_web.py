import re
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from nowcast.api.app import app


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = set()

    def handle_starttag(self, tag, attrs):
        self.urls.update(value for name, value in attrs if name in ("href", "src") and value)


def test_web_static_files_and_api_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert client.get("/missing.js").status_code == 404

        for path, media_type in (
            ("/app.js", "text/javascript"),
            ("/js/alerts.js", "text/javascript"),
            ("/js/api.js", "text/javascript"),
            ("/js/charts.js", "text/javascript"),
            ("/js/dom.js", "text/javascript"),
            ("/js/drawer.js", "text/javascript"),
            ("/js/format.js", "text/javascript"),
            ("/js/map.js", "text/javascript"),
            ("/js/overview.js", "text/javascript"),
            ("/js/palette.js", "text/javascript"),
            ("/js/state.js", "text/javascript"),
            ("/js/timeline.js", "text/javascript"),
            ("/pipeline-content.js", "text/javascript"),
            ("/style.css", "text/css"),
            ("/maplibre.css", "text/css"),
            ("/maplibre-gl.js", "text/javascript"),
            ("/api/frames?layer=obs", "application/json"),
            ("/geo/kabkota.geojson", "application/geo+json"),
        ):
            asset = client.get(path)
            assert asset.status_code == 200
            assert asset.headers["content-type"].startswith(media_type)


def test_web_references_use_approved_urls():
    with TestClient(app) as client:
        refs = References()
        refs.feed(client.get("/").text)
        assert len([url for url in refs.urls if url.startswith("#")]) == 7
        assert {url for url in refs.urls if not url.startswith("#")} == {
            "/app.js",
            "/pipeline-content.js",
            "/style.css",
            "/maplibre.css",
            "/maplibre-gl.js",
        }

        script = "\n".join(
            client.get(path).text
            for path in (
                "/app.js",
                "/js/alerts.js",
                "/js/api.js",
                "/js/charts.js",
                "/js/dom.js",
                "/js/drawer.js",
                "/js/format.js",
                "/js/map.js",
                "/js/overview.js",
                "/js/palette.js",
                "/js/state.js",
                "/js/timeline.js",
            )
        )
        urls = set(re.findall(r"""['"`]((?:https?://|/[a-z])[^'"`\s]*)""", script))
        urls.discard("http://www.w3.org/2000/svg")
        assert {url.split("/")[1] for url in urls} == {"api", "tiles", "geo"}
