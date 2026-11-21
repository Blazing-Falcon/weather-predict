"""Capture the README animations and screenshots from a running HephaWeather site.

    uv run python docs/media/capture.py --base https://hephaweather.farhankf.my.id

Needs ffmpeg and the JetBrains Mono font, which the overlays use.
"""

import argparse
import io
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent
SCALE = 1.5  # device pixels per CSS pixel; enough for a sharp 2x zoom at 1200 px wide
FPS = 12
WIDTH = 1200
WIB = timezone(timedelta(hours=7))
BG, PANEL, LINE, FG, MUTED, YELLOW = "#1d2021", "#282828", "#504945", "#ebdbb2", "#a89984", "#fabd2f"

LANES = [
    ("", "whole pipeline", "sources → ingest → bronze → silver → gold → model → web app"),
    ("cloud", "cloud chain", "Himawari-9 every 10 min → raw scans → cloud tops per kab/kota → map and score"),
    ("rain", "rain chain", "IMERG every 30 min, about 5 h late → rain per kab/kota → the truth to check against"),
    ("bmkg", "bmkg chain", "BMKG's own forecast, 2 runs a day → the baseline to beat"),
    ("training", "training", "10 years of IMERG + MERGIR → Spark builds training rows → LightGBM, trained offline"),
    ("forecast", "forecast and check", "every 30 min: 514 kab/kota × 3 leads → checked once IMERG catches up"),
]

SETTLE = """async index => {
    await nowcast.setFrame(index);
    await new Promise(done => {
        const map = nowcast.state.map;
        map.once("idle", done);
        map.triggerRepaint();
    });
}"""

# Lights the lane's boxes up to one column, with the classes the page uses for a selected lane.
LIGHT = """async ([laneId, upto]) => {
    const { PIPELINE } = await import("/pipeline-content.js");
    const column = new Map(PIPELINE.nodes.map(node => [node.id, node.col]));
    const lane = PIPELINE.lanes.find(lane => lane.id === laneId);
    const reached = new Set(lane.nodes.filter(id => column.get(id) <= upto));
    const edges = new Map(PIPELINE.edges.map(edge => [`${edge.from}~${edge.to}`, edge]));
    for (const element of document.querySelectorAll("#plDiagram [data-pl]")) {
        const [type, value] = element.dataset.pl.split("/");
        const edge = edges.get(value);
        const lit = type === "node" ? reached.has(value)
            : type === "edge" && reached.has(edge.from) && reached.has(edge.to);
        element.classList.toggle("pl-dim", !lit);
        element.classList.toggle("pl-lit", lit);
    }
    return [Math.min(...lane.nodes.map(id => column.get(id))), Math.max(...lane.nodes.map(id => column.get(id)))];
}"""


def font(size, bold=False):
    return ImageFont.truetype(f"JetBrainsMono-{'Bold' if bold else 'Regular'}.ttf", size)


def write(draw, y, size, spans, *, bold=False, chars=None, cursor=False, center=None):
    """Draws coloured spans centred on the full text, so a partly typed line does not move."""
    face = font(size, bold)
    full = "".join(text for text, _ in spans)
    x = (center or WIDTH / 2) - draw.textlength(full, font=face) / 2
    left = len(full) if chars is None else chars
    for text, color in spans:
        shown = text[:left]
        draw.text((x, y), shown, font=face, fill=color)
        x += draw.textlength(shown, font=face)
        left -= len(shown)
    if cursor:
        draw.rectangle((x + 4, y + size * 0.15, x + 4 + size * 0.55, y + size * 1.15), fill=YELLOW)


class Reel:
    """Frames on disk with how long each one shows, encoded together as one animation."""

    def __init__(self, folder):
        self.folder, self.entries = Path(folder), []

    def add(self, image, seconds):
        path = self.folder / f"{len(self.entries):05d}.png"
        image.save(path)
        self.entries.append((path, seconds))

    def save(self, path):
        listing = self.folder / "frames.txt"
        lines = [f"file '{frame}'\nduration {seconds:.3f}" for frame, seconds in self.entries]
        # The concat reader ignores the last duration unless the last file is listed again.
        listing.write_text("\n".join(lines + [f"file '{self.entries[-1][0]}'"]) + "\n")
        if path.suffix == ".gif":
            codec = ["-filter_complex",
                     "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"]
        else:
            # Moving clouds change the whole map every frame, which makes a GIF about four times larger.
            codec = ["-c:v", "libwebp_anim", "-quality", "80", "-compression_level", "6"]
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             *codec, "-fps_mode", "vfr", "-loop", "0", str(path)],
            check=True,
        )
        print(f"{path.name}: {len(self.entries)} frames, {path.stat().st_size / 1e6:.1f} MB")


def card(height, lines):
    image = Image.new("RGB", (WIDTH, height), BG)
    draw = ImageDraw.Draw(image)
    for y, size, spans, options in lines:
        write(draw, y, size, spans, **options)
    return image


def hero(browser, base, folder):
    reel = Reel(folder)
    height = WIDTH * 9 // 16
    name = [("Hepha", YELLOW), ("Weather", FG)]
    lines = lambda chars, cursor, rest: [
        (height * 0.36, 72, name, {"bold": True, "chars": chars, "cursor": cursor}),
        (height * 0.36 + 110, 26, [("a dumb rain nowcast for 514 kab/kota in indonesia", FG)] if rest else [], {}),
        (height * 0.36 + 156, 19, [("48 h replay · himawari-9 clouds · our chance of rain", MUTED)] if rest else [], {}),
    ]
    for chars in range(1, len("HephaWeather") + 1):
        reel.add(card(height, lines(chars, True, False)), 1 / FPS)
    for cursor in (True, False, True, False):
        reel.add(card(height, lines(None, cursor, True)), 0.4)

    view_w, view_h = 1600, 900
    page = browser.new_page(viewport={"width": view_w, "height": view_h}, device_scale_factor=SCALE)
    page.goto(base + "#map", wait_until="networkidle")
    page.wait_for_function("() => nowcast.state.mapReady && nowcast.state.map.loaded()")
    page.mouse.move(0, 0)
    last = page.evaluate("nowcast.state.issueFrame")
    for index in range(last + 1):  # loads every cloud image once, so each capture only redraws
        page.evaluate(SETTLE, index)
    # Java, Bali and south Kalimantan, and the open map between the side panels, in page pixels.
    (west, north), (east, south), (left, right) = page.evaluate("""() => {
        const map = nowcast.state.map, box = map.getContainer().getBoundingClientRect();
        const corners = [[105.0, -2.0], [116.0, -9.0]].map(point => {
            const p = map.project(point);
            return [p.x + box.left, p.y + box.top];
        });
        const side = selector => document.querySelector(selector).getBoundingClientRect();
        return [...corners, [side(".ov-left").right, side(".ov-right").left]];
    }""")
    zoom = 2.0
    half = view_w / zoom / 2
    target = (min(right - half - 8, max(left + half + 8, (west + east) / 2)), (north + south) / 2)
    zoom_from = last - 36
    for index in range(last + 1):
        page.evaluate(SETTLE, index)
        shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
        progress = min(1, max(0, (index - zoom_from) / (last - zoom_from)))
        eased = progress * progress * (3 - 2 * progress)
        scale = 1 + (zoom - 1) * eased
        width, high = view_w / scale, view_h / scale
        cx = min(view_w - width / 2, max(width / 2, view_w / 2 + (target[0] - view_w / 2) * eased))
        cy = min(view_h - high / 2, max(high / 2, view_h / 2 + (target[1] - view_h / 2) * eased))
        box = [round(v * SCALE) for v in (cx - width / 2, cy - high / 2, cx + width / 2, cy + high / 2)]
        frame = shot.crop(box).resize((WIDTH, height), Image.Resampling.LANCZOS)
        when = datetime.fromtimestamp(page.evaluate(f"nowcast.frameTime({index})") / 1000, WIB)
        label = f"{when:%a %d %b %H:%M} WIB · our chance of rain, next 3 h"
        draw = ImageDraw.Draw(frame, "RGBA")
        face = font(17)
        span = draw.textlength(label, font=face)
        center = WIDTH * 770 / view_w  # between the left panel and the area panel
        draw.rounded_rectangle((center - span / 2 - 14, 62, center + span / 2 + 14, 98), 8, fill=(40, 40, 40, 235), outline=LINE)
        draw.text((center - span / 2, 70), label, font=face, fill=FG)
        reel.add(frame, 1 / FPS if index < last else 1.2)
    page.close()

    reel.add(card(height, [
        (height * 0.40, 40, [("hephaweather.farhankf.my.id", YELLOW)], {"bold": True}),
        (height * 0.40 + 70, 19, [("data from NASA · NOAA/JMA · BMKG", MUTED)], {}),
    ]), 2.4)
    reel.save(OUT / "hero.webp")


def pipeline(browser, base, folder):
    reel = Reel(folder)
    page = browser.new_page(viewport={"width": 1440, "height": 1300}, device_scale_factor=SCALE)
    page.goto(base + "#pipeline", wait_until="networkidle")
    page.wait_for_selector("#plDiagram [data-pl]")
    box = page.evaluate("""() => {
        const a = document.getElementById("plLanes").getBoundingClientRect();
        const b = document.getElementById("plDiagram").getBoundingClientRect();
        return {x: Math.min(a.left, b.left) - 12, y: a.top - 12, width: Math.max(a.right, b.right) - Math.min(a.left, b.left) + 24, height: b.bottom - a.top + 40};
    }""")
    band = 64

    def frame(title, text):
        shot = Image.open(io.BytesIO(page.screenshot(clip=box))).convert("RGB")
        shot = shot.resize((WIDTH, round(shot.height * WIDTH / shot.width)), Image.Resampling.LANCZOS)
        image = Image.new("RGB", (WIDTH, shot.height + band), PANEL)
        image.paste(shot, (0, 0))
        draw = ImageDraw.Draw(image)
        draw.line((0, shot.height, WIDTH, shot.height), fill=LINE)
        face, bold = font(18), font(18, True)
        x = 28
        draw.text((x, shot.height + 20), title, font=bold, fill=YELLOW)
        draw.text((x + draw.textlength(title, font=bold) + 18, shot.height + 20), text, font=face, fill=FG)
        return image

    for lane, title, text in LANES:
        page.evaluate("hash => { location.hash = hash; }", "pipeline" + (f"/lane/{lane}" if lane else ""))
        page.wait_for_timeout(600)
        if lane:
            first, end = page.evaluate(LIGHT, [lane, -1])
            for upto in range(first, end + 1):
                page.evaluate(LIGHT, [lane, upto])
                reel.add(frame(title, text), 0.25)
        reel.add(frame(title, text), 2.0)
    page.close()
    reel.save(OUT / "pipeline.gif")


def pages(browser, base):
    page = browser.new_page(viewport={"width": 1440, "height": 810}, device_scale_factor=1)
    tiles = []
    for route in ("overview", "alerts", "model", "history"):
        page.goto(base + "#" + route, wait_until="networkidle")
        page.wait_for_timeout(1500)
        tiles.append(Image.open(io.BytesIO(page.screenshot())).convert("RGB").resize((800, 450), Image.Resampling.LANCZOS))
    page.close()
    gap = 6
    grid = Image.new("RGB", (2 * 800 + gap, 2 * 450 + gap), LINE)
    for i, tile in enumerate(tiles):
        grid.paste(tile, ((i % 2) * (800 + gap), (i // 2) * (450 + gap)))
    path = OUT / "pages.png"
    grid.quantize(256, dither=Image.Dither.NONE).save(path, optimize=True)
    print(f"{path.name}: {path.stat().st_size / 1e6:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="https://hephaweather.farhankf.my.id/")
    parser.add_argument("--only", choices=["hero", "pipeline", "pages"])
    args = parser.parse_args()
    base = args.base.rstrip("/") + "/"
    with sync_playwright() as playwright, tempfile.TemporaryDirectory() as folder:
        browser = playwright.chromium.launch()
        if args.only in (None, "hero"):
            (Path(folder) / "hero").mkdir()
            hero(browser, base, Path(folder) / "hero")
        if args.only in (None, "pipeline"):
            (Path(folder) / "pipeline").mkdir()
            pipeline(browser, base, Path(folder) / "pipeline")
        if args.only in (None, "pages"):
            pages(browser, base)
        browser.close()


if __name__ == "__main__":
    main()
