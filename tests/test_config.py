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
