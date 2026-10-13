import json
import os
from shutil import rmtree
from subprocess import run
from time import perf_counter

import lightgbm as lgb
import numpy as np
import pyarrow.compute as pc
import pyarrow.dataset as ds
from matplotlib.figure import Figure

from nowcast import config, paths
from nowcast.model import baselines, metrics
from nowcast.model.train import split_filters

GROUPS = {
    "cloud_now": ("cold_pct", "tb_min", "tb_mean", "cold_trend_1h", "nb_cold_pct"),
    "motion": ("adv1_pct", "adv2_pct", "adv3_pct", "adv_max_pct", "flow_kmh"),
    "imerg_lag": (
        "rain_lag",
        "wet_lag",
        "rainmax_lag",
        "rain_sum_3h",
        "rain_sum_6h",
        "rain_sum_24h",
        "nb_rain_lag",
    ),
    "climatology": ("clim_wet_pct", "clim_p"),
    "time": ("hour_sin", "hour_cos", "doy_sin", "doy_cos"),
}
GEOGRAPHY = ("island", "area_km2")
LAGS = (300, 360, 420, 480)
OUTPUT = config.REPO_ROOT / "docs" / "experiments"
METRIC_COLUMNS = {
    "brier": "Brier",
    "bss": "BSS",
    "auc": "AUC",
    "csi_at_best": "CSI",
    "threshold": "Cutoff",
}


def load_lead(dataset, splits, features, lead):
    target, clim = f"y{lead}", f"clim_p{lead}"
    frames, labels, keys = {}, {}, {}
    for name, in_split in splits.items():
        table = dataset.to_table(
            columns=["t", "area_id", *features[:-1], clim, target],
            filter=in_split & ds.field(target).is_valid(),
        ).sort_by([("t", "ascending"), ("area_id", "ascending")])
        keys[name] = table.select(["t", "area_id", target, clim]).combine_chunks()
        frame = (
            table.drop_columns(["t", "area_id"])
            .to_pandas(categories=["island"], split_blocks=True)
            .rename(columns={clim: "clim_p"})
        )
        labels[name] = frame.pop(target).to_numpy()
        frames[name] = frame
    return frames, labels, keys


def evaluate(dataset, splits, settings, lead, variants, expected_keys=None):
    frames, labels, keys = load_lead(dataset, splits, variants["all"], lead)
    if expected_keys is not None:
        assert all(keys[name].equals(expected_keys[name]) for name in splits), (
            f"Different split rows, targets or climatology for +{lead} h"
        )

    clim = {
        name: frames[name]["clim_p"].to_numpy(dtype=np.float64) / 100 for name in ("valid", "test")
    }
    eligible = {name: np.isfinite(p) for name, p in clim.items()}
    y_valid = labels["valid"][eligible["valid"]]
    y_test = labels["test"][eligible["test"]]
    clim_score = metrics.brier(y_test, clim["test"][eligible["test"]])
    motion = baselines.motion_threshold(labels["valid"], frames["valid"]["adv_max_pct"])

    scores = {}
    for name, features in variants.items():
        model = lgb.LGBMClassifier(**settings["lgbm"])
        model.fit(
            frames["train"][features],
            labels["train"],
            eval_X=frames["valid"][features],
            eval_y=labels["valid"],
            eval_metric="binary_logloss",
            callbacks=[lgb.early_stopping(settings["early_stopping_rounds"], verbose=False)],
        )
        p_valid = model.predict_proba(frames["valid"][features])[:, 1][eligible["valid"]]
        p_test = model.predict_proba(frames["test"][features])[:, 1][eligible["test"]]
        threshold = metrics.best_csi_threshold(y_valid, p_valid)
        test = metrics.scores(y_test, p_test, clim_score, threshold, with_auc=True)
        scores[name] = {
            "best_iteration": model.best_iteration_,
            "threshold": threshold,
            **{key: test[key] for key in METRIC_COLUMNS if key != "threshold"},
        }
    return {
        "rows_per_split": {name: len(y) for name, y in labels.items()},
        "validation_scoring_rows": len(y_valid),
        "test_scoring_rows": len(y_test),
        "test_climatology_brier": clim_score,
        "motion_baseline_cutoff_pct": motion,
        "runs": scores,
    }, keys


def check_lag(dataset):
    table = dataset.to_table(columns=["t", "imerg_slot"])
    lag = pc.unique(pc.minutes_between(table["imerg_slot"], table["t"])).to_pylist()
    assert len(lag) == 1 and lag[0] in LAGS, f"Unexpected table lag in minutes: {lag}"
    return lag[0]


def figures(leads, ablation, lag_scores):
    labels = ["Cloud now", "Motion", "Lagged IMERG", "Climatology", "Time"]
    fig = Figure(figsize=(9, 4.8), layout="constrained")
    ax = fig.subplots()
    x = np.arange(len(GROUPS))
    for i, lead in enumerate(leads):
        runs = ablation[lead]["runs"]
        ax.bar(
            x + (i - (len(leads) - 1) / 2) * 0.24,
            [runs["all"]["bss"] - runs[f"minus_{group}"]["bss"] for group in GROUPS],
            width=0.24,
            label=f"+{lead} h",
        )
    ax.axhline(0, color="0.4", linewidth=0.8)
    ax.set(xticks=x, xticklabels=labels, ylabel="Test BSS drop (all − without group)")
    ax.legend()
    fig.savefig(OUTPUT / "img" / "04_ablation.png", dpi=150)

    fig = Figure(figsize=(8, 4.8), layout="constrained")
    ax = fig.subplots()
    for lead in leads:
        ax.plot(LAGS, [lag_scores[lag][lead]["bss"] for lag in LAGS], "o-", label=f"+{lead} h")
    ax.set(
        xticks=LAGS,
        xticklabels=["300\nplanning only", "360", "420", "480"],
        xlabel="IMERG lag from slot start (minutes)",
        ylabel="Test BSS vs climatology",
    )
    ax.legend()
    fig.savefig(OUTPUT / "img" / "04_lag.png", dpi=150)


def main():
    started = perf_counter()
    cfg = config.settings()
    settings = cfg["model"]
    leads = cfg["target"]["leads_h"]
    periods = settings["split"]
    splits = split_filters(periods)
    start = min(period[0] for period in periods.values())
    end = max(period[1] for period in periods.values())
    temporary = paths.data_root() / "tmp"
    dataset = ds.dataset(temporary / "features" / f"{start}_{end}", format="parquet")
    sample_lag = check_lag(dataset)
    features = [*settings["features"], "clim_p"]
    assert set(features) == set(GEOGRAPHY).union(*GROUPS.values())

    variants = {"all": features}
    for group, names in GROUPS.items():
        variants[f"minus_{group}"] = [name for name in features if name not in names]
    for group, names in GROUPS.items():
        variants[f"only_{group}"] = [
            name for name in features if name in names or name in GEOGRAPHY
        ]

    ablation, sample_keys = {}, {}
    for lead in leads:
        print(f"Ablation +{lead} h ({sample_lag} min IMERG lag)", flush=True)
        ablation[lead], sample_keys[lead] = evaluate(dataset, splits, settings, lead, variants)

    lag_scores = {}
    for lag in LAGS:
        print(f"Rebuilding and scoring lag {lag} min", flush=True)
        output = temporary / "exp04" / f"lag{lag}"
        output.mkdir(parents=True, exist_ok=False)
        try:
            # Exit the Spark process before fitting so its JVM releases the driver heap.
            run(
                [
                    "nowcast",
                    "features",
                    "build",
                    "--start",
                    start,
                    "--end",
                    end,
                    "--out",
                    str(output),
                ],
                check=True,
                env={**os.environ, "IMERG_LAG_MINUTES": str(lag)},
            )
            rebuilt = ds.dataset(output, format="parquet")
            assert check_lag(rebuilt) == lag
            lag_scores[lag] = {}
            for lead in leads:
                print(f"Lag {lag} min, fitting +{lead} h", flush=True)
                result, _ = evaluate(
                    rebuilt, splits, settings, lead, {"all": features}, sample_keys[lead]
                )
                lag_scores[lag][lead] = result["runs"]["all"]
        finally:
            rmtree(output)

    figures(leads, ablation, lag_scores)
    elapsed = perf_counter() - started
    result = {
        "periods_utc_inclusive": {**periods, "climatology": settings["climatology_period"]},
        "rain_threshold_mm_h": cfg["target"]["threshold_mm_h"],
        "parameters": settings["lgbm"],
        "early_stopping_rounds": settings["early_stopping_rounds"],
        "isotonic_calibration": False,
        "ablation_lag_minutes": sample_lag,
        "feature_groups": GROUPS,
        "variants": variants,
        "ablation": ablation,
        "lag_sensitivity": lag_scores,
        "runtime_seconds": round(elapsed, 2),
    }
    (OUTPUT / "results" / "04_ablation.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )

    header = " ".join(f"{label:>8}" for label in METRIC_COLUMNS.values())
    print(f"\nAblation ({sample_lag} min, test, raw probabilities)")
    print(f"Lead {'Variant':<21} {header}")
    for lead, entry in ablation.items():
        for name, score in entry["runs"].items():
            values = " ".join(f"{score[key]:8.4f}" for key in METRIC_COLUMNS)
            print(f"+{lead}h  {name:<21} {values}")
    print("\nIMERG lag sensitivity (all features, test, raw probabilities)")
    print(f"Lead {'Lag (min)':<21} {header}")
    for lag, scores in lag_scores.items():
        for lead, score in scores.items():
            values = " ".join(f"{score[key]:8.4f}" for key in METRIC_COLUMNS)
            print(f"+{lead}h  {lag:<21} {values}")
    print(f"\nTotal runtime: {elapsed:.2f} s")


if __name__ == "__main__":
    main()
