from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from nowcast import __version__, http

URL = "https://example.test/data"


@pytest.fixture
def sleep(monkeypatch):
    sleep = Mock()
    monkeypatch.setattr(http.time, "sleep", sleep)
    return sleep


@pytest.mark.parametrize(
    ("status", "headers", "delay"),
    [
        (503, {}, 2.0),
        (429, {"Retry-After": "3.5"}, 3.5),
        (429, {"Retry-After": "invalid"}, 2.0),
    ],
)
def test_get_retries_status(sleep, status, headers, delay):
    handler = Mock(side_effect=[httpx.Response(status, headers=headers), httpx.Response(200)])
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert http.get(c, URL).status_code == 200

    assert handler.call_count == 2
    sleep.assert_called_once_with(delay)


def test_get_returns_404_immediately(sleep):
    handler = Mock(return_value=httpx.Response(404))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert http.get(c, URL).status_code == 404

    handler.assert_called_once()
    sleep.assert_not_called()


def test_get_returns_final_retryable_status_and_redacts_retry_url(monkeypatch, sleep):
    token = uuid4().hex
    monkeypatch.setenv("EARTHDATA_TOKEN", token)
    logger = Mock()
    monkeypatch.setattr(http, "get_logger", Mock(return_value=logger))
    handler = Mock(side_effect=[httpx.Response(503), httpx.Response(502)])

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert http.get(c, f"{URL}?token={token}", retries=1).status_code == 502

    logger.warning.assert_called_once_with("Retrying GET %s after %s", f"{URL}?token=***", "503")
    sleep.assert_called_once_with(2.0)


def test_get_reraises_final_connect_error(sleep):
    error = httpx.ConnectError("Connection failed")
    handler = Mock(side_effect=error)
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as c,
        pytest.raises(httpx.ConnectError) as exc_info,
    ):
        http.get(c, URL, retries=2)

    assert exc_info.value is error
    assert handler.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2.0, 4.0]


def test_client_requires_token_for_auth(monkeypatch):
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="^EARTHDATA_TOKEN is not set$"):
        http.client(auth=True)


def test_client_sets_headers_and_options(monkeypatch):
    token = uuid4().hex
    monkeypatch.setenv("EARTHDATA_TOKEN", token)

    with http.client(auth=True) as c:
        assert c.headers["Authorization"] == f"Bearer {token}"
        assert c.headers["User-Agent"] == f"nowcast/{__version__}"
        assert c.follow_redirects
        assert c.timeout == httpx.Timeout(30.0)

    with http.client(timeout=5.0) as c:
        assert "Authorization" not in c.headers
        assert c.timeout == httpx.Timeout(5.0)
