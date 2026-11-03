"use strict";

// "How it works" view. app.js calls renderExplain() when #explain opens. Everything stays inside
// this closure because classic scripts share one global scope with app.js, whose helpers (read,
// meta, areas, malangId, P, legend, rainClass, fmtTime, clockWib) are used only at call time.
window.renderExplain = (() => {
  const MIN = 60 * 1000;
  const HALF = 30 * MIN;
  const LAG = 360 * MIN;
  const state = { area: null, lane: "both", stage: "bronze", split: "test", lead: "1" };
  const tiles = new Map();
  let setup;
  let data;

  const esc = value => String(value ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const num = (value, digits = 0) => value == null ? "—"
    : Number(value).toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const iso = ms => new Date(ms).toISOString().replace(".000Z", "Z");
  const stemTime = stem => Date.UTC(+stem.slice(0, 4), +stem.slice(4, 6) - 1, +stem.slice(6, 8), +stem.slice(9, 11), +stem.slice(11, 13));
  const stemOf = ms => iso(ms).replace(/[-:]/g, "").slice(0, 13) + "Z";
  const wib = value => fmtTime(value);
  const areaName = id => {
    const area = areaById.get(id);
    return area ? area.kemendagri_name ?? area.geo_name : `Area ${id}`;
  };
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const el = id => document.getElementById(id);
  // Near-black or near-white text, whichever reads better on a fill colour.
  const ink = hex => {
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.18 ? "#1d2021" : "#fbf1c7";
  };

  // ---------- reference text ----------
  const NOTES = {
    area_id: "Area key, 0 to 518", geo_name: "Name in geoBoundaries", kemendagri_code: "Government code",
    kemendagri_name: "Government name", prov_code: "Province code", prov_name: "Province", island: "Island group",
    tz: "Time zone", is_admin: "False for the 5 lake, reservoir and forest polygons", centroid_lon: "Centre, longitude",
    centroid_lat: "Centre, latitude", area_km2: "From the full-resolution polygon", bmkg_adm4: "Village code used for BMKG",
    bmkg_lon: "BMKG point, longitude", bmkg_lat: "BMKG point, latitude",
    cell: "Latitude index × 480 + longitude index", weight: "Share of the cell inside the polygon",
    neighbour_id: "The other area", kind: "border, or nearest for areas with no land border", dist_km: "Distance between centres",
    slot: "Start of the half-hour, UTC", rain_mean_x10: "Weighted mean rate × 10 (0.1 mm/h)", rain_max_x10: "Wettest cell × 10",
    wet_pct: "Weighted share of cells at 0.1 mm/h or more", product_version: "IMERG version",
    source: "1 = MERGIR, 2 = Himawari-9", tb_min: "Coldest cell, K", tb_mean_x10: "Weighted mean × 10 (0.1 K)",
    cold_pct: "Share colder than 233 K", adv1_pct: "Share under cold cloud moved 1 h ahead", adv2_pct: "Moved 2 h ahead",
    adv3_pct: "Moved 3 h ahead", flow_kmh: "Mean speed of the cold cloud. Empty when there is no cold cloud",
    analysis_time: "BMKG run time", first_seen: "First sweep that saw this run", adm4: "Village code", valid_time: "Step time",
    lead_h: "Forecast window, hours", weather: "BMKG weather code", tp_x10: "Rain amount × 10, assumed mm per step",
    tcc_pct: "Cloud cover, %", t: "Issue slot. The windows start here", hu_pct: "Humidity, %", ws_x10: "Wind × 10, km/h",
    wd_deg: "Wind direction, degrees", station_id: "Station or airport code", obs_time: "Report hour, UTC", lat: "Latitude", lon: "Longitude",
    wx: "Weather groups in the report", is_rain: "Rain in the present weather", precip_mm: "Rain amount, mostly empty",
    issued_at: "Clock time the row was written", prob_pct: "Chance of rain, %", model_version: "Model that made it",
    cloud_slot: "Newest cloud slot used", imerg_slot: "Newest IMERG slot used", verified_at: "When IMERG covered the window",
    observed: "The window had rain", clim_pct: "Climatology chance, %", persist_pct: "Persistence: 100 if the 6 h old slot had rain",
    motion_pct: "Motion only: 100 if moved cold cloud covers enough", bmkg_rain: "BMKG forecast rain. Empty without a run",
    bmkg_analysis_time: "BMKG run used", day: "UTC day", scope: "national, island or province", scope_id: "Which one",
    n: "Number of windows (scores) or slots (climatology)", brier: "Brier score", bss: "Brier skill score", csi: "Critical success index",
    pod: "Probability of detection", far: "False alarm ratio", base_rate: "Share of windows with rain",
    layer: "Map layer", value: "Value for that layer (% or 0.1 mm/h)", month: "Local month", hour_local: "Local hour",
    p1: "Windows of 1 h with rain, %", p2: "Windows of 2 h with rain, %", p3: "Windows of 3 h with rain, %",
  };
  // Columns whose meaning differs by table.
  const TABLE_NOTES = {
    silver_bmkg: { lead_h: "Hours after the run", t: "Air temperature, °C" },
    silver_gauge: { source: "ghcnh or metar" },
    gold_scores: { source: "model, clim, persist, motion or bmkg", n: "Windows scored" },
    gold_climatology: { n: "Half-hour slots counted", rain_mean_x10: "Mean rate × 10 (0.1 mm/h)", wet_pct: "Mean wet share, %" },
  };
  const FEATURES = {
    cold_pct: ["Cloud now", "Share colder than 233 K, %"], tb_min: ["Cloud now", "Coldest cell, K"],
    tb_mean: ["Cloud now", "Mean brightness temperature, K"], cold_trend_1h: ["Cloud now", "Change of cold share in 1 h"],
    nb_cold_pct: ["Cloud now", "Neighbours' cold share, %"], adv1_pct: ["Cloud motion", "Moved cold cloud, +1 h, %"],
    adv2_pct: ["Cloud motion", "Moved cold cloud, +2 h, %"], adv3_pct: ["Cloud motion", "Moved cold cloud, +3 h, %"],
    adv_max_pct: ["Cloud motion", "Largest of the three, %"], flow_kmh: ["Cloud motion", "Cold cloud speed, km/h"],
    rain_lag: ["Recent rain", "Mean rate 6 h ago, mm/h"], wet_lag: ["Recent rain", "Wet share 6 h ago, %"],
    rainmax_lag: ["Recent rain", "Wettest cell 6 h ago, mm/h"], rain_sum_3h: ["Recent rain", "Total in 3 h up to then, mm"],
    rain_sum_6h: ["Recent rain", "Total in 6 h, mm"], rain_sum_24h: ["Recent rain", "Total in 24 h, mm"],
    nb_rain_lag: ["Recent rain", "Neighbours' mean rate, mm/h"], hour_sin: ["Time", "Local hour, sine"],
    hour_cos: ["Time", "Local hour, cosine"], doy_sin: ["Time", "Day of year, sine"], doy_cos: ["Time", "Day of year, cosine"],
    area_km2: ["Place", "Area, km²"], island: ["Place", "Island group"], clim_wet_pct: ["Climatology", "Usual wet share, %"],
    clim_p1: ["Climatology", "Usual chance, 1 h window"], clim_p2: ["Climatology", "Usual chance, 2 h window"],
    clim_p3: ["Climatology", "Usual chance, 3 h window"],
  };
  const SOURCES = { model: "Our model", clim: "Climatology", persist: "Persistence", motion: "Motion only", bmkg: "BMKG" };

  // Pipeline stages: what each does in the batch layer and in the speed layer.
  const STAGES = [
    { id: "sources", name: "Sources", sub: "NASA, NOAA, BMKG", lanes: ["batch", "speed"],
      what: "The system reads public services: NASA OPeNDAP for IMERG and MERGIR crops, the NOAA Himawari-9 bucket on AWS, the BMKG forecast API, and gauge files.",
      batch: "Backfill: every half-hour from 2016-01 to 2026-10, 189,936 IMERG slots and 189,936 MERGIR frames.",
      speed: "Live: IMERG checked every 10 min, Himawari as each scan lands, BMKG every 15 min, METAR every hour.",
      io: ["Requests to public services", "Raw files and JSON"] },
    { id: "bronze", name: "Bronze", sub: "raw files", lanes: ["batch", "speed"],
      what: "Raw files are kept as they came, for a short time. Backfill crops are deleted as soon as their month is in silver.",
      batch: "Backfill crops, deleted month by month (88.5 GB downloaded in total).",
      speed: "Live crops and scans, kept for hours (Himawari) to 12 months (BMKG, which cannot be fetched again).",
      io: ["Raw files", "Files under bronze/"] },
    { id: "registry", name: "Area registry", sub: "ref tables", lanes: ["batch", "speed"],
      what: "Built once: the 519 areas, the cell weights on both grids, the neighbours and one BMKG point per area.",
      batch: "Used by every backfill month.", speed: "Used by every live tick.",
      io: ["geoBoundaries, wilayah.id, BMKG", "ref/*.parquet"] },
    { id: "silver", name: "Silver", sub: "per-area tables", lanes: ["batch", "speed"],
      what: "Each grid becomes one row per area and half-hour, with weighted means and shares. BMKG steps and gauge reports become rows too.",
      batch: "130 monthly partitions per table, written by the backfills.",
      speed: "The live ticks merge new slots into the current month.",
      io: ["Bronze files and the area registry", "silver/*/year=YYYY/month=MM/"] },
    { id: "features", name: "Features", sub: "Spark", lanes: ["batch", "speed"],
      what: "Spark builds one row per area and issue time with 27 numbers, using only data known at that time, plus the targets for training.",
      batch: "22,143,120 rows in 539 s for training. The table is deleted after training.",
      speed: "514 rows at each issue, held in memory only.",
      io: ["Silver tables, neighbours, climatology", "Feature rows"] },
    { id: "model", name: "Model", sub: "LightGBM × 3", lanes: ["batch", "speed"],
      what: "One LightGBM model per lead. Training fits them on 2016 to 2023. Live scoring runs them on each new issue.",
      batch: "Training: 1,910 s, peak memory 4.6 GB, one model card per version.",
      speed: "Scoring: 1,542 probabilities per issue.",
      io: ["Feature rows", "Probabilities, and models/<version>/"] },
    { id: "gold", name: "Gold", sub: "forecasts, scores", lanes: ["batch", "speed"],
      what: "Forecasts, their later verification, daily scores, map frames, the climatology and the map images.",
      batch: "Climatology from 2016 to 2023. Hindcasts for past issue times.",
      speed: "New forecasts each issue, verification when IMERG covers a window, frames about every 2 min.",
      io: ["Probabilities, silver rain", "gold/*"] },
    { id: "api", name: "API", sub: "FastAPI + DuckDB", lanes: ["speed"],
      what: "FastAPI answers the web app. DuckDB reads the gold and silver Parquet files in place, so no database server is needed.",
      batch: "Not part of the batch layer.", speed: "On each request. Finished slots are cached for a day, live answers for 60 s.",
      io: ["Gold and silver Parquet", "JSON, PNG and WebP images, GeoJSON"] },
    { id: "web", name: "Web app", sub: "MapLibre", lanes: ["speed"],
      what: "The browser draws the kab/kota polygons and colours them from one array of 519 values per half-hour.",
      batch: "Not part of the batch layer.", speed: "Checks for new data every 60 s.",
      io: ["JSON from the API", "The map and the pages"] },
  ];

  // ---------- small renderers ----------
  function rowsTable(key, extra = "") {
    const stage = typeof key === "string" ? data.samples.stages[key] : key;
    const notes = { ...NOTES, ...TABLE_NOTES[typeof key === "string" ? key : key.key] };
    const schema = new Map(stage.schema);
    const type = text => text.replace("timestamp[us, tz=UTC]", "timestamp UTC").replace(/dictionary<values=(\w+).*>/, "dictionary<$1>");
    const sample = stage.sample[0] ?? {};
    return `<div class="scroll-x"><table class="x-table x-shape"><thead><tr><th>Column</th><th>Type</th><th>Sample</th><th>Meaning</th></tr></thead><tbody>${
      [...schema].map(([name, kind]) => `<tr><td><code>${esc(name)}</code></td><td class="mono">${esc(type(kind))}</td><td class="mono">${esc(sample[name] ?? "null")}</td><td>${esc(notes[name] ?? "")}</td></tr>`).join("")
    }</tbody></table></div><p class="note">${esc(stage.path)} · ${num(stage.rows)} rows in ${num(stage.files)} file${stage.files === 1 ? "" : "s"} · ${size(stage.bytes)} · ${esc(stage.compression)}${extra}</p>`;
  }

  function shape(id) {
    const s = data.samples.stages;
    const at = `<p class="note">Snapshot: ${esc(data.samples.area_name)}, issue ${esc(wib(data.samples.issue_t))}.</p>`;
    if (id === "sources") {
      return `<ul class="x-list">
        <li>IMERG: one <code>.nc4</code> crop per half-hour from NASA OPeNDAP (NetCDF4/HDF5), 55 to 90 KB.</li>
        <li>MERGIR: one <code>.nc4</code> crop per hour, two frames, about 0.83 to 0.94 MB.</li>
        <li>Himawari-9: three <code>.DAT.bz2</code> segments per scan in the satellite's own binary format, about 9 MB.</li>
        <li>BMKG: JSON per village, ${num(s.bronze_bmkg.steps)} 3 h steps per answer in this sample.</li>
        <li>GHCNh: pipe-separated text. METAR: JSON with the raw report text.</li></ul>`;
    }
    if (id === "bronze") {
      const imerg = s.bronze_imerg;
      return `${at}<h4>IMERG crop</h4><p class="mono x-file">${esc(imerg.file)}</p>
        <p>${esc(imerg.variable)}. ${num(imerg.bytes)} bytes. The ${imerg.cells.length} cells of this area hold ${imerg.cells.map(c => num(c[2], 1)).join(", ")} mm/h.</p>
        <h4>Himawari-9 segments</h4><ul class="x-list mono">${s.bronze_himawari.files.map(([name, bytes]) => `<li>${esc(name)} · ${num(bytes)} bytes</li>`).join("")}</ul>
        <h4>BMKG answer, one step</h4><p class="note">${esc(s.bronze_bmkg.file)}</p>
        <pre class="x-json">${esc(JSON.stringify({ ...s.bronze_bmkg.record, body: { data: [{ cuaca: [[s.bronze_bmkg.step, "…"]] }] } }, null, 1))}</pre>
        <h4>MERGIR crop</h4><p><code>Tb</code> int16 in K, <code>[time 2][lat 495][lon 1319]</code>, fill -9999. Deleted after each month, so no sample is on disk.</p>`;
    }
    if (id === "registry") {
      return `${at}<h4>ref/dim_area</h4>${rowsTable("ref_dim_area")}<h4>ref/imerg_membership</h4>${rowsTable("ref_imerg_membership")}<h4>ref/neighbours</h4>${rowsTable("ref_neighbours")}`;
    }
    if (id === "silver") {
      return `${at}<h4>silver/rain_kabkota</h4>${rowsTable("silver_rain")}<h4>silver/cloud_kabkota</h4>${rowsTable("silver_cloud")}
        <h4>silver/bmkg_forecast</h4>${rowsTable("silver_bmkg")}<h4>silver/gauge_obs</h4>${rowsTable("silver_gauge", ". The newest rain report on file, from any station")}`;
    }
    if (id === "features") return `${at}${featureTable(data.samples.area_id)}`;
    if (id === "model") {
      const card = data.model?.card;
      if (!card) return `<p class="note">Model results are unavailable.</p>`;
      const p = card.parameters;
      return `<dl class="kv x-kv"><dt>Version</dt><dd>${esc(card.version)}</dd><dt>Trained</dt><dd>${esc(wib(card.created_at))}</dd>
        <dt>Trees per lead</dt><dd>${Object.values(card.leads).map(l => num(l.best_iteration)).join(", ")}</dd><dt>Leaves, learning rate</dt><dd>${p.num_leaves}, ${p.learning_rate}</dd>
        <dt>Inputs</dt><dd>${card.features.length}</dd><dt>IMERG lag</dt><dd>${card.imerg_lag_minutes} min</dd><dt>Rain threshold</dt><dd>${card.threshold_mm_h} mm/h</dd></dl>
        <p class="note">Files: <code>models/${esc(card.version)}/model_lead1.txt</code> to <code>model_lead3.txt</code> and <code>card.json</code>. Live: <code>/api/model</code>.</p>`;
    }
    if (id === "gold") {
      return `${at}<h4>gold/predictions</h4>${rowsTable({ ...s.gold_predictions, key: "gold_predictions", sample: s.gold_predictions.sample.slice(0, 1) }, `. This issue: ${s.gold_predictions.sample.map(r => r.prob_pct + "%").join(", ")} for +1, +2, +3 h`)}
        <h4>gold/verification</h4>${rowsTable("gold_verification")}<h4>gold/scores_daily</h4>${rowsTable("gold_scores")}
        <h4>gold/map_frames</h4>${rowsTable("gold_frames", `. This slot: ${s.gold_frames.sample.map(r => `${r.layer} ${r.value}`).join(", ")}`)}
        <h4>gold/climatology</h4>${rowsTable("gold_climatology")}`;
    }
    if (id === "api") {
      const detail = data.detail;
      if (!detail) return `<p class="note">The area answer is unavailable.</p>`;
      const tail = list => list.slice(-1);
      const sample = { area_id: detail.area_id, area: { kemendagri_name: detail.area.kemendagri_name, tz: detail.area.tz, "…": "…" },
        observed: tail(detail.observed), predictions: detail.predictions.slice(-3), bmkg: tail(detail.bmkg), cloud: tail(detail.cloud),
        scores: { scope: detail.scores.scope, rows: ["…"] }, climatology: ["…", tail(detail.climatology)[0]] };
      return `<p class="note">Live answer of <code>/api/areas/${detail.area_id}</code>, shortened to the newest row of each list:</p><pre class="x-json">${esc(JSON.stringify(sample, null, 1))}</pre>`;
    }
    return `<p>The app asks <code>/api/frames?layer=prob3</code> for one array of 519 values per half-hour and colours each polygon by its class:</p>${swatches()}`;
  }

  function swatches(active) {
    const [title, labels] = legend.fc;
    return `<div class="x-swatches" role="list" aria-label="${esc(title)}">${labels.map((label, index) =>
      `<span role="listitem" class="x-sw${index === active ? " on" : ""}"><i style="background:${P().fc[index]}"></i>${esc(label)}</span>`).join("")}</div>`;
  }

  function featureTable(id) {
    const f = data.samples.stages.features;
    const row = f.rows[id];
    if (!row) return `<p class="note">No feature row for this area in the snapshot.</p>`;
    const value = (name, v) => v == null ? "empty" : name === "island" ? v : num(v, Number.isInteger(v) ? 0 : 3);
    return `<div class="scroll-x"><table class="x-table x-shape"><thead><tr><th>Group</th><th>Feature</th><th class="num">Value</th><th>Meaning</th></tr></thead><tbody>${
      f.columns.map((name, i) => `<tr><td>${FEATURES[name][0]}</td><td><code>${name}</code></td><td class="num">${esc(value(name, row[i]))}</td><td>${FEATURES[name][1]}</td></tr>`).join("")
    }</tbody></table></div><p class="note">The models read 25 of these: <code>clim_p1</code> to <code>clim_p3</code> become one <code>clim_p</code> for each lead. With these inputs the models gave ${f.probabilities[id].join("%, ")}% for +1, +2, +3 h. Check: the live models on all ${num(f.check.recomputed)} snapshot rows give the stored value ${f.check.differ_from_stored === 0 ? "every time" : `except ${f.check.differ_from_stored} times`}.</p>`;
  }

  // ---------- live numbers in the text ----------
  function fillLive() {
    const pipe = data.pipeline;
    const set = (key, text) => document.querySelectorAll(`#view-explain [data-live="${key}"]`).forEach(node => { node.textContent = text; });
    const minutesText = value => value == null ? "not measured" : `${num(value, 1)} min`;
    set("lat-imerg", minutesText(pipe?.latency_minutes?.imerg?.median_24h));
    set("lat-himawari", minutesText(pipe?.latency_minutes?.himawari?.median_24h));
    set("delay", minutesText(pipe?.health?.score?.delay_minutes));
    const billions = value => value == null ? "—" : `${num(value / 1e9, 1)} billion`;
    set("vol-imerg", billions(pipe?.processed_vs_stored?.imerg?.values_processed));
    set("vol-mergir", billions(pipe?.processed_vs_stored?.mergir?.values_processed));
    set("vol-silver", pipe ? size(pipe.processed_vs_stored.silver_bytes) : "—");
    set("budget", pipe ? `${pipe.storage_budget_gb} GB` : "—");
    const test = data.model?.card?.leads;
    set("bss-headline", test ? Object.values(test).map(l => `${Math.round(l.metrics.test.model.bss * 100)}%`).join(", ").replace(/, (?=[^,]*$)/, " and ") : "—");
    if (pipe) {
      el("xRetention").innerHTML = `<caption>Storage now and retention window per dataset (live)</caption><thead><tr><th>Dataset</th><th class="num">Size</th><th>Kept for</th></tr></thead><tbody>${
        Object.entries(pipe.storage).map(([name, item]) => `<tr><td><code>${esc(name)}</code></td><td class="num">${size(item.bytes)}</td><td>${esc(item.cap?.keep ?? (item.cap?.keep_versions ? `last ${item.cap.keep_versions} versions` : "no window, counted in the disk check"))}</td></tr>`).join("")
      }</tbody>`;
    }
  }

  // ---------- glossary tooltips ----------
  function setupTerms(root) {
    const tip = el("xTip");
    let pinned = null;
    const show = button => {
      const entry = el("g-" + button.dataset.term);
      if (!entry) return;
      tip.innerHTML = `<b>${esc(entry.textContent)}</b> ${esc(entry.nextElementSibling.dataset.short)}`;
      tip.hidden = false;
      button.setAttribute("aria-describedby", "xTip");
      const box = button.getBoundingClientRect();
      const width = Math.min(300, innerWidth - 32);
      tip.style.width = width + "px";
      tip.style.left = Math.max(16, Math.min(innerWidth - width - 16, box.left)) + "px";
      const below = box.bottom + 8;
      tip.style.top = (below + tip.offsetHeight > innerHeight ? box.top - tip.offsetHeight - 8 : below) + "px";
    };
    const hide = () => {
      tip.hidden = true;
      root.querySelectorAll("[aria-describedby=xTip]").forEach(node => node.removeAttribute("aria-describedby"));
      pinned?.setAttribute("aria-expanded", "false");
      pinned = null;
    };
    root.querySelectorAll(".term").forEach(button => button.setAttribute("aria-expanded", "false"));
    root.addEventListener("pointerover", event => {
      const button = event.target.closest(".term");
      if (button && !pinned && event.pointerType === "mouse") show(button);
    });
    root.addEventListener("pointerout", event => {
      if (event.target.closest(".term") && !pinned && event.pointerType === "mouse") hide();
    });
    root.addEventListener("focusin", event => {
      const button = event.target.closest(".term");
      if (button && !pinned) show(button);
    });
    root.addEventListener("focusout", event => {
      if (event.target.closest(".term") && !pinned) hide();
    });
    root.addEventListener("click", event => {
      const button = event.target.closest(".term");
      if (!button) {
        if (pinned) hide();
        return;
      }
      const again = pinned === button;
      hide();
      if (again) return;
      pinned = button;
      button.setAttribute("aria-expanded", "true");
      show(button);
    });
    root.addEventListener("keydown", event => {
      if (event.key === "Escape" && !tip.hidden) hide();
    });
    addEventListener("scroll", () => {
      if (pinned) show(pinned);
      else if (!tip.hidden) hide();
    }, { passive: true });
  }

  // ---------- area pickers ----------
  function setupPickers(root) {
    const admin = areas.filter(area => area.is_admin);
    const provinces = [...new Set(admin.map(area => area.prov_name))].sort();
    const options = provinces.map(prov => `<optgroup label="${esc(prov)}">${admin.filter(a => a.prov_name === prov)
      .sort((a, b) => areaName(a.area_id).localeCompare(areaName(b.area_id)))
      .map(a => `<option value="${a.area_id}">${esc(areaName(a.area_id))}</option>`).join("")}</optgroup>`).join("");
    root.querySelectorAll(".x-area").forEach(select => {
      select.innerHTML = options;
      select.value = state.area;
      select.addEventListener("change", () => chooseArea(Number(select.value)));
    });
  }

  async function chooseArea(id) {
    state.area = id;
    document.querySelectorAll("#view-explain .x-area").forEach(select => { select.value = id; });
    data.detail = null;
    el("xFollow").innerHTML = `<li class="note">Loading ${esc(areaName(id))}…</li>`;
    data.detail = await read(`/api/areas/${id}`, "Area details");
    if (state.area !== id) return;
    drawGrid();
    renderFollow();
    setupLoop();
    if (state.stage === "api") renderStage();
  }

  // ---------- grid cells (section 4) ----------
  async function tilePixels(stem) {
    if (!tiles.has(stem)) {
      tiles.set(stem, fetch(`/tiles/rain/${stem}.png`).then(response => {
        if (!response.ok) throw new Error(stem);
        return response.blob();
      }).then(blob => createImageBitmap(blob, { colorSpaceConversion: "none", premultiplyAlpha: "none" })).then(image => {
        const canvas = document.createElement("canvas");
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.drawImage(image, 0, 0);
        const rgba = context.getImageData(0, 0, image.width, image.height).data;
        const values = new Uint8Array(image.width * image.height);
        for (let i = 0; i < values.length; i++) values[i] = rgba[i * 4];
        return values;
      }).catch(() => {
        tiles.delete(stem);
        return null;
      }));
    }
    return tiles.get(stem);
  }

  function cellsOf(id) {
    const m = data.membership;
    const pairs = m.areas[id] ?? [];
    const cells = [];
    for (let i = 0; i < pairs.length; i += 2) {
      const cell = pairs[i];
      cells.push({ cell, weight: pairs[i + 1], lat: Math.floor(cell / m.width), lon: cell % m.width });
    }
    return cells;
  }

  // The rain image stores row 0 at the north edge; cell indices count rows from the south.
  const rainAt = (values, lat, lon) => values[(data.membership.height - 1 - lat) * data.membership.width + lon] / 10;

  function gridSlots() {
    return [...meta.tile_times.rain].sort();
  }

  async function drawGrid() {
    const id = state.area;
    const slots = gridSlots();
    const slider = el("xGridSlot");
    const stem = slots[Number(slider.value)];
    const m = data.membership;
    const cells = cellsOf(id);
    const canvas = el("xGridCanvas");
    if (!cells.length || !stem) {
      el("xGridResult").textContent = !stem ? "No rain image is archived." : "This area has no cells.";
      return;
    }
    el("xGridSlotText").textContent = `${wib(stemTime(stem))} (slot start)`;
    const values = await tilePixels(stem);
    if (state.area !== id || slots[Number(slider.value)] !== stem) return;
    const lats = cells.map(c => c.lat);
    const lons = cells.map(c => c.lon);
    const box = { south: Math.min(...lats) - 1, north: Math.max(...lats) + 1, west: Math.min(...lons) - 1, east: Math.max(...lons) + 1 };
    const cols = box.east - box.west + 1;
    const rows = box.north - box.south + 1;
    const width = Math.min(el("xGrid").clientWidth, 720);
    const px = Math.max(4, Math.min(72, Math.floor(width / cols), Math.floor(480 / rows)));
    const ratio = devicePixelRatio || 1;
    canvas.width = cols * px * ratio;
    canvas.height = rows * px * ratio;
    canvas.style.width = cols * px + "px";
    canvas.style.height = rows * px + "px";
    const g = canvas.getContext("2d");
    g.setTransform(ratio, 0, 0, ratio, 0, 0);
    g.fillStyle = css("--panel2");
    g.fillRect(0, 0, cols * px, rows * px);
    const member = new Map(cells.map(c => [c.cell, c]));
    const colors = P().ob;
    for (let lat = box.south; lat <= box.north; lat++) {
      for (let lon = box.west; lon <= box.east; lon++) {
        const x = (lon - box.west) * px;
        const y = (box.north - lat) * px;
        const rain = values ? rainAt(values, lat, lon) : null;
        g.globalAlpha = member.has(lat * m.width + lon) ? 1 : 0.35;
        g.fillStyle = rain == null ? css("--panel2") : colors[rainClass(rain)];
        g.fillRect(x + 1, y + 1, px - 2, px - 2);
      }
    }
    g.globalAlpha = 1;
    // Polygon outline from the display copy of the boundaries.
    const feature = data.geo.features.find(f => f.properties.i === id);
    const lon0 = m.lon0 + (box.west - 0.5) * m.step;
    const lat1 = m.lat0 + (box.north + 0.5) * m.step;
    if (feature) {
      g.strokeStyle = css("--fg0");
      g.lineWidth = 2;
      g.beginPath();
      const polygons = feature.geometry.type === "Polygon" ? [feature.geometry.coordinates] : feature.geometry.coordinates;
      for (const polygon of polygons) {
        for (const ring of polygon) {
          ring.forEach(([lon, lat], index) => {
            const x = (lon - lon0) / m.step * px;
            const y = (lat1 - lat) / m.step * px;
            if (index) g.lineTo(x, y);
            else g.moveTo(x, y);
          });
          g.closePath();
        }
      }
      g.stroke();
    }
    if (px >= 44) {
      g.font = `600 ${Math.min(13, px / 4)}px ${css("--f-mono")}`;
      g.textAlign = "center";
      for (const c of cells) {
        const x = (c.lon - box.west) * px + px / 2;
        const y = (box.north - c.lat) * px + px / 2;
        const rain = values ? rainAt(values, c.lat, c.lon) : null;
        const fill = rain == null ? css("--panel2") : colors[rainClass(rain)];
        g.fillStyle = ink(fill.startsWith("#") ? fill : "#3c3836");
        g.strokeStyle = fill;
        g.lineWidth = 4;
        g.lineJoin = "round";
        for (const [text, dy] of [[`w ${c.weight.toFixed(2)}`, -2], [rain == null ? "" : rain.toFixed(1), Math.min(14, px / 3.5)]]) {
          g.strokeText(text, x, y + dy);
          g.fillText(text, x, y + dy);
        }
      }
    }
    canvas.setAttribute("aria-label", `${areaName(id)}: ${cells.length} IMERG cells. Details in the table below.`);
    data.gridBox = { box, px, member, values };
    gridResult(id, stem, cells, values);
    el("xGridKey").innerHTML = `<span class="note">Rain, mm/h:</span>${legend.ob[1].map((label, index) => `<span class="x-sw"><i style="background:${colors[index]}"></i>${esc(label.replace(/ \(.*\)/, ""))}</span>`).join("")}<span class="note">Faded cells are outside the area. The outline is the area.</span>`;
  }

  function gridResult(id, stem, cells, values) {
    const slotIso = iso(stemTime(stem));
    const table = el("xGridTable");
    const m = data.membership;
    table.innerHTML = `<thead><tr><th class="num">Cell</th><th class="num">Lon</th><th class="num">Lat</th><th class="num">Weight</th><th class="num">Rain mm/h</th></tr></thead><tbody>${
      cells.map(c => `<tr><td class="num">${c.cell}</td><td class="num">${(m.lon0 + c.lon * m.step).toFixed(2)}</td><td class="num">${(m.lat0 + c.lat * m.step).toFixed(2)}</td><td class="num">${c.weight.toFixed(3)}</td><td class="num">${values ? rainAt(values, c.lat, c.lon).toFixed(1) : "—"}</td></tr>`).join("")
    }</tbody>`;
    if (!values) {
      el("xGridResult").textContent = "The rain image for this half-hour is unavailable.";
      return;
    }
    const total = cells.reduce((sum, c) => sum + c.weight, 0);
    const mean = cells.reduce((sum, c) => sum + c.weight * rainAt(values, c.lat, c.lon), 0) / total;
    const stored = data.detail?.area_id === id ? data.detail.observed.find(o => Date.parse(o.slot) === Date.parse(slotIso)) : null;
    el("xGridResult").innerHTML = `${cells.length} cells, weights add up to ${total.toFixed(3)}. Computed here: Σ(weight × rain) / Σ weight = <b>${mean.toFixed(2)} mm/h</b>. `
      + (stored ? `Stored in silver: <b>rain_mean_x10 = ${stored.rain_mean_x10}</b> (${(stored.rain_mean_x10 / 10).toFixed(1)} mm/h).`
        : "The API returns only the last 48 h of stored rows, so the stored value for this half-hour is not shown.");
  }

  function setupGrid() {
    const slider = el("xGridSlot");
    const slots = gridSlots();
    slider.max = Math.max(0, slots.length - 1);
    const used = meta.latest_issue_slot ? stemOf(Date.parse(meta.latest_issue_slot) - LAG) : null;
    slider.value = Math.max(0, used && slots.includes(used) ? slots.indexOf(used) : slots.length - 1);
    slider.addEventListener("input", drawGrid);
    const hover = el("xGridHover");
    const canvas = el("xGridCanvas");
    const point = event => {
      const info = data.gridBox;
      if (!info) return;
      const rect = canvas.getBoundingClientRect();
      const lon = info.box.west + Math.floor((event.clientX - rect.left) / info.px);
      const lat = info.box.north - Math.floor((event.clientY - rect.top) / info.px);
      const m = data.membership;
      const c = info.member.get(lat * m.width + lon);
      const rain = info.values ? rainAt(info.values, lat, lon) : null;
      hover.hidden = false;
      hover.innerHTML = `Cell ${lat * m.width + lon} · ${(m.lon0 + lon * m.step).toFixed(2)}°E ${(m.lat0 + lat * m.step).toFixed(2)}°<br>Rain ${rain == null ? "—" : rain.toFixed(1) + " mm/h"} · ${c ? "weight " + c.weight.toFixed(3) : "outside the area"}`;
      const parent = canvas.parentElement.getBoundingClientRect();
      hover.style.left = Math.min(parent.width - 220, event.clientX - parent.left + 12) + "px";
      hover.style.top = event.clientY - parent.top + 12 + "px";
    };
    canvas.addEventListener("pointermove", point);
    canvas.addEventListener("pointerdown", point);
    canvas.addEventListener("pointerleave", () => { hover.hidden = true; });
  }

  // ---------- pipeline diagram (section 5) ----------
  function setupStages(root) {
    el("xStages").innerHTML = STAGES.map(stage => `<li><button type="button" class="x-stage" data-stage="${stage.id}" aria-pressed="${stage.id === state.stage}" aria-controls="xStagePanel"><b>${stage.name}</b><span>${stage.sub}</span></button></li>`).join("");
    root.addEventListener("click", event => {
      const stage = event.target.closest("[data-stage]");
      const lane = event.target.closest("[data-lane]");
      if (stage) {
        state.stage = stage.dataset.stage;
        renderStage();
        if (innerWidth <= 1000) el("xStagePanel").scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      if (lane) {
        state.lane = lane.dataset.lane;
        root.querySelectorAll("[data-lane]").forEach(b => b.setAttribute("aria-pressed", b === lane));
        renderStage();
      }
    });
    renderStage();
  }

  function renderStage() {
    const stage = STAGES.find(s => s.id === state.stage);
    document.querySelectorAll("#view-explain .x-stage").forEach(button => {
      const item = STAGES.find(s => s.id === button.dataset.stage);
      button.setAttribute("aria-pressed", button.dataset.stage === state.stage);
      button.classList.toggle("dim", state.lane !== "both" && !item.lanes.includes(state.lane));
    });
    const when = lane => `<div class="x-when${state.lane !== "both" && state.lane !== lane ? " dim" : ""}"><span class="label">${lane === "batch" ? "Batch layer" : "Speed layer"}</span><p>${esc(stage[lane])}</p></div>`;
    el("xStagePanel").innerHTML = `<h3>${stage.name}</h3><p>${esc(stage.what)}</p><div class="x-whens">${when("batch")}${when("speed")}</div>
      <p class="x-io"><span class="label">In</span> ${esc(stage.io[0])} <span class="label">Out</span> ${esc(stage.io[1])}</p>
      <h4 class="x-shape-h">Data shape</h4>${shape(stage.id)}`;
  }

  // ---------- follow one area (section 6) ----------
  function windowWet(observed, t, lead) {
    const slots = new Map(observed.map(o => [Date.parse(o.slot), o.rain_mean_x10]));
    const values = Array.from({ length: lead * 2 }, (_, i) => slots.get(t + i * HALF));
    if (values.some(v => v == null)) return null;
    return values.some(v => v >= 5);
  }

  function renderFollow() {
    const detail = data.detail;
    const list = el("xFollow");
    if (!detail) {
      list.innerHTML = `<li class="note">Area details are unavailable.</li>`;
      return;
    }
    const id = detail.area_id;
    const issues = [...new Set(detail.predictions.map(p => Date.parse(p.slot)))].sort((a, b) => a - b);
    const t = issues.at(-1);
    if (t == null) {
      list.innerHTML = `<li class="note">No forecast for this area in the last 48 h.</li>`;
      return;
    }
    const rows = detail.predictions.filter(p => Date.parse(p.slot) === t).sort((a, b) => a.lead_h - b.lead_h);
    const imergSlot = t - LAG;
    const rain = detail.observed.find(o => Date.parse(o.slot) === imergSlot);
    const cloud = detail.cloud.find(c => Date.parse(c.slot) === t);
    el("xFollowIssue").textContent = `Newest issue ${wib(t)}, written ${clockWib(rows[0].issued_at)}`;
    const cells = cellsOf(id);
    const p3 = rows.find(r => r.lead_h === 3)?.prob_pct;
    const cls = p3 == null ? null : Math.min(4, Math.floor(p3 / 20));
    const checked = issues.filter(s => windowWet(detail.observed, s, 3) != null).at(-1);
    const checkRows = checked == null ? [] : detail.predictions.filter(p => Date.parse(p.slot) === checked).sort((a, b) => a.lead_h - b.lead_h);
    const snapshot = data.samples;
    list.innerHTML = `
      <li><h3>Grid cells</h3><p>${esc(areaName(id))} touches ${cells.length} IMERG cell${cells.length === 1 ? "" : "s"} (${detail.area.area_km2.toFixed(0)} km²). The forecast used the IMERG slot of ${esc(wib(imergSlot))}, 6 hours before the issue. <button type="button" class="linkbtn" data-goto="x-grids">See the cells drawn</button></p></li>
      <li><h3>Silver rows</h3><div class="scroll-x"><table class="x-table"><thead><tr><th>Table</th><th>Slot</th><th>Values</th></tr></thead><tbody>
        <tr><td><code>rain_kabkota</code></td><td>${esc(wib(imergSlot))}</td><td>${rain ? `rain_mean_x10 = ${rain.rain_mean_x10} (${(rain.rain_mean_x10 / 10).toFixed(1)} mm/h)` : "not in the archive"}</td></tr>
        <tr><td><code>cloud_kabkota</code></td><td>${esc(wib(t))}</td><td>${cloud ? `cold_pct = ${cloud.cold_pct}, tb_min = ${cloud.tb_min} K (${Math.round(cloud.tb_min - 273.15)} °C), source ${cloud.source === 2 ? "Himawari-9" : "MERGIR"}` : "no scan"}</td></tr>
      </tbody></table></div></li>
      <li><h3>Feature row</h3><p class="note">Feature rows are not stored. This row is from the saved snapshot of the issue ${esc(wib(snapshot.issue_t))}, not the newest issue.</p>
        <details><summary>All 27 numbers for ${esc(areaName(id))}</summary>${featureTable(id)}</details></li>
      <li><h3>Three probabilities</h3><div class="x-probs">${rows.map(r => `<div><span class="label">Next ${r.lead_h} h</span><b>${r.prob_pct}%</b><span class="note">${clock(t)} to ${clockWib(t + r.lead_h * 60 * MIN)}</span></div>`).join("")}</div></li>
      <li><h3>Map colour</h3><p>The map shows the chosen window (+3 h here: ${p3 ?? "—"}%) in class ${cls == null ? "—" : `"${esc(legend.fc[1][cls])}"`}.</p>${swatches(cls)}</li>
      <li><h3>Later check</h3>${checkRows.length ? `<p>The newest issue that IMERG has covered fully is ${esc(wib(checked))}. Rule: rain when the area mean reaches 0.5 mm/h (rain_mean_x10 at least 5) in any half-hour of the window.</p>
        <div class="x-probs">${checkRows.map(r => { const wet = windowWet(detail.observed, checked, r.lead_h); return `<div><span class="label">Next ${r.lead_h} h</span><b>${r.prob_pct}%</b><span class="${wet ? "x-wet" : "x-dry"}">${wet ? "it rained" : "it stayed dry"}</span></div>`; }).join("")}</div>`
        : `<p class="note">IMERG has not yet covered a full window of the forecasts in the last 48 h.</p>`}</li>`;
  }

  // ---------- reliability and gain charts (section 8) ----------
  function renderScores() {
    const card = data.model?.card;
    const table = el("xScoreTable");
    if (!card) {
      table.innerHTML = `<caption>Model results are unavailable.</caption>`;
      return;
    }
    const split = state.split;
    table.innerHTML = `<thead><tr><th>Lead</th><th>Forecast</th><th class="num">BSS</th><th class="num">AUC</th><th class="num">CSI</th><th class="num">POD</th><th class="num">FAR</th><th class="num">Wet share</th></tr></thead><tbody>${
      Object.entries(card.leads).map(([lead, l]) => Object.entries(l.metrics[split]).map(([source, m], index) =>
        `<tr class="${source === "model" ? "x-model-row" : ""}${index === 0 ? " x-group" : ""}"><td>${index === 0 ? `+${lead} h` : ""}</td><td>${SOURCES[source]}</td><td class="num">${num(m.bss, 3)}</td><td class="num">${m.auc == null ? "—" : num(m.auc, 3)}</td><td class="num">${num(m["csi_at_0.5"], 3)}</td><td class="num">${num(m["pod_at_0.5"], 3)}</td><td class="num">${num(m["far_at_0.5"], 3)}</td><td class="num">${num(m.base_rate * 100, 1)}%</td></tr>`).join("")).join("")
    }</tbody>`;
    const lead = card.leads[state.lead];
    reliability(lead.reliability[split].model);
    gain(lead.feature_importance);
  }

  function chartTip(host) {
    let tip = host.querySelector(".x-hover");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "x-hover";
      tip.hidden = true;
      host.append(tip);
    }
    return tip;
  }

  function reliability(bins) {
    const host = el("xRel");
    const width = Math.min(host.clientWidth || 320, 420);
    const height = width;
    const pad = { l: 40, r: 12, t: 10, b: 34 };
    const w = width - pad.l - pad.r;
    const h = height - pad.t - pad.b;
    const x = v => pad.l + v * w;
    const y = v => pad.t + (1 - v) * h;
    const points = bins.filter(b => b.mean_predicted != null);
    const ticks = [0, 0.2, 0.4, 0.6, 0.8, 1];
    host.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Reliability: forecast chance against observed rain share, ${points.length} bins">
      ${ticks.map(t => `<line class="x-gridline" x1="${x(0)}" x2="${x(1)}" y1="${y(t)}" y2="${y(t)}"/><text x="${pad.l - 6}" y="${y(t) + 4}" text-anchor="end">${t * 100}%</text><text x="${x(t)}" y="${height - pad.b + 16}" text-anchor="middle">${t * 100}%</text>`).join("")}
      <line class="x-ref" x1="${x(0)}" y1="${y(0)}" x2="${x(1)}" y2="${y(1)}"/>
      <polyline class="x-line" points="${points.map(b => `${x(b.mean_predicted)},${y(b.observed_frequency)}`).join(" ")}"/>
      ${points.map((b, i) => `<circle class="x-dot" data-i="${i}" cx="${x(b.mean_predicted)}" cy="${y(b.observed_frequency)}" r="5"/>`).join("")}
      <text x="${pad.l + w / 2}" y="${height - 4}" text-anchor="middle">Forecast chance</text>
      <text transform="translate(11 ${pad.t + h / 2}) rotate(-90)" text-anchor="middle">Observed share</text>
    </svg><details><summary>Bins as a table</summary><table class="x-table"><thead><tr><th>Bin</th><th class="num">Mean forecast</th><th class="num">Observed</th><th class="num">Windows</th></tr></thead><tbody>${
      points.map(b => `<tr><td>${Math.round(b.lower * 100)} to ${Math.round(b.upper * 100)}%</td><td class="num">${num(b.mean_predicted * 100, 1)}%</td><td class="num">${num(b.observed_frequency * 100, 1)}%</td><td class="num">${num(b.count)}</td></tr>`).join("")
    }</tbody></table></details>`;
    const tip = chartTip(host);
    host.querySelector("svg").addEventListener("pointermove", event => {
      const dot = event.target.closest(".x-dot");
      if (!dot) {
        tip.hidden = true;
        return;
      }
      const b = points[dot.dataset.i];
      tip.hidden = false;
      tip.innerHTML = `${Math.round(b.lower * 100)} to ${Math.round(b.upper * 100)}% bin<br>Mean forecast ${num(b.mean_predicted * 100, 1)}%, rained ${num(b.observed_frequency * 100, 1)}%<br>${num(b.count)} windows`;
      tip.style.left = Math.min(width - 200, Number(dot.getAttribute("cx")) + 10) + "px";
      tip.style.top = Number(dot.getAttribute("cy")) + 10 + "px";
    });
    host.querySelector("svg").addEventListener("pointerleave", () => { tip.hidden = true; });
  }

  function gain(importance) {
    const host = el("xGain");
    const top = Object.entries(importance).sort((a, b) => b[1] - a[1]).slice(0, 8);
    const width = Math.min(host.clientWidth || 320, 520);
    const label = 108;
    const row = 30;
    const height = top.length * row + 8;
    const max = top[0][1];
    const scale = v => (width - label - 52) * v / max;
    host.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Top ${top.length} inputs by share of gain">
      ${top.map(([name, value], i) => `<g><title>${name}: ${num(value * 100, 1)}% of the gain</title>
        <text x="${label - 8}" y="${i * row + 19}" text-anchor="end" class="x-mono">${name}</text>
        <path class="x-bar" d="${barPath(label, i * row + 7, scale(value), 16)}"/>
        <text x="${label + scale(value) + 6}" y="${i * row + 19}" class="x-val">${num(value * 100, 1)}%</text></g>`).join("")}
    </svg>`;
  }

  // A bar from the baseline with a 4 px rounded end.
  function barPath(x, y, length, thick) {
    const r = Math.min(4, length / 2, thick / 2);
    return `M${x},${y}h${length - r}a${r},${r} 0 0 1 ${r},${r}v${thick - 2 * r}a${r},${r} 0 0 1 -${r},${r}h-${length - r}z`;
  }

  function renderLiveScores() {
    const table = el("xLiveScores");
    const rows = data.scores;
    if (!rows?.length) {
      table.innerHTML = `<caption>Live daily scores, all areas, +3 h: none yet.</caption>`;
      return;
    }
    const days = [...new Set(rows.map(r => r.day))].sort().reverse();
    const order = ["model", "bmkg", "clim", "persist", "motion"];
    table.innerHTML = `<caption>Live daily scores, all areas, +3 h (live)</caption><thead><tr><th>Day (UTC)</th><th>Forecast</th><th class="num">Windows</th><th class="num">BSS</th><th class="num">CSI</th><th class="num">Wet share</th></tr></thead><tbody>${
      days.map(day => order.map(source => rows.find(r => r.day === day && r.source === source)).filter(Boolean).map((r, index) =>
        `<tr class="${r.source === "model" ? "x-model-row" : ""}${index === 0 ? " x-group" : ""}"><td>${index === 0 ? r.day : ""}</td><td>${SOURCES[r.source]}</td><td class="num">${num(r.n)}</td><td class="num">${num(r.bss, 3)}</td><td class="num">${num(r.csi, 3)}</td><td class="num">${num(r.base_rate * 100, 1)}%</td></tr>`).join("")).join("")
    }</tbody>`;
  }

  // ---------- issue-time timeline (section 9) ----------
  function loopIssues() {
    return data.detail ? [...new Set(data.detail.predictions.map(p => Date.parse(p.slot)))].sort((a, b) => a - b) : [];
  }

  function setupLoop() {
    const slider = el("xLoopT");
    const issues = loopIssues();
    slider.max = Math.max(0, issues.length - 1);
    slider.value = slider.max;
    renderLoop();
  }

  function renderLoop() {
    const detail = data.detail;
    const host = el("xLoopChart");
    const issues = loopIssues();
    if (!detail || !issues.length) {
      host.innerHTML = `<p class="note">No forecast for this area in the last 48 h.</p>`;
      el("xLoopFacts").innerHTML = "";
      return;
    }
    const t = issues[Number(el("xLoopT").value)];
    const rows = detail.predictions.filter(p => Date.parse(p.slot) === t).sort((a, b) => a.lead_h - b.lead_h);
    el("xLoopText").textContent = wib(t);
    const start = t - 7 * 60 * MIN;
    const end = t + 3.5 * 60 * MIN;
    const width = Math.max(280, Math.min(host.clientWidth || 720, 980));
    const narrow = width < 560;
    const left = narrow ? 8 : 112;
    const right = 10;
    const x = ms => left + (ms - start) / (end - start) * (width - left - right);
    const lanes = narrow ? { rain: 40, cloud: 116, win: 176, axis: 268 } : { rain: 22, cloud: 98, win: 150, axis: 232 };
    const height = lanes.axis + 22;
    const cut = t - LAG;
    const observed = detail.observed.filter(o => Date.parse(o.slot) >= start && Date.parse(o.slot) < end);
    const clouds = detail.cloud.filter(c => Date.parse(c.slot) >= start && Date.parse(c.slot) <= t);
    const slotW = x(start + HALF) - x(start);
    const colors = P().ob;
    const barH = v => Math.max(2, Math.min(56, 8 + Math.sqrt(v / 10) * 18));
    const labels = narrow
      ? `<text x="${left}" y="${lanes.rain - 8}">Rain (IMERG)</text><text x="${left}" y="${lanes.cloud - 8}">Cold cloud (Himawari)</text><text x="${left}" y="${lanes.win - 8}">Forecast windows</text>`
      : `<text x="${left - 10}" y="${lanes.rain + 36}" text-anchor="end">Rain (IMERG)</text><text x="${left - 10}" y="${lanes.cloud + 24}" text-anchor="end">Cold cloud</text><text x="${left - 10}" y="${lanes.win + 40}" text-anchor="end">Windows</text>`;
    const hourTicks = [];
    for (let ms = Math.ceil(start / (60 * MIN)) * 60 * MIN; ms <= end; ms += 60 * MIN) hourTicks.push(ms);
    const every = narrow ? 3 : 1;
    host.innerHTML = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Timeline of the issue at ${esc(wib(t))}">
      <defs><pattern id="xHatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="6" class="x-hatch"/></pattern></defs>
      ${labels}
      <rect x="${x(cut + HALF)}" y="${lanes.rain}" width="${x(end) - x(cut + HALF)}" height="60" fill="url(#xHatch)" opacity=".5"><title>Not known at the issue time</title></rect>
      ${observed.map(o => { const v = o.rain_mean_x10; const ms = Date.parse(o.slot); const known = ms <= cut; const hgt = barH(v);
        return `<rect x="${x(ms) + 1}" y="${lanes.rain + 60 - hgt}" width="${Math.max(1, slotW - 2)}" height="${hgt}" rx="2" fill="${v === 0 ? "var(--line2)" : colors[rainClass(v / 10)]}" opacity="${known ? 1 : 0.55}"><title>${esc(wib(ms))}: ${(v / 10).toFixed(1)} mm/h${known ? "" : ", not known at t"}</title></rect>`; }).join("")}
      ${clouds.map(c => { const ms = Date.parse(c.slot); const hgt = Math.max(2, c.cold_pct * 0.4);
        return `<rect x="${x(ms) + 1}" y="${lanes.cloud + 40 - hgt}" width="${Math.max(1, slotW - 2)}" height="${hgt}" rx="2" class="${ms === t ? "x-cloud-now" : "x-cloud"}"><title>${esc(wib(ms))}: cold_pct ${c.cold_pct}</title></rect>`; }).join("")}
      ${rows.map((r, i) => { const wet = windowWet(detail.observed, t, r.lead_h); const y0 = lanes.win + i * 24;
        const text = `+${r.lead_h} h: ${r.prob_pct}%${wet == null ? "" : wet ? ", rained" : ", dry"}`;
        return `<rect x="${x(t)}" y="${y0}" width="${x(t + r.lead_h * 60 * MIN) - x(t)}" height="18" rx="4" class="x-window ${wet == null ? "" : wet ? "wet" : "dry"}"/><text x="${x(t) - 6}" y="${y0 + 13}" text-anchor="end" class="x-wtext">${text}</text>`; }).join("")}
      <line x1="${x(cut + HALF)}" x2="${x(cut + HALF)}" y1="${lanes.rain - 4}" y2="${lanes.axis}" class="x-mark"/><text x="${x(cut + HALF) + (narrow ? 4 : -4)}" y="${lanes.axis - 4}" text-anchor="${narrow ? "start" : "end"}" class="x-marktext">IMERG cut-off</text>
      <line x1="${x(t)}" x2="${x(t)}" y1="${lanes.rain - 4}" y2="${lanes.axis}" class="x-mark now"/><text x="${x(t) + 4}" y="${lanes.axis - 4}" class="x-marktext now">t</text>
      <line x1="${left}" x2="${width - right}" y1="${lanes.axis}" y2="${lanes.axis}" class="x-axis"/>
      ${hourTicks.map((ms, i) => i % every || x(ms) < left + 16 || x(ms) > width - 16 ? "" : `<text x="${x(ms)}" y="${lanes.axis + 16}" text-anchor="middle">${clock(ms)}</text>`).join("")}
    </svg>`;
    const runs = [...new Map(detail.bmkg.filter(b => b.analysis_time).map(b => [b.analysis_time, b])).values()];
    const run = runs.filter(b => Date.parse(b.first_seen) <= t).sort((a, b) => Date.parse(b.analysis_time) - Date.parse(a.analysis_time))[0];
    const rain = detail.observed.find(o => Date.parse(o.slot) === cut);
    const issued = rows[0]?.issued_at;
    el("xLoopFacts").innerHTML = `<dl class="kv x-kv">
      <dt>Issue time t</dt><dd>${esc(wib(t))}</dd>
      <dt>Written</dt><dd>${issued ? `${esc(clockWib(issued))} (${num((Date.parse(issued) - t) / MIN, 1)} min after t)` : "—"}</dd>
      <dt>Newest IMERG slot used</dt><dd>${esc(wib(cut))}${rain ? `, ${(rain.rain_mean_x10 / 10).toFixed(1)} mm/h` : ""}</dd>
      <dt>Cloud scan used</dt><dd>${esc(wib(t))}</dd>
      <dt>BMKG run in force</dt><dd>${run ? `${esc(wib(run.analysis_time))}, first seen ${esc(clockWib(run.first_seen))}` : "none archived"}</dd>
      <dt>All 3 h windows checked</dt><dd>${windowWet(detail.observed, t, 3) == null ? "not yet: IMERG has not covered the window" : "yes"}</dd></dl>`;
  }

  // ---------- setup ----------
  async function build() {
    const section = el("view-explain");
    const [html, samples, membership, geo, pipeline, model, scores] = await Promise.all([
      fetch("/explain.html").then(r => r.ok ? r.text() : null).catch(() => null),
      read("/explain-samples.json", "Explanation samples"),
      read("/explain-membership.json", "Grid weights"),
      read("/geo/kabkota.geojson", "Area boundaries"),
      read("/api/pipeline", "Pipeline statistics"),
      read("/api/model", "Model results"),
      read("/api/scores?scope=national&lead=3&days=7", "National scores"),
    ]);
    if (!html || !samples || !membership || !geo || !meta) {
      section.innerHTML = `<div class="page gutter"><h1>How it works</h1><p class="note">This page is unavailable because its data did not load. Use Retry above.</p></div>`;
      setup = null;
      return;
    }
    section.innerHTML = html;
    data = { samples, membership, geo, pipeline, model, scores, detail: null };
    state.area = malangId ?? samples.area_id;
    const root = section;
    root.addEventListener("click", event => {
      const link = event.target.closest("[data-goto]");
      if (!link) return;
      event.preventDefault();
      const target = el(link.dataset.goto);
      target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      target.querySelector("h2").focus({ preventScroll: true });
    });
    root.querySelectorAll(".x-sec h2").forEach(h => h.setAttribute("tabindex", "-1"));
    fillLive();
    setupTerms(root);
    setupPickers(root);
    setupGrid();
    setupStages(root);
    root.querySelectorAll("[data-split]").forEach(b => b.addEventListener("click", () => {
      state.split = b.dataset.split;
      root.querySelectorAll("[data-split]").forEach(o => o.setAttribute("aria-pressed", o === b));
      renderScores();
    }));
    root.querySelectorAll("[data-lead]").forEach(b => b.addEventListener("click", () => {
      state.lead = b.dataset.lead;
      root.querySelectorAll("[data-lead]").forEach(o => o.setAttribute("aria-pressed", o === b));
      renderScores();
    }));
    el("xLoopT").addEventListener("input", renderLoop);
    renderScores();
    renderLiveScores();
    await chooseArea(state.area);
    let width = innerWidth;
    new ResizeObserver(() => {
      if (innerWidth === width || section.hidden) return;
      width = innerWidth;
      redraw();
    }).observe(document.body);
    new MutationObserver(redraw).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    prefersLight.addEventListener("change", redraw);
  }

  // Canvas and inline colours come from the palette, so they are drawn again on a theme change.
  function redraw() {
    if (!data) return;
    drawGrid();
    renderFollow();
    renderScores();
    renderLoop();
    if (state.stage === "web") renderStage();
  }

  return () => {
    setup ??= build();
    return setup;
  };
})();
