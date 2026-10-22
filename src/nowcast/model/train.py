import json
import shutil
import subprocess
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import lightgbm as lgb
import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import typer
from sklearn.isotonic import IsotonicRegression

from nowcast import config, paths
from nowcast.features.build import date_range
from nowcast.model import baselines, metrics


def split_filters(periods: dict) -> dict:
    bounds = {name: date_range(*period) for name, period in periods.items()}
    ordered = sorted(bounds.values())
    if any(end > start for (_, end), (start, _) in pairwise(ordered)):
        raise ValueError("Model split dates overlap")
    t = ds.field("t")
    return {
        name: (t >= pa.scalar(start)) & (t < pa.scalar(stop))
        for name, (start, stop) in bounds.items()
    }


def fit_lead(dataset, splits, settings, lead, rain_threshold, output: Path | None) -> dict:
    target = f"y{lead}"
    clim = f"clim_p{lead}"
    columns = ["t", "area_id", *settings["features"], clim, target]

    def load_split(name):
        table = dataset.to_table(
            columns=columns, filter=splits[name] & ds.field(target).is_valid()
        )
        table = table.sort_by([("t", "ascending"), ("area_id", "ascending")])
        frame = table.drop_columns(["t", "area_id"]).to_pandas(
            categories=["island"], split_blocks=True
        )
        del table
        frame = frame.rename(columns={clim: "clim_p"})
        return frame.pop(target).to_numpy(), frame

    features = [*settings["features"], "clim_p"]
    train_y, train_frame = load_split("train")
    y, frame = load_split("valid")
    rows_per_split = {"train": len(train_y)}
    model = lgb.LGBMClassifier(**settings["lgbm"])
    model.fit(
        train_frame,
        train_y,
        eval_X=frame,
        eval_y=y,
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(settings["early_stopping_rounds"], verbose=False)],
    )
    del train_frame, train_y
    prediction = model.predict_proba(frame)[:, 1]
    raw_reliability = metrics.reliability(y, prediction)
    calibrate = any(
        row["count"] >= 0.01 * len(y)
        and abs(row["mean_predicted"] - row["observed_frequency"]) > 0.05
        for row in raw_reliability
    )
    if calibrate:
        isotonic = IsotonicRegression(out_of_bounds="clip")
        isotonic.fit(prediction, y)
        if output is not None:
            calibration = {
                "thresholds": isotonic.X_thresholds_.tolist(),
                "values": isotonic.y_thresholds_.tolist(),
            }
            (output / f"isotonic_lead{lead}.json").write_text(
                json.dumps(calibration, indent=2) + "\n", encoding="utf-8"
            )

    motion_cutoff = baselines.motion_threshold(y, frame["adv_max_pct"])
    split_scores, reliability, thresholds = {}, {}, {}
    for name in ("valid", *(name for name in splits if name not in {"train", "valid"})):
        if name != "valid":
            y, frame = load_split(name)
            prediction = model.predict_proba(frame)[:, 1]
        if calibrate:
            prediction = isotonic.predict(prediction)
        rows_per_split[name] = len(y)
        climatology = frame["clim_p"].to_numpy(dtype=np.float64) / 100
        eligible = np.isfinite(climatology)
        y = y[eligible]
        probabilities = {
            "model": prediction[eligible],
            "clim": climatology[eligible],
            "persist": (frame["rain_lag"].to_numpy()[eligible] >= rain_threshold).astype(float),
            "motion": (frame["adv_max_pct"].to_numpy()[eligible] >= motion_cutoff).astype(float),
        }
        del frame, prediction, climatology, eligible
        if name == "valid":
            thresholds = {
                source: metrics.best_csi_threshold(y, p) for source, p in probabilities.items()
            }
        clim_score = metrics.brier(y, probabilities["clim"])
        split_scores[name] = {
            source: metrics.scores(
                y, p, clim_score, thresholds[source], with_auc=source in {"model", "clim"}
            )
            for source, p in probabilities.items()
        }
        reliability[name] = {source: metrics.reliability(y, p) for source, p in probabilities.items()}
        del y, probabilities

    if output is not None:
        model.booster_.save_model(str(output / f"model_lead{lead}.txt"))
    gains = model.booster_.feature_importance(importance_type="gain")
    if gains.sum():
        gains = gains / gains.sum()
    return {
        "best_iteration": model.best_iteration_,
        "rows_per_split": rows_per_split,
        "metrics": split_scores,
        "csi_thresholds": {"probability": thresholds, "motion_adv_max_pct": motion_cutoff},
        "calibration_used": calibrate,
        "feature_importance": dict(zip(features, gains.tolist())),
        "reliability": reliability,
        "raw_validation_reliability": raw_reliability,
    }


def figures(leads: dict, output: Path, prefix: str = "step-06") -> None:
    from matplotlib.figure import Figure
    from matplotlib.ticker import PercentFormatter

    output.mkdir(parents=True, exist_ok=True)
    test_splits = [name for name in next(iter(leads.values()))["reliability"] if name != "valid"]
    fig = Figure(figsize=(4 * len(leads), 4 * len(test_splits)), layout="constrained")
    axes = fig.subplots(len(test_splits), len(leads), squeeze=False)
    for name, row in zip(test_splits, axes):
        for ax, (lead, result) in zip(row, leads.items()):
            ax.plot([0, 1], [0, 1], "--", color="0.7", linewidth=1)
            for source, label in (("model", "LightGBM"), ("clim", "Climatology")):
                bins = [row for row in result["reliability"][name][source] if row["count"]]
                ax.plot(
                    [row["mean_predicted"] for row in bins],
                    [row["observed_frequency"] for row in bins],
                    "o-",
                    label=label,
                )
            ax.set(
                title=f"{name} +{lead} h",
                xlabel="Mean predicted probability",
                ylabel="Observed frequency",
                xlim=(0, 1),
                ylim=(0, 1),
            )
            ax.legend(loc="upper left")
    fig.savefig(output / f"{prefix}-reliability.png", dpi=150)

    fig = Figure(figsize=(10, 5), layout="constrained")
    for ax, lead in zip(fig.subplots(1, 2), (1, 3)):
        gain = leads[lead]["feature_importance"]
        top = sorted(gain, key=gain.get)[-15:]
        ax.barh(top, [gain[feature] for feature in top])
        ax.set(title=f"+{lead} h", xlabel="Share of total gain")
        ax.xaxis.set_major_formatter(PercentFormatter(1))
    fig.savefig(output / f"{prefix}-importance.png", dpi=150)


def train(
    keep_features: Annotated[bool, typer.Option(help="Keep the temporary feature table.")] = False,
    check_repro: Annotated[bool, typer.Option(help="Train twice and compare test metrics.")] = False,
    figure_prefix: Annotated[str, typer.Option(help="File name prefix for the figures.")] = "step-06",
) -> None:
    """Train and score one LightGBM classifier per forecast lead."""
    settings = config.settings()
    model, target = settings["model"], settings["target"]
    periods = model["split"]
    splits = split_filters(periods)
    test_splits = [name for name in splits if name not in {"train", "valid"}]
    start = min(period[0] for period in periods.values())
    end = max(period[1] for period in periods.values())
    root = paths.data_root()
    table_path = root / "tmp" / "features" / f"{start}_{end}"
    dataset = ds.dataset(table_path, format="parquet")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=config.REPO_ROOT, text=True
    ).strip()
    short_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", commit], cwd=config.REPO_ROOT, text=True
    ).strip()
    created = datetime.now(UTC)
    version = f"{created:%Y%m%dT%H%MZ}-{short_sha}"
    card = {
        "version": version,
        "git_commit": commit,
        "created_at": created.isoformat(),
        "data_periods": {**periods, "climatology": model["climatology_period"]},
        "imerg_lag_minutes": int(config.env("IMERG_LAG_MINUTES", str(model["imerg_lag_minutes"]))),
        "threshold_mm_h": target["threshold_mm_h"],
        "features": [*model["features"], "clim_p"],
        "parameters": {**model["lgbm"], "early_stopping_rounds": model["early_stopping_rounds"]},
        "bmkg": "BMKG was not scored because no archive was available for the sample month.",
        "scoring_rows": "All sources use rows with both a target and a climatology probability.",
        "leads": {},
    }
    model_root = root / "models"
    model_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=model_root) as temp:
        output = Path(temp)
        for lead in target["leads_h"]:
            result = fit_lead(dataset, splits, model, lead, target["threshold_mm_h"], output)
            card["leads"][lead] = result
            if check_repro:
                repeat = fit_lead(dataset, splits, model, lead, target["threshold_mm_h"], None)
                for name in test_splits:
                    for source, score in result["metrics"][name].items():
                        actual = [score, *result["reliability"][name][source]]
                        expected = [
                            repeat["metrics"][name][source],
                            *repeat["reliability"][name][source],
                        ]
                        for a, b in zip(actual, expected, strict=True):
                            np.testing.assert_allclose(
                                np.array(list(a.values()), dtype=float),
                                np.array(list(b.values()), dtype=float),
                                rtol=0,
                                atol=1e-12,
                                err_msg=f"{name} +{lead} h {source}",
                            )
        del dataset
        (output / "card.json").write_text(
            json.dumps(card, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        figures(card["leads"], config.REPO_ROOT / "docs" / "progress" / "img", figure_prefix)
        output.rename(model_root / version)
    if not keep_features:
        shutil.rmtree(table_path)

    width = max(len("split"), *(len(name) for name in test_splits))
    typer.echo(
        f"{'split':{width}} lead source       n      base   Brier   BSS     "
        "CSI@0.5 CSI@best POD@0.5 FAR@0.5 AUC"
    )
    for name in test_splits:
        for lead, result in card["leads"].items():
            for source, score in result["metrics"][name].items():
                values = [
                    score[key]
                    for key in (
                        "base_rate",
                        "brier",
                        "bss",
                        "csi_at_0.5",
                        "csi_at_best",
                        "pod_at_0.5",
                        "far_at_0.5",
                        "auc",
                    )
                ]
                stats = " ".join(f"{x:7.4f}" if x is not None else f"{'--':>7}" for x in values)
                typer.echo(f"{name:{width}} +{lead}h  {source:7} {score['n']:7d} {stats}")
    typer.echo(f"Wrote {model_root / version}")
    if check_repro:
        typer.echo("repro ok")
