import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from nowcast.sources.gauges import SCHEMA, is_rain, merge, parse_ghcnh, parse_metar, rain_groups

FIXTURES = Path(__file__).parent / "fixtures"
GHCNH = FIXTURES / "ghcnh_IDI0000WARR_2026_head.psv"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("RA", ["RA"]),
        ("-RA BR", ["-RA"]),
        ("+SHRA", ["+SHRA"]),
        ("TSRA", ["TSRA"]),
        ("DZ", ["DZ"]),
        ("-TSRA", ["-TSRA"]),
        ("SHRAGR", ["SHRAGR"]),
        ("-FZDZ GR", ["-FZDZ", "GR"]),
        ("VCSH", []),
        ("VCTS", []),
        ("VCTSRA", []),
        ("TS", []),
        ("BR HZ FG", []),
        ("WARR", []),
        ("METAR WARA 091230Z 13004KT 9999 FEW020 26/24 Q1008", []),
        ("SPECI COR WARR 091230Z 13004KT 3500 -RA BKN020 26/24 Q1008=", ["-RA"]),
        ("WARR 091230Z AUTO VRB02KT 1 1/2SM R12/3000FT +RA BKN020", ["+RA"]),
        ("WIII 091230Z 24005KT 9999 SCT020 TEMPO 2000 RA BKN010", []),
        ("METAR WAAA 091230Z VRB02KT 9999 SCT020 RMK RA RECENT", []),
        ("METAR WIII 091230Z VRB02KT 4000 BR BKN020 RA", []),
        ("METAR WIII 091230Z VRB02KT RA 9999 BKN020", []),
        ("METAR WIII 091230Z VRB02KT 9999 BECMG 2000 TSRA", []),
        ("METAR WADD 091230Z VRB02KT CAVOK NOSIG RA", []),
        ("METAR WADD 091230Z VRB02KT 5000 -RA NOSIG +RA", ["-RA"]),
    ],
)
def test_rain_groups(text, expected):
    assert rain_groups(text) == expected
    assert is_rain(text) is bool(expected)


def test_ghcnh_fixture():
    rows = parse_ghcnh(GHCNH, 2026, 1)
    assert len(rows) == 2
    assert rows[0] == {
        "station_id": "IDI0000WARR",
        "source": "ghcnh",
        "obs_time": datetime(2026, 1, 1, tzinfo=UTC),
        "lat": -7.3798,
        "lon": 112.7869,
        "wx": "",
        "is_rain": False,
        "precip_mm": None,
    }
    assert parse_ghcnh(GHCNH, 2026, 2) == []


def test_ghcnh_weather_precedence_and_first_duplicate(tmp_path):
    with GHCNH.open(newline="") as file:
        first, second = csv.DictReader(file, delimiter="|")
    first.update(REM="MET060METAR WARR 010000Z VRB02KT 6000 FEW020", pres_wx_MW1="RA:61")
    second.update(REM="", pres_wx_AU1="-RA:02", pres_wx_AW1="HZ:04", precipitation="2.5")
    duplicate = first | {"REM": "METAR WARR 010000Z VRB02KT 4000 +RA BKN010"}
    path = tmp_path / "input.psv"
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(first), delimiter="|")
        writer.writeheader()
        writer.writerows([first, duplicate, second])

    rows = parse_ghcnh(path, 2026, None)
    assert [(r["wx"], r["is_rain"], r["precip_mm"]) for r in rows] == [
        ("", False, None),
        ("-RA", True, 2.5),
    ]


def test_metar_fixture():
    payload = json.loads((FIXTURES / "metar_aviationweather_sample.json").read_text())
    rows = parse_metar(payload)
    assert [(r["station_id"], r["wx"], r["is_rain"]) for r in rows] == [
        ("WIII", "-RA", True),
        ("WADD", "", False),
        ("WARR", "", False),
    ]
    assert rows[0]["obs_time"] == datetime(2026, 11, 19, 20, 30, tzinfo=UTC)
    assert (rows[0]["lat"], rows[0]["lon"], rows[0]["precip_mm"]) == (-6.125, 106.659, None)

    raw_only = payload[0] | {"wxString": None, "precip": 0.1}
    assert parse_metar([raw_only])[0]["wx"] == "-RA"
    assert parse_metar([raw_only])[0]["precip_mm"] == pytest.approx(2.54)


def test_merge_replaces_and_sorts(tmp_path):
    path = tmp_path / "year=2026" / "month=01" / "part-ghcnh.parquet"
    first, second = parse_ghcnh(GHCNH, 2026, 1)
    merge(path, [first | {"area_id": 2}, second | {"area_id": 2}])
    path.with_name(f"{path.name}.tmp").touch()
    outside = second | {"station_id": "IDI0000WIII", "area_id": None}
    merge(path, [first | {"area_id": 2, "wx": "+RA", "is_rain": True}, outside])

    table = pq.ParquetFile(path).read()
    assert table.schema == SCHEMA
    assert [(r["area_id"], r["obs_time"], r["wx"], r["is_rain"]) for r in table.to_pylist()] == [
        (2, first["obs_time"], "+RA", True),
        (2, second["obs_time"], "", False),
        (None, second["obs_time"], "", False),
    ]
