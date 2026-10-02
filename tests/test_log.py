import logging
import re
from io import StringIO
from uuid import uuid4

from nowcast.log import get_logger


def test_logging_redacts_messages_arguments_and_tracebacks(monkeypatch):
    parent = logging.getLogger("nowcast")
    monkeypatch.setattr(parent, "handlers", [])
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    logger = get_logger("nowcast.test")
    stream = StringIO()
    monkeypatch.setattr(parent.handlers[0], "stream", stream)
    token = uuid4().hex
    monkeypatch.setenv("EARTHDATA_TOKEN", token)

    logger.info(f"Message contains {token}")
    logger.info("Credentials contain %s", "Bearer abc.def")
    try:
        raise ValueError(token)
    except ValueError:
        logger.exception("Request failed")

    output = stream.getvalue()
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ INFO nowcast\.test: ", output)
    assert "Message contains ***" in output
    assert "Credentials contain ***" in output
    assert "ValueError: ***" in output
    assert token not in output
    assert "abc.def" not in output
    assert get_logger("test") is logger
    assert len(parent.handlers) == 1
