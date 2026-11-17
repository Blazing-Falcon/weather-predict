import json
import os
import subprocess
import sys

import pytest

from nowcast import config, paths


def test_settings():
    settings = config.settings()
    assert settings["imerg"]["lon_index"] == [2740, 3219]
    assert settings["mergir"]["lat_index"] == [1333, 1827]
    assert settings["target"]["leads_h"] == [1, 2, 3]


def test_retention():
    retention = config.retention()
    assert retention["budget_gb"] == 8
    assert retention["datasets"]["silver/rain_kabkota"]["keep"] == "10 years"


def test_data_root_default(monkeypatch):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    assert paths.data_root() == config.REPO_ROOT / "data"


def test_data_root_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert paths.data_root() == tmp_path


def test_data_root_relative(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_ROOT", "./local-data")
    assert paths.data_root() == config.REPO_ROOT / "local-data"


def test_ensure_layout(monkeypatch, tmp_path):
    root = tmp_path / "data"
    monkeypatch.setenv("DATA_ROOT", str(root))
    assert paths.ensure_layout() == root
    for name in ("ref", "bronze", "silver", "gold", "models", "state", "tmp"):
        assert paths.zone(name) == root / name
        assert (root / name).is_dir()
    assert (root / "state" / "health").is_dir()


def test_invalid_zone():
    with pytest.raises(ValueError, match="nope"):
        paths.zone("nope")


def test_config_directory_override(tmp_path, monkeypatch):
    (tmp_path / "settings.yaml").write_text("container: settings\n")
    (tmp_path / "retention.yaml").write_text("container: retention\n")
    monkeypatch.setenv("NOWCAST_CONFIG_DIR", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from nowcast import config
from nowcast.registry import grids
from nowcast.sources import gauges
print(json.dumps([
    str(config.CONFIG_DIR), config.settings(), config.retention(),
    str(grids.MERGIR_PATH), str(gauges.GEO),
]))
"""], check=True, capture_output=True, text=True, env=os.environ.copy(),
    )
    assert json.loads(result.stdout) == [
        str(tmp_path), {"container": "settings"}, {"container": "retention"},
        str(tmp_path / "geo" / "mergir_grid.json"), str(tmp_path / "geo"),
    ]


def test_web_directory_override(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>Container Web</h1>")
    monkeypatch.setenv("NOWCAST_WEB_DIR", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", """
import json
from fastapi.testclient import TestClient
from nowcast import config
from nowcast.api.app import app
with TestClient(app) as client:
    response = client.get("/")
    print(json.dumps([str(config.WEB_DIR), response.status_code, response.text]))
"""], check=True, capture_output=True, text=True, env=os.environ.copy(),
    )
    assert json.loads(result.stdout) == [str(tmp_path), 200, "<h1>Container Web</h1>"]
