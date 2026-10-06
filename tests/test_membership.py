import numpy as np
import pytest
from shapely import box

from nowcast.registry.grids import Grid, imerg_grid
from nowcast.registry.membership import overlap_weights


@pytest.mark.parametrize(
    ("bounds", "expected_cells", "expected_weights"),
    [
        ((0, 0, 2, 2), [0, 1, 3, 4], [1, 1, 1, 1]),
        ((1, 0, 1.5, 0.5), [1], [0.25]),
        ((2.125, 1.125, 2.375, 1.375), [5], [0.0625]),
    ],
)
def test_overlap_weights(bounds, expected_cells, expected_weights):
    axis = np.array([0.5, 1.5, 2.5])
    cells, weights = overlap_weights(box(*bounds), Grid("test", axis, axis))

    np.testing.assert_array_equal(cells, expected_cells)
    np.testing.assert_array_equal(weights, expected_weights)
    assert cells.dtype == np.int32
    assert weights.dtype == np.float32


def test_grid_edges_and_cell_area():
    grid = Grid("test", np.array([0.05, 0.15]), np.array([0.0, 0.1, 0.22]))
    assert (grid.width, grid.height) == (2, 3)
    np.testing.assert_allclose(grid.lon_edges, [0.0, 0.1, 0.2], atol=1e-12)
    np.testing.assert_allclose(grid.lat_edges, [-0.05, 0.05, 0.16, 0.28])
    assert grid.cell_km2[0] == pytest.approx(123.6, abs=0.1)


def test_imerg_grid():
    grid = imerg_grid()
    assert (grid.width, grid.height) == (480, 180)
    np.testing.assert_allclose(grid.lon[[0, -1]], [94.05, 141.95])
    np.testing.assert_allclose(grid.lat[[0, -1]], [-11.45, 6.45])
