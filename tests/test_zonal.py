import numpy as np
import pyarrow as pa

from nowcast.zonal import Zonal


def test_zonal_valid_cells():
    membership = pa.table(
        {
            "area_id": [4, 4, 4, 9, 9, 9, 9],
            "cell": [0, 1, 2, 2, 3, 4, 5],
            "weight": [1, 0.5, 1, 1, 0.25, 0.75, 0],
        }
    )
    zonal = Zonal(membership, 6)
    values = np.array([2, 8, -9999.9, 4, 0, 100])
    valid = values >= 0
    wet = values >= 4

    np.testing.assert_array_equal(zonal.area_ids, [4, 9])
    np.testing.assert_array_equal(zonal.mean(values, valid), [4, 1])
    np.testing.assert_array_equal(zonal.max(values, valid), [8, 4])
    np.testing.assert_array_equal(zonal.share(wet, valid), [1 / 3, 0.25])

    valid[3:5] = False
    np.testing.assert_array_equal(zonal.mean(values, valid), [4, np.nan])
    np.testing.assert_array_equal(zonal.max(values, valid), [8, np.nan])
    np.testing.assert_array_equal(zonal.share(wet, valid), [1 / 3, np.nan])
