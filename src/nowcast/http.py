import math
import time
from collections.abc import Callable

import httpx

from nowcast import __version__, config
from nowcast.log import get_logger, redact


def rate_limit(
    interval: float,
    *,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Callable[[httpx.Request], None]:
    monotonic = monotonic if monotonic is not None else time.monotonic
    sleep = sleep if sleep is not None else time.sleep
    next_request = 0.0

    def wait(_request: httpx.Request) -> None:
        nonlocal next_request
        delay = next_request - monotonic()
        if delay > 0:
            sleep(delay)
        next_request = monotonic() + interval

    return wait


def client(auth: bool = False, timeout: float = 30.0) -> httpx.Client:
    headers = {"User-Agent": f"nowcast/{__version__}"}
    if auth:
        token = config.env("EARTHDATA_TOKEN")
        if not token:
            raise RuntimeError("EARTHDATA_TOKEN is not set")
        headers["Authorization"] = f"Bearer {token}"

    return httpx.Client(headers=headers, follow_redirects=True, timeout=timeout)


def get(
    c: httpx.Client, url: str, *, retries: int = 3, backoff: float = 2.0, **kwargs
) -> httpx.Response:
    for attempt in range(retries):
        delay = backoff * 2**attempt
        try:
            response = c.get(url, **kwargs)
        except httpx.TransportError as error:
            reason = type(error).__name__
        else:
            status = response.status_code
            if status != 429 and not 500 <= status < 600:
                return response
            reason = str(status)
            if status == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    pass
                else:
                    if math.isfinite(retry_after) and retry_after >= 0:
                        delay = retry_after

        get_logger(__name__).warning("Retrying GET %s after %s", redact(url), reason)
        time.sleep(delay)

    return c.get(url, **kwargs)
