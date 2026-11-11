from datetime import UTC, datetime

import httpx
import typer

from nowcast import config, http
from nowcast.log import get_logger
from nowcast.timeslots import utc_text


def analysis_time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def analysis_date(payload: dict) -> str | None:
    value = next(
        (
            step["analysis_date"]
            for point in payload.get("data", [])
            for group in point.get("cuaca", [])
            for step in group
            if step.get("analysis_date")
        ),
        None,
    )
    return utc_text(analysis_time(value)) if value else None


def request_rate() -> int:
    try:
        rpm = int(config.env("BMKG_MAX_RPM", str(config.settings()["bmkg"]["max_rpm"])))
        if rpm < 1:
            raise ValueError
    except ValueError:
        raise typer.BadParameter("BMKG_MAX_RPM must be a positive integer.") from None
    return min(rpm, 55)


def fetch(client: httpx.Client, url: str, adm4: str) -> httpx.Response | None:
    try:
        return http.get(client, url, params={"adm4": adm4})
    except httpx.RequestError as error:
        get_logger(__name__).warning(
            "BMKG request for %s failed: %s", adm4, type(error).__name__,
        )
        return None


def fetch_json(client, url, **kwargs):
    try:
        response = http.get(client, url, **kwargs)
        if response.status_code != 200:
            return {}, response.status_code
        payload = response.json()
    except (httpx.RequestError, ValueError) as error:
        return {}, type(error).__name__
    if not isinstance(payload, dict):
        return {}, "TypeError"
    return payload, None
