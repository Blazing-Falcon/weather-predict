import numpy as np
import pyarrow as pa
from numpy.typing import NDArray
from scipy.sparse import csr_matrix


class Zonal:
    def __init__(self, membership: pa.Table, cells: int) -> None:
        self.area_ids, rows = np.unique(membership["area_id"].to_numpy(), return_inverse=True)
        weights = membership["weight"].to_numpy()
        positive = weights > 0
        self.weights = csr_matrix(
            (weights[positive], (rows[positive], membership["cell"].to_numpy()[positive])),
            shape=(len(self.area_ids), cells),
            dtype=np.float64,
        )
        self._rows = np.repeat(np.arange(len(self.area_ids)), np.diff(self.weights.indptr))

    def mean(self, values: NDArray, valid: NDArray[np.bool_]) -> NDArray[np.float64]:
        return self._weighted(np.where(valid, values, 0), valid)

    def max(self, values: NDArray, valid: NDArray[np.bool_]) -> NDArray[np.float64]:
        cells = self.weights.indices
        maximum = np.full(len(self.area_ids), -np.inf)
        np.maximum.at(maximum, self._rows, np.where(valid[cells], values[cells], -np.inf))
        maximum[maximum == -np.inf] = np.nan
        return maximum

    def share(self, mask: NDArray[np.bool_], valid: NDArray[np.bool_]) -> NDArray[np.float64]:
        return self._weighted(mask & valid, valid)

    def _weighted(self, values: NDArray, valid: NDArray[np.bool_]) -> NDArray[np.float64]:
        total = self.weights @ valid
        return np.divide(
            self.weights @ values, total, out=np.full(len(total), np.nan), where=total > 0
        )
