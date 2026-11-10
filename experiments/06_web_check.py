"""Screenshot the running Nowcast web app and report browser failures."""

import argparse

from playwright.sync_api import Error, expect, sync_playwright

from nowcast import paths

VIEWS = (
    "map",
    "overview",
    "alerts",
    "model",
    "history",
    "pipeline",
    "settings",
    "pipeline/node/sv_rain",
    "pipeline/edge/br_imerg~sv_rain",
    "pipeline/node/br_bmkg",
    "pipeline/lane/forecast",
    "pipeline/col/gold",
)
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
                    expect(page.locator(f"#view-{name.split('/')[0]}")).to_have_attribute(
                        "aria-busy", "false", timeout=30000
                    )
                    expect(page.locator("#dataStatus")).to_be_hidden()
                    if name == "map":
                        expect(page.locator("#map canvas.maplibregl-canvas")).to_be_visible()
                        page.wait_for_function("map.loaded()")
                    if name.startswith("pipeline/"):
                        expect(page.locator("#plExplain")).to_be_visible()
                        expect(page.locator("#plExplain")).not_to_contain_text(
                            "Loading current data"
                        )
                    if name.startswith("pipeline"):
                        geometry = page.evaluate("""() => {
                            const nodes = [...document.querySelectorAll('.pl-node')];
                            const boxes = nodes.map(node => node.querySelector('rect').getBBox());
                            const problems = [];
                            nodes.forEach((node, i) => {
                                const box = boxes[i];
                                for (const text of node.querySelectorAll('text')) {
                                    const bounds = text.getBBox();
                                    if (bounds.x < box.x || bounds.y < box.y
                                        || bounds.x + bounds.width > box.x + box.width
                                        || bounds.y + bounds.height > box.y + box.height) {
                                        problems.push('Text overflow: ' + text.textContent);
                                    }
                                }
                            });
                            for (const path of document.querySelectorAll('.dg .edge')) {
                                for (let distance = 1; distance < path.getTotalLength(); distance++) {
                                    const p = path.getPointAtLength(distance);
                                    if (boxes.some(b => p.x > b.x + 0.5 && p.x < b.x + b.width - 0.5
                                        && p.y > b.y + 0.5 && p.y < b.y + b.height - 0.5)) {
                                        problems.push('Edge crosses box: ' + path.getAttribute('d'));
                                        break;
                                    }
                                }
                            }
                            return problems;
                        }""")
                        checks.extend(geometry)
                        if name.startswith("pipeline/") and width < 900:
                            expect(page.locator("#plExplain h2")).to_be_in_viewport()
                            assert page.locator("#plExplain h2").bounding_box()["y"] >= 48
                        page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(
                        path=shots / f"{name.replace('/', '-')}-{width}.png",
                        full_page=name.startswith("pipeline"),
                    )
                    if name.startswith("pipeline"):
                        page.evaluate("settings.theme = 'light'; applyTheme()")
                        page.screenshot(
                            path=shots / f"{name.replace('/', '-')}-light-{width}.png",
                            full_page=True,
                        )
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
