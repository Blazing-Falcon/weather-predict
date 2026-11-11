import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nowcast.registry.dim_area import island_and_tz
from nowcast.storage import write_table


@pytest.mark.parametrize(
    ("code", "island", "tz"),
    [
        ("21", "Sumatera", "WIB"),
        ("31", "Jawa", "WIB"),
        ("53", "Bali-Nusa Tenggara", "WITA"),
        ("62", "Kalimantan", "WIB"),
        ("63", "Kalimantan", "WITA"),
        ("76", "Sulawesi", "WITA"),
        ("82", "Maluku", "WIT"),
        ("96", "Papua", "WIT"),
    ],
)
def test_island_and_tz(code, island, tz):
    assert island_and_tz(code) == (island, tz)


def test_write_table_replaces_existing_file(tmp_path):
    path = tmp_path / "test.parquet"
    first = pa.table({"value": [1]})
    second = pa.table({"value": [2, 3]})

    write_table(first, path)
    assert pq.read_table(path).equals(first)
    assert list(tmp_path.iterdir()) == [path]

    write_table(second, path)
    assert pq.read_table(path).equals(second)
    assert list(tmp_path.iterdir()) == [path]
    assert pq.read_metadata(path).row_group(0).column(0).compression == "ZSTD"
