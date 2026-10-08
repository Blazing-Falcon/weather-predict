import cv2
import numpy as np
from numpy.typing import NDArray

from nowcast.registry.grids import Grid
from nowcast.zonal import Zonal

PRESETS = {
    "ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
    "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
    "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
}


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
