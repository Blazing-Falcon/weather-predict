import logging
import re
import time
from contextlib import contextmanager
from threading import Lock

import typer

from nowcast import config

_lock = Lock()


def redact(text: str) -> str:
    token = config.env("EARTHDATA_TOKEN")
    text = re.sub(r"Bearer[ \t]+\S+", "***", text, flags=re.IGNORECASE)
    return text.replace(token, "***") if token else text


class _RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger("nowcast")
    with _lock:
        if not any(
            isinstance(handler.formatter, _RedactingFormatter) for handler in logger.handlers
        ):
            handler = logging.StreamHandler()
            handler.setFormatter(
                _RedactingFormatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%SZ",
                )
            )
            logger.setLevel(config.env("LOG_LEVEL") or "INFO")
            logger.propagate = False
            logger.addHandler(handler)

    if name != "nowcast" and not name.startswith("nowcast."):
        name = f"nowcast.{name}"
    return logging.getLogger(name)


@contextmanager
def command_errors(*errors):
    """Turn the given errors into a redacted message and exit code 1."""
    try:
        yield
    except errors as error:
        typer.echo(redact(str(error)), err=True)
        raise typer.Exit(1) from None
