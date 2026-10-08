import numpy as np
from numpy.typing import NDArray

from nowcast.zonal import Zonal


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
