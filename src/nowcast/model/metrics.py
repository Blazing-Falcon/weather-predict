import numpy as np


def brier(y, p) -> float:
    return float(np.mean(np.square(p - y)))


def bss(score: float, clim_score: float) -> float | None:
    return 1 - score / clim_score if clim_score else None


def contingency(y, p, threshold: float) -> tuple[int, int, int]:
    wet = p >= threshold
    hits = np.count_nonzero(wet & (y == 1))
    false_alarms = np.count_nonzero(wet & (y == 0))
    misses = np.count_nonzero(~wet & (y == 1))
    return hits, false_alarms, misses


def csi(hits: int, false_alarms: int, misses: int) -> float | None:
    total = hits + false_alarms + misses
    return hits / total if total else None


def pod(hits: int, misses: int) -> float | None:
    return hits / (hits + misses) if hits + misses else None


def far(hits: int, false_alarms: int) -> float | None:
    return false_alarms / (hits + false_alarms) if hits + false_alarms else None


def _ranked_counts(y, p):
    order = np.argsort(p)[::-1]
    ranked = p[order]
    ends = np.r_[np.flatnonzero(ranked[:-1] != ranked[1:]), len(ranked) - 1]
    hits = np.cumsum(y[order])[ends]
    return ranked[ends], hits, ends + 1 - hits


def best_csi_threshold(y, p) -> float:
    thresholds, hits, false_alarms = _ranked_counts(y, p)
    totals = hits[-1] + false_alarms
    scores = np.divide(hits, totals, out=np.zeros(len(hits)), where=totals != 0)
    return float(thresholds[np.argmax(scores)])


def auc(y, p) -> float | None:
    _, hits, false_alarms = _ranked_counts(y, p)
    pairs = int(hits[-1]) * int(false_alarms[-1])
    if not pairs:
        return None
    return float(np.trapezoid(np.r_[0, hits], np.r_[0, false_alarms]) / pairs)


def reliability(y, p) -> list[dict]:
    edges = np.arange(11) / 10
    bins = np.searchsorted(edges[1:-1], p, side="right")
    counts = np.bincount(bins, minlength=10)
    predicted = np.bincount(bins, weights=p, minlength=10)
    observed = np.bincount(bins, weights=y, minlength=10)
    return [
        {
            "lower": float(edges[i]),
            "upper": float(edges[i + 1]),
            "mean_predicted": float(predicted[i] / count) if count else None,
            "observed_frequency": float(observed[i] / count) if count else None,
            "count": int(count),
        }
        for i, count in enumerate(counts)
    ]


def scores(y, p, clim_score: float, threshold: float, *, with_auc: bool) -> dict:
    hits, false_alarms, misses = contingency(y, p, 0.5)
    best_hits, best_false_alarms, best_misses = contingency(y, p, threshold)
    score = brier(y, p)
    return {
        "n": len(y),
        "base_rate": float(np.mean(y)),
        "brier": score,
        "bss": bss(score, clim_score),
        "csi_at_0.5": csi(hits, false_alarms, misses),
        "csi_at_best": csi(best_hits, best_false_alarms, best_misses),
        "pod_at_0.5": pod(hits, misses),
        "far_at_0.5": far(hits, false_alarms),
        "pod_at_best": pod(best_hits, best_misses),
        "far_at_best": far(best_hits, best_false_alarms),
        "auc": auc(y, p) if with_auc else None,
    }
