"""Measure map raster flicker and raw rain grid coverage in the running web app."""

import argparse
import io
import json
import time
from datetime import datetime, timedelta

import numpy as np
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from nowcast import paths

BASE, APP = "", None
# Area of the 1440 x 900 map that no overlay card covers.
CLIP = {"x": 340, "y": 60, "width": 700, "height": 560}

RENDER_LOG = """clip => {
    window.renderLog = [];
    window.metric = "cloud";
    const canvas = map.getCanvas();
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    const box = canvas.getBoundingClientRect();
    const scale = canvas.width / box.width;
    const [x, w, h] = [clip.x - box.left, clip.width, clip.height].map(v => Math.round(v * scale));
    const y = canvas.height - Math.round((clip.y - box.top + clip.height) * scale);
    const pixels = new Uint8Array(w * h * 4);
    // The drawing buffer is still intact during the render event, so this reads what the frame drew.
    map.on("render", () => {
        gl.readPixels(x, y, w, h, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
        let hits = 0, total = 0;
        for (let i = 0; i < pixels.length; i += 16) {
            const [r, g, b] = [pixels[i], pixels[i + 1], pixels[i + 2]];
            const spread = Math.max(r, g, b) - Math.min(r, g, b);
            hits += metric === "cloud" ? (r + g + b) / 3 > 120 && spread < 60
                : b - r > 40 || (r > 180 && g < 150);
            total++;
        }
        renderLog.push({
            frame, playing: !!playing, share: hits / total,
            cloud: map.getLayoutProperty("cloud", "visibility"),
            rain: map.getLayoutProperty("rain", "visibility"),
            areas: frameRow(layerData[layer], frame)?.some(value => value != null) ?? false,
        });
    });
}"""


def stamp(time):
    return time.strftime("%Y%m%dT%H%MZ")


def tile_frames(meta):
    """Frame indices that have a rain tile (same slot) and a cloud tile (scan in the slot's half hour)."""
    start = datetime.fromisoformat(meta["latest_issue_slot"]) - timedelta(hours=48)
    rain, cloud = set(meta["tile_times"]["rain"]), meta["tile_times"]["cloud"]
    obs = datetime.fromisoformat(meta["latest_observed_slot"])
    frames = [start + timedelta(minutes=30 * index) for index in range(97)]
    return (
        {i for i, t in enumerate(frames) if t <= obs and stamp(t) in rain},
        {
            i
            for i, t in enumerate(frames)
            if any(stamp(t - timedelta(minutes=30)) < scan <= stamp(t) for scan in cloud)
        },
        max(i for i, t in enumerate(frames) if t <= obs),
    )


def shot(page):
    return np.asarray(Image.open(io.BytesIO(page.screenshot(clip=CLIP))).convert("RGB")).astype(int)


def rain_share(pixels):
    """Share of pixels in the dark-theme rain colours (blue ramp or pink)."""
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    return float(((blue - red > 40) | ((red > 180) & (green < 150))).mean())


def open_map(browser, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    if APP:
        page.route(
            "**/app.js", lambda route: route.fulfill(path=APP, content_type="text/javascript")
        )
    page.goto(BASE + "/", timeout=30000)
    expect(page.locator("#view-map")).to_have_attribute("aria-busy", "false", timeout=30000)
    page.wait_for_function("() => map.loaded()")
    if page.locator("#drawer").is_visible():
        page.get_by_role("button", name="Close area panel").click()
    page.evaluate(RENDER_LOG, CLIP)
    return page, errors


def settle(page):
    page.wait_for_function("() => map.loaded() && !map.isMoving()")
    page.wait_for_timeout(150)


def play(page, out, name, seconds):
    """Play from frame 0, keep the per-render log and a strip of screenshots."""
    page.evaluate(f"setFrame(0); metric = {json.dumps(name)}")
    settle(page)
    page.evaluate("renderLog.length = 0")
    page.get_by_role("button", name="Play").click()
    strip = []
    end = time.monotonic() + seconds
    while time.monotonic() < end and page.evaluate("!!playing"):
        strip.append(shot(page)[::4, ::4])
    if page.evaluate("!!playing"):
        page.get_by_role("button", name="Pause").click()
    columns = 10
    rows = [
        np.concatenate(
            strip[i : i + columns]
            + [np.zeros_like(strip[0])] * (columns - len(strip[i : i + columns])),
            axis=1,
        )
        for i in range(0, min(len(strip), 40), columns)
    ]
    Image.fromarray(np.concatenate(rows).astype("uint8")).save(out / f"{name}-strip.png")
    return [r for r in page.evaluate("renderLog") if r["playing"]]


def flicker_report(out, name, log, has_tile):
    """A render is blank when its frame has a tile but it drew under 25% of the typical share."""
    with_tile = [r for r in log if r["frame"] in has_tile]
    typical = float(np.percentile([r["share"] for r in with_tile], 90))
    blank = [r for r in with_tile if r["share"] < 0.25 * typical]
    hidden = [r for r in with_tile if r[name] != "visible"]
    frames = sorted({r["frame"] for r in blank})
    print(
        f"\n{name} play: {len(log)} renders over frames {log[0]['frame']}..{log[-1]['frame']}, "
        f"90th percentile share {typical:.4f}"
    )
    print(
        f"  blank renders on a frame with a tile: {len(blank)} of {len(with_tile)}, on {len(frames)} frames"
    )
    print(f"  renders with the layer hidden on a frame with a tile: {len(hidden)}")
    # One column per render: share height in green, full red when blank, grey when the frame has no tile.
    plot = Image.new("RGB", (len(log), 100), (29, 32, 33))
    for x, r in enumerate(log):
        blank_render = r["frame"] in has_tile and r["share"] < 0.25 * typical
        height = 98 if blank_render else int(min(1, r["share"] / (typical * 1.2)) * 98)
        color = (
            (146, 131, 116)
            if r["frame"] not in has_tile
            else (251, 73, 52)
            if blank_render
            else (184, 187, 38)
        )
        for y in range(99 - height, 100):
            plot.putpixel((x, y), color)
    plot.resize((max(len(log), 600), 100), Image.NEAREST).save(out / f"{name}-shares.png")
    return {
        "renders": len(log),
        "with_tile": len(with_tile),
        "p90": typical,
        "blank": len(blank),
        "blank_frames": len(frames),
        "hidden": len(hidden),
    }


def rain_grid(page, out, last_obs):
    """Step through every observed frame with the raw grid on."""
    page.locator("#layers label:has(input[value=ob])").click()
    page.locator("#raw").check()
    missing, rows = [], []
    for index in range(last_obs + 1):
        page.evaluate(f"setFrame({index})")
        page.wait_for_function("() => map.loaded()")
        page.wait_for_timeout(250)
        # The 60 s refresh can move the timeline, so read the slot and its tile from the page.
        slot = page.evaluate("""() => {
            const stamp = new Date(tms(frame)).toISOString().replace(/[-:]/g, "").slice(0, 13) + "Z";
            return {stamp, tile: meta.tile_times.rain.includes(stamp),
                    observed: tms(frame) <= Date.parse(meta.latest_observed_slot),
                    visible: map.getLayoutProperty("rain", "visibility") === "visible"};
        }""")
        if not slot["observed"]:
            continue
        share = rain_share(shot(page))
        note = page.locator("#rainTileNote")
        text = note.text_content() if note.is_visible() else ""
        rows.append((slot["stamp"], slot["tile"], slot["visible"], round(share, 4), text))
        if slot["tile"] and (not slot["visible"] or share == 0):
            missing.append(slot["stamp"])
            page.screenshot(path=out / f"rain-missing-{slot['stamp']}.png")
    print(f"\nRaw rain grid: {len(rows)} observed slots, {sum(row[1] for row in rows)} with a tile")
    print(f"  slots with a tile where the grid did not show: {len(missing)} {missing}")
    for stamp, tile, visible, share, text in rows:
        if not tile or stamp in missing:
            print(f"  {stamp}: tile={tile} visible={visible} share={share} note={text!r}")
    return {
        "slots": len(rows),
        "not_shown": missing,
        "gaps": [(stamp, visible, text) for stamp, tile, visible, _, text in rows if not tile],
    }


def refresh(playwright):
    """Change the archive stamp and watch the 60 s refresh on the map and the overview."""
    pages = []
    for view in ("map", "overview"):
        # One browser each, so neither page is a background tab with paused rendering.
        browser = playwright.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        bump = [0]

        def handle(route, request, bump=bump):
            response = route.fetch()
            data = response.json()
            bump[0] += 1
            data["frames_updated"] += bump[0]
            route.fulfill(response=response, body=json.dumps(data))

        page.route("**/api/meta", handle)
        if APP:
            page.route(
                "**/app.js", lambda route: route.fulfill(path=APP, content_type="text/javascript")
            )
        page.goto(BASE + ("/" if view == "map" else "/#overview"))
        expect(page.locator(f"#view-{view}")).to_have_attribute("aria-busy", "false", timeout=30000)
        if view == "map":
            page.wait_for_function("() => map.loaded()")
            page.evaluate(RENDER_LOG, CLIP)
            page.evaluate("setFrame(60)")
        else:
            page.evaluate("window.scrollTo(0, 600)")
            page.wait_for_timeout(500)
            scrolled = page.evaluate("scrollY")
        pages.append((view, page, browser))
    page.wait_for_timeout(64000)
    result = {}
    for view, page, browser in pages:
        if view == "map":
            log = page.evaluate("renderLog")
            result["map_renders_without_area_values"] = sum(not r["areas"] for r in log)
            print(
                f"\nRefresh on map: {result['map_renders_without_area_values']} of {len(log)} renders had no area values"
            )
        else:
            result["overview_scroll"] = [scrolled, page.evaluate("scrollY")]
            print(f"Refresh on overview: scrollY {scrolled} -> {result['overview_scroll'][1]}")
        browser.close()
    return result


def main():
    global BASE, APP
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", help="Output folder name, for example before or after")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument(
        "--app", help="Serve this app.js instead of the one in web/, to measure an older version"
    )
    args = parser.parse_args()
    BASE, APP = args.url.rstrip("/"), args.app
    out = paths.zone("tmp") / "render-check" / args.label
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--enable-unsafe-swiftshader"])
        page, errors = open_map(browser, 1440, 900)
        meta = page.evaluate("meta")
        rain_tiles, cloud_tiles, last_obs = tile_frames(meta)
        print(
            f"Frames with a cloud tile: {len(cloud_tiles)} of 97; missing {sorted(set(range(97)) - cloud_tiles)}"
        )
        print(
            f"Observed frames 0..{last_obs}; missing rain tiles {sorted(set(range(last_obs + 1)) - rain_tiles)}"
        )

        page.locator("#layers label:has(input[value=bm])").click()
        summary = {
            "cloud": flicker_report(
                out, "cloud", play(page, out, "cloud", args.seconds), cloud_tiles
            )
        }
        page.locator("#layers label:has(input[value=ob])").click()
        page.locator("#raw").check()
        page.locator("#cloudsOn").uncheck()
        summary["rain"] = flicker_report(
            out, "rain", play(page, out, "rain", args.seconds), rain_tiles
        )
        page.locator("#raw").uncheck()
        page.locator("#cloudsOn").check()
        summary["rain_grid"] = rain_grid(page, out, last_obs)
        summary["console_errors"] = errors
        browser.close()
        if not args.skip_refresh:
            summary["refresh"] = refresh(playwright)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\nConsole errors: {len(errors)}. Output: {out}")


if __name__ == "__main__":
    main()
