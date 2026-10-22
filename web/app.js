"use strict";

const $ = id => document.getElementById(id);
const store = {
  get(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem("rn." + key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("rn." + key, JSON.stringify(value));
    } catch {
      // Preferences still work for the current page when storage is unavailable.
    }
  },
};
const settings = {
  theme: store.get("theme", "dark"),
  thr: store.get("thr", 0.5),
  zoom: store.get("zoom", "id"),
  cloud: store.get("cloud", 0.65),
  gauges: store.get("gauges", true),
};
const watch = new Set(store.get("watch", []));
const views = ["map", "overview", "alerts", "model", "history", "pipeline", "settings"];
const prefersLight = matchMedia("(prefers-color-scheme: light)");
const halfHour = 30 * 60 * 1000;
const frameCount = 103;
const issueFrame = 96;
const leads = [1, 2, 3];
const blank = "—";
const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23", timeZone: "Asia/Jakarta",
});
const archiveDateFormat = new Intl.DateTimeFormat("en-GB", {
  year: "numeric", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23", timeZone: "Asia/Jakarta",
});
const clockFormat = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "Asia/Jakarta",
});
const dayFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "Asia/Jakarta" });
const fmtTime = value => dateFormat.format(new Date(value)).replace(",", "") + " WIB";
const clock = value => clockFormat.format(new Date(value));
const clockWib = value => value == null ? blank : clock(value) + " WIB";
const pct = value => value == null ? blank : Math.round(value * 100) + "%";
const percent = value => value == null ? blank : Math.round(value) + "%";
const decimal = (value, digits = 2) => value == null ? blank : value.toFixed(digits);
const temp = value => value == null ? blank : Math.round(value - 273.15) + " °C";
const countFormat = new Intl.NumberFormat("en-GB");
const compactFormat = new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 1 });
const count = value => value == null ? blank : countFormat.format(value);
const size = value => value >= 1e9 ? (value / 1e9).toFixed(2) + " GB"
  : value >= 1e6 ? (value / 1e6).toFixed(1) + " MB" : Math.round(value / 1e3) + " KB";
const minutes = value => value == null ? blank
  : value < 60 ? Math.round(value) + " min"
    : value < 1440 ? (value / 60).toFixed(1) + " h" : Math.round(value / 1440) + " d";
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const emptyRow = (columns, message) => `<tr><td colspan="${columns}" class="note">${message}</td></tr>`;
const bmkgText = record => record?.weather == null ? blank
  : `${record.rain ? "Rain" : "No rain"} · code ${record.weather}`;
const areaLabel = area => area.kemendagri_name?.startsWith("Kota ")
  ? area.kemendagri_name : area.geo_name.startsWith("Kota ") ? area.geo_name
    : area.is_admin ? "Kab. " + area.geo_name : area.geo_name;
const svgEl = (tag, attrs, label) => {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  if (label != null) element.textContent = label;
  return element;
};
const sources = {
  himawari: "Himawari-9 clouds", bmkg: "BMKG forecast", imerg: "IMERG Early rain",
  metar: "METAR gauges", model: "Our model", mergir: "MERGIR clouds", ghcnh: "GHCNh gauges",
};
const scoreNames = {
  model: ["Our model", "var(--blue)"], motion: ["Cloud motion only", "var(--purple)"],
  persist: ["Same as IMERG now", "var(--gray)"], clim: ["Local climatology", "var(--aqua)"],
  bmkg: ["BMKG", "var(--orange)"], nomotion: ["LightGBM without cloud motion", "var(--gray)"],
};

let view = "map";
let meta;
let areas = [];
let areaById = new Map();
let islands = [];
let modelIssue;
let timelineStart;
let latestObs;
let obsFrame;
let ticks = [];
let frame = issueFrame;
let selected = null;
let historyA = null;
let historyB = null;
let historySelection = 0;
let alertLoad = 0;
let alertData;
let latestProbabilities = new Map();
let route = 0;
let map;
let mapSetup;
let mapReady = false;
let pendingArea = null;
let layer = "fc";
let raw = false;
let cloudsOn = true;
let playing;
let hovered;
let hoverPoint;
const areaDetails = new Map();
const frameData = new Map();
const frameStamps = new Map();
const requests = new Map();

function read(url, label) {
  if (!requests.has(url)) {
    requests.set(url, fetch(url, { signal: AbortSignal.timeout(20000) }).then(response => {
      if (!response.ok) throw new Error(label);
      return response.json();
    }).catch(() => {
      requests.delete(url);
      $("dataStatus").textContent = `${label} could not load. Reload to try again.`;
      $("dataStatus").hidden = false;
      return null;
    }));
  }
  return requests.get(url);
}

const tms = index => timelineStart + index * halfHour;
const archivedFrame = stamp => (Date.parse(stamp) - timelineStart) / halfHour;
const threshold = () => Math.round(settings.thr * 100);
const alertUrl = (limit = threshold()) => `/api/alerts?threshold=${limit}`;

function initMeta(data) {
  meta = data;
  areas = meta.areas.map(area => ({ ...area, label: areaLabel(area) }));
  areaById = new Map(areas.map(area => [area.area_id, area]));
  islands = [...new Set(areas.map(area => area.island).filter(Boolean))];
  const malang = areas.find(area => area.label === "Kota Malang");
  historyA ??= malang?.area_id ?? areas[0]?.area_id ?? null;
  for (const area of areas.toSorted((a, b) => a.label.localeCompare(b.label))) {
    $("hA").add(new Option(area.label, area.area_id));
    $("hB").add(new Option(area.label, area.area_id));
  }
  for (const island of islands) $("alIsland").add(new Option(island, island));
  const provinces = new Map(areas.filter(a => a.prov_code && a.prov_name).map(a => [a.prov_code, a.prov_name]));
  for (const [code, name] of [...provinces].sort((a, b) => a[1].localeCompare(b[1]))) {
    $("alProvince").add(new Option(name, code));
  }

  setTimeline();
}

function setTimeline() {
  modelIssue = meta.latest_issue_slot;
  timelineStart = (modelIssue ? Date.parse(modelIssue) : Math.floor(Date.parse(meta.as_of) / halfHour) * halfHour)
    - issueFrame * halfHour;
  latestObs = meta.latest_observed_slot;
  obsFrame = latestObs == null ? -1 : Math.min(issueFrame, Math.floor(archivedFrame(latestObs)));
  ticks = Array.from({ length: frameCount }, (_, index) => {
    const time = tms(index);
    const hour = clock(time);
    return hour === "00:00" ? [index, dayFormat.format(time)] : hour === "12:00" ? [index, hour] : null;
  }).filter(Boolean);
  drawTrack();
  updateWhen();
}

const archiveStamp = data => [
  data.latest_issue_slot, data.latest_observed_slot, data.latest_slots?.himawari, data.latest_slots?.bmkg,
  data.frames_updated,
].join("|");

// Frames are keyed by slot time, so older frames keep drawing until the newer archive arrives.
async function loadFrames(...layers) {
  const stamp = archiveStamp(meta);
  const window = new URLSearchParams({
    from: new Date(tms(0)).toISOString(), to: new Date(tms(frameCount - 1)).toISOString(),
  });
  await Promise.all(layers.map(async name => {
    if (frameStamps.get(name) === stamp) return;
    const data = await read(`/api/frames?layer=${name}&${window}`, `${name} frames`);
    if (!data) return;
    frameData.set(name, new Map(data.slots.map((slot, index) => [Date.parse(slot), data.values[index]])));
    frameStamps.set(name, stamp);
  }));
}

function useLatestProbabilities(ranking) {
  if (!ranking?.issue_slot || Date.parse(ranking.issue_slot) !== Date.parse(modelIssue)) return;
  latestProbabilities = new Map(leads.map(lead => {
    const row = [];
    for (const area of ranking.alerts) row[area.area_id] = area.prob_pct[lead];
    return [lead, row];
  }));
}

function forecastLead(index) {
  return index <= issueFrame ? 3 : Math.ceil((index - issueFrame) / 2);
}

function frameRow(name, index) {
  if (name === "forecast") {
    const lead = forecastLead(index);
    const row = frameData.get("prob" + lead)?.get(tms(Math.min(index, issueFrame)));
    return row ?? (index >= issueFrame ? latestProbabilities.get(lead) : null);
  }
  return frameData.get(name)?.get(tms(index));
}

function valueAt(name, index, id) {
  return frameRow(name, index)?.[id] ?? null;
}

function applyTheme() {
  if (settings.theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = settings.theme;
  document.querySelectorAll("[data-theme-set]").forEach(button => {
    button.setAttribute("aria-pressed", button.dataset.themeSet === settings.theme);
  });
  paint();
  drawDrawer();
  if (view === "history" && meta) renderHistory();
}

// Map palette and controls
const palette = {
  dark: {
    sea: "#1d2021", land: "#282828", edge: "#282828", fg: "#ebdbb2", sel: "#fabd2f",
    hover: "#fbf1c7", nodata: "#3c3836", cloud: [251, 241, 199],
    fc: ["#504945", "#076678", "#458588", "#83a598", "#d3869b"],
    ob: ["#504945", "#076678", "#458588", "#83a598", "#d3869b"],
    bm: ["#665c54", "#458588"], ds: ["#504945", "#fe8019"],
  },
  light: {
    sea: "#f9f5d7", land: "#f2e5bc", edge: "#fbf1c7", fg: "#3c3836", sel: "#af3a03",
    hover: "#282828", nodata: "#ebdbb2", cloud: [80, 73, 69],
    fc: ["#d5c4a1", "#83a598", "#458588", "#076678", "#8f3f71"],
    ob: ["#d5c4a1", "#83a598", "#458588", "#076678", "#8f3f71"],
    bm: ["#bdae93", "#83a598"], ds: ["#d5c4a1", "#af3a03"],
  },
};
const P = () => palette[settings.theme === "system" ? (prefersLight.matches ? "light" : "dark") : settings.theme];
const legend = {
  fc: ["Chance of rain, next 3 h", ["under 20%", "20–40%", "40–60%", "60–80%", "80% or more"]],
  ob: ["Rain rate, mm/h (IMERG)", ["dry (under 0.1)", "0.1–1", "1–3", "3–8", "8 or more"]],
  bm: ["BMKG forecast", ["No rain", "Rain"]],
  ds: ["Model vs BMKG, next 3 h", ["Agree", "Disagree"]],
};
const layerData = { fc: "forecast", ob: "obs", bm: "bmkg", ds: "disagree" };
const zoomBounds = {
  id: [[94.8, -11.2], [141.2, 6.2]], jawa: [[105.1, -8.9], [116.3, -5.7]],
  jatim: [[110.9, -8.9], [116.3, -6.6]],
};
const mapPadding = () => innerWidth > 820
  ? { top: 20, bottom: 100, left: 330, right: 390 }
  : { top: 10, bottom: 10, left: 10, right: 10 };
const rainClass = value => value < 0.1 ? 0 : value < 1 ? 1 : value < 3 ? 2 : value < 8 ? 3 : 4;

function colorClass(value) {
  if (value == null) return -1;
  if (layer === "fc") return Math.min(4, Math.floor(value / 20));
  if (layer === "ob") return rainClass(value / 10);
  if (layer === "bm") return Number(value === 100);
  return value;
}

function drawLegend() {
  const [title, keys] = legend[layer];
  $("legTitle").textContent = layer === "fc" ? `Chance of rain, next ${forecastLead(frame)} h` : title;
  $("legend").innerHTML = [...keys, "No data"].map((label, index) =>
    `<div class="row"><span class="sw" style="background:${P()[layer][index] ?? P().nodata}"></span>${label}</div>`
  ).join("");
  const rgb = P().cloud.join(",");
  $("cloudRamp").style.background = `linear-gradient(90deg, rgba(${rgb},0), rgba(${rgb},${settings.cloud}))`;
  $("cloudLeg").hidden = !cloudsOn;
}

function drawTrack() {
  const pos = index => index / (frameCount - 1) * 100;
  const start = pos(Math.max(0, obsFrame));
  const now = pos(issueFrame);
  $("deco").innerHTML = `
    <div class="gap" style="left:${start}%;width:${now - start}%"></div>
    <div class="fut" style="left:${now}%;right:0"></div>
    ${obsFrame >= 0 && obsFrame !== issueFrame ? `<div class="mk sat" style="left:${start}%"><span>IMERG</span></div>` : ""}
    <div class="mk now" style="left:${now}%"><span>${modelIssue ? "issue" : "now"}</span></div>`;
  $("ticks").innerHTML = ticks.map(([index, label]) => `<span style="left:${pos(index)}%">${label}</span>`).join("");
}

function updateWhen() {
  if (!meta) return;
  const minutesFromIssue = (frame - issueFrame) * 30;
  const elapsed = Math.abs(minutesFromIssue);
  const rel = minutesFromIssue === 0 ? (modelIssue ? "latest issue" : "now")
    : (minutesFromIssue < 0 ? "−" : "+") + (elapsed >= 60 ? `${Math.floor(elapsed / 60)} h` : "")
      + (elapsed % 60 ? ` ${elapsed % 60} min` : "");
  $("frame").setAttribute("aria-valuetext", fmtTime(tms(frame)));
  $("wT").textContent = fmtTime(tms(frame));
  const forecastOnly = frame > issueFrame;
  const observationPending = latestObs == null || tms(frame) > Date.parse(latestObs);
  const status = forecastOnly ? "forecast only" : observationPending ? "IMERG pending" : "archive";
  $("wS").textContent = rel + " · " + status;
  let banner = "";
  if (layer === "ob" && observationPending) {
    banner = latestObs == null ? "No IMERG observations are archived."
      : `IMERG is available through ${fmtTime(latestObs)}.`;
  } else if (cloudsOn && forecastOnly) {
    banner = modelIssue ? `Forecast only. Issued ${fmtTime(modelIssue)}.` : "No model issue is archived.";
  }
  $("banner").textContent = banner;
  $("banner").hidden = !banner;
  const row = frameRow(layerData[layer], frame);
  const note = row?.some(value => value != null) ? ""
    : layer === "bm" ? "No BMKG run archived for this time."
      : layer === "ds" ? "No forecast comparison archived for this time."
        : layer === "fc" ? "No model forecast archived for this time."
          : "No area rain measurements archived for this time.";
  $("layerNote").textContent = note;
  $("layerNote").hidden = !note;
}

const raster = {};
const tileImages = new Map();

function addRaster(name, before) {
  const bounds = meta.tile_bounds?.[name];
  if (!bounds) return;
  const canvas = document.createElement("canvas");
  canvas.width = bounds.width;
  canvas.height = bounds.height;
  raster[name] = { canvas, ctx: canvas.getContext("2d", { willReadFrequently: true }), revision: 0 };
  const { west, east, north, south } = bounds;
  map.addSource(name, { type: "canvas", canvas, animate: false,
    coordinates: [[west, north], [east, north], [east, south], [west, south]] });
  map.addLayer({ id: name, type: "raster", source: name, layout: { visibility: "none" },
    paint: { "raster-fade-duration": 0, "raster-resampling": name === "rain" ? "nearest" : "linear" } }, before);
}

const stem = index => new Date(tms(index)).toISOString().replace(/[-:]/g, "").slice(0, 13) + "Z";
const stemTime = scan => Date.parse(scan.replace(/(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)Z/, "$1-$2-$3T$4:$5Z"));

// The latest image at or before the frame, up to 2 h old, so a short gap keeps the last picture.
function tileFor(name, index) {
  if (name === "rain" && (latestObs == null || tms(index) > Date.parse(latestObs))) return null;
  return meta.tile_times[name].findLast(time => time <= stem(index) && time > stem(index - 4));
}

function tileBlob(name, scan) {
  const url = name === "rain" ? `/tiles/rain/${scan}.png` : `/tiles/cloud/${scan}.webp`;
  if (!tileImages.has(url)) {
    tileImages.set(url, fetch(url, { signal: AbortSignal.timeout(15000) }).then(response => {
      if (!response.ok) throw new Error("Tile");
      return response.blob();
    }).catch(() => {
      tileImages.delete(url);
      return null;
    }));
  }
  return tileImages.get(url);
}

async function drawRaster(name, visible) {
  const target = raster[name];
  const note = $(name + "TileNote");
  if (!target) {
    note.hidden = !visible;
    return;
  }
  const revision = ++target.revision;
  const scan = visible ? tileFor(name, frame) : null;
  const label = name === "rain" ? "raw rain image" : "cloud image";
  note.textContent = scan ? `No ${label} archived for this time. Showing the one from ${clockWib(stemTime(scan))}.`
    : `No ${label} archived for this time.`;
  note.hidden = !visible || (scan ? scan > stem(frame - 1) : name === "rain" && tms(frame) > Date.parse(latestObs));
  if (!scan) {
    map.setLayoutProperty(name, "visibility", "none");
    return;
  }
  const next = playing && tileFor(name, frame + 1);
  if (next) tileBlob(name, next);
  const appearance = name === "rain" ? P().ob : P().cloud;
  const key = [scan, appearance, name === "cloud" ? settings.cloud : ""].join(":");
  if (target.key === key) {
    map.setLayoutProperty(name, "visibility", "visible");
    return;
  }

  // The previous image stays on the map until the new one is drawn.
  const blob = await tileBlob(name, scan);
  if (target.revision !== revision) return;
  const image = blob && await createImageBitmap(blob).catch(() => null);
  if (target.revision !== revision) {
    image?.close();
    return;
  }
  if (!image) {
    note.textContent = `The ${label} could not load.`;
    note.hidden = false;
    return;
  }

  const { canvas, ctx } = target;
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);
  ctx.drawImage(image, 0, 0);
  image.close();
  const pixels = ctx.getImageData(0, 0, width, height);
  const data = pixels.data;
  const colors = P().ob.map(hex => [
    parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16),
  ]);
  // One RGBA entry per grey level. Both arrays are read as 32-bit words, so byte order matches.
  const lut = new Uint8ClampedArray(256 * 4);
  for (let value = 0; value < 256; value++) {
    const a = Math.min(1, Math.max(0, (value / 255 - 0.28) / 0.55));
    lut.set(name === "cloud" ? [...appearance, Math.round(255 * settings.cloud * a * a * (3 - 2 * a))]
      : [...colors[rainClass(value / 10)], value ? 235 : 0], value * 4);
  }
  const entries = new Uint32Array(lut.buffer);
  const words = new Uint32Array(data.buffer);
  for (let i = 0; i < words.length; i++) words[i] = entries[data[i * 4]];
  ctx.putImageData(pixels, 0, 0);
  target.key = key;
  map.setLayoutProperty(name, "visibility", "visible");
  const source = map.getSource(name);
  source.play();
  map.once("render", () => source.pause());
}

function paint() {
  drawLegend();
  if (!mapReady) return;
  const p = P();
  const showRaw = raw && layer === "ob";
  const expression = ["match", ["coalesce", ["feature-state", "k"], -1]];
  p[layer].forEach((color, index) => expression.push(index, color));
  expression.push(p.nodata);
  map.setPaintProperty("bg", "background-color", p.sea);
  map.setPaintProperty("nb", "fill-color", p.land);
  map.setPaintProperty("kab-fill", "fill-color", expression);
  map.setPaintProperty("kab-fill", "fill-opacity", showRaw ? 0 : 1);
  map.setPaintProperty("kab-line", "line-color", showRaw ? p.fg : p.edge);
  map.setPaintProperty("kab-line", "line-opacity", showRaw ? 0.35 : 1);
  map.setPaintProperty("kab-sel", "line-color", p.sel);
  map.setPaintProperty("kab-hover", "line-color", p.hover);
  map.setPaintProperty("gauges", "circle-stroke-color", p.fg);
  map.setLayoutProperty("gauges", "visibility", settings.gauges ? "visible" : "none");
  return Promise.all([drawRaster("rain", showRaw), drawRaster("cloud", cloudsOn && frame <= issueFrame)]);
}

function applyFrame() {
  const values = frameRow(layerData[layer], frame);
  if (mapReady) {
    for (const id of mapAreaIds) map.setFeatureState({ source: "kab", id }, { k: colorClass(values?.[id]) });
    if (hovered != null) showTip(hovered, hoverPoint);
  }
  updateWhen();
  drawDrawer();
  return paint();
}

let mapAreaIds = [];
let mapAreas = new Map();

function showTip(id, point) {
  const area = mapAreas.get(id);
  const value = valueAt(layerData[layer], frame, id);
  const description = value == null ? "No data for this time" : layer === "fc"
    ? `${percent(value)} chance of rain · +${forecastLead(frame)} h` : layer === "ob"
      ? `${(value / 10).toFixed(1)} mm/h` : legend[layer][1][colorClass(value)];
  const cold = valueAt("cold", frame, id);
  const tip = $("tip");
  tip.innerHTML = `<b>${escape(area.label)}</b><br>${description}${cloudsOn && cold != null ? `<br><span class="note">Cold cloud ${percent(cold)}</span>` : ""}`;
  tip.hidden = false;
  const stage = document.querySelector(".stage").getBoundingClientRect();
  const bounds = $("map").getBoundingClientRect();
  let x = point.x + bounds.left - stage.left + 14;
  let y = point.y + bounds.top - stage.top + 14;
  if (x + tip.offsetWidth > stage.width - 8) x -= tip.offsetWidth + 28;
  if (y + tip.offsetHeight > bounds.bottom - stage.top) y -= tip.offsetHeight + 28;
  tip.style.left = Math.max(4, x) + "px";
  tip.style.top = Math.max(4, y) + "px";
}

function zoomTo(name, animate) {
  if (mapReady) map.fitBounds(zoomBounds[name], { padding: mapPadding(), duration: animate ? 700 : 0 });
  document.querySelectorAll("[data-z]").forEach(button => button.setAttribute("aria-pressed", button.dataset.z === name));
}

async function initMap() {
  const [kab, neighbours] = await Promise.all([
    read("/geo/kabkota.geojson", "Area boundaries"), read("/geo/neighbours.geojson", "Neighbouring boundaries"),
  ]);
  const loading = $("loading");
  if (!window.maplibregl || !kab) {
    loading.textContent = !window.maplibregl ? "The map library could not load." : "Area boundaries are unavailable.";
    return;
  }
  mapAreas = new Map(kab.features.map(({ properties: p }) => [p.i,
    areaById.get(p.i) ?? { area_id: p.i, label: p.n, centroid_lon: p.x, centroid_lat: p.y },
  ]));
  mapAreaIds = [...mapAreas.keys()];

  try {
    map = new maplibregl.Map({
      container: "map", style: { version: 8, sources: {}, layers: [
        { id: "bg", type: "background", paint: { "background-color": P().sea } },
      ] },
      bounds: zoomBounds[settings.zoom], fitBoundsOptions: { padding: mapPadding() },
      attributionControl: false, dragRotate: false, pitchWithRotate: false, renderWorldCopies: false,
      minZoom: 1, maxZoom: 12,
    });
  } catch {
    loading.textContent = "The map could not start. The other pages are available.";
    return;
  }
  map.touchZoomRotate.disableRotation();
  await new Promise(resolve => map.once("load", resolve));
  map.on("error", () => {
    $("dataStatus").textContent = "Some map layers could not render.";
    $("dataStatus").hidden = false;
  });
  const features = meta.gauge_stations.map(station => ({
    type: "Feature", properties: { icao: station.icao },
    geometry: { type: "Point", coordinates: [station.lon, station.lat] },
  }));
  map.addSource("gauges", { type: "geojson", data: { type: "FeatureCollection", features } });
  map.addSource("nb", { type: "geojson", data: neighbours ?? { type: "FeatureCollection", features: [] } });
  map.addSource("kab", { type: "geojson", data: kab });
  map.addLayer({ id: "nb", type: "fill", source: "nb", paint: { "fill-color": P().land } });
  map.addLayer({ id: "kab-fill", type: "fill", source: "kab", paint: { "fill-color": P().nodata } });
  map.addLayer({ id: "kab-line", type: "line", source: "kab",
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.3, 7, 0.8, 9, 1.4] } });
  addRaster("rain", "kab-line");
  addRaster("cloud");
  for (const [id, width] of [["kab-hover", 1.6], ["kab-sel", 2.6]]) {
    map.addLayer({ id, type: "line", source: "kab", filter: ["==", ["get", "i"], -1], paint: { "line-width": width } });
  }
  map.addLayer({ id: "gauges", type: "circle", source: "gauges", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2.5, 8, 5],
    "circle-opacity": 0, "circle-stroke-width": 1.4,
  } });

  map.on("mousemove", "kab-fill", event => {
    const id = event.features[0].properties.i;
    map.getCanvas().style.cursor = "pointer";
    if (id !== hovered) map.setFilter("kab-hover", ["==", ["get", "i"], id]);
    hovered = id;
    hoverPoint = event.point;
    showTip(id, event.point);
  });
  map.on("mouseleave", "kab-fill", () => {
    map.getCanvas().style.cursor = "";
    hovered = null;
    map.setFilter("kab-hover", ["==", ["get", "i"], -1]);
    $("tip").hidden = true;
  });
  map.on("click", "kab-fill", event => openDrawer(event.features[0].properties.i, false));
  mapReady = true;
  zoomTo(settings.zoom, false);
  loading.hidden = true;
}

// Charts and area drawer
function traces(svg, series, x, y) {
  for (const { values, color, width = 2, dash = "0" } of series) {
    let path = "";
    values.forEach((value, index) => {
      if (value == null) return;
      const continuation = index > 0 && values[index - 1] != null;
      path += `${continuation ? "L" : "M"}${x(index)},${y(value)} `;
      if (!continuation && values[index + 1] == null) {
        svg.append(svgEl("circle", { cx: x(index), cy: y(value), r: 2.5, fill: color }));
      }
    });
    if (path) svg.append(svgEl("path", {
      d: path, fill: "none", stroke: color, "stroke-width": width,
      "stroke-dasharray": dash, "stroke-linejoin": "round",
    }));
  }
}

function lineChart(svg, w, h, series, { n, hi, yt, format, xt = [], marks = [] }) {
  const left = 42, right = 26, top = 10, bottom = 24;
  const x = index => left + index * (w - left - right) / (n - 1);
  const y = value => top + (1 - value / hi) * (h - top - bottom);
  svg.replaceChildren();
  for (const value of yt) {
    svg.append(svgEl("line", { x1: left, x2: w - right, y1: y(value), y2: y(value), stroke: "var(--line)" }),
      svgEl("text", { x: left - 6, y: y(value) + 3.5, "text-anchor": "end" }, format(value)));
  }
  for (const [index, label] of xt) {
    svg.append(svgEl("text", { x: x(index), y: h - 6, "text-anchor": "middle" }, label));
  }
  for (const [index, label] of marks) {
    svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: top, y2: h - bottom,
      stroke: "var(--muted)", "stroke-dasharray": "3 3" }),
    svgEl("text", { x: x(index) - 4, y: top + 9, "text-anchor": "end" }, label));
  }
  traces(svg, series, x, y);
}

function barsH(svg, w, h, rows, { max, format, left = 150, right = 52 }) {
  svg.replaceChildren();
  const rowHeight = (h - 12) / rows.length;
  rows.forEach(({ label, values }, index) => {
    const y = 6 + index * rowHeight;
    svg.append(svgEl("text", { x: left - 8, y: y + rowHeight / 2 + 3.5, "text-anchor": "end" }, label));
    const height = (rowHeight - 8) / values.length;
    values.forEach(([value, color], i) => {
      if (value == null) return;
      const width = Math.max(0, value / max * (w - left - right));
      svg.append(svgEl("rect", { x: left, y: y + 4 + i * height, width, height: height - 1, rx: 2, fill: color }),
        svgEl("text", { x: left + width + 5, y: y + 4 + (i + 0.5) * height + 3 }, format(value)));
    });
  });
}

function chartNote(svg, message) {
  const { width, height } = svg.viewBox.baseVal;
  svg.replaceChildren(svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, message));
}

function tiles(id, values) {
  $(id).innerHTML = values.map(([label, value, detail, color]) => `
    <div class="panel tile s3 t-${color}"><span class="label">${label}</span>
    <span class="v">${value}</span><span class="d">${detail}</span></div>`).join("");
}

async function openDrawer(id, zoom) {
  const area = mapAreas.get(id);
  if (!area || !mapReady) return;
  selected = id;
  $("drawer").hidden = false;
  $("national").hidden = true;
  map.setFilter("kab-sel", ["==", ["get", "i"], id]);
  if (zoom) {
    const { centroid_lon: x, centroid_lat: y } = area;
    map.fitBounds([[x - 1.2, y - 0.8], [x + 1.2, y + 0.8]], { padding: mapPadding(), duration: 600 });
  }
  drawDrawer();
  if (!areaDetails.has(id)) {
    const detail = await read(`/api/areas/${id}`, "Area details");
    areaDetails.set(id, detail);
    if (selected === id) drawDrawer();
  }
}

function drawDrawer() {
  if (selected == null) return;
  const area = mapAreas.get(selected);
  const data = areaDetails.get(selected);
  const cloud = new Map();
  for (const row of data?.cloud ?? []) {
    const index = Math.ceil(archivedFrame(row.slot));
    if (index >= 0 && index <= issueFrame) cloud.set(index, row);
  }
  const rain = new Map((data?.observed ?? []).map(row => [archivedFrame(row.slot), row.rain_mean_x10]));
  const bmkgSlots = new Map((data?.bmkg ?? []).map(row => [archivedFrame(row.slot), row]));
  const bmkg = bmkgSlots.get(frame);
  const predicted = (data?.predictions ?? []).filter(row => row.lead_h === 3);
  const prob = new Map(predicted.map(row => [archivedFrame(row.slot), row.prob_pct]));
  const obs = index => index <= issueFrame ? rain.get(index) ?? valueAt("obs", index, selected) : null;
  const forecast = index => valueAt("forecast", index, selected) ?? (index <= issueFrame ? prob.get(index) : null);
  const model = forecast(frame);
  const modelLabel = `Our model, next ${forecastLead(frame)} h`;
  const measured = obs(frame);
  const bm = valueAt("bmkg", frame, selected);
  const cloudRow = cloud.get(frame);
  const cold = cloudRow?.cold_pct ?? valueAt("cold", frame, selected);
  $("dName").textContent = area.label;
  $("dIsland").textContent = area.island ?? "kab/kota";
  $("dNow").innerHTML = [
    [modelLabel, percent(model), "var(--blue)"],
    ["BMKG", bmkg?.weather != null ? bmkgText(bmkg) : bm == null ? blank : bm ? "Rain" : "No rain", "var(--orange)"],
    ["Coldest cloud top", temp(cloudRow?.tb_min), "var(--fg0)"],
    ["IMERG rain", measured != null ? `${(measured / 10).toFixed(1)} mm/h`
      : frame <= issueFrame && frame > obsFrame ? "not in yet" : blank, "var(--fg0)"],
  ].map(([label, value, color]) => `<div><span class="l">${label}</span><span class="v" style="color:${color}">${value}</span></div>`).join("");

  const svg = $("dChart");
  svg.replaceChildren();
  const indices = Array.from({ length: frameCount }, (_, index) => index);
  const x = index => 32 + index * 302 / (frameCount - 1);
  const cloudY = value => 8 + (20 - Math.max(-90, Math.min(20, value))) / 110 * 30;
  const rainTop = Math.max(2, Math.ceil(Math.max(...indices.map(index => (obs(index) ?? 0) / 10))));
  const pY = value => 106 + (1 - value / 100) * 56;
  for (const [y, text] of [[16, "20"], [38, "−90"], [54, rainTop], [93, "0"], [176, "BMKG"]]) {
    svg.append(svgEl("text", { x: 28, y, "text-anchor": "end" }, text));
  }
  for (const [y, label] of [[15, "cloud top °C"], [53, "IMERG mm/h"], [115, "our P(rain)"]]) {
    svg.append(svgEl("text", { x: 34, y }, label));
  }
  svg.append(svgEl("line", { x1: 32, x2: 334, y1: 94, y2: 94, stroke: "var(--line)" }));
  for (const value of [0, 50, 100]) {
    svg.append(svgEl("line", { x1: 32, x2: 334, y1: pY(value), y2: pY(value),
      stroke: "var(--line)", "stroke-dasharray": value === 50 ? "2 3" : "0" }),
    svgEl("text", { x: 28, y: pY(value) + 3.5, "text-anchor": "end" }, percent(value)));
  }
  traces(svg, [{ values: indices.map(index => {
    const tb = cloud.get(index)?.tb_min;
    return tb == null ? null : tb - 273.15;
  }), color: "var(--fg)", width: 1.3 }], x, cloudY);
  traces(svg, [
    { values: indices.map(index => index <= issueFrame ? forecast(index) : null), color: "var(--blue)" },
    { values: indices.map(index => index >= issueFrame ? forecast(index) : null), color: "var(--blue)", dash: "3 2" },
  ], x, pY);
  for (const index of indices) {
    const value = obs(index) / 10;
    if (value > 0) {
      const height = Math.max(1, value / rainTop * 40);
      svg.append(svgEl("rect", { x: x(index) - 1, y: 94 - height, width: 2.2, height, fill: P().ob[rainClass(value)] }));
    }
    const archived = valueAt("bmkg", index, selected);
    const areaRain = bmkgSlots.get(index)?.rain;
    const wet = archived ?? (areaRain == null ? null : Number(areaRain));
    if (wet != null) svg.append(svgEl("rect", { x: x(index) - 1.4, y: 172, width: 2.9, height: 11, fill: P().bm[Number(wet > 0)] }));
  }
  for (const [index, color, dash] of [
    [obsFrame, "var(--muted)", "3 3"], [issueFrame, "var(--fg0)", "0"], [frame, "var(--yellow)", "0"],
  ]) {
    if (index >= 0) svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: 5, y2: 187,
      stroke: color, "stroke-dasharray": dash, "stroke-width": color === "var(--yellow)" ? 2 : 1 }));
  }
  for (const index of [0, 24, 48, 72, issueFrame]) {
    svg.append(svgEl("text", { x: x(index), y: 207, "text-anchor": "middle" },
      index === issueFrame ? "issue" : `−${(issueFrame - index) / 2}h`));
  }

  const score = data?.scores.rows.findLast(row => row.source === "model" && row.lead_h === 3);
  const month = new Date(tms(frame) + ({ WIB: 7, WITA: 8, WIT: 9 }[area.tz] ?? 0) * 2 * halfHour);
  const clim = data?.climatology.find(row => row.month === month.getUTCMonth() + 1 && row.hour_local === month.getUTCHours());
  const details = data ? [cold != null ? `Cold cloud ${percent(cold)}` : null,
    score ? `${data.scores.scope === "area" ? "Area" : "Province"} CSI ${decimal(score.csi)}` : "No area or province scores yet",
    clim ? `Local usual rain chance ${percent(clim.p3)}` : null,
  ].filter(Boolean).join(" · ") : areaDetails.has(selected) ? "Area details unavailable." : "Loading area details…";
  $("dDetail").textContent = details;
  $("dWatch").setAttribute("aria-pressed", watch.has(selected));
  $("dWatch").textContent = watch.has(selected) ? "★ Watching" : "☆ Watch";
}

function renderScoreboard(scores) {
  const latest = scores.findLast(row => row.lead_h === 3)?.day;
  $("national").querySelector(".phead .note").textContent = latest ? `${latest} UTC · +3 h` : "Latest verified day · +3 h";
  $("natBody").innerHTML = latest ? scores.filter(row => row.day === latest && row.lead_h === 3).map(row => {
    const [label, color] = scoreNames[row.source];
    return `<tr><td><span class="key" style="background:${color}"></span>${label}</td>
      <td class="num">${decimal(row.csi)}</td><td class="num">${pct(row.pod)}</td><td class="num">${pct(row.far)}</td></tr>`;
  }).join("") : emptyRow(4, "No verified national scores yet.");
}

function renderMapStatus(data) {
  const issueAge = meta.freshness_minutes.model;
  const live = issueAge != null && issueAge >= 0 && issueAge <= 60;
  $("sourceState").textContent = live ? "LIVE" : modelIssue ? "ARCHIVE" : "NO ISSUE";
  $("sourceStatus").classList.toggle("archive", !live);
  $("mapIssue").textContent = modelIssue ? fmtTime(modelIssue) : "";
  $("mapCloud").textContent = clockWib(meta.latest_slots.himawari);
  $("mapRain").textContent = clockWib(latestObs);
  $("mapBmkg").textContent = clockWib(meta.latest_slots.bmkg);
  if (!data) return;
  const processed = data.processed_vs_stored;
  $("procN").textContent = compactFormat.format(processed.imerg.values_processed + processed.mergir.values_processed);
  $("mapDisk").textContent = size(data.storage_total_bytes);
}

function getShare(values, predicate) {
  const present = values?.filter(value => value != null);
  return present?.length ? present.reduce((sum, value) => sum + predicate(value), 0) / present.length : null;
}

function renderOverview(ranking) {
  const obs = frameRow("obs", obsFrame);
  const coldTime = [...(frameData.get("cold")?.keys() ?? [])].findLast(time => time <= tms(issueFrame));
  const cold = frameData.get("cold")?.get(coldTime);
  const forecast = frameRow("forecast", issueFrame);
  const tally = (values, limit) => values?.some(value => value != null)
    ? count(values.filter(value => value != null && value >= limit).length) : blank;
  const alertCounts = [1, 2, 3].map(rank => alertData?.alerts.filter(row => row.rank === rank).length);
  $("ovTime").textContent = modelIssue ? fmtTime(modelIssue) + " · latest model issue" : "No model issue archived";
  tiles("ovTiles", [
    ["Under cold cloud", tally(cold, 25), cold ? `of ${count(cold.filter(value => value != null).length)} reported · ${clockWib(coldTime)}` : "No cold-cloud frames archived", "yellow"],
    ["Rain likely next 3 h", tally(forecast, 60), "our model ≥ 60%", "blue"],
    ["Alerts", count(alertData?.alerts.length), alertData ? `${alertCounts[2]} Awas · ${alertCounts[1]} Siaga · ${alertCounts[0]} Waspada` : "Alert data unavailable", "red"],
    [`Raining${obsFrame >= 0 ? " at " + clock(tms(obsFrame)) : ""}`, tally(obs, 5), "IMERG, the latest slot in", "green"],
  ]);
  const indices = Array.from({ length: frameCount }, (_, index) => index);
  const series = [
    { values: indices.map(index => getShare(frameRow("forecast", index), value => value / 100)), color: "var(--blue)", dash: "4 3" },
    { values: indices.map(index => getShare(frameRow("cold", index), value => value >= 25)), color: "var(--fg)", width: 1.5 },
    { values: indices.map(index => getShare(frameRow("obs", index), value => value >= 5)), color: "var(--aqua)", width: 2.2 },
  ];
  const hi = Math.max(0.3, Math.ceil(Math.max(...series.flatMap(line => line.values)) * 10) / 10);
  lineChart($("ovShare"), 760, 230, series, {
    n: frameCount, hi, yt: Array.from({ length: Math.round(hi * 10) + 1 }, (_, i) => i / 10), format: pct, xt: ticks,
    marks: [[obsFrame, "IMERG"], [issueFrame, "issue"]].filter(([index]) => index >= 0),
  });
  $("freshTime").textContent = clockWib(meta.as_of);
  $("fresh").innerHTML = ["himawari", "bmkg", "imerg", "metar", "model"].map(key => `
    <span>${sources[key]}</span><span class="mono note">${clockWib(meta.latest_slots[key])}</span>
    <span class="${key === "imerg" ? "lag" : "note"}">${minutes(meta.freshness_minutes[key])}</span>`).join("");
  const rows = islands.map(island => ({
    label: island, values: [[getShare(areas.filter(area => area.island === island)
      .map(area => forecast?.[area.area_id]), value => value >= 60), "var(--blue)"]],
  }));
  if (rows.some(row => row.values[0][0] != null)) {
    barsH($("ovIsland"), 460, 230, rows, { max: Math.max(0.2, ...rows.map(row => row.values[0][0])), format: pct });
  } else chartNote($("ovIsland"), "No island forecasts archived.");
  const top = (ranking?.alerts ?? []).filter(row => row.prob_pct[3] != null)
    .sort((a, b) => b.prob_pct[3] - a.prob_pct[3]).slice(0, 10);
  $("ovTop").innerHTML = top.map(row => `
    <tr><td><button class="linkbtn" data-open="${row.area_id}">${escape(areaById.get(row.area_id)?.label ?? row.name)}</button></td>
    <td class="note">${escape(row.island)}</td><td class="mono"><span class="bar" style="width:${row.prob_pct[3] / 2}px"></span>${percent(row.prob_pct[3])}</td>
    <td class="num">${temp(row.tb_min)}</td><td><span class="chip">${bmkgText(row.bmkg)}</span></td></tr>`
  ).join("") || emptyRow(5, "No ranked forecasts available.");
}

function renderAlerts() {
  const q = $("alQ").value.trim().toLowerCase();
  const island = $("alIsland").value;
  const province = $("alProvince").value;
  const minimum = Number($("alLvl").value);
  const onlyWatch = $("alWatch").checked;
  const rows = (alertData?.alerts ?? []).filter(row => {
    const label = areaById.get(row.area_id)?.label ?? row.name ?? "";
    return row.rank >= minimum && (!island || row.island === island) && (!province || row.province_code === province)
      && (!q || label.toLowerCase().includes(q)) && (!onlyWatch || watch.has(row.area_id));
  });
  $("alCount").textContent = count(rows.length) + (rows.length === 1 ? " area" : " areas");
  $("alTime").textContent = alertData?.issue_slot ? `Issued ${fmtTime(alertData.issue_slot)}` : "No alert issue available";
  $("alBody").innerHTML = rows.map(row => {
    const label = escape(areaById.get(row.area_id)?.label ?? row.name);
    return `<tr><td><button class="star" data-star="${row.area_id}" aria-pressed="${watch.has(row.area_id)}" aria-label="Watch ${label}">★</button></td>
      <td><span class="lvl ${row.level.toLowerCase()}">${row.level}</span></td><td>${label}</td><td class="note">${escape(row.island)}</td>
      ${leads.map(lead => `<td class="num">${percent(row.prob_pct[lead])}</td>`).join("")}
      <td class="num">${temp(row.tb_min)}</td><td class="num">${percent(row.cold_pct)}</td>
      <td><span class="chip">${bmkgText(row.bmkg)}</span></td><td><button class="linkbtn" data-open="${row.area_id}">map</button></td></tr>`;
  }).join("") || emptyRow(11, !alertData ? "Alert data unavailable."
    : !alertData.issue_slot ? "No forecast issue archived." : "No areas match. Lower the level or clear the search.");
}

async function loadAlerts() {
  const request = ++alertLoad;
  const data = await read(alertUrl(), "Alerts");
  if (request !== alertLoad) return;
  alertData = data;
  $("alertN").textContent = data?.issue_slot ? count(data.alerts.length) : "";
  document.querySelector("[data-view][aria-current=page]").scrollIntoView({ block: "nearest", inline: "nearest" });
  if (view === "alerts") renderAlerts();
}

// Model, history, pipeline and settings
function renderModel(data) {
  const card = data?.card;
  const lead = card?.leads[3];
  const scores = lead?.metrics.test;
  const model = scores?.model;
  const signed = value => value == null ? blank : (value < 0 ? "−" : "+") + pct(Math.abs(value));
  const period = name => card.data_periods[name].join(" to ");
  const cutoff = lead?.csi_thresholds.probability.model;
  $("mdSplit").textContent = card
    ? `Trained ${period("train")} UTC (${count(lead.rows_per_split.train)} rows). Validated ${period("valid")} UTC. Tested ${period("test")} UTC. Rain threshold ${card.threshold_mm_h} mm/h. Best validation CSI cutoff ${pct(cutoff)}.`
    : "No model card archived.";
  tiles("mdTiles", [
    ["Hit score (CSI)", decimal(model?.["csi_at_0.5"]), `cloud motion ${decimal(scores?.motion?.["csi_at_0.5"])} · same-as-now ${decimal(scores?.persist?.["csi_at_0.5"])}`, "blue"],
    ["Rain caught", pct(model?.["pod_at_0.5"]), `false alarms ${pct(model?.["far_at_0.5"])}`, "green"],
    ["Brier skill", signed(model?.bss), "compared with local climatology", "yellow"],
    ["AUC", decimal(model?.auc), `${count(model?.n)} scored test rows`, "orange"],
  ]);
  const bins = lead?.reliability.test.model.filter(row => row.count > 0) ?? [];
  const calibration = $("mdCal");
  if (bins.length) {
    calibration.replaceChildren();
    const x = value => 42 + value * 394;
    const y = value => 10 + (1 - value) * 256;
    for (const value of [0, 0.25, 0.5, 0.75, 1]) {
      calibration.append(
        svgEl("line", { x1: 42, x2: 436, y1: y(value), y2: y(value), stroke: "var(--line)" }),
        svgEl("text", { x: 36, y: y(value) + 3.5, "text-anchor": "end" }, pct(value)),
        svgEl("text", { x: x(value), y: 280, "text-anchor": "middle" }, pct(value)));
    }
    calibration.append(svgEl("text", { x: 239, y: 296, "text-anchor": "middle" }, "forecast chance"),
      svgEl("line", { x1: x(0), y1: y(0), x2: x(1), y2: y(1), stroke: "var(--gray)", "stroke-dasharray": "4 4" }),
      svgEl("polyline", { points: bins.map(row => `${x(row.mean_predicted)},${y(row.observed_frequency)}`).join(" "),
        fill: "none", stroke: "var(--blue)", "stroke-width": 1.5 }),
      svgEl("text", { x: 48, y: 28 }, "perfect = dashed line · dot size = count"));
    for (const row of bins) calibration.append(svgEl("circle", { cx: x(row.mean_predicted),
      cy: y(row.observed_frequency), r: 3 + Math.min(7, Math.log10(row.count) * 1.6), fill: "var(--blue)", opacity: 0.85 }));
  } else chartNote(calibration, "No calibration data archived.");

  const hourly = data?.live_csi.by_local_hour.filter(row => row.lead_h === 3) ?? [];
  const regional = data?.live_csi.by_island.filter(row => row.lead_h === 3) ?? [];
  $("mdLive").textContent = hourly.length || regional.length ? "" : "No live verification scores for the last 30 days.";
  if (hourly.some(row => row.csi != null)) {
    const series = ["persist", "motion", "model"].map(source => ({
      color: scoreNames[source][1],
      values: Array.from({ length: 24 }, (_, hour) => hourly.find(row => row.source === source && row.hour_local === hour)?.csi),
    }));
    lineChart($("mdHour"), 460, 300, series, { n: 24, hi: 1, yt: [0, 0.25, 0.5, 0.75, 1],
      format: decimal, xt: [[0, "00"], [6, "06"], [12, "12"], [18, "18"], [23, "23"]] });
  } else chartNote($("mdHour"), "No verified hourly CSI yet.");
  const regionRows = islands.map(island => ({ label: island, values: ["model", "motion"].map(source =>
    [regional.find(row => row.island === island && row.source === source)?.csi, scoreNames[source][1]]) }));
  if (regionRows.some(row => row.values.some(([value]) => value != null))) {
    barsH($("mdIsland"), 460, 260, regionRows, { max: 1, format: decimal });
  } else chartNote($("mdIsland"), "No verified island CSI yet.");

  const gains = Object.entries(lead?.feature_importance ?? {}).filter(([, value]) => value > 0);
  gains.sort((a, b) => b[1] - a[1]);
  if (gains.length) {
    const rows = gains.slice(0, 10).map(([label, value]) => ({ label, values: [[value,
      /^(adv|flow)/.test(label) ? "var(--purple)" : "var(--aqua)"]] }));
    barsH($("mdFeat"), 460, 260, rows, { max: gains[0][1] * 1.1, format: pct, left: 150 });
  } else chartNote($("mdFeat"), "No feature importance archived.");
  $("mdRunsNote").textContent = scores?.nomotion
    ? "Brier skill compares each forecast with local climatology."
    : "Brier skill compares each forecast with local climatology. The model card has no separate test without cloud motion.";
  $("mdRuns").innerHTML = Object.entries(scores ?? {}).map(([source, score]) => `
    <tr><td>${scoreNames[source][0]}</td><td class="num">${decimal(score.brier, 3)}</td>
    <td class="num">${signed(score.bss)}</td><td class="num">${decimal(score["csi_at_0.5"])}</td><td class="num">${decimal(score.auc)}</td></tr>`
  ).join("") || emptyRow(5, "No test scores archived.");
  $("mdVersions").innerHTML = (data?.history ?? []).map(row => `
    <tr><td class="mono">${escape(row.version)}</td><td>${fmtTime(row.created_at)}</td>
    ${leads.map(lead => `<td class="num">${signed(row.test_bss[lead])}</td>`).join("")}</tr>`
  ).join("") || emptyRow(5, "No model versions archived.");
}

async function renderHistory() {
  const request = ++historySelection;
  $("hA").value = historyA ?? "";
  $("hB").value = historyB ?? "";
  const ids = [historyA, historyB].filter(id => id != null);
  const history = (await Promise.all(ids.map(id => read(`/api/history/${id}`, "Rain history")))).filter(Boolean);
  if (request !== historySelection) return;
  const primary = history.find(data => data.area_id === historyA);
  const a = areaById.get(historyA);
  const heat = $("hHeat");
  $("hHeatT").textContent = a ? "When it rains in " + a.label : "When it rains";
  $("hCoverage").textContent = primary?.monthly.length
    ? `${count(primary.monthly.reduce((sum, row) => sum + row.slots, 0))} archived slots` : "No archived rain";
  $("hPeriod").textContent = primary?.climatology.length
    ? `Mean wet share · ${primary.timezone} · ${primary.climatology_period.join(" to ")}`
    : "Mean wet share · no climatology available";
  const cells = new Map(primary?.climatology.map(row => [row.month * 24 + row.hour_local, row.wet_pct]));
  heat.replaceChildren();
  const cw = 562 / 24, ch = 236 / 12;
  months.forEach((month, index) => {
    const y = 4 + index * ch;
    heat.append(svgEl("text", { x: 28, y: y + ch / 2 + 3.5, "text-anchor": "end" }, month));
    for (let hour = 0; hour < 24; hour++) {
      const value = cells.get((index + 1) * 24 + hour);
      const rect = svgEl("rect", { x: 34 + hour * cw, y, width: cw - 1, height: ch - 1, rx: 1.5,
        fill: value == null ? "var(--panel2)" : P().fc[Math.min(4, Math.floor(value / 20))] });
      rect.append(svgEl("title", {}, `${month} ${String(hour).padStart(2, "0")}:00 ${primary?.timezone ?? ""} · ${value == null ? "no data" : percent(value) + " mean wet share"}`));
      heat.append(rect);
    }
  });
  for (let hour = 0; hour < 24; hour += 3) {
    heat.append(svgEl("text", { x: 34 + (hour + 0.5) * cw, y: 255, "text-anchor": "middle" },
      String(hour).padStart(2, "0") + "h"));
  }
  $("hHeatNote").textContent = "Uncolored cells have no archived climatology.";
  const years = [...new Set(history.flatMap(data => data.yearly.map(row => row.year)))].sort((a, b) => a - b);
  const historyColor = data => data.area_id === historyA ? "var(--blue)" : "var(--orange)";
  $("hYearLeg").innerHTML = history.map(data =>
    `<span><i style="background:${historyColor(data)}"></i>${escape(areaById.get(data.area_id).label)}</span>`).join("");

  const yearChart = $("hYear");
  if (years.length) {
    yearChart.replaceChildren();
    const step = (440 - 46 - 8) / years.length;
    const max = Math.max(1, ...history.flatMap(data => data.yearly.map(row => row.rain_mm))) * 1.1;
    const y = value => 10 + (1 - value / max) * 222;
    for (const value of [0, max / 4, max / 2, max * 0.75, max]) {
      yearChart.append(svgEl("line", { x1: 46, x2: 432, y1: y(value), y2: y(value), stroke: "var(--line)" }),
        svgEl("text", { x: 40, y: y(value) + 3, "text-anchor": "end" }, count(Math.round(value))));
    }
    years.forEach((year, index) => {
      const width = Math.min(64, step * 0.7 / history.length);
      const center = 46 + (index + 0.5) * step;
      history.forEach((data, series) => {
        const row = data.yearly.find(row => row.year === year);
        if (!row) return;
        const rect = svgEl("rect", { x: center + (series - history.length / 2) * width, y: y(row.rain_mm),
          width: width - 2, height: y(0) - y(row.rain_mm), rx: 2, fill: historyColor(data) });
        rect.append(svgEl("title", {}, `${decimal(row.rain_mm, 1)} mm over ${count(row.slots)} slots`));
        yearChart.append(rect);
      });
      yearChart.append(svgEl("text", { x: center, y: 250, "text-anchor": "middle" }, year));
    });
  } else chartNote(yearChart, "No yearly totals archived.");

  const season = $("hSeason");
  const hasSeason = history.some(data => data.wet_season.some(row => row.onset_month != null));
  if (hasSeason) {
    season.replaceChildren();
    const x = month => 65 + month / 12 * 1115;
    const barHeight = 174 / years.length;
    for (let index = 0; index < 12; index++) {
      season.append(svgEl("line", { x1: x(index), x2: x(index), y1: 8, y2: 190, stroke: "var(--line)" }),
        svgEl("text", { x: x(index) + 4, y: 210 }, months[(index + 8) % 12]));
    }
    years.forEach((year, index) => {
      const top = 8 + index * barHeight;
      season.append(svgEl("text", { x: 58, y: top + barHeight / 2 + 3.5, "text-anchor": "end" },
        `${year}/${String(year + 1).slice(2)}`));
      history.forEach((data, series) => {
        const row = data.wet_season.find(row => row.year === year);
        if (row?.onset_month == null) return;
        const start = row.onset_month - 9;
        const end = row.last_wet_dekad;
        const endMonth = end ? (end.month + 3) % 12 + end.dekad / 3 : start;
        const height = Math.min(22, barHeight / history.length - 3);
        season.append(svgEl("rect", { x: x(start), y: top + barHeight / 2 + (series - history.length / 2) * height,
          width: Math.max(3, x(endMonth) - x(start)), height: height - 1, rx: 2, fill: historyColor(data) }));
      });
    });
  } else chartNote(season, "No wet-season onset in the available archive.");
  $("hRule").textContent = primary?.wet_season_rule ?? "";
  $("hMonths").innerHTML = history.flatMap(data => data.monthly.map(row => `
    <tr><td>${escape(areaById.get(data.area_id).label)}</td><td>${months[row.month - 1]} ${row.year}</td>
    <td class="num">${decimal(row.rain_mm, 1)}</td><td class="num">${count(row.slots)}</td></tr>`
  )).join("") || emptyRow(4, "No monthly totals archived.");
}

function renderPipeline(data) {
  if (!data) return;
  $("runBody").innerHTML = data.dagster_runs.map(row => `
    <tr><td class="mono">${escape(row.job)}</td><td class="mono">${row.start_time == null ? blank : clockWib(row.start_time * 1000)}</td>
    <td class="num">${row.duration_seconds == null ? blank : decimal(row.duration_seconds, 1) + " s"}</td>
    <td><span class="chip" style="color:${row.status === "SUCCESS" ? "var(--green)" : row.status === "FAILURE" ? "var(--red)" : "var(--muted)"}">${escape(row.status.toLowerCase())}</span></td></tr>`
  ).join("") || emptyRow(4, "No Dagster runs archived.");
  const rows = Object.entries(data.storage).sort((a, b) => b[1].bytes - a[1].bytes);
  const top = rows.filter(([, entry]) => entry.bytes > 0).slice(0, 8).map(([label, entry]) =>
    ({ label, values: [[entry.bytes, "var(--aqua)"]] }));
  if (top.length) barsH($("plStore"), 520, 240, top, { max: rows[0][1].bytes, format: size, left: 185, right: 66 });
  else chartNote($("plStore"), "No retained dataset bytes.");
  const processed = data.processed_vs_stored;
  $("plTotals").textContent = `${count(processed.imerg.values_processed)} IMERG values · ${count(processed.mergir.values_processed)} MERGIR values · ${size(processed.silver_bytes)} silver · ${size(data.storage_total_bytes)} total · ${data.storage_budget_gb} GB budget.`;
  $("plCaps").innerHTML = rows.map(([label, entry]) => `
    <tr><td class="mono">${escape(label)}</td><td>${size(entry.bytes)}</td>
    <td>${escape(entry.cap?.keep ?? (entry.cap?.keep_versions != null ? entry.cap.keep_versions + " versions" : blank))}</td></tr>`
  ).join("");
  $("plLatency").innerHTML = Object.entries(sources).map(([source, label]) => {
    const latency = data.latency_minutes[source];
    const stamp = meta.latest_slots[source];
    return `<tr><td>${label}</td><td>${stamp ? archiveDateFormat.format(new Date(stamp)) + " WIB" : blank}</td><td>${minutes(latency?.last)}</td>
      <td>${minutes(latency?.median_24h)}</td><td>${minutes(latency?.p90_24h)}</td></tr>`;
  }).join("");
  $("plHealth").innerHTML = Object.entries(data.health).map(([name, entry]) => {
    const stamp = entry.last_tick ?? entry.run_at;
    const status = entry.points_failed != null ? `${count(entry.points_failed)} points failed`
      : entry.budget_exceeded ? "Budget exceeded" : "";
    return `<span>${escape(name)}</span><span class="mono note">${stamp ? fmtTime(stamp) : blank}</span><span class="lag">${status}</span>`;
  }).join("");
  $("plBackfill").innerHTML = Object.entries(data.imerg_backfill).sort(([a], [b]) => b.localeCompare(a)).map(([month, entry]) => `
    <tr><td>${escape(month)}</td><td>${count(entry.slots)}</td><td>${count(entry.expected)}</td>
    <td>${entry.silver_bytes == null ? blank : size(entry.silver_bytes)}</td><td>${entry.done ? "Complete" : "In progress"}</td></tr>`
  ).join("") || emptyRow(5, "No measured backfill progress recorded.");
}

function renderSettings() {
  $("setThr").value = settings.thr;
  $("setZoom").value = settings.zoom;
  $("setCloud").value = settings.cloud;
  $("setGauges").checked = settings.gauges;
  const labels = [...watch].map(id => areaById.get(id)?.label).filter(Boolean);
  $("setWatchD").textContent = labels.length ? labels.join(", ") : "No areas yet. Use ☆ in an area panel or on the Alerts page.";
  $("setGaugeD").textContent = meta?.gauge_stations.length ? `Show ${meta.gauge_stations.length} METAR airports as rings.`
    : "Show METAR airports as rings.";
}

async function renderView(current) {
  if (current === "settings") renderSettings();
  const data = await metaRequest;
  if (view !== current) return;
  if (!data) {
    if (current === "map") $("loading").textContent = "Map data is unavailable.";
    return;
  }
  if (current === "map") {
    renderMapStatus();
    const [pipeline, scores, ranking] = await Promise.all([
      read("/api/pipeline", "Pipeline statistics"), read("/api/scores?scope=national&lead=3", "National scores"),
      read(alertUrl(0), "Latest forecast"), loadFrames("prob1", "prob2", "prob3", "obs", "cold", "bmkg", "disagree"),
      mapSetup ??= initMap(),
    ]);
    if (view !== current) return;
    useLatestProbabilities(ranking);
    renderMapStatus(pipeline);
    if (scores) renderScoreboard(scores);
    if (mapReady) map.resize();
    const id = pendingArea;
    pendingArea = null;
    await Promise.all([
      applyFrame(),
      id != null ? openDrawer(id, true)
        : selected == null && !drawerClosed && innerWidth > 820 && malangId != null ? openDrawer(malangId, false) : null,
    ]);
  } else if (current === "overview") {
    const [ranking] = await Promise.all([
      read(alertUrl(0), "Forecast ranking"), loadFrames("prob1", "prob2", "prob3", "obs", "cold"), initialAlerts,
    ]);
    if (view === current) {
      useLatestProbabilities(ranking);
      renderOverview(ranking);
    }
  } else if (current === "alerts") {
    await initialAlerts;
    renderAlerts();
  } else if (current === "model") {
    renderModel(await read("/api/model", "Model results"));
  } else if (current === "history") {
    await renderHistory();
  } else if (current === "pipeline") {
    renderPipeline(await read("/api/pipeline", "Pipeline statistics"));
  } else if (current === "settings") renderSettings();
}

// Navigation and input events
let drawerClosed = false;
let malangId;
let metaRequest;
let initialAlerts;

async function show() {
  const requested = location.hash.slice(1);
  const current = views.includes(requested) ? requested : "map";
  const nav = ++route;
  view = current;
  for (const name of views) {
    const active = name === current;
    $("view-" + name).hidden = !active;
    const link = document.querySelector(`[data-view="${name}"]`);
    if (active) {
      link.setAttribute("aria-current", "page");
      link.scrollIntoView({ block: "nearest", inline: "nearest" });
    } else link.removeAttribute("aria-current");
  }
  document.documentElement.classList.toggle("is-map", current === "map");
  window.scrollTo(0, 0);
  if (current !== "map") stop();
  const section = $("view-" + current);
  section.setAttribute("aria-busy", "true");
  await renderView(current);
  await initialAlerts;
  if (nav === route) section.setAttribute("aria-busy", "false");
}

function goToArea(id) {
  pendingArea = id;
  frame = issueFrame;
  $("frame").value = frame;
  location.hash = "map";
}

function toggleWatch(id) {
  if (watch.has(id)) watch.delete(id);
  else watch.add(id);
  store.set("watch", [...watch]);
}

function stop() {
  clearInterval(playing);
  playing = null;
  $("play").textContent = "▶";
  $("play").setAttribute("aria-label", "Play");
}

function setFrame(index) {
  frame = index;
  $("frame").value = frame;
  applyFrame();
}

document.addEventListener("click", event => {
  const open = event.target.closest("[data-open]");
  if (open) goToArea(Number(open.dataset.open));
  const star = event.target.closest("[data-star]");
  if (star) {
    toggleWatch(Number(star.dataset.star));
    renderAlerts();
    document.querySelector(`[data-star="${star.dataset.star}"]`)?.focus();
  }
  const theme = event.target.closest("[data-theme-set]");
  if (theme) {
    settings.theme = theme.dataset.themeSet;
    store.set("theme", settings.theme);
    applyTheme();
  }
});

document.querySelectorAll("#layers input").forEach(input => input.addEventListener("change", () => {
  layer = input.value;
  document.querySelectorAll("#layers label").forEach(label => label.classList.toggle("on", label.contains(input)));
  $("raw").disabled = layer !== "ob";
  $("rawWrap").classList.toggle("disabled", layer !== "ob");
  setFrame(layer === "ob" && obsFrame >= 0 ? Math.min(frame, obsFrame) : frame);
}));
document.querySelector("#layers label").classList.add("on");
document.querySelectorAll("[data-z]").forEach(button =>
  button.addEventListener("click", () => zoomTo(button.dataset.z, true)));
$("raw").addEventListener("change", event => {
  raw = event.target.checked;
  paint();
});
$("cloudsOn").addEventListener("change", event => {
  cloudsOn = event.target.checked;
  paint();
  updateWhen();
});
$("frame").addEventListener("input", event => setFrame(Number(event.target.value)));
$("play").addEventListener("click", () => {
  if (!meta) return;
  if (playing) {
    stop();
    return;
  }
  const end = () => layer === "ob" && obsFrame >= 0 ? obsFrame : frameCount - 1;
  if (frame >= end()) setFrame(0);
  $("play").textContent = "❚❚";
  $("play").setAttribute("aria-label", "Pause");
  playing = setInterval(() => {
    if (frame >= end()) stop();
    else setFrame(frame + 1);
  }, 260);
});
$("dClose").addEventListener("click", () => {
  drawerClosed = true;
  selected = null;
  $("drawer").hidden = true;
  $("national").hidden = false;
  map.setFilter("kab-sel", ["==", ["get", "i"], -1]);
});
$("dWatch").addEventListener("click", () => {
  toggleWatch(selected);
  drawDrawer();
});
$("dHist").addEventListener("click", () => {
  historyA = selected;
});
for (const id of ["alQ", "alIsland", "alProvince", "alLvl", "alWatch"]) {
  $(id).addEventListener("input", renderAlerts);
}
$("hA").addEventListener("change", event => {
  historyA = Number(event.target.value);
  if (historyA === historyB) historyB = null;
  renderHistory();
});
$("hB").addEventListener("change", event => {
  const id = event.target.value;
  historyB = id === "" || Number(id) === historyA ? null : Number(id);
  renderHistory();
});
for (const [id, key, event, value] of [
  ["setThr", "thr", "change", input => Number(input.value)],
  ["setZoom", "zoom", "change", input => input.value],
  ["setCloud", "cloud", "input", input => Number(input.value)],
  ["setGauges", "gauges", "change", input => input.checked],
]) {
  $(id).addEventListener(event, async e => {
    settings[key] = value(e.target);
    store.set(key, settings[key]);
    if (key === "thr") await loadAlerts();
    else paint();
  });
}
$("setWatchClear").addEventListener("click", () => {
  watch.clear();
  store.set("watch", []);
  renderSettings();
});
prefersLight.addEventListener("change", () => {
  if (settings.theme === "system") applyTheme();
});
addEventListener("hashchange", show);

applyTheme();
metaRequest = read("/api/meta", "Archive metadata").then(data => {
  if (data) {
    initMeta(data);
    malangId = areas.find(area => area.label === "Kota Malang")?.area_id;
  }
  return data;
});
initialAlerts = loadAlerts();
show();

// New slots land every few minutes; reload the timeline and frames when the archive moves.
setInterval(async () => {
  if (playing || !meta) return;
  requests.clear();
  const data = await read("/api/meta", "Archive metadata");
  if (!data || archiveStamp(data) === archiveStamp(meta)) return;
  const atIssue = frame === issueFrame;
  const shown = tms(frame);
  meta = data;
  setTimeline();
  setFrame(atIssue ? issueFrame : Math.max(0, Math.min(frameCount - 1, Math.round((shown - tms(0)) / halfHour))));
  initialAlerts = loadAlerts();
  renderView(view);
}, 60000);
