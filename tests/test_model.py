from datetime import UTC, datetime

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pytest

from nowcast.model import baselines, metrics
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
