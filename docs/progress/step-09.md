# Step 9: Web App

Date: 2026-11-20. Status: built, **awaiting juan's sign-off**.

## What Was Built

`web/` (served by the API at `/`): mockup v6 split into `index.html`, `app.js`, `style.css`, with every mock data source replaced by the API. Gruvbox dark by default with the light variant, MapLibre GL JS 4.7.1 (served from `web/` since the render fixes, before that from cdnjs) with no basemap tiles, the time slider with play, zoom presets, status pill and area drawer, as in the mockup. Credits: BMKG, NASA GES DISC, NOAA, aviationweather.gov, geoBoundaries.

## Done When

`uv run python experiments/06_web_check.py` (headless Chromium):

```
| Page | Width | Console errors | Failed requests |
|---|---:|---:|---:|
| map | 1440 | 0 | 0 |
| map | 390 | 0 | 0 |
| overview | 1440 | 0 | 0 |
| overview | 390 | 0 | 0 |
| alerts | 1440 | 0 | 0 |
| alerts | 390 | 0 | 0 |
| model | 1440 | 0 | 0 |
| model | 390 | 0 | 0 |
| history | 1440 | 0 | 0 |
| history | 390 | 0 | 0 |
| pipeline | 1440 | 0 | 0 |
| pipeline | 390 | 0 | 0 |
| settings | 1440 | 0 | 0 |
| settings | 390 | 0 | 0 |
```

Screenshots were compared by eye against the mockup at both widths. One fix came out of that review: the map ringed all 106 listed airports, so it now rings only the 27 that sent a METAR in the last 24 h, as in the mockup.

## Known Gaps Against the Mockup

- With one sample month and a few hours of live data, the drawer's 48-hour chart, the History heatmaps and the live Model charts (CSI by hour and island) are mostly empty and say so. They fill as the archive grows.
- The BMKG layer shows archived runs only; before the archiver's first run there is nothing to show.
- Inside the cloud box the neighbouring countries are not drawn. The mockup behaves the same way, so this is not a regression.
