"""Screenshot the running Nowcast web app and report browser failures."""

import argparse

from playwright.sync_api import Error, expect, sync_playwright

from nowcast import paths

VIEWS = ("map", "overview", "alerts", "model", "history", "pipeline", "settings")
VIEWPORTS = ((1440, 900), (390, 844))


def capture_map_states(page, shots, width):
    expect(page.locator("#map canvas.maplibregl-canvas")).to_be_visible()
    if page.locator("#drawer").is_visible():
        page.get_by_role("button", name="Close area panel").click()
    page.get_by_role("button", name="Jawa Timur").click()
    page.wait_for_function("!map.isMoving()")
    point = page.evaluate(
        """() => {
            const area = areaById.get(malangId);
            const point = map.project([area.centroid_lon, area.centroid_lat]);
            return {x: point.x, y: point.y};
        }"""
    )
    page.locator("#map canvas.maplibregl-canvas").click(position=point)
    expect(page.locator("#dName")).to_have_text("Kota Malang")
    expect(page.locator("#dDetail")).not_to_have_text("Loading area details…")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=shots / f"map-drawer-{width}.png", full_page=True)

    slider = page.get_by_role("slider", name="Time", exact=True)
    issue = int(slider.input_value())
    slider.fill(str(issue - 48))
    expect(page.locator("#wS")).to_contain_text("−24 h")
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(path=shots / f"map-back-24h-{width}.png", full_page=True)

    page.get_by_role("link", name="Settings").click()
    page.get_by_role("button", name="Light", exact=True).click()
    page.get_by_role("link", name="Map", exact=True).click()
    expect(page.locator("#view-map")).to_have_attribute("aria-busy", "false")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    expect(page.locator("#dName")).to_have_text("Kota Malang")
    page.screenshot(path=shots / f"map-light-{width}.png", full_page=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    base = parser.parse_args().url.rstrip("/")
    shots = paths.zone("tmp") / "web-shots"
    shots.mkdir(parents=True, exist_ok=True)
    problems = []
    print("| Page | Width | Console errors | Failed requests |", flush=True)
    print("|---|---:|---:|---:|", flush=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader"])
        for name in VIEWS:
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                errors, failed, checks = [], [], []
                page.on(
                    "console",
                    lambda message, errors=errors: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
                page.on(
                    "requestfailed",
                    lambda request, failed=failed: failed.append(
                        f"{request.method} {request.url} ({request.failure})"
                    ),
                )
                page.on(
                    "response",
                    lambda response, failed=failed: (
                        failed.append(f"{response.status} {response.url}")
                        if response.status >= 400
                        else None
                    ),
                )
                try:
                    page.goto(base + ("/" if name == "map" else f"/#{name}"), timeout=30000)
                    expect(page.locator(f"#view-{name}")).to_have_attribute(
                        "aria-busy", "false", timeout=30000
                    )
                    expect(page.locator("#dataStatus")).to_be_hidden()
                    if name == "map":
                        expect(page.locator("#map canvas.maplibregl-canvas")).to_be_visible()
                        page.wait_for_function("map.loaded()")
                    page.screenshot(path=shots / f"{name}-{width}.png")
                    if name == "map":
                        capture_map_states(page, shots, width)
                except (Error, AssertionError) as error:
                    checks.append(str(error))
                print(f"| {name} | {width} | {len(errors)} | {len(failed)} |", flush=True)
                problems.extend(
                    f"{name} {width}: {message}" for message in errors + failed + checks
                )
                page.close()
        browser.close()
    print(f"Screenshots: {shots}")
    if problems:
        print(*problems, sep="\n")
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
