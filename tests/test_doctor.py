import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from nowcast import doctor
from nowcast.cli import app


@pytest.mark.parametrize(
    ("days", "status", "detail"),
    [
        (30, "PASS", "set (expires in 30 days)"),
        (3, "WARN", "set (expires in 3 days)"),
        ("garbage", "WARN", "set (expiry unknown)"),
        (None, "FAIL", "missing"),
    ],
)
def test_token_expiry(monkeypatch, capsys, days, status, detail):
    token = None
    if days is None:
        monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    else:
        token = uuid4().hex
        if isinstance(days, int):
            expiry = datetime.now(UTC) + timedelta(days=days, hours=1)
            payload = base64.urlsafe_b64encode(
                json.dumps({"exp": int(expiry.timestamp())}).encode()
            )
            token = f"e30.{payload.decode().rstrip('=')}.{token}"
        monkeypatch.setenv("EARTHDATA_TOKEN", token)
    monkeypatch.setattr(doctor, "LOCAL_CHECKS", (doctor.earthdata_token,))
    monkeypatch.setattr(doctor, "NETWORK_CHECKS", ())

    assert doctor.run_doctor() == int(status == "FAIL")
    output = capsys.readouterr().out
    assert output.splitlines()[0] == f"{status}  earthdata_token  {detail}"
    if token is not None:
        assert token not in output


@pytest.mark.parametrize(("status", "code"), [("PASS", 0), ("FAIL", 1)])
def test_doctor_exit_code(monkeypatch, status, code):
    def local():
        return "PASS", "ok"

    def network(request):
        return status, "checked"

    monkeypatch.setattr(doctor, "LOCAL_CHECKS", (local,))
    monkeypatch.setattr(doctor, "NETWORK_CHECKS", (network,))

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == code
    assert f"{2 - code} pass, 0 warn, {code} fail" in result.output
