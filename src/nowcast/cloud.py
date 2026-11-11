from datetime import datetime

import cv2
import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from nowcast.registry.grids import Grid
from nowcast.storage import scaled
from nowcast.timeslots import SLOT, UTC_US
from nowcast.zonal import Zonal

CLOUD_PARTS = {1: "part-mergir.parquet", 2: "part-himawari.parquet"}
CLOUD_SCHEMA = pa.schema([
    ("slot", UTC_US),
    ("area_id", pa.int16()),
    ("source", pa.uint8()),
    ("tb_min", pa.int16()),
    ("tb_mean_x10", pa.int16()),
    ("cold_pct", pa.uint8()),
    ("adv1_pct", pa.uint8()),
    ("adv2_pct", pa.uint8()),
    ("adv3_pct", pa.uint8()),
    ("flow_kmh", pa.uint8()),
])
PRESETS = {
    "ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
    "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
    "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
}


def valid_tb(tb: NDArray) -> NDArray[np.bool_]:
    return (tb >= 150) & (tb <= 340)


def features(
    tb: NDArray, valid: NDArray[np.bool_], cold_k: float, zonal: Zonal
) -> tuple[NDArray, NDArray, NDArray]:
    values, valid_cells = tb.ravel(), valid.ravel()
    return (
        -zonal.max(-values, valid_cells),
        zonal.mean(values, valid_cells),
        zonal.share(values < cold_k, valid_cells),
    )


class CloudMotion:
    def __init__(self, grid: Grid, settings: dict) -> None:
        self.dis = cv2.DISOpticalFlow_create(PRESETS[settings["preset"]])
        self.y, self.x = np.indices((grid.height, grid.width), dtype=np.float32)
        mid_lat = np.mean(grid.lat[[0, -1]])
        lat_step = np.diff(grid.lat).mean()
        lon_step = np.diff(grid.lon).mean() * np.cos(np.deg2rad(mid_lat))
        self.pixel_km = (lat_step + lon_step) / 2 * 111.195
        self.sigma = settings["sigma_km"] / self.pixel_km
        self.warm_k = settings["warm_k"]
        self.range_k = self.warm_k - settings["cold_end_k"]
        self.steps = settings["steps"]

    def image(self, tb: NDArray, valid: NDArray[np.bool_]) -> NDArray[np.uint8] | None:
        if not valid.any():
            return None
        filled = np.where(valid, tb, np.median(tb[valid])).astype(np.float32)
        return (np.clip((self.warm_k - filled) / self.range_k, 0, 1) * 255).astype(np.uint8)

    def field(self, previous: NDArray, current: NDArray) -> NDArray[np.float32]:
        flow = self.dis.calc(previous, current, None)
        return cv2.GaussianBlur(flow, (0, 0), self.sigma)

    def advect(self, cold: NDArray, flow: NDArray, steps: int) -> NDArray[np.float32]:
        return cv2.remap(
            cold, self.x - steps * flow[..., 0], self.y - steps * flow[..., 1],
            cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
        )

    def features(
        self, previous: NDArray, current: NDArray, cold: NDArray[np.bool_], zonal: Zonal
    ) -> tuple[list[NDArray], float | None]:
        flow = self.field(previous, current)
        cold_float = cold.astype(np.float32)
        all_cells = np.ones(cold.size, dtype=bool)
        shares = [
            zonal.mean(self.advect(cold_float, flow, steps).ravel(), all_cells)
            for steps in self.steps
        ]
        speed = np.hypot(flow[..., 0], flow[..., 1]) * (2 * self.pixel_km)
        return (
            [*shares, zonal.mean(speed.ravel(), cold.ravel())],
            float(speed[cold].mean()) if cold.any() else None,
        )


def cloud_table(slots: list[datetime], area_ids: NDArray, source: int, metrics: NDArray) -> pa.Table:
    """Build area-major rows from metrics shaped (metric, area, slot)."""
    areas, frames = len(area_ids), len(slots)
    return pa.Table.from_arrays(
        [
            pa.array(slots, type=UTC_US).take(np.tile(np.arange(frames, dtype=np.int32), areas)),
            pa.array(np.repeat(area_ids, frames), type=pa.int16()),
            pa.array(np.full(areas * frames, source, dtype=np.uint8)),
            scaled(metrics[0].ravel(), 1, np.int16),
            scaled(metrics[1].ravel(), 10, np.int16),
            *(scaled(metric.ravel(), 100, np.uint8) for metric in metrics[2:6]),
            scaled(metrics[6].ravel(), 1, np.uint8),
        ],
        schema=CLOUD_SCHEMA,
    )


def cloud_rows(
    scan: datetime, tb: NDArray, previous: tuple[datetime, NDArray] | None,
    zonal: Zonal, motion: CloudMotion, cold_k: float,
) -> pa.Table:
    valid = valid_tb(tb)
    tb_min, tb_mean, cold_pct = features(tb, valid, cold_k, zonal)
    adv = np.full((4, len(zonal.area_ids)), np.nan)
    if previous is not None and previous[0] == scan - SLOT:
        previous_tb = previous[1]
        previous_image = motion.image(previous_tb, valid_tb(previous_tb))
        image = motion.image(tb, valid)
        if previous_image is not None and image is not None:
            adv, _ = motion.features(previous_image, image, valid & (tb < cold_k), zonal)
    metrics = np.array([tb_min, tb_mean, cold_pct, *adv])[:, :, None]
    return cloud_table([scan], zonal.area_ids, 2, metrics)
