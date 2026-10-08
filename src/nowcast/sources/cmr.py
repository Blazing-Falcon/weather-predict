from datetime import UTC, datetime

from nowcast import config, http


def granules(collection: str, start: datetime, end: datetime) -> list[dict]:
    bounds = [dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") for dt in (start, end)]
    params = {
        "collection_concept_id": collection,
        "temporal": ",".join(bounds),
        "sort_key": "start_date",
        "page_size": 2000,
    }
    url = config.settings()["cmr"]["granules"]
    header = "CMR-Search-After"
    headers = {}
    results = []
    with http.client() as client:
        while True:
            response = http.get(client, url, params=params, headers=headers).raise_for_status()
            entries = response.json()["feed"]["entry"]
            if not entries:
                return results
            results.extend(
                {key: entry[key] for key in ("title", "time_start", "time_end")}
                for entry in entries
            )
            if header not in response.headers:
                return results
            headers = {header: response.headers[header]}
