import argparse
import json
from datetime import UTC, datetime, timedelta
from time import perf_counter

import cv2
import geopandas as gpd
import httpx
import numpy as np
import pyarrow.parquet as pq
from matplotlib import pyplot as plt

from nowcast import config, paths
from nowcast.cloud.features import valid_tb
from nowcast.cloud.flow import CloudMotion
from nowcast.log import redact
from nowcast.registry.grids import mergir_grid
from nowcast.sources import cmr, mergir
from nowcast.zonal import Zonal

START = datetime(2025, 1, 8, tzinfo=UTC)
END = START + timedelta(days=7)
STEPS = (2, 4, 6)
PRESETS = ("fast", "medium")
SIGMAS = (0, 33, 66, 132)
OUTPUT = config.REPO_ROOT / "docs" / "experiments"
JAVA = (104.5, -9, 115, -4.5)


def score(forecast, forecast_pct, truth, truth_pct, cells, areas):
    hit = np.count_nonzero(forecast & truth & cells)
    union = np.count_nonzero((forecast | truth) & cells)
    errors = np.abs(forecast_pct[areas] - truth_pct[areas])
    return errors.mean() if errors.size else 0.0, hit / union if union else 1.0


def evaluate(granules, crop_dir, grid, zonal, source):
    motions = {
        (preset, sigma): CloudMotion(grid, source["flow"] | {"preset": preset, "sigma_km": sigma})
        for preset in PRESETS
        for sigma in SIGMAS
    }
    frames = 2 * len(granules)
    cold = np.empty((frames, grid.height, grid.width), dtype=bool)
    observed = np.empty_like(cold)
    images = np.empty(cold.shape, dtype=np.uint8)
    cold_pct = np.empty((frames, len(zonal.area_ids)))
    has_image = np.zeros(frames, dtype=bool)
    imager = motions[PRESETS[0], 0]
    for index, granule in enumerate(granules):
        for t, tb in enumerate(mergir.read_crop(crop_dir / granule.title, grid), 2 * index):
            valid = valid_tb(tb)
            observed[t] = valid
            cold[t] = valid & (tb < source["cold_k"])
            cold_pct[t] = np.rint(100 * zonal.share(cold[t].ravel(), valid.ravel()))
            image = imager.image(tb, valid)
            if image is not None:
                images[t] = image
                has_image[t] = True

    west, south, east, north = JAVA
    ys = slice(*np.searchsorted(grid.lat, [south, north]))
    xs = slice(*np.searchsorted(grid.lon, [west, east]))
    example_candidates = np.flatnonzero(has_image[1:-4] & has_image[:-5] & has_image[5:]) + 1
    coverage = cold[example_candidates, ys, xs].mean(axis=(1, 2))
    example_t = int(example_candidates[np.argmin(np.abs(coverage - 0.20))])
    examples = {}
    totals = {label: np.zeros((len(STEPS), 2)) for label in ["Persistence", *motions]}
    dis_seconds = dict.fromkeys(PRESETS, 0.0)
    blur_seconds = dict.fromkeys(motions, 0.0)
    speeds = {label: [] for label in motions}
    all_cells = np.ones(grid.width * grid.height, dtype=bool)
    forecast_frames = frames - STEPS[0]
    counts = np.zeros((len(STEPS), 2), dtype=int)
    area_pairs = np.zeros(len(STEPS), dtype=int)
    motion_frames = 0
    fallback_frames = 0
    for t in range(forecast_frames):
        use_motion = t > 0 and has_image[t - 1] and has_image[t]
        leads = []
        for j, k in enumerate(STEPS):
            if t + k >= frames:
                continue
            cells = observed[t] & observed[t + k]
            if not cells.any():
                continue
            areas = np.isfinite(cold_pct[t]) & np.isfinite(cold_pct[t + k])
            counts[j] += (areas.any(), 1)
            area_pairs[j] += areas.sum()
            base = score(cold[t], cold_pct[t], cold[t + k], cold_pct[t + k], cells, areas)
            totals["Persistence"][j] += base
            if use_motion:
                leads.append((j, k, cells, areas))
            else:
                for label in motions:
                    totals[label][j] += base
        if not use_motion:
            fallback_frames += int(has_image[t])
        if not leads:
            continue

        motion_frames += 1
        cold_float = cold[t].astype(np.float32)
        for preset in PRESETS:
            started = perf_counter()
            raw = motions[preset, 0].dis.calc(images[t - 1], images[t], None)
            dis_seconds[preset] += perf_counter() - started
            for sigma in SIGMAS:
                label = (preset, sigma)
                motion = motions[label]
                started = perf_counter()
                flow = cv2.GaussianBlur(raw, (0, 0), motion.sigma) if sigma else raw
                blur_seconds[label] += perf_counter() - started
                if cold[t].any():
                    vx, vy = flow[cold[t]].T
                    speeds[label].append(float(np.hypot(vx, vy).mean() * (2 * motion.pixel_km)))
                for j, k, cells, areas in leads:
                    advected = motion.advect(cold_float, flow, k)
                    adv_pct = np.rint(100 * zonal.mean(advected.ravel(), all_cells))
                    mask = advected >= 0.5
                    totals[label][j] += score(
                        mask,
                        adv_pct,
                        cold[t + k],
                        cold_pct[t + k],
                        cells,
                        areas,
                    )
                    if t == example_t and k == 4:
                        examples[label] = mask[ys, xs].copy()
        if t % 50 == 0:
            print(f"Scored {t + 1}/{forecast_frames} analysis frames", flush=True)

    persistence = totals["Persistence"] / counts
    rows = []
    for label, total in totals.items():
        preset, sigma = (None, None) if label == "Persistence" else label
        means = total / counts
        rows.append(
            {
                "setting": label if preset is None else f"{preset} {sigma} km",
                "preset": preset,
                "sigma_km": sigma,
                "field_ms_per_frame": (
                    None
                    if preset is None
                    else 1000 * (dis_seconds[preset] + blur_seconds[label]) / motion_frames
                ),
                "median_cold_speed_kmh": 0.0 if preset is None else float(np.median(speeds[label])),
                "leads": [
                    {
                        "hours": k // 2,
                        "mae_frames": int(n_mae),
                        "csi_frames": int(n_csi),
                        "area_pairs": int(pairs),
                        "mae_pp": float(mae),
                        "csi": float(csi),
                        "mae_skill": float(1 - mae / base_mae),
                        "csi_difference": float(csi - base_csi),
                    }
                    for k, (n_mae, n_csi), pairs, (mae, csi), (base_mae, base_csi) in zip(
                        STEPS, counts, area_pairs, means, persistence, strict=True
                    )
                ],
            }
        )
    best = min(rows[1:], key=lambda row: np.mean([lead["mae_pp"] for lead in row["leads"]]))
    example = (
        cold[example_t, ys, xs],
        examples[best["preset"], best["sigma_km"]],
        cold[example_t + 4, ys, xs],
    )
    return (
        {
            "frames": frames,
            "invalid_pixels": observed.size - int(np.count_nonzero(observed)),
            "fully_invalid_frames": frames - int(has_image.sum()),
            "motion_frames": motion_frames,
            "fallback_frames": fallback_frames,
            "pixel_km": float(motions[PRESETS[0], 0].pixel_km),
            "dis_ms_per_frame": {
                preset: 1000 * seconds / motion_frames for preset, seconds in dis_seconds.items()
            },
            "settings": rows,
            "best_setting": best["setting"],
            "example_utc": (START + example_t * mergir.SLOT_LENGTH).isoformat(),
        },
        example,
        (ys, xs),
    )


def figures(result, example, slices, grid):
    plt.switch_backend("Agg")
    image_dir = OUTPUT / "img"
    image_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8.4, 4.6), layout="constrained")
    colors = ("#1676a3", "#a34315")
    styles = ("-", "--", "-.", ":")
    for row in result["settings"]:
        if row["preset"] is None:
            color, style, width, order = "#161a1d", "-", 3, 4
        else:
            color = colors[PRESETS.index(row["preset"])]
            style = styles[SIGMAS.index(row["sigma_km"])]
            width, order = 1.5, 2
        ax.plot(
            [lead["hours"] for lead in row["leads"]],
            [lead["mae_pp"] for lead in row["leads"]],
            color=color,
            linestyle=style,
            linewidth=width,
            zorder=order,
            label=row["setting"],
        )
    ax.set(
        title="Cold-Cloud Area Forecast Error",
        xlabel="Lead (hours)",
        ylabel="Mean absolute error (percentage points)",
        xticks=[1, 2, 3],
    )
    ax.grid(axis="y", color="#e5e7eb")
    ax.legend(loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
    fig.savefig(image_dir / "03_area_mae.png", dpi=170)
    plt.close(fig)

    ys, xs = slices
    extent = [
        grid.lon_edges[xs.start],
        grid.lon_edges[xs.stop],
        grid.lat_edges[ys.start],
        grid.lat_edges[ys.stop],
    ]
    outlines = gpd.read_file(paths.zone("ref") / "kabkota_full.geojson", bbox=JAVA)
    coastline = outlines.geometry.union_all().simplify(0.004).boundary
    start = datetime.fromisoformat(result["example_utc"])
    end = start + timedelta(hours=2)
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 2.7), layout="constrained", sharey=True)
    fig.suptitle(f"Java and the Java Sea | {start:%Y-%m-%d} UTC | {result['best_setting']}")
    labels = f"Observed {start:%H:%M}", f"Advected {end:%H:%M}", f"Observed {end:%H:%M}"
    for ax, title, mask in zip(axes, labels, example, strict=True):
        ax.imshow(mask, origin="lower", extent=extent, interpolation="nearest", cmap="Blues")
        gpd.GeoSeries([coastline]).plot(ax=ax, color="#56616d", linewidth=0.45)
        ax.set(title=title, xlim=extent[:2], ylim=extent[2:], xlabel="Longitude (°E)", ylabel="")
    axes[0].set_ylabel("Latitude (°)")
    fig.savefig(image_dir / "03_java_example.png", dpi=170)
    plt.close(fig)


def print_summary(result):
    print("MAE in percentage points, MAE skill as %, CSI and ΔCSI as fractions.")
    print(
        f"{'Setting':<15} {'+1 h MAE/CSI/skill/ΔCSI':>28} "
        f"{'+2 h MAE/CSI/skill/ΔCSI':>28} {'+3 h MAE/CSI/skill/ΔCSI':>28} "
        f"{'Field ms':>9} {'km/h':>7}"
    )
    for row in result["settings"]:
        scores = [
            f"{lead['mae_pp']:5.2f}/{lead['csi']:.3f}/{lead['mae_skill']:+6.1%}/"
            f"{lead['csi_difference']:+.3f}"
            for lead in row["leads"]
        ]
        runtime = row["field_ms_per_frame"]
        field = f"{runtime:.2f}" if runtime is not None else "—"
        print(
            f"{row['setting']:<15} {scores[0]:>28} {scores[1]:>28} {scores[2]:>28} "
            f"{field:>9} {row['median_cold_speed_kmh']:7.2f}"
        )
    timings = ", ".join(
        f"{preset} {ms:.2f} ms/frame" for preset, ms in result["dis_ms_per_frame"].items()
    )
    print(f"Raw DIS: {timings}")
    print(f"Lowest mean area MAE: {result['best_setting']}")


def main():
    parser = argparse.ArgumentParser(description="Compare MERGIR cold-cloud motion settings.")
    parser.add_argument("--keep", action="store_true", help="Keep the downloaded experiment crops.")
    args = parser.parse_args()
    cv2.setNumThreads(1)
    run_at = datetime.now(UTC)
    grid = mergir_grid()
    source = config.settings()["mergir"]
    membership = pq.read_table(paths.zone("ref") / "mergir_membership.parquet")
    zonal = Zonal(membership, grid.width * grid.height)
    assert len(zonal.area_ids) == 514
    crop_dir = paths.zone("tmp") / "exp03"
    entries = cmr.granules(source["collection"], START, END - timedelta(seconds=1))
    granules = mergir.select_granules(entries, START, END)
    assert [granule.slot for granule in granules] == [
        START + i * mergir.HOUR for i in range(168)
    ], "Expected all 168 consecutive hourly MERGIR files"
    try:
        mergir.download_month(granules, crop_dir, grid, 8)
        print(f"Ready: {len(granules)} hourly MERGIR crops", flush=True)
        result, example, slices = evaluate(granules, crop_dir, grid, zonal, source)
        result.update(
            run_at_utc=run_at.isoformat(timespec="seconds"),
            start_utc=START.isoformat(),
            last_frame_utc=(END - mergir.SLOT_LENGTH).isoformat(),
            hourly_files=len(granules),
            areas=len(zonal.area_ids),
            crop_shape_lat_lon=[grid.height, grid.width],
            cold_threshold_k=source["cold_k"],
            opencv_version=cv2.__version__,
            opencv_threads=cv2.getNumThreads(),
        )
        figures(result, example, slices, grid)
        results_dir = OUTPUT / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "03_flow_settings.json").write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        print_summary(result)
    finally:
        if not args.keep:
            for granule in granules:
                crop = crop_dir / granule.title
                crop.unlink(missing_ok=True)
                crop.with_suffix(".nc4.tmp").unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        raise SystemExit(redact(str(error))) from None
