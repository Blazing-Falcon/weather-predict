import json
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
from PIL import Image

from nowcast import config
from nowcast.model import baselines, metrics, train
from nowcast.model.train import split_filters


def test_probability_scores():
    y = np.array([1, 0, 1, 0])
    p = np.array([0.75, 0.75, 0.25, 0.0])
    score = metrics.brier(y, p)
    assert score == 19 / 64
    assert metrics.bss(score, 0.25) == -3 / 16
    assert metrics.bss(score, 0) is None
    assert metrics.auc(y, p) == 0.625
    assert metrics.auc(np.zeros(4, dtype=int), p) is None
    assert metrics.best_csi_threshold(y, p) == 0.25
    hits, false_alarms, misses = metrics.contingency(y, p, 0.5)
    assert metrics.csi(hits, false_alarms, misses) == 1 / 3
    assert metrics.pod(hits, misses) == 0.5
    assert metrics.far(hits, false_alarms) == 0.5
    assert metrics.far(0, 0) is None


def test_reliability_bins():
    y = np.array([0, 1, 1, 0, 1, 1])
    p = np.array([0.0, 0.08, 0.1, 0.3, 0.9, 1.0])
    bins = metrics.reliability(y, p)
    assert [row["count"] for row in bins] == [2, 1, 0, 1, 0, 0, 0, 0, 0, 2]
    assert bins[0]["mean_predicted"] == 0.04
    assert bins[0]["observed_frequency"] == 0.5
    assert bins[2]["mean_predicted"] is None
    assert bins[2]["observed_frequency"] is None
    assert bins[-1]["mean_predicted"] == 0.95
    assert bins[-1]["observed_frequency"] == 1.0


def test_motion_threshold():
    y = np.array([0, 0, 1, 1, 0])
    adv = np.array([0.0, 16.0, 20.0, 65.0, np.nan])
    assert baselines.motion_threshold(y, adv) == 17
    assert baselines.motion_threshold(np.array([0, 1]), np.array([59, 65])) == 60


def test_split_dates_are_disjoint():
    times = [datetime(2025, 1, day, hour, tzinfo=UTC) for day in range(1, 6) for hour in (0, 23)]
    dataset = ds.dataset(pa.table({"t": times}))
    periods = {
        "train": ["2025-01-02", "2025-01-02"],
        "valid": ["2025-01-03", "2025-01-03"],
        "test": ["2025-01-04", "2025-01-04"],
    }
    actual = {
        name: dataset.to_table(filter=condition)["t"].to_pylist()
        for name, condition in split_filters(periods).items()
    }
    assert actual == {"train": times[2:4], "valid": times[4:6], "test": times[6:8]}
    periods["valid"][0] = "2025-01-02"
    with pytest.raises(ValueError, match="overlap"):
        split_filters(periods)


@pytest.mark.parametrize("extra_split", [False, True])
def test_train_scores_every_test_split(tmp_path, monkeypatch, capsys, extra_split):
    periods = {
        "train": ["2025-01-01"] * 2,
        "valid": ["2025-01-02"] * 2,
        "test": ["2025-01-03"] * 2,
    }
    dates = [datetime(2025, 1, day, tzinfo=UTC) for day in (1, 2, 3)]
    if extra_split:
        periods["test_2026"] = ["2026-01-01"] * 2
        dates.append(datetime(2026, 1, 1, tzinfo=UTC))
    count = len(dates)
    y = [1, 0, 1, 0] * count
    clim = [75.0, 25.0, 75.0, 25.0] * 3
    if extra_split:
        clim += [25.0, 75.0, 25.0, 75.0]
    table = pa.table({
        "t": [day for day in dates for _ in range(4)],
        "area_id": [4, 3, 2, 1] * count,
        "rain_lag": [1.0, 0.0, 2.0, 0.0] * count,
        "adv_max_pct": [50.0, 5.0, 30.0, 10.0] * count,
        "island": ["Jawa"] * len(y),
        **{f"clim_p{lead}": clim for lead in (1, 2, 3)},
        **{f"y{lead}": y for lead in (1, 2, 3)},
    })
    features = tmp_path / "tmp" / "features" / f"{dates[0]:%Y-%m-%d}_{dates[-1]:%Y-%m-%d}"
    features.mkdir(parents=True)
    pq.write_table(table, features / "part.parquet")

    model = config.settings()["model"]
    monkeypatch.setitem(model, "split", periods)
    monkeypatch.setitem(model, "features", ["rain_lag", "adv_max_pct", "island"])
    monkeypatch.setitem(model, "lgbm", model["lgbm"] | {
        "n_estimators": 5, "min_child_samples": 1, "n_jobs": 1,
    })
    monkeypatch.setitem(model, "early_stopping_rounds", 2)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(train, "subprocess", SimpleNamespace(check_output=lambda *a, **k: "abc"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))

    kwargs = {"figure_prefix": "full-run"} if extra_split else {}
    train.train(keep_features=True, check_repro=True, **kwargs)

    card = json.loads(next((tmp_path / "models").glob("*/card.json")).read_text())
    for result in card["leads"].values():
        assert result["rows_per_split"] == dict.fromkeys(periods, 4)
        assert list(result["metrics"]) == list(periods)[1:]
        assert list(result["reliability"]) == list(periods)[1:]
        for name, scores in result["metrics"].items():
            assert set(scores) == {"model", "clim", "persist", "motion"}
            assert scores["clim"]["brier"] == (0.5625 if name == "test_2026" else 0.0625)
            assert scores["persist"]["brier"] == 0.0
            assert all(score["n"] == 4 for score in scores.values())

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[0] == "split"
    assert [line.split()[0] for line in lines[1:-2]] == [
        name for name in list(periods)[2:] for _ in range(12)
    ]
    assert lines[-1] == "repro ok"
    images = tmp_path / "docs" / "progress" / "img"
    prefix = kwargs.get("figure_prefix", "step-06")
    with Image.open(images / f"{prefix}-reliability.png") as image:
        assert image.size == (1800, 600 * (count - 2))
    assert (images / f"{prefix}-importance.png").is_file()
