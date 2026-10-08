import numpy as np
import pyarrow as pa

from nowcast import config
from nowcast.cloud.features import features, valid_tb
from nowcast.cloud.flow import CloudMotion
from nowcast.registry.grids import Grid
from nowcast.zonal import Zonal


def test_features_ignore_invalid_cells():
    tb = np.array([[210, 240, -9999], [341, 149, 233]], dtype=np.int16)
    zonal = Zonal(pa.table({
        "area_id": [1, 1, 1, 2, 2, 3],
        "cell": [0, 1, 2, 3, 4, 5],
        "weight": [1, 3, 8, 1, 1, 1],
    }), tb.size)

    coldest, mean, cold = features(tb, valid_tb(tb), 233, zonal)

    np.testing.assert_allclose(coldest, [210, np.nan, 233])
    np.testing.assert_allclose(mean * 10, [2325, np.nan, 2330])
    np.testing.assert_allclose(cold * 100, [25, np.nan, 0])


def test_eastward_flow_and_advection():
    grid = Grid("synthetic", np.arange(160) * 0.036, np.arange(128) * 0.036)
    motion = CloudMotion(grid, config.settings()["mergir"]["flow"])
    distance = np.hypot(motion.x - 72, motion.y - 64)
    previous = (290 - 95 * np.exp(-(distance / 24) ** 2 / 2)).astype(np.int16)
    current = np.roll(previous, 3, axis=1)
    cold = current < 233

    field = motion.field(motion.image(previous, valid_tb(previous)),
                         motion.image(current, valid_tb(current)))
    moved = motion.advect(cold.astype(np.float32), field, 2)

    np.testing.assert_allclose(field[cold].mean(axis=0), [3, 0], atol=0.4)
    shift = (moved * motion.x).sum() / moved.sum() - motion.x[cold].mean()
    np.testing.assert_allclose(shift, 6, atol=0.8)
