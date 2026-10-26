# UI/UX Audit of the Web App

Date: 2026-11-20. Scope: the seven views of `web/` at 1440 px and 390 px, dark and light, against `reference/mockup-v6/` and `reference/webapp.md`. A Codex researcher (gpt-6.2-astra, read-only) wrote the first draft from 176 headless Chromium screenshots and DOM metrics. Claude checked the main claims in the code, the screenshots and the live API, corrected two, dropped none and added two. Screenshots and `metrics.json` are in `data/tmp/ui-audit/` (not in git).

## Summary

The app matches the mockup layout well and is faster than the mockup on every page (non-map views ready in 0.22 to 0.45 s). The main problems are information that can be wrong or stale (LIVE pill, drawer data, the History period, forecast-lead labels), weak error states, light-theme contrast on small text, and a 390 px layout that hides key alert values and makes some charts unreadable.

## High Severity

| ID | Page, Element | Evidence | Fix |
|---|---|---|---|
| A01 | Map, Overview: LIVE pill and freshness | The 60 s poll drops the new `/api/meta` when the archive stamp did not change, so source ages and the 60 min LIVE cutoff freeze exactly when ingestion stops. `web/app.js:1299`, `web/app.js:761` | Apply `freshness_minutes` and the status pill on every successful poll. Use the archive stamp only to decide whether to reload frames. |
| A02 | Map: area drawer data | `areaDetails` is never cleared. A failed fetch is stored as `null` and never retried; after a refresh the drawer mixes new frame values with old cloud, BMKG and score data. `web/app.js:651`, `web/app.js:1299` | Clear or re-key `areaDetails` when the archive stamp changes, do not cache a failed fetch, and refetch the open area. |
| A03 | Map: forecast lead labels after "issue" | At +2 h the drawer says "Our model, next 2 h" and the legend "Chance of rain, next 2 h", but the value is P(rain within 2 h of the 01:30 issue), not of the 2 h after 03:30. The layer radio still says "next 3 h". `app-mapstate-1440-dark-slider-forecast-plus2h.png`, `web/app.js:206`, `web/app.js:675` | Use one phrase everywhere, such as "Rain by 03:30 WIB (issued 01:30)", in the drawer, tooltip, legend and layer list. |
| A04 | History: climatology period | Kota Malang shows "2025-01-01 to 2025-01-21" over a full 12-month heatmap. Cause: the API process started 02:53 UTC, before the config moved to the full run (06:56 UTC), and `config.settings()` is cached for the process life. The live `/api/history` still returns `['2025-01-01', '2025-01-21']`; `config/settings.yaml:53` now says 2016 to 2023. `app-history-390-light-full.png`, `src/nowcast/config.py:14` | Restart the API. Better: read the period from the climatology build metadata, so the label cannot drift from the data. |
| A05 | All views: error states | With every `/api/*` call failing, the map shows "Map data is unavailable" beside a "LOADING" pill, Alerts shows "0 areas", Overview and Model draw empty charts, and the banner names only the last failure ("Alerts could not load"). The banner is never hidden again, so one transient failure stays on screen until reload. `app-state-error-*.png`, `web/app.js:129` | Per view, tell loading, zero rows and failure apart. Show "unavailable" instead of 0, list every failed source in the banner, clear it on the next success, and add a Retry button. |
| A06 | All views, light theme: small text contrast | `--muted` text fails WCAG AA 4.5:1 in light: on page 4.42, on panel 4.29, on `--panel2` 3.55 (dark on `--panel2`: 4.17). Waspada text (`--yellow` on light panel) is 3.33. Unpressed watch star: 2.26 dark, 1.92 light. MapLibre canvas focus ring: 1.16. See the table below. `web/style.css:3`, `web/style.css:58`, `web/style.css:153` | Darken light `--muted` to `#665c54` (5.92 on page, 5.74 on panel, 4.75 on `--panel2`), use `--fg` for notes on dark `--panel2`, give Waspada text a darker yellow in light such as `#8f5902` (5.14 on panel), raise `.star` to `--muted`, and give `.maplibregl-canvas:focus-visible` the yellow ring. |

## Medium Severity

| ID | Page, Element | Evidence | Fix |
|---|---|---|---|
| A07 | All views: BMKG weather shown as raw codes | The drawer and tables show "No rain · code 10" and "Rain · code 61". The BMKG payload itself names them: 10 Udara Kabur, 61 Hujan Ringan, 63 Hujan Sedang, 95 Hujan Petir (bronze run 2026-11-20T12). The mockup shows "Hujan Ringan". `web/app.js:67`, `app-overview-1440-light.png` | Map the 11 codes seen in bronze to their Indonesian names (English in a tooltip). Keep the rain or no-rain colour. |
| A08 | Alerts, 390 px: hidden values | The first screen shows only star, level, name and island; the three lead probabilities and the map button need sideways scroll. The page is 11,605 px tall. No column sorting, which the spec asks for. `app-alerts-390-dark.png` | On narrow screens, show the highest probability and the map action in the row. Add sortable headers with `aria-sort`, default severity first. |
| A09 | History, Overview, 390 px: chart text | SVG charts scale with a fixed `viewBox`. The 1200-wide wet-season chart renders its labels at about 2.7 px, the Overview share chart at about 4.3 px. `app-history-390-light-full.png`, `web/index.html:160` | Draw ticks for the rendered width (fewer labels on mobile) or let the plot scroll inside its panel. |
| A10 | Map: keyboard and focus in the drawer | An area can be opened by keyboard only from the Overview and Alerts lists, not for any of the 514 areas. Opening the drawer does not move focus; Close hides the focused button and focus is lost; no Escape. `web/app.js:639`, `web/app.js:1237` | Add an area search box on the map that calls `openDrawer`. Focus the drawer heading on open, close on Escape, and return focus to the opener. |
| A11 | All views, 390 px: navigation | Only Map, Overview and Alerts are visible; the tab strip scrolls with a hidden scrollbar and no cue. `app-map-390-dark.png`, `web/style.css:28` | Add a fade or arrow cue at the strip edge, or a compact view menu. |
| A12 | Alerts, Map, Settings: tap targets | Watch star 22 × 19 px and "map" button 28 × 19 px are below the WCAG 2.2 AA 24 px minimum; zoom buttons 24 px high, Play 36 × 36, range inputs 16 px high. 236 targets under 44 px on Alerts. `metrics.json` | Pad `.star`, `.linkbtn`, `.btn`, `.play` and range thumbs to at least 44 px hit areas on touch screens, keeping the glyph size. |
| A13 | Map: raster failure | When a new cloud or raw rain image fails, the old image stays and the note says only "could not load", without saying which time is on screen. `web/app.js:405` | On failure, hide the old raster or label its real time in the note. |
| A14 | Map: legends | The cloud ramp stays in the legend when clouds are hidden (forecast-only frames). Airport rings have no legend entry. BMKG wet swatch is blue-green, while the spec reserves orange for BMKG. `app-mapstate-1440-dark-layer-bm.png`, `web/app.js:240` | Show only active overlays, add a ring key ("METAR airport, rain reported"), and decide with juan whether the BMKG layer uses the orange identity. |
| A15 | Map, 1440 px: left card clipped | The left stack overflows its scroll box by 14 px; the credits end is cut ("Earth." at the slider edge). `app-map-1440-dark.png`, `web/style.css:67` | Compact the status card or move the credits into a collapsible About line. |
| A16 | Map: drawer chart keys | The BMKG strip has no wet or dry key; the vertical lines (IMERG cutoff, issue, selection) have no key; the "cloud top °C" label is drawn under the line. `app-mapstate-1440-dark-drawer.png`, `web/app.js:690` | Add a one-line key under the chart and move the panel labels above their plot area. |
| A17 | Overview, Map, Pipeline: time labels | "Raining at 20:30" has no zone or day, although the issue is the next day 01:30 WIB. Map source clocks and Pipeline run starts also omit the date. Pipeline says "BMKG 519 kab/kota", but 519 is the array width; 514 are admin areas. `app-overview-1440-light.png`, `web/app.js:794`, `web/index.html:182` | Use "20:30 WIB, 20 Nov" when the day differs from the issue day. Change the diagram text to the real count from `/api/meta`. |
| A18 | Overview: thresholds not stated | The "Raining" tile counts areas with ≥ 0.5 mm/h, while the map's first wet bin starts at 0.1 mm/h. "Wettest kab/kota" ranks by probability, not amount. `web/app.js:794`, `web/index.html:112` | State "≥ 0.5 mm/h" on the tile and rename the table "Highest chance of rain, next 3 h". |
| A19 | Pipeline, 390 px: order | The 1000 px-wide diagram comes first and shows only two columns; live health is far below. The page is 11,050 px tall. `app-pipeline-390-light.png` | On narrow screens, put health and lag first, add a scroll cue to the diagram, and collapse old run rows. |
| A20 | Map: slider for screen readers | `aria-valuetext` gives only the time, not "+2 h, forecast only" or "IMERG pending". The drawer's `aria-live` text is rewritten on every frame during playback. `web/app.js:306`, `web/index.html:82` | Add the relative time and state to `aria-valuetext`; update the live region only on user actions, not during playback. |
| A21 | Map: first paint | Cold desktop map ready in 2.98 s (warm 1.11 s). Area colours wait for pipeline stats, scores and all seven frame layers. Hypothesis: painting the selected layer first would cut felt time. `metrics.json`, `web/app.js:1092` | Paint geometry and the active layer as soon as they arrive; load the scoreboard, stats and other layers after. Measure with `experiments/06_web_check.py`. |
| A22 | Model: KPI labels | Summary KPIs use a 50% cutoff beside the "best validation CSI cutoff"; CSI charts show only +3 h. `web/app.js:869` | Label the cutoff used and show all three leads. |

## Low Severity

| ID | Page, Element | Evidence | Fix |
|---|---|---|---|
| A23 | All charts: data access | SVGs have names but no values; History cells need hover; Model hourly lines differ only by colour. `web/app.js:904` | Add a short text summary per chart, a numeric heatmap key, and dash patterns for overlapping lines. |
| A24 | Settings: controls | Cloud opacity has no visible value; the airport checkbox is named only "Show". `web/index.html:268` | Show the percentage next to the slider; name the checkbox "Show airport rings". |
| A25 | Map: "140.4bn" | Unclear unit and source. `web/app.js:772` | Write "140.4 billion grid values (IMERG + MERGIR)". |
| A26 | Map: reduced motion | Camera moves (700 ms preset, 600 ms area) ignore `prefers-reduced-motion`; the LIVE pulse respects it. Not tested. `web/app.js:494`, `web/app.js:648` | Use duration 0 when reduced motion is set. |

## Contrast Ratios

Computed with WCAG relative luminance; Claude recomputed every pair below.

| Pair | Dark | Light |
|---|---|---|
| Body `--fg` on `--bg` | 11.95 | 10.53 |
| `--muted` on `--bg` | 5.90 | **4.42** |
| `--muted` on `--panel` | 5.30 | **4.29** |
| `--muted` on `--panel2` | **4.17** | **3.55** |
| `--yellow` (focus ring, Waspada) on `--panel` | 8.69 | 3.33 (text **fails**, ring passes 3:1) |
| Unpressed star `--line2` on `--panel` | **2.26** | **1.92** |
| Canvas focus ring `#101010` on sea `#1d2021` | **1.16** | not captured |

Bold marks a failure (4.5:1 for text, 3:1 for focus and control cues). The eleven palette tokens match `reference/webapp.md` in both themes.

## Keep (Better Than or Equal to the Mockup)

- No page has horizontal overflow at 390 px; wide tables and the Pipeline diagram scroll inside their panels.
- The No data class, separate IMERG-pending and forecast-only track marks, the dated WIB readout, the selected-area outline and the hover tooltip with units.
- Honest absence notes on Model, History and Pipeline, the explicit Waspada, Siaga and Awas thresholds, and province and island filters.
- Local MapLibre, compact per-slot arrays, raster prefetch, visible yellow focus rings on controls, `aria-current`, `aria-pressed`, `aria-busy` and named map region. No unnamed controls, unlabelled SVGs or images without alt text were found.

## Not Checked

Real touch devices, screen readers, network transfer sizes, and the light-theme canvas focus ring. The mockup Settings page hangs headless screenshots, so only one mockup Settings shot exists; that comparison used `reference/mockup-v6/src.html`.
