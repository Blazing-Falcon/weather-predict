# Step 9: UI Polish

Date: 2026-11-20. Input: `docs/ui-audit.md` (26 findings). juan picked A01 to A12. All twelve are fixed and committed; A13 to A26 are open.

## Fixes

| ID | Fix | Commit |
|---|---|---|
| A01 | The 60 s poll applies every new `/api/meta`, so the LIVE pill and freshness ages move when ingestion stops. | `0202bc0` |
| A02 | Drawer details are dropped when the archive moves; a failed fetch is not cached and is retried. | `0202bc0` |
| A03 | Legend, drawer and tooltip print the window a probability covers ("02:00–04:00 WIB") instead of "next 2 h". | `c552734` |
| A04 | The API process had cached the January sample config since 02:53 UTC. Restarted it (juan's OK, PID by port 8000); History now shows 2016-01-01 to 2023-12-31. No code change. | restart |
| A05 | Failures are listed by label in one banner with Retry, which clears on success. Views show "unavailable" instead of 0 or empty axes. The pill says OFFLINE. Play waits for a timeline. | `0202bc0` |
| A06 | Light `--muted` `#665c54` (5.74:1 on panel), light Waspada `#8f5902` (5.14:1), dark muted text on `--panel2` uses `--fg` (8.45:1), watch star `--muted`, yellow focus ring on the map canvas. | `1e3253f` |
| A07 | BMKG codes show the Indonesian name from the BMKG payload (Hujan Ringan, Udara Kabur, ...), with the English name and code on hover. | `c552734` |
| A08 | At 560 px and below, alert rows are two- or three-line cards with all lead probabilities, cloud top, BMKG and the map button. Sorting by level, name, lead or cloud top (headers with `aria-sort`, a select on mobile). | `78f59cd` |
| A09 | Overview, Model and History charts draw at their rendered width with fewer ticks; chart text is at least 11 px at 390 px (was 2.7 px). | `78f59cd` |
| A10 | Map search for any kab/kota. User opens move focus to the drawer heading; Close and Escape return focus. | `a449b93` |
| A11 | The tab strip fades at the right edge while more tabs exist. | `1e3253f` |
| A12 | Touch and narrow screens: 44 × 44 px hit areas for stars, links, buttons, Play, tabs and sliders; desktop star and link reach 24 × 24 px. | `1e3253f` |

## Verification

- `uv run python experiments/06_web_check.py` on the final code: 0 console errors and 0 failed requests on all 14 rows (7 views × 2 widths).
- Screenshots of every view at 1440 px and 390 px in dark and light after each fix and at the end (`data/tmp/ui-fix/`, not in git). Codex's Playwright checks for each fix: error and Retry states, sort order, chart text sizes, target sizes, contrast ratios, and the search-and-focus flow at both widths.
- Each Codex diff was reviewed. Two needed a second pass: u01 keyed failures by URL (a moved timeline left labels stuck), and u04 squeezed alert details into 10 px Arial.

## Open Points

- Planned (juan, not built): configurable replay window up to 1 month. Needs the 7-day API span cap and the 7-day frame and cloud tile retention raised, the 1 to 17 Nov 2026 rain and cloud gap filled, and about 30 days of hindcast built.
- Planned (juan, not built): end the slider at the latest issue and drop the 3 h forward part, which only showed the same issue at leads 1 to 3. A "next 1 h | 2 h | 3 h" toggle on the forecast layer replaces it and also works on past frames. This differs from the guide's "3 h forward" slider, by juan's choice.
- Planned (juan, not built): the 130-row IMERG backfill table on Pipeline closes by default behind a summary line ("130 months, 2016-01 to 2026-10, all complete"), like "Storage by dataset".
- Planned (juan, not built): on the BMKG layer, the map tooltip shows the BMKG forecast name ("Hujan Ringan · Light Rain"), its 3 h step and its run time, not only Rain or No rain. The map frames need the BMKG weather code and run for this.
- Planned (juan, not built): the History "Yearly rain" chart tags El Niño and La Niña years from NOAA's Relative Oceanic Niño Index (RONI), the official ENSO index since February 2026. A year is tagged when a 5-season episode at ±0.5 °C covers at least 3 of the June to November seasons. With the current values: La Niña 2016, 2017, 2020 to 2022, 2024 and 2025; El Niño 2023 and 2026 (provisional).
- A13 to A26 (raster failure note, legends, clipped credits on the 1440 px left card, drawer chart keys, time zones on day changes, Pipeline mobile order, slider `aria-valuetext`, first paint, Model KPI labels and the low items).
- `config.settings()` is cached for the life of the API process, so any config change needs an API restart.
- The drawer heading uses the browser's default focus outline, not the yellow ring.
- Not checked: real touch devices and screen readers.
