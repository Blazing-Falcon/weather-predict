import geopandas as gpd
import pytest
from shapely import box

from nowcast.registry.neighbours import neighbour_table


def test_border_and_nearest():
    areas = gpd.GeoDataFrame(
        {
            "area_id": [3, 2, 0, 1],
            "geo_name": ["Isolated", "East", "West", "Middle"],
            "centroid_lon": [10.5, 2.5, 0.5, 1.501],
            "centroid_lat": [0.0] * 4,
        },
        geometry=[
            box(10, -0.5, 11, 0.5),
            box(2, -0.5, 3, 0.5),
            box(0, -0.5, 1, 0.5),
            box(1.001, -0.5, 2, 0.5),
        ],
    )
    rows = neighbour_table(areas).to_pylist()
    assert [(row["area_id"], row["neighbour_id"], row["kind"]) for row in rows] == [
        (0, 1, "border"),
        (1, 0, "border"),
        (1, 2, "border"),
        (2, 1, "border"),
        (3, 0, "nearest"),
        (3, 1, "nearest"),
        (3, 2, "nearest"),
    ]
    assert rows[0]["dist_km"] == rows[1]["dist_km"]
    assert rows[-1]["dist_km"] == pytest.approx(889.56, abs=0.01)


def test_isolated_area_uses_six_closest_centroids():
    starts = [*range(7), 10]
    areas = gpd.GeoDataFrame(
        {
            "area_id": range(8),
            "geo_name": [str(x) for x in starts],
            "centroid_lon": [x + 0.5 for x in starts],
            "centroid_lat": [0.0] * 8,
        },
        geometry=[box(x, -0.5, x + 1, 0.5) for x in starts],
    )

    rows = neighbour_table(areas).to_pylist()
    assert [row["neighbour_id"] for row in rows if row["area_id"] == 7] == list(range(1, 7))
