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
  lead: store.get("lead", 3),
  replay: store.get("replay", 48),
  alertSource: store.get("alertSource", "model"),
};
const watch = new Set(store.get("watch", []));
const views = ["map", "overview", "alerts", "model", "history", "pipeline", "settings"];
const prefersLight = matchMedia("(prefers-color-scheme: light)");
const halfHour = 30 * 60 * 1000;
const chartFrames = 48 * 2 + 1;
let issueFrame = settings.replay * 2;
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
const dayClockWib = value => `${dayFormat.format(new Date(value))} ${clockWib(value)}`;
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
const bmkgNames = {
  0: ["Cerah", "Sunny"],
  1: ["Cerah", "Sunny"],
  2: ["Cerah Berawan", "Partly Cloudy"],
  3: ["Berawan", "Mostly Cloudy"],
  10: ["Udara Kabur", "Mist/Haze"],
  17: ["Petir", "Thunder"],
  45: ["Kabut/Asap", "Fog/Smoke"],
  61: ["Hujan Ringan", "Light Rain"],
  63: ["Hujan Sedang", "Moderate Rain"],
  65: ["Hujan Lebat", "Heavy Rain"],
  95: ["Hujan Petir", "Thunderstorm"],
};
function bmkgText(record, className = "") {
  if (record?.weather == null) return `<span class="${className}">${blank}</span>`;
  const code = `code ${escape(record.weather)}`;
  const names = bmkgNames[record.weather];
  const title = names ? `${names[1]} · ${code}` : code;
  return `<span class="${className}" title="${title}">${names?.[0] ?? code}</span>`;
}
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
let timelineEnd;
let latestObs;
let obsFrame;
let frame = issueFrame;
let selected = null;
let drawerDetailFailed = false;
let drawerOpener = null;
let historyA = null;
let historyB = null;
let historySelection = 0;
let alertLoad = 0;
let alertData;
let bmkgAlertData;
let bmkgAlertsRequest;
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
let pendingFrames = 0;
let paintedLayer;
let paintedValues;
let hovered;
let hoverPoint;
const areaDetails = new Map();
const frameStamps = new Map();
const requests = new Map();
const failedLabels = new Set();
let mapLayerError = false;

function renderDataStatus() {
  const labels = [...failedLabels];
  $("dataStatusText").textContent = [
    labels.length ? `${labels.join(", ")} could not load.` : "",
    mapLayerError ? "Some map layers could not render." : "",
  ].filter(Boolean).join(" ");
  $("dataStatus").hidden = !labels.length && !mapLayerError;
}

function read(url, label, cache = "default") {
  if (!requests.has(url)) {
    const request = fetch(url, { cache, signal: AbortSignal.timeout(20000) }).then(response => {
      if (!response.ok) throw new Error(label);
      return response.json();
    }).then(data => {
      if (failedLabels.delete(label)) renderDataStatus();
      return data;
    }).catch(() => {
      if (requests.get(url) === request) requests.delete(url);
      if (!failedLabels.has(label)) {
        failedLabels.add(label);
        renderDataStatus();
      }
      return null;
    });
    requests.set(url, request);
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
  malangId = malang?.area_id;
  historyA ??= malang?.area_id ?? areas[0]?.area_id ?? null;
  for (const area of areas.toSorted((a, b) => a.label.localeCompare(b.label))) {
    $("hA").add(new Option(area.label, area.area_id));
    $("hB").add(new Option(area.label, area.area_id));
    $("areaLabels").append(new Option("", area.label));
  }
  for (const island of islands) $("alIsland").add(new Option(island, island));
  const provinces = new Map(areas.filter(a => a.prov_code && a.prov_name).map(a => [a.prov_code, a.prov_name]));
  for (const [code, name] of [...provinces].sort((a, b) => a[1].localeCompare(b[1]))) {
    $("alProvince").add(new Option(name, code));
  }

  setTimeline();
  $("frame").value = frame;
  updateWhen();
  $("play").disabled = false;
}

function timelineTicks(start, hours) {
  const step = hours === 48 ? 24 : hours === 168 ? 48 : 5 * 48;
  const wibSlot = (start / halfHour + 14) % 48;
  const ticks = [];
  for (let index = (48 - wibSlot) % Math.min(step, 48); index <= hours * 2; index += step) {
    const time = start + index * halfHour;
    ticks.push([index, clock(time) === "00:00" ? dayFormat.format(time) : "12:00"]);
  }
  return ticks;
}

function setTimeline() {
  modelIssue = meta.latest_issue_slot;
  timelineEnd = modelIssue ? Date.parse(modelIssue) : Math.floor(Date.parse(meta.as_of) / halfHour) * halfHour;
  issueFrame = settings.replay * 2;
  timelineStart = timelineEnd - issueFrame * halfHour;
  latestObs = meta.latest_observed_slot;
  obsFrame = latestObs == null ? -1 : Math.min(issueFrame, Math.floor(archivedFrame(latestObs)));
  $("frame").max = issueFrame;
  drawTrack();
}

const archiveStamp = data => [
  data.latest_issue_slot, data.latest_observed_slot, data.latest_slots?.himawari, data.latest_slots?.bmkg,
  data.frames_updated,
].join("|");

const frameKey = (name, hours) => name + ":" + hours;

async function loadFrames(hours, ...layers) {
  const end = timelineEnd;
  const stamp = archiveStamp(meta);
  const window = new URLSearchParams({
    from: new Date(end - hours * 2 * halfHour).toISOString(), to: new Date(end).toISOString(),
  });
  await Promise.all(layers.map(async name => {
    const key = frameKey(name, hours);
    const cached = frameStamps.get(key);
    if (cached?.stamp === stamp && cached.end === end) return;
    const data = await read(`/api/frames?layer=${name}&${window}`, `${name} frames`);
    if (!data || stamp !== archiveStamp(meta) || end !== timelineEnd) return;
    const rows = new Map(data.slots.map((slot, index) => [Date.parse(slot), data.values[index]]));
    frameStamps.set(key, { stamp, end, rows });
  }));
}

function loadMapFrames() {
  const layers = settings.replay === 48 ? ["prob1", "prob2", "prob3", "obs", "cold", "bmkg", "disagree"]
    : [layerData[layer], ...(cloudsOn ? ["cold"] : [])];
  if (layer === "bm") layers.push("bmkg_code", "bmkg_step", "bmkg_age");
  return loadFrames(settings.replay, ...layers);
}

function useLatestProbabilities(ranking) {
  if (!ranking?.issue_slot || Date.parse(ranking.issue_slot) !== Date.parse(modelIssue)) return;
  latestProbabilities = new Map(leads.map(lead => {
    const row = [];
    for (const area of ranking.alerts) row[area.area_id] = area.prob_pct[lead];
    return [lead, row];
  }));
}

function forecastWindow(index, comparison = false) {
  const start = tms(index);
  const lead = comparison ? 3 : settings.lead;
  return `${clock(start)}–${clockWib(start + lead * 2 * halfHour)}`;
}

function frameRow(name, index, hours = settings.replay) {
  const row = frameStamps.get(frameKey(name, hours))?.rows.get(tms(index));
  return row ?? (index === issueFrame && name.startsWith("prob") ? latestProbabilities.get(Number(name.at(-1))) : null);
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
  fc: ["Chance of rain", ["under 20%", "20–40%", "40–60%", "60–80%", "80% or more"]],
  ob: ["Rain rate, mm/h (IMERG)", ["dry (under 0.1)", "0.1–1", "1–3", "3–8", "8 or more"]],
  bm: ["BMKG forecast", ["No rain", "Rain"]],
  ds: ["Model vs BMKG", ["Agree", "Disagree"]],
};
const layerData = { get fc() { return "prob" + settings.lead; }, ob: "obs", bm: "bmkg", ds: "disagree" };
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
  const window = meta && (layer === "fc" || layer === "ds")
    ? `, ${forecastWindow(frame, layer === "ds")}` : "";
  $("legTitle").textContent = title + window;
  $("forecastWindow").hidden = layer !== "fc";
  $("legend").innerHTML = [...keys, "No data"].map((label, index) =>
    `<div class="row"><span class="sw" style="background:${P()[layer][index] ?? P().nodata}"></span>${label}</div>`
  ).join("");
  const rgb = P().cloud.join(",");
  $("cloudRamp").style.background = `linear-gradient(90deg, rgba(${rgb},0), rgba(${rgb},${settings.cloud}))`;
  $("cloudLeg").hidden = !cloudsOn;
}

function drawTrack() {
  const pos = index => index / issueFrame * 100;
  const start = pos(Math.max(0, obsFrame));
  $("deco").innerHTML = `
    <div class="gap" style="left:${start}%;right:0"></div>
    ${obsFrame >= 0 && obsFrame !== issueFrame ? `<div class="mk sat" style="left:${start}%"><span>IMERG</span></div>` : ""}
    <div class="mk now"><span>latest</span></div>`;
  $("ticks").innerHTML = timelineTicks(timelineStart, settings.replay)
    .map(([index, label]) => `<span style="left:${pos(index)}%">${label}</span>`).join("");
}

function updateWhen() {
  if (!meta) return;
  const elapsed = (issueFrame - frame) * 30;
  const rel = elapsed === 0 ? (modelIssue ? "latest issue" : "now")
    : "−" + (elapsed >= 60 ? `${Math.floor(elapsed / 60)} h` : "")
      + (elapsed % 60 ? ` ${elapsed % 60} min` : "");
  $("frame").setAttribute("aria-valuetext", fmtTime(tms(frame)));
  $("wT").textContent = fmtTime(tms(frame));
  const observationPending = latestObs == null || tms(frame) > Date.parse(latestObs);
  $("wS").textContent = rel + " · " + (observationPending ? "IMERG pending" : "archive");
  const banner = layer === "ob" && observationPending
    ? latestObs == null ? "No IMERG observations are archived."
      : `IMERG is available through ${fmtTime(latestObs)}.`
    : "";
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
  const end = stem(index);
  const scan = meta.tile_times[name].findLast(time => time <= end);
  return scan > stem(index - 4) ? scan : null;
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
  const next = playing && frame < issueFrame && tileFor(name, frame + 1);
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

function paint(updateStyle = true) {
  drawLegend();
  if (!mapReady) return;
  const showRaw = raw && layer === "ob";
  if (updateStyle) {
    const p = P();
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
  }
  return Promise.all([drawRaster("rain", showRaw), drawRaster("cloud", cloudsOn)]);
}

async function applyFrame(playbackStep = false) {
  const values = frameRow(layerData[layer], frame);
  const layerChanged = layer !== paintedLayer;
  if (mapReady) {
    if (layerChanged || values !== paintedValues) {
      for (const id of mapAreaIds) map.setFeatureState({ source: "kab", id }, { k: colorClass(values?.[id]) });
      paintedLayer = layer;
      paintedValues = values;
    }
    if (hovered != null) showTip(hovered, hoverPoint);
  }
  updateWhen();
  drawDrawer(playbackStep);
  pendingFrames++;
  try {
    await paint(layerChanged);
  } finally {
    pendingFrames--;
  }
}

let mapAreaIds = [];
let mapAreas = new Map();

function showTip(id, point) {
  const area = mapAreas.get(id);
  const value = valueAt(layerData[layer], frame, id);
  let description = value == null ? "No data for this time" : layer === "fc"
    ? `${percent(value)} chance of rain ${forecastWindow(frame)}` : layer === "ob"
      ? `${(value / 10).toFixed(1)} mm/h` : legend[layer][1][colorClass(value)];
  if (layer === "bm") {
    const code = valueAt("bmkg_code", frame, id);
    const step = valueAt("bmkg_step", frame, id);
    const age = valueAt("bmkg_age", frame, id);
    description = "No data for this time";
    if (value != null && code != null && step != null && age != null) {
      const slot = tms(frame);
      const start = slot - step * 60000;
      const hour = 2 * halfHour;
      const run = Math.floor((slot - age * hour) / hour) * hour;
      const names = bmkgNames[code]?.join(" · ") ?? `BMKG code ${code}`;
      description = `${names}<br>BMKG ${clock(start)}–${clockWib(start + 3 * hour)}, run ${dayClockWib(run)}`;
    }
  }
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
    mapLayerError = true;
    renderDataStatus();
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
  map.on("click", "kab-fill", event => openDrawer(event.features[0].properties.i, false, map.getCanvas()));
  mapReady = true;
  $("areaSearch").disabled = false;
  zoomTo(settings.zoom, false);
  loading.hidden = true;
}

// Charts and area drawer
const chartWidths = new WeakMap();
function fitChart(svg) {
  const box = svg.viewBox.baseVal;
  const limit = chartWidths.get(svg) ?? box.width;
  chartWidths.set(svg, limit);
  box.width = Math.min(svg.clientWidth || limit, limit);
  return box.width;
}

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
  marks.forEach(([index, label], i) => {
    svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: top, y2: h - bottom,
      stroke: "var(--muted)", "stroke-dasharray": "3 3" }),
    svgEl("text", { x: x(index) - 4, y: top + 9 + (w < 500 ? i * 13 : 0), "text-anchor": "end" }, label));
  });
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

async function openDrawer(id, zoom, opener) {
  const area = mapAreas.get(id);
  if (!area || !mapReady) return;
  selected = id;
  drawerDetailFailed = false;
  $("drawer").hidden = false;
  $("national").hidden = true;
  map.setFilter("kab-sel", ["==", ["get", "i"], id]);
  if (zoom) {
    const { centroid_lon: x, centroid_lat: y } = area;
    map.fitBounds([[x - 1.2, y - 0.8], [x + 1.2, y + 0.8]], { padding: mapPadding(), duration: 600 });
  }
  drawDrawer();
  if (opener) {
    drawerOpener = opener;
    const narrow = innerWidth <= 820;
    $("dName").focus({ preventScroll: narrow });
    if (narrow) $("drawer").scrollIntoView({ block: "start" });
  }
  if (!areaDetails.has(id)) {
    const stamp = archiveStamp(meta);
    const detail = await read(`/api/areas/${id}`, "Area details", "no-cache");
    if (stamp !== archiveStamp(meta)) return;
    if (detail) areaDetails.set(id, detail);
    if (selected === id) {
      drawerDetailFailed = !detail;
      drawDrawer();
    }
  }
}

function drawDrawer(playbackStep = false) {
  if (selected == null) return;
  const area = mapAreas.get(selected);
  const data = areaDetails.get(selected);
  const cloud = new Map();
  for (const row of data?.cloud ?? []) {
    const index = Math.ceil(archivedFrame(row.slot));
    cloud.set(index, row);
  }
  const rain = new Map((data?.observed ?? []).map(row => [archivedFrame(row.slot), row.rain_mean_x10]));
  const bmkgSlots = new Map((data?.bmkg ?? []).map(row => [archivedFrame(row.slot), row]));
  const bmkg = bmkgSlots.get(frame);
  const predictions = new Map((data?.predictions ?? []).map(row => [row.lead_h + ":" + archivedFrame(row.slot), row.prob_pct]));
  const areaValue = (name, index) => valueAt(name, index, selected) ?? frameRow(name, index, 48)?.[selected];
  const obs = index => rain.get(index) ?? areaValue("obs", index);
  const forecast = (index, lead) => areaValue("prob" + lead, index) ?? predictions.get(lead + ":" + index);
  const model = forecast(frame, settings.lead);
  const modelLabel = `Our model, rain <span style="white-space:nowrap">${forecastWindow(frame)}</span>`;
  const measured = obs(frame);
  const bm = areaValue("bmkg", frame);
  const cloudRow = cloud.get(frame);
  const cold = cloudRow?.cold_pct ?? areaValue("cold", frame);
  $("dName").textContent = area.label;
  $("dIsland").textContent = area.island ?? "kab/kota";
  $("dNow").innerHTML = [
    [modelLabel, percent(model), "var(--blue)"],
    ["BMKG", bmkg?.weather != null ? bmkgText(bmkg) : bm == null ? blank : bm ? "Rain" : "No rain", "var(--orange)"],
    ["Coldest cloud top", temp(cloudRow?.tb_min), "var(--fg0)"],
    ["IMERG rain", measured != null ? `${(measured / 10).toFixed(1)} mm/h`
      : frame > obsFrame ? "not in yet" : blank, "var(--fg0)"],
  ].map(([label, value, color]) => `<div><span class="l">${label}</span><span class="v" style="color:${color}">${value}</span></div>`).join("");

  const svg = $("dChart");
  const first = issueFrame - chartFrames + 1;
  const x = index => 32 + (index - first) * 302 / (chartFrames - 1);
  if (!playbackStep) {
    svg.replaceChildren();
    const indices = Array.from({ length: chartFrames }, (_, index) => first + index);
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
    }), color: "var(--fg)", width: 1.3 }], index => x(first + index), cloudY);
    traces(svg, [{ values: indices.map(index => forecast(index, 3)), color: "var(--blue)" }], index => x(first + index), pY);
    for (const index of indices) {
      const value = obs(index) / 10;
      if (value > 0) {
        const height = Math.max(1, value / rainTop * 40);
        svg.append(svgEl("rect", { x: x(index) - 1, y: 94 - height, width: 2.2, height, fill: P().ob[rainClass(value)] }));
      }
      const archived = areaValue("bmkg", index);
      const areaRain = bmkgSlots.get(index)?.rain;
      const wet = archived ?? (areaRain == null ? null : Number(areaRain));
      if (wet != null) svg.append(svgEl("rect", { x: x(index) - 1.4, y: 172, width: 2.9, height: 11, fill: P().bm[Number(wet > 0)] }));
    }
    for (const [index, color, dash] of [
      [obsFrame, "var(--muted)", "3 3"], [issueFrame, "var(--fg0)", "0"],
    ]) {
      if (index >= first) svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: 5, y2: 187,
        stroke: color, "stroke-dasharray": dash }));
    }
    for (const index of [first, first + 24, first + 48, first + 72, issueFrame]) {
      svg.append(svgEl("text", { x: x(index), y: 207, "text-anchor": index === issueFrame ? "end" : "middle" },
        index === issueFrame ? "issue" : `−${(issueFrame - index) / 2}h`));
    }
    svg.append(svgEl("line", { id: "dCursor", y1: 5, y2: 187, stroke: "var(--yellow)", "stroke-width": 2 }));
  }
  const cursor = $("dCursor");
  cursor.setAttribute("visibility", frame < first ? "hidden" : "visible");
  cursor.setAttribute("x1", x(frame));
  cursor.setAttribute("x2", x(frame));

  const score = data?.scores.rows.findLast(row => row.source === "model" && row.lead_h === 3);
  const month = new Date(tms(frame) + ({ WIB: 7, WITA: 8, WIT: 9 }[area.tz] ?? 0) * 2 * halfHour);
  const clim = data?.climatology.find(row => row.month === month.getUTCMonth() + 1 && row.hour_local === month.getUTCHours());
  const details = data ? [cold != null ? `Cold cloud ${percent(cold)}` : null,
    score ? `${data.scores.scope === "area" ? "Area" : "Province"} CSI ${decimal(score.csi)}` : "No area or province scores yet",
    clim ? `Local usual rain chance ${percent(clim.p3)}` : null,
  ].filter(Boolean).join(" · ") : drawerDetailFailed ? "Area details unavailable." : "Loading area details…";
  $("dDetail").textContent = details;
  $("dWatch").setAttribute("aria-pressed", watch.has(selected));
  $("dWatch").textContent = watch.has(selected) ? "★ Watching" : "☆ Watch";
}

function renderScoreboard(scores) {
  const latest = scores?.findLast(row => row.lead_h === 3)?.day;
  $("national").querySelector(".phead .note").textContent = latest ? `${latest} UTC · +3 h` : "Latest verified day · +3 h";
  $("natBody").innerHTML = latest ? scores.filter(row => row.day === latest && row.lead_h === 3).map(row => {
    const [label, color] = scoreNames[row.source];
    return `<tr><td><span class="key" style="background:${color}"></span>${label}</td>
      <td class="num">${decimal(row.csi)}</td><td class="num">${pct(row.pod)}</td><td class="num">${pct(row.far)}</td></tr>`;
  }).join("") : emptyRow(4, scores ? "No verified national scores yet." : "National scores unavailable.");
}

function renderMapStatus(data) {
  const issueAge = meta?.freshness_minutes.model;
  const live = issueAge != null && issueAge >= 0 && issueAge <= 60;
  $("sourceState").textContent = !meta ? "OFFLINE" : live ? "LIVE" : meta.latest_issue_slot ? "ARCHIVE" : "NO ISSUE";
  $("sourceStatus").classList.toggle("archive", !live);
  if (!meta) return;
  $("mapIssue").textContent = meta.latest_issue_slot ? fmtTime(meta.latest_issue_slot) : "";
  $("mapCloud").textContent = clockWib(meta.latest_slots.himawari);
  $("mapRain").textContent = clockWib(meta.latest_observed_slot);
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

function renderFreshness() {
  $("freshTime").textContent = meta ? clockWib(meta.as_of) : "";
  $("fresh").innerHTML = meta ? ["himawari", "bmkg", "imerg", "metar", "model"].map(key => `
    <span>${sources[key]}</span><span class="mono note">${clockWib(meta.latest_slots[key])}</span>
    <span class="${key === "imerg" ? "lag" : "note"}">${minutes(meta.freshness_minutes[key])}</span>`).join("")
    : '<span class="note">Data freshness unavailable.</span>';
}

function renderOverview(ranking) {
  renderFreshness();
  const share = $("ovShare"), island = $("ovIsland");
  const shareWidth = fitChart(share), islandWidth = fitChart(island);
  if (!meta) {
    $("ovTime").textContent = "Archive metadata unavailable.";
    chartNote(share, "Rain share unavailable.");
    chartNote(island, "Island forecasts unavailable.");
    $("ovTop").innerHTML = emptyRow(5, "Forecast ranking unavailable.");
    return;
  }
  const obs = frameRow("obs", obsFrame, 48);
  const coldRows = frameStamps.get(frameKey("cold", 48))?.rows;
  const coldTime = [...(coldRows?.keys() ?? [])].findLast(time => time <= timelineEnd);
  const cold = coldRows?.get(coldTime);
  const forecast = frameRow("prob3", issueFrame, 48);
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
  const first = issueFrame - chartFrames + 1;
  const indices = Array.from({ length: chartFrames }, (_, index) => first + index);
  const series = [
    { values: indices.map(index => getShare(frameRow("prob3", index, 48), value => value / 100)), color: "var(--blue)" },
    { values: indices.map(index => getShare(frameRow("cold", index, 48), value => value >= 25)), color: "var(--fg)", width: 1.5 },
    { values: indices.map(index => getShare(frameRow("obs", index, 48), value => value >= 5)), color: "var(--aqua)", width: 2.2 },
  ];
  const stamp = archiveStamp(meta);
  const shareFailed = ["prob3", "obs", "cold"].some(name => frameStamps.get(frameKey(name, 48))?.stamp !== stamp);
  const shareValues = series.flatMap(line => line.values);
  if (shareFailed && shareValues.every(value => value == null)) {
    chartNote(share, "Rain share unavailable.");
  } else {
    const hi = Math.max(0.3, Math.ceil(Math.max(...shareValues) * 10) / 10);
    const ticks = timelineTicks(tms(first), 48);
    lineChart(share, shareWidth, 230, series, {
      n: chartFrames, hi, yt: Array.from({ length: Math.round(hi * 10) + 1 }, (_, i) => i / 10), format: pct,
      xt: shareWidth < 500 ? ticks.filter(([, label]) => label !== "12:00") : ticks,
      marks: [[obsFrame - first, "IMERG"], [chartFrames - 1, "issue"]].filter(([index]) => index >= 0),
    });
  }
  const rows = islands.map(island => ({
    label: island, values: [[getShare(areas.filter(area => area.island === island)
      .map(area => forecast?.[area.area_id]), value => value >= 60), "var(--blue)"]],
  }));
  if (rows.some(row => row.values[0][0] != null)) {
    barsH(island, islandWidth, 230, rows, { max: Math.max(0.2, ...rows.map(row => row.values[0][0])), format: pct });
  } else chartNote(island, !ranking && frameStamps.get(frameKey("prob3", 48))?.stamp !== stamp
    ? "Island forecasts unavailable." : "No island forecasts archived.");
  const top = (ranking?.alerts ?? []).filter(row => row.prob_pct[3] != null)
    .sort((a, b) => b.prob_pct[3] - a.prob_pct[3]).slice(0, 10);
  $("ovTop").innerHTML = top.map(row => `
    <tr><td><button class="linkbtn" data-open="${row.area_id}">${escape(areaById.get(row.area_id)?.label ?? row.name)}</button></td>
    <td class="note">${escape(row.island)}</td><td class="mono"><span class="bar" style="width:${row.prob_pct[3] / 2}px"></span>${percent(row.prob_pct[3])}</td>
    <td class="num">${temp(row.tb_min)}</td><td>${bmkgText(row.bmkg, "chip")}</td></tr>`
  ).join("") || emptyRow(5, ranking ? "No ranked forecasts available." : "Forecast ranking unavailable.");
}

const alertSort = $("alSort");
const alertSortHeaders = document.querySelectorAll("[data-alert-sort]");
const alertLabel = row => areaById.get(row.area_id)?.label ?? row.name;

function renderAlerts() {
  const bmkg = settings.alertSource === "bmkg";
  const data = bmkg ? bmkgAlertData : alertData;
  const category = bmkg ? "Category" : "Level";
  $("alLvl").hidden = bmkg;
  $("alModelIntro").hidden = bmkg;
  $("alBmkgIntro").hidden = !bmkg;
  $("alBmkgRun").textContent = data?.bmkg_analysis_time == null ? "" : ` BMKG run ${dayClockWib(data.bmkg_analysis_time)}.`;
  $("alRank").textContent = category;
  $("alBmkgNow").textContent = bmkg ? "BMKG now" : "BMKG";
  $("alRankDesc").textContent = `${category}: high to low`;
  $("alRankAsc").textContent = `${category}: low to high`;
  $("alBody").classList.toggle("bmkg-alerts", bmkg);
  const [key, direction] = (alertSort.value || "rank-descending").split("-");
  for (const button of alertSortHeaders) {
    if (button.dataset.alertSort === key) button.parentElement.setAttribute("aria-sort", direction);
    else button.parentElement.removeAttribute("aria-sort");
  }
  if (!data) {
    const message = data === undefined ? "Loading alerts…" : "Alerts unavailable";
    $("alCount").textContent = message;
    $("alTime").textContent = "";
    $("alBody").innerHTML = emptyRow(11, message);
    return;
  }
  const q = $("alQ").value.trim().toLowerCase();
  const island = $("alIsland").value;
  const province = $("alProvince").value;
  const minimum = Number($("alLvl").value);
  const onlyWatch = $("alWatch").checked;
  const rows = data.alerts.filter(row => {
    const label = alertLabel(row);
    return (bmkg || row.rank >= minimum) && (!island || row.island === island) && (!province || row.province_code === province)
      && (!q || label.toLowerCase().includes(q)) && (!onlyWatch || watch.has(row.area_id));
  });
  if (alertSort.value) {
    const value = row => key === "name" ? alertLabel(row)
      : key === "rank" ? (bmkg ? row.bmkg_3h.weather : row.rank) : key === "cloud" ? row.tb_min : row.prob_pct[key];
    const sign = direction === "ascending" ? 1 : -1;
    rows.sort((a, b) => {
      const left = value(a), right = value(b);
      if (left == null || right == null) return Number(left == null) - Number(right == null);
      return sign * (key === "name" ? left.localeCompare(right, "id") : left - right);
    });
  }
  $("alCount").textContent = count(rows.length) + (rows.length === 1 ? " area" : " areas");
  $("alTime").textContent = data.issue_slot ? `Issued ${fmtTime(data.issue_slot)}` : "No alert issue available";
  $("alBody").innerHTML = rows.map(row => {
    const label = escape(alertLabel(row));
    const chip = bmkg ? bmkgText(row.bmkg_3h, "chip") : `<span class="lvl ${row.level.toLowerCase()}">${row.level}</span>`;
    return `<tr class="al-row"><td><button class="star" data-star="${row.area_id}" aria-pressed="${watch.has(row.area_id)}" aria-label="Watch ${label}">★</button></td>
      <td>${chip}</td><td class="al-name">${label}</td><td class="note al-detail">${escape(row.island)}</td>
      ${leads.map(lead => `<td class="num al-detail" data-lead="+${lead} h">${percent(row.prob_pct[lead])}</td>`).join("")}
      <td class="num al-detail al-cloud">${temp(row.tb_min)}</td><td class="num al-detail al-cold">${percent(row.cold_pct)}</td>
      <td class="al-detail al-bmkg">${bmkgText(row.bmkg, "chip")}</td><td class="al-map"><button class="linkbtn" data-open="${row.area_id}">map</button></td></tr>`;
  }).join("") || emptyRow(11, !data.issue_slot ? "No forecast issue archived."
    : bmkg ? "No areas match." : "No areas match. Lower the level or clear the search.");
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

async function loadBmkgAlerts() {
  const request = bmkgAlertsRequest = read("/api/alerts?source=bmkg", "BMKG alerts");
  const data = await request;
  if (request !== bmkgAlertsRequest) return;
  bmkgAlertData = data;
  if (view === "alerts") renderAlerts();
}

// Model, history, pipeline and settings
function renderModel(data) {
  const calibration = $("mdCal"), hour = $("mdHour"), island = $("mdIsland"), feature = $("mdFeat");
  const calWidth = fitChart(calibration), hourWidth = fitChart(hour);
  const islandWidth = fitChart(island), featureWidth = fitChart(feature);
  if (!data) {
    $("mdSplit").textContent = "Model results unavailable.";
    $("mdTiles").replaceChildren();
    $("mdLive").textContent = "";
    for (const [id, message] of [
      ["mdCal", "Calibration unavailable."], ["mdHour", "Hourly CSI unavailable."],
      ["mdIsland", "Island CSI unavailable."], ["mdFeat", "Feature importance unavailable."],
    ]) chartNote($(id), message);
    $("mdRuns").innerHTML = emptyRow(5, "Test scores unavailable.");
    $("mdRunsNote").textContent = "";
    $("mdVersions").innerHTML = emptyRow(5, "Model versions unavailable.");
    return;
  }
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
  if (bins.length) {
    calibration.replaceChildren();
    const x = value => 42 + value * (calWidth - 66);
    const y = value => 10 + (1 - value) * 256;
    for (const value of [0, 0.25, 0.5, 0.75, 1]) {
      calibration.append(
        svgEl("line", { x1: x(0), x2: x(1), y1: y(value), y2: y(value), stroke: "var(--line)" }),
        svgEl("text", { x: 36, y: y(value) + 3.5, "text-anchor": "end" }, pct(value)),
        svgEl("text", { x: x(value), y: calWidth < 460 ? 282 : 280, "text-anchor": "middle" }, pct(value)));
    }
    calibration.append(svgEl("text", { x: x(0.5), y: 296, "text-anchor": "middle" }, "forecast chance"),
      svgEl("line", { x1: x(0), y1: y(0), x2: x(1), y2: y(1), stroke: "var(--gray)", "stroke-dasharray": "4 4" }),
      svgEl("polyline", { points: bins.map(row => `${x(row.mean_predicted)},${y(row.observed_frequency)}`).join(" "),
        fill: "none", stroke: "var(--blue)", "stroke-width": 1.5 }));
    const notes = calWidth < 460 ? ["perfect = dashed line", "dot size = count"]
      : ["perfect = dashed line · dot size = count"];
    notes.forEach((note, i) => calibration.append(svgEl("text", { x: 48, y: 28 + i * 14 }, note)));
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
    lineChart(hour, hourWidth, 300, series, { n: 24, hi: 1, yt: [0, 0.25, 0.5, 0.75, 1],
      format: decimal, xt: [[0, "00"], [6, "06"], [12, "12"], [18, "18"], [23, "23"]] });
  } else chartNote(hour, "No verified hourly CSI yet.");
  const modelIslands = meta ? islands : [...new Set(regional.map(row => row.island))];
  const regionRows = modelIslands.map(island => ({ label: island, values: ["model", "motion"].map(source =>
    [regional.find(row => row.island === island && row.source === source)?.csi, scoreNames[source][1]]) }));
  if (regionRows.some(row => row.values.some(([value]) => value != null))) {
    barsH(island, islandWidth, 260, regionRows, { max: 1, format: decimal });
  } else chartNote(island, "No verified island CSI yet.");

  const gains = Object.entries(lead?.feature_importance ?? {}).filter(([, value]) => value > 0);
  gains.sort((a, b) => b[1] - a[1]);
  if (gains.length) {
    const rows = gains.slice(0, 10).map(([label, value]) => ({ label, values: [[value,
      /^(adv|flow)/.test(label) ? "var(--purple)" : "var(--aqua)"]] }));
    barsH(feature, featureWidth, 260, rows, { max: gains[0][1] * 1.1, format: pct, left: 150 });
  } else chartNote(feature, "No feature importance archived.");
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
  const heatWidth = fitChart(heat);
  $("hHeatT").textContent = a ? "When it rains in " + a.label : "When it rains";
  $("hCoverage").textContent = primary?.monthly.length
    ? `${count(primary.monthly.reduce((sum, row) => sum + row.slots, 0))} archived slots` : "No archived rain";
  $("hPeriod").textContent = primary?.climatology.length
    ? `Mean wet share · ${primary.timezone} · ${primary.climatology_period.join(" to ")}`
    : "Mean wet share · no climatology available";
  const cells = new Map(primary?.climatology.map(row => [row.month * 24 + row.hour_local, row.wet_pct]));
  heat.replaceChildren();
  const cw = (heatWidth - 38) / 24, ch = 236 / 12;
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
  for (let hour = 0; hour < 24; hour += cw * 3 < 24 ? 6 : 3) {
    heat.append(svgEl("text", { x: 34 + (hour + 0.5) * cw, y: 255, "text-anchor": "middle" },
      String(hour).padStart(2, "0") + "h"));
  }
  const years = [...new Set(history.flatMap(data => data.yearly.map(row => row.year)))].sort((a, b) => a - b);
  const enso = new Map(history.flatMap(data => (data.enso ?? []).map(row => [row.year, row])));
  const historyColor = data => data.area_id === historyA ? "var(--blue)" : "var(--orange)";
  $("hYearLeg").innerHTML = history.map(data =>
    `<span><i style="background:${historyColor(data)}"></i>${escape(areaById.get(data.area_id).label)}</span>`).join("")
    + '<span class="enso-key">EN El Niño · LN La Niña (June to November phase, NOAA RONI)</span>';

  const yearChart = $("hYear");
  const yearWidth = fitChart(yearChart);
  const step = (yearWidth - 54) / Math.max(1, years.length);
  const staggerLabels = step < 26;
  yearChart.viewBox.baseVal.height = staggerLabels ? 296 : 268;
  const phases = [...enso.values()].filter(row => row.phase).sort((a, b) => a.year - b.year);
  yearChart.setAttribute("aria-label", ["Yearly rain totals", ...phases.map(({ year, phase, provisional }) =>
    `${year}: ${phase}${provisional ? " (provisional)" : ""}`)].join(". "));
  if (years.length) {
    yearChart.replaceChildren();
    const max = Math.max(1, ...history.flatMap(data => data.yearly.map(row => row.rain_mm))) * 1.1;
    const y = value => 10 + (1 - value / max) * 222;
    for (const value of [0, max / 4, max / 2, max * 0.75, max]) {
      yearChart.append(svgEl("line", { x1: 46, x2: yearWidth - 8, y1: y(value), y2: y(value), stroke: "var(--line)" }),
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
      const labelY = 246 + (staggerLabels ? (index % 2) * 28 : 0);
      yearChart.append(svgEl("text", { x: center, y: labelY, "text-anchor": "middle" }, year));
      const { phase, provisional } = enso.get(year) ?? {};
      if (phase) {
        const warm = phase === "El Niño";
        const tag = svgEl("text", { x: center, y: labelY + 14, "text-anchor": "middle",
          class: warm ? "enso-tag enso-warm" : "enso-tag" }, (warm ? "EN" : "LN") + (provisional ? "?" : ""));
        tag.append(svgEl("title", {}, `${year}: ${phase} (June to November, NOAA RONI)`));
        yearChart.append(tag);
      }
    });
  } else chartNote(yearChart, "No yearly totals archived.");

  const season = $("hSeason");
  const seasonWidth = fitChart(season);
  const hasSeason = history.some(data => data.wet_season.some(row => row.onset_month != null));
  if (hasSeason) {
    season.replaceChildren();
    const step = (seasonWidth - 85) / 12;
    const x = month => 65 + month * step;
    const barHeight = 174 / years.length;
    for (let index = 0; index < 12; index++) {
      season.append(svgEl("line", { x1: x(index), x2: x(index), y1: 8, y2: 190, stroke: "var(--line)" }));
      if (step >= 24 || index % 2 === 0) {
        season.append(svgEl("text", { x: x(index) + 4, y: 210 }, months[(index + 8) % 12]));
      }
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
  $("hMonths").innerHTML = history.flatMap(data => data.monthly.map(row => `
    <tr><td>${escape(areaById.get(data.area_id).label)}</td><td>${months[row.month - 1]} ${row.year}</td>
    <td class="num">${decimal(row.rain_mm, 1)}</td><td class="num">${count(row.slots)}</td></tr>`
  )).join("") || emptyRow(4, "No monthly totals archived.");
}

function renderPipeline(data) {
  plStorage = data?.storage || {};
  renderPipelineSelection();
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
  const backfill = Object.entries(data.imerg_backfill).sort(([a], [b]) => b.localeCompare(a));
  const backfillSummary = $("plBackfillSummary");
  const backfillContent = $("plBackfillContent");
  backfillContent.replaceChildren();
  if (!backfill.length) {
    backfillSummary.textContent = "No measured backfill progress recorded.";
    backfillSummary.parentElement.open = false;
    return;
  }
  const pending = backfill.filter(([, entry]) => !entry.done).length;
  backfillSummary.textContent = `${count(backfill.length)} ${backfill.length === 1 ? "month" : "months"}, ${backfill.at(-1)[0]} to ${backfill[0][0]}, ${pending ? count(pending) + " in progress" : "all complete"}`;
  backfillContent.append($("plBackfillTable").content.cloneNode(true));
  $("plBackfill").innerHTML = backfill.map(([month, entry]) => `
    <tr><td>${escape(month)}</td><td>${count(entry.slots)}</td><td>${count(entry.expected)}</td>
    <td>${entry.silver_bytes == null ? blank : size(entry.silver_bytes)}</td><td>${entry.done ? "Complete" : "In progress"}</td></tr>`
  ).join("");
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
  await metaRequest;
  if (view !== current) return;
  if (current === "alerts") {
    await (settings.alertSource === "bmkg" ? loadBmkgAlerts() : initialAlerts);
    renderAlerts();
  } else if (current === "model") {
    renderModel(await read("/api/model", "Model results"));
  } else if (!meta) {
    if (current === "map") {
      renderMapStatus();
      renderScoreboard(null);
      $("loading").textContent = "Map data is unavailable.";
    } else if (current === "overview") renderOverview();
  } else if (current === "map") {
    renderMapStatus();
    const [pipeline, scores, ranking] = await Promise.all([
      read("/api/pipeline", "Pipeline statistics"), read("/api/scores?scope=national&lead=3", "National scores"),
      read(alertUrl(0), "Forecast ranking"), loadMapFrames(),
      mapSetup ??= initMap(),
    ]);
    if (view !== current) return;
    useLatestProbabilities(ranking);
    renderMapStatus(pipeline);
    renderScoreboard(scores);
    if (mapReady) map.resize();
    const opener = pendingArea?.opener;
    const id = pendingArea?.id ?? selected ?? (!drawerClosed && innerWidth > 820 ? malangId : null);
    pendingArea = null;
    await Promise.all([
      applyFrame(),
      id != null && (opener || !areaDetails.has(id)) ? openDrawer(id, Boolean(opener), opener) : null,
    ]);
  } else if (current === "overview") {
    const [ranking] = await Promise.all([
      read(alertUrl(0), "Forecast ranking"), loadFrames(48, "prob1", "prob2", "prob3", "obs", "cold"), initialAlerts,
    ]);
    if (view === current) {
      useLatestProbabilities(ranking);
      renderOverview(ranking);
    }
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

async function refreshMeta() {
  const data = await read("/api/meta", "Archive metadata", "no-cache");
  if (!data) return false;
  if (!meta) {
    initMeta(data);
    return true;
  }
  const changed = archiveStamp(data) !== archiveStamp(meta);
  meta = data;
  renderMapStatus();
  if (view === "overview") renderFreshness();
  if (!changed) return false;
  areaDetails.clear();
  latestProbabilities.clear();
  for (const url of requests.keys()) {
    if (url.startsWith("/api/areas/")) requests.delete(url);
  }
  resetTimeline();
  if (selected != null) openDrawer(selected, false);
  return true;
}

async function show() {
  const requested = location.hash.slice(1).split("/")[0];
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

function goToArea(id, opener) {
  pendingArea = { id, opener };
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

function setFrame(index, playbackStep = false) {
  frame = index;
  $("frame").value = frame;
  return applyFrame(playbackStep);
}

function resetTimeline() {
  const atIssue = frame === issueFrame;
  const shown = tms(frame);
  setTimeline();
  return setFrame(atIssue ? issueFrame : Math.max(0, Math.min(issueFrame, Math.round((shown - timelineStart) / halfHour))));
}

async function updateMapFrames() {
  if (!meta) return;
  await loadMapFrames();
  if (view === "map") await applyFrame();
}

document.addEventListener("click", event => {
  const open = event.target.closest("[data-open]");
  if (open) goToArea(Number(open.dataset.open), open);
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
  updateMapFrames();
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
  applyFrame();
  if (cloudsOn) updateMapFrames();
});
$("replay").value = settings.replay;
$("replay").addEventListener("change", event => {
  stop();
  settings.replay = Number(event.target.value);
  store.set("replay", settings.replay);
  if (!meta) {
    frame = issueFrame = settings.replay * 2;
    return;
  }
  resetTimeline();
  updateMapFrames();
});
document.querySelectorAll('input[name="lead"]').forEach(input => {
  input.checked = Number(input.value) === settings.lead;
  input.addEventListener("change", () => {
    settings.lead = Number(input.value);
    store.set("lead", settings.lead);
    applyFrame();
    updateMapFrames();
  });
});
$("frame").addEventListener("input", event => setFrame(Number(event.target.value)));
$("play").addEventListener("click", () => {
  if (!meta) return;
  if (playing) {
    stop();
    return;
  }
  const end = () => layer === "ob" && obsFrame >= 0 ? obsFrame : issueFrame;
  if (frame >= end()) setFrame(0);
  $("play").textContent = "❚❚";
  $("play").setAttribute("aria-label", "Pause");
  playing = setInterval(() => {
    if (pendingFrames) return;
    if (frame >= end()) stop();
    else setFrame(frame + 1, true);
  }, { 48: 260, 168: 90, 720: 32 }[settings.replay]);
});
$("dataRetry").addEventListener("click", async () => {
  $("dataRetry").disabled = true;
  requests.clear();
  mapLayerError = false;
  renderDataStatus();
  if (!meta || failedLabels.has("Archive metadata")) metaRequest = refreshMeta();
  initialAlerts = loadAlerts();
  if (view === "map" && !mapReady) {
    await mapSetup;
    if (!mapReady) mapSetup = undefined;
  }
  await show();
  $("dataRetry").disabled = false;
});
const areaSearch = $("areaSearch");
function findArea(event) {
  const area = areas.find(area => area.label === areaSearch.value);
  if (!area) return;
  event.preventDefault();
  areaSearch.value = "";
  openDrawer(area.area_id, true, areaSearch);
}
areaSearch.addEventListener("change", findArea);
areaSearch.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.isComposing) findArea(event);
});

function closeDrawer() {
  drawerClosed = true;
  selected = null;
  $("drawer").hidden = true;
  $("national").hidden = false;
  map.setFilter("kab-sel", ["==", ["get", "i"], -1]);
  const opener = drawerOpener;
  drawerOpener = null;
  (opener?.isConnected && !opener.closest("[hidden]") ? opener : areaSearch).focus();
}
$("dClose").addEventListener("click", closeDrawer);
document.addEventListener("keydown", event => {
  if (event.defaultPrevented || event.key !== "Escape" || view !== "map" || $("drawer").hidden) return;
  const target = event.target;
  if (target !== areaSearch && target !== map.getCanvas()
      && (!$("drawer").contains(target) || target.matches("input, textarea, [contenteditable]"))) return;
  event.preventDefault();
  closeDrawer();
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
for (const input of document.querySelectorAll('input[name="alertSource"]')) {
  input.checked = input.value === settings.alertSource;
  input.addEventListener("change", () => {
    settings.alertSource = input.value;
    store.set("alertSource", settings.alertSource);
    renderAlerts();
    if (settings.alertSource === "bmkg") loadBmkgAlerts();
  });
}
renderAlerts();
alertSort.addEventListener("change", renderAlerts);
for (const button of alertSortHeaders) {
  button.addEventListener("click", () => {
    const key = button.dataset.alertSort;
    const current = button.parentElement.getAttribute("aria-sort");
    const direction = current ? (current === "descending" ? "ascending" : "descending")
      : key === "name" || key === "cloud" ? "ascending" : "descending";
    alertSort.value = `${key}-${direction}`;
    renderAlerts();
  });
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
let resizeTimer;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (["overview", "model", "history"].includes(view)) renderView(view);
  }, 200);
});

const tabs = document.querySelector("nav.tabs");
const updateTabCue = () => tabs.classList.toggle("more-tabs", tabs.scrollWidth - tabs.clientWidth - tabs.scrollLeft > 1);
tabs.addEventListener("scroll", updateTabCue);
const tabResize = new ResizeObserver(updateTabCue);
tabResize.observe(tabs);
for (const link of tabs.children) tabResize.observe(link);

const plNodes = new Map(PIPELINE.nodes.map(node => [node.id, node]));
const plEdges = new Map(PIPELINE.edges.map(edge => [`${edge.from}~${edge.to}`, edge]));
const plCache = new Map();
let plStorage = {};
const plKey = () => location.hash.split('/').slice(1).join('/');
const plAttrs = (key, title) => `data-pl="${escape(key)}" tabindex="0" role="button" aria-label="${escape(title)}" aria-pressed="${plKey() === key}"`;
const plChip = (key, title) => `<button type="button" class="chip" ${plAttrs(key, title)}>${escape(title)}</button>`;
const plNodeChip = id => plChip(`node/${id}`, plNodes.get(id).title);
const plTable = (heads, rows) => `<div class="scroll-x"><table><thead><tr>${heads.map(h => `<th>${escape(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
const plPos = node => ({x: 24 + node.col * 195, y: 80 + node.row * 84, h: (node.rows || 1) * 84 - 30});
function drawPipeline() {
  let svg = `<svg class="dg" viewBox="0 0 1380 850" aria-label="Pipeline diagram"><defs><marker id="plArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="var(--gray)"/></marker></defs>`;
  svg += `<rect class="band-speed" x="12" y="47" width="384" height="356" rx="8"/><text class="bandl" x="24" y="66">LIVE</text><rect class="band-batch" x="12" y="467" width="384" height="356" rx="8"/><text class="bandl" x="24" y="486">HISTORY</text>`;
  svg += PIPELINE.columns.map((col, i) => `<text class="col" x="${24 + i * 195}" y="28" ${plAttrs(`col/${col.id}`, col.title)}>${escape(col.title)}</text>`).join('');
  const busX = plPos(plNodes.get('srv_api')).x - 12;
  const busY = 80 + 27;
  const joins = [];
  const tracks = new Map(), gutters = new Map();
  const routeGap = (col, y1, y2) => {
    const lanes = tracks.get(col) || [];
    const low = Math.min(y1, y2), high = Math.max(y1, y2);
    let lane = lanes.findIndex(spans => spans.every(([a, b]) => high < a || low > b));
    if (lane < 0) lane = lanes.length;
    (lanes[lane] ||= []).push([low, high]);
    tracks.set(col, lanes);
    return 24 + col * 195 + 165 + 4 + lane * 3;
  };
  svg += [...plEdges].map(([id, edge]) => {
    const from = plNodes.get(edge.from), to = plNodes.get(edge.to), a = plPos(from), b = plPos(to);
    const y = a.y + 27, endY = b.y + 27;
    const start = a.x + 165;
    let d;
    if (to.id === 'srv_api') {
      const gutter = a.y + a.h + 6 + (gutters.get(from.row) || 0) * 5;
      gutters.set(from.row, (gutters.get(from.row) || 0) + 1);
      const gap = routeGap(from.col, y, gutter);
      d = `M${start} ${y}H${gap}V${gutter}H${busX}`;
      joins.push(gutter);
    } else if (to.col === from.col && Math.abs(to.row - from.row) === 1) {
      const down = to.row > from.row;
      d = `M${a.x + 82.5} ${down ? a.y + a.h : a.y}V${down ? b.y : b.y + b.h}`;
    } else if (to.col === from.col) {
      const side = routeGap(from.col - 1, y, endY);
      d = `M${a.x} ${y}H${side}V${endY}H${b.x}`;
    } else if (to.col < from.col) {
      const side = routeGap(from.col - 1, y, endY);
      d = `M${a.x} ${y}H${side}V${endY}H${b.x + 165}`;
    } else if (to.col === from.col + 1) {
      const gap = routeGap(from.col, y, endY);
      d = `M${start} ${y}H${gap}V${endY}H${b.x}`;
    } else {
      const gutter = a.y + a.h + 6 + (gutters.get(from.row) || 0) * 5;
      gutters.set(from.row, (gutters.get(from.row) || 0) + 1);
      const gap = routeGap(from.col, y, gutter);
      const endGap = routeGap(to.col - 1, gutter, endY);
      d = `M${start} ${y}H${gap}V${gutter}H${endGap}V${endY}H${b.x}`;
    }
    return `<g class="pl-edge" ${plAttrs(`edge/${id}`, `${from.title} → ${to.title}: ${edge.label}`)}><title>${escape(edge.label)}</title><path class="edge" d="${d}" ${to.id === 'srv_api' ? '' : 'marker-end="url(#plArrow)"'}/><path class="pl-hit" d="${d}"/></g>`;
  }).join('');
  svg += `<path class="edge pl-bus" ${plAttrs('node/srv_api', 'Web app')} d="M${busX} ${Math.max(...joins)}V${busY}H${busX + 12}" marker-end="url(#plArrow)"/>`;
  svg += PIPELINE.nodes.map(node => {
    const {x, y, h} = plPos(node);
    const words = node.sub.split(' '), lines = [''];
    for (const word of words) {
      if ((lines[lines.length - 1] + word).length > 25) lines.push('');
      lines[lines.length - 1] += (lines[lines.length - 1] ? ' ' : '') + word;
    }
    return `<g class="pl-node" ${plAttrs(`node/${node.id}`, node.title)}><rect class="box" x="${x}" y="${y}" width="165" height="${h}" rx="5"/><text class="t" x="${x + 9}" y="${y + 20}">${escape(node.title)}</text>${lines.map((line, i) => `<text class="s" x="${x + 9}" y="${y + 35 + i * 12}">${escape(line)}</text>`).join('')}${node.id === 'srv_api' ? views.map((page, i) => `<text class="s" x="${x + 9}" y="${y + 65 + i * 20}">${page}</text>`).join('') : ''}</g>`;
  }).join('');
  $('plDiagram').innerHTML = svg + '</svg>';
  $('plLanes').innerHTML = plChip('', 'Whole pipeline') + PIPELINE.lanes.map(lane => plChip(`lane/${lane.id}`, lane.title)).join('');
  $('plJump').innerHTML = '<option value="">Whole pipeline</option>' + PIPELINE.columns.map((col, i) => `<optgroup label="${escape(col.title)}">${PIPELINE.nodes.filter(n => n.col === i).map(n => `<option value="node/${n.id}">${escape(n.title)}</option>`).join('')}</optgroup>`).join('') + `<optgroup label="Lanes">${PIPELINE.lanes.map(l => `<option value="lane/${l.id}">${escape(l.title)}</option>`).join('')}</optgroup>`;
}
const plTime = value => value ? `${dayFormat.format(new Date(value))}, ${clockWib(value)}` : blank;
const plType = type => type.startsWith('timestamp') ? 'time (UTC)' : type.startsWith('dictionary') || type === 'string' ? 'text' : type === 'bool' ? 'yes/no' : type;
function plValue(value, key = '', tz = 'WIB') {
  if (value == null) return value;
  if (Array.isArray(value)) return value.map(item => plValue(item, '', tz));
  if (typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, plValue(v, k, tz)]));
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:/.test(value)) {
    return plTime(key === 'local_datetime' ? value.replace(' ', 'T') + {WIB: '+07:00', WITA: '+08:00', WIT: '+09:00'}[tz] : value);
  }
  return value;
}
function plShape(node, compact = false) {
  const data = plCache.get(node.id)?.data || plStorage[node.dataset];
  if (node.id === 'gd_features') return `<p>${escape(node.note)}</p>`;
  if (node.example) return `<pre>${escape(node.example.replace(/\d{4}-\d{2}-\d{2}T[\d:]+Z/g, value => plTime(value)))}</pre>`;
  if (!node.dataset) return '';
  const columns = data?.columns?.length ? data.columns : Object.keys(node.fields || {}).map(name => ({name, type: ''}));
  if (!columns.length) return '';
  if (compact) return `<dl class="pl-shape">${columns.map(col => `<dt>${escape(col.name)}</dt><dd>${escape(plType(col.type))}</dd>`).join('')}</dl>`;
  return plTable(['Name', 'Type', 'Meaning'], columns.map(col => [escape(col.name), escape(plType(col.type)), escape(node.fields?.[col.name] || '')]));
}
function plNodeBody(node, data, status = '') {
  const incoming = PIPELINE.edges.filter(e => e.to === node.id), outgoing = PIPELINE.edges.filter(e => e.from === node.id);
  const storage = plStorage[node.dataset];
  const cap = storage?.cap, newest = data?.newest || data?.files?.[0]?.modified || data?.live?.created_at || storage?.newest;
  const shape = plShape(node);
  let current = '';
  if (data?.sample?.length) current = '<div class="pl-sample">' + plTable(data.columns.map(c => c.name), data.sample.map(row => data.columns.map(c => {
    const value = plValue(row[c.name], c.name) ?? '';
    const area = c.name === 'area_id' && areaById.get(Number(value));
    return escape(String(value) + (area ? ` · ${area.geo_name}` : ''));
  }))) + '</div>';
  if (data?.files) current = '<div class="pl-files">' + plTable(['Name', 'Bytes', 'Time WIB'], data.files.map(f => [escape(f.name), count(f.bytes), escape(plTime(f.modified))])) + `</div><p>${count(data.file_count)} files</p>`;
  if (data?.json_preview) current += `<pre>${escape(JSON.stringify(plValue(data.json_preview, '', areaById.get(data.json_preview.area_id)?.tz), null, 2))}</pre>`;
  if (data?.versions) current = `<p>Live version: ${escape(data.live.version || blank)} · ${escape(plTime(data.live.created_at))}</p>` + plTable(['Lead (h)', 'Test Brier skill'], Object.entries(data.test_bss).map(([lead, skill]) => [escape(lead), decimal(skill, 3)]));
  return `<h2>${plNodeChip(node.id)} <span class="chip">${escape(node.kind)}</span></h2>
    <div class="pl-facts"><span>${escape(node.sub)}</span>${cap?.keep ? `<span>keep ${escape(cap.keep)}</span>` : cap?.keep_versions ? `<span>keep ${cap.keep_versions} versions</span>` : ''}${storage ? `<span>${size(storage.bytes)}</span>` : ''}${newest ? `<span>newest ${escape(plTime(newest))}</span>` : ''}${data?.rows != null ? `<span>${count(data.rows)} rows</span>` : ''}${node.command ? `<code>${escape(node.command)}</code>` : ''}</div>
    <div class="pl-detail"><div><p>${escape(node.summary)}</p>
    ${node.from || incoming.length ? `<h3>Comes from</h3>${node.from ? `<p class="mono">${escape(node.from)}</p>` : ''}<div class="pl-chips">${incoming.map(e => plNodeChip(e.from)).join('')}</div>` : ''}
    ${node.how?.length || incoming.some(e => e.do) ? `<h3>Steps</h3>${node.how ? `<ol>${node.how.map(step => `<li>${escape(step)}</li>`).join('')}</ol>` : ''}${incoming.filter(e => e.do).map(e => `<p>On the way in: ${plChip(`edge/${e.from}~${e.to}`, e.label)}: ${escape(e.do)}</p>`).join('')}` : ''}
    ${node.note && node.id !== 'gd_features' ? `<p>${escape(node.note)}</p>` : ''}${outgoing.length ? `<h3>Goes to</h3><div class="pl-chips">${outgoing.map(e => plNodeChip(e.to)).join('')}</div>` : ''}</div>
    <div>${shape ? `<h3>Data shape</h3>${shape}` : ''}${status || current ? `<h3>Current data</h3>${status}${current}` : ''}</div></div>`;
}
async function renderPipelineSelection() {
  const key = plKey(), [kind, id] = key.split('/');
  const node = kind === 'node' && plNodes.get(id), edge = kind === 'edge' && plEdges.get(id);
  const lane = kind === 'lane' && PIPELINE.lanes.find(l => l.id === id);
  const col = kind === 'col' && PIPELINE.columns.find(c => c.id === id);
  const active = !!(node || edge || lane || col);
  let nodes = new Set();
  if (node) {
    nodes.add(id);
    PIPELINE.edges.filter(e => e.from === id || e.to === id).forEach(e => { nodes.add(e.from); nodes.add(e.to); });
  } else if (edge) nodes = new Set([edge.from, edge.to]);
  else if (lane) nodes = new Set(lane.nodes);
  else if (col) nodes = new Set(PIPELINE.nodes.filter(n => PIPELINE.columns[n.col] === col).map(n => n.id));
  for (const element of document.querySelectorAll('#plDiagram [data-pl], #plLanes [data-pl]')) {
    const [type, value] = element.dataset.pl.split('/');
    const e = plEdges.get(value);
    const lit = type === 'node' ? nodes.has(value) : type === 'edge' ? (edge ? value === id : node ? e.from === id || e.to === id : nodes.has(e.from) && nodes.has(e.to)) : element.dataset.pl === key;
    element.classList.toggle('pl-dim', active && !lit && !element.closest('#plLanes'));
    element.classList.toggle('pl-lit', active && lit);
    element.setAttribute('aria-pressed', String(element.dataset.pl === key));
  }
  $('plJump').value = node || lane ? key : '';
  $('plOverview').hidden = active;
  $('plExplain').hidden = !active;
  if (!active) return;
  if (matchMedia('(max-width: 899px)').matches) requestAnimationFrame(() => {
    const first = node || plNodes.get(edge ? edge.from : [...nodes][0]);
    const target = $('plDiagram').querySelector(`[data-pl="node/${first.id}"]`);
    const box = target.getBoundingClientRect(), scroller = $('plDiagram');
    scroller.scrollLeft += box.left - scroller.getBoundingClientRect().left - 16;
    $('plExplain').scrollIntoView({block: 'start'});
  });
  if (node) {
    let cached = plCache.get(id);
    $('plExplain').innerHTML = plNodeBody(node, cached?.data, node.dataset ? '<p>Loading current data…</p>' : '');
    if (!node.dataset) return;
    try {
      if (!cached || Date.now() - cached.time >= 60000) {
        const response = await fetch(`/api/pipeline/node/${id}`, {signal: AbortSignal.timeout(10000)});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        cached = {data: await response.json(), time: Date.now()};
        plCache.set(id, cached);
      }
      if (plKey() === key) $('plExplain').innerHTML = plNodeBody(node, cached.data);
    } catch (error) {
      if (plKey() === key) $('plExplain').innerHTML = plNodeBody(node, null, `<p>Current data unavailable: ${escape(error.message)}</p>`);
    }
  } else if (edge) {
    $('plExplain').innerHTML = `<h2>${plNodeChip(edge.from)} → ${plNodeChip(edge.to)}</h2><p>${plChip(key, edge.label)}</p>${edge.do ? `<p>${escape(edge.do)}</p>` : ''}<div class="pl-cards">${['Before', 'After'].map((title, i) => { const n = plNodes.get(i ? edge.to : edge.from); return `<div class="panel" ${plAttrs(`node/${n.id}`, n.title)}><h3>${title}: ${escape(n.title)}</h3>${plShape(n, true)}</div>`; }).join('')}</div>`;
  } else if (lane) {
    $('plExplain').innerHTML = `<h2>${escape(lane.title)}</h2><p>${escape(lane.text)}</p>` + [...plEdges].filter(([, e]) => nodes.has(e.from) && nodes.has(e.to)).sort((a, b) => plNodes.get(a[1].from).col - plNodes.get(b[1].from).col).map(([eid, e]) => `<p>${plChip(`edge/${eid}`, `${plNodes.get(e.from).title} → ${plNodes.get(e.to).title} · ${e.label}`)}${e.do ? `<br>${escape(e.do)}` : ''}</p>`).join('');
  } else {
    $('plExplain').innerHTML = `<h2>${escape(col.title)}</h2><p>${escape(col.text)}</p>` + plTable(['Node', 'How often', 'Size', 'Newest data'], [...nodes].map(nid => { const n = plNodes.get(nid), data = plCache.get(nid)?.data; return [plNodeChip(nid), escape(n.sub), plStorage[n.dataset] ? size(plStorage[n.dataset].bytes) : blank, escape(plTime(data?.newest || data?.files?.[0]?.modified || plStorage[n.dataset]?.newest))]; }));
    $('plExplain').querySelectorAll('tbody tr').forEach(row => { const button = row.querySelector('[data-pl]'); row.dataset.pl = button.dataset.pl; });
  }
}
drawPipeline();
$('view-pipeline').addEventListener('click', event => {
  const target = event.target.closest('[data-pl]');
  if (target) location.hash = 'pipeline' + (target.dataset.pl ? '/' + target.dataset.pl : '');
});
$('view-pipeline').addEventListener('keydown', event => {
  const target = event.target.closest('[data-pl]');
  if (target && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); target.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
});
addEventListener('keydown', event => { if (event.key === 'Escape' && view === 'pipeline') location.hash = 'pipeline'; });
$('plJump').addEventListener('change', event => { location.hash = 'pipeline' + (event.target.value ? '/' + event.target.value : ''); });


applyTheme();
metaRequest = refreshMeta();
initialAlerts = loadAlerts();
show();

// New slots land every few minutes; reload the timeline and frames when the archive moves.
setInterval(async () => {
  if (playing || !meta || $("dataRetry").disabled) return;
  requests.clear();
  if (!await refreshMeta()) return;
  initialAlerts = loadAlerts();
  renderView(view);
}, 60000);
