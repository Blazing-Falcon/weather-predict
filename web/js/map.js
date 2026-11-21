import { read, renderDataStatus } from "./api.js";
import { $, mapPadding } from "./dom.js";
import { drawDrawer, openDrawer } from "./drawer.js";
import {
  bmkgNames, clock, clockWib, compactFormat, dayClockWib, decimal, emptyRow, escape, fmtTime, pct,
  percent, scoreNames, size,
} from "./format.js";
import { paletteColors, rainClass } from "./palette.js";
import { halfHour, state, store } from "./state.js";
import { forecastWindow, frameRow, frameTime, loadFrames, setTimeline, valueAt } from "./timeline.js";

let raw = false;
let pendingFrames = 0;
let paintedLayer;
let paintedValues;
let hovered;
let hoverPoint;

export function loadMapFrames() {
  const layers = state.settings.replay === 48 ? ["prob1", "prob2", "prob3", "obs", "cold", "bmkg", "disagree"]
    : [...new Set([layerData[state.layer], "obs", ...(state.cloudsOn ? ["cold"] : [])])];
  if (state.layer === "bm") layers.push("bmkg_code", "bmkg_step", "bmkg_age");
  return loadFrames(state.settings.replay, ...layers);
}

const legend = {
  fc: ["Chance of rain", ["under 20%", "20–40%", "40–60%", "60–80%", "80% or more"]],
  ob: ["Rain rate, mm/h (IMERG)", ["dry (under 0.1)", "0.1–1", "1–3", "3–8", "8 or more"]],
  bm: ["BMKG forecast", ["No rain", "Rain"]],
  ds: ["Model vs BMKG", ["Agree", "Disagree"]],
};

export const layerData = { get fc() { return "prob" + state.settings.lead; }, ob: "obs", bm: "bmkg", ds: "disagree" };
const zoomBounds = {
  id: [[94.8, -11.2], [141.2, 6.2]], jawa: [[105.1, -8.9], [116.3, -5.7]],
  jatim: [[110.9, -8.9], [116.3, -6.6]],
};

function colorClass(value) {
  if (value == null) return -1;
  if (state.layer === "fc") return Math.min(4, Math.floor(value / 20));
  if (state.layer === "ob") return rainClass(value / 10);
  if (state.layer === "bm") return Number(value === 100);
  return value;
}

function drawLegend() {
  const [title, keys] = legend[state.layer];
  const window = state.meta && (state.layer === "fc" || state.layer === "ds")
    ? `, ${forecastWindow(state.frame, state.layer === "ds")}` : "";
  $("legTitle").textContent = title + window;
  $("forecastWindow").hidden = state.layer !== "fc";
  $("legend").innerHTML = [...keys, "No data"].map((label, index) =>
    `<div class="row"><span class="sw" style="background:${paletteColors()[state.layer][index] ?? paletteColors().nodata}"></span>${label}</div>`
  ).join("");
  const rgb = paletteColors().cloud.join(",");
  $("cloudRamp").style.background = `linear-gradient(90deg, rgba(${rgb},0), rgba(${rgb},${state.settings.cloud}))`;
  $("cloudLeg").hidden = !state.cloudsOn;
}

export function updateWhen() {
  if (!state.meta) return;
  const elapsed = (state.issueFrame - state.frame) * 30;
  const rel = elapsed === 0 ? (state.modelIssue ? "latest issue" : "now")
    : "−" + (elapsed >= 60 ? `${Math.floor(elapsed / 60)} h` : "")
      + (elapsed % 60 ? ` ${elapsed % 60} min` : "");
  $("frame").setAttribute("aria-valuetext", fmtTime(frameTime(state.frame)));
  $("wT").textContent = fmtTime(frameTime(state.frame));
  const observationPending = state.latestObs == null || frameTime(state.frame) > Date.parse(state.latestObs);
  $("wS").textContent = rel + " · " + (observationPending ? "IMERG pending" : "archive");
  const banner = state.layer === "ob" && observationPending
    ? state.latestObs == null ? "No IMERG observations are archived."
      : `IMERG is available through ${fmtTime(state.latestObs)}.`
    : "";
  $("banner").textContent = banner;
  $("banner").hidden = !banner;
  const row = frameRow(layerData[state.layer], state.frame);
  const note = row?.some(value => value != null) ? ""
    : state.layer === "bm" ? "No BMKG run archived for this time."
      : state.layer === "ds" ? "No forecast comparison archived for this time."
        : state.layer === "fc" ? "No model forecast archived for this time."
          : "No area rain measurements archived for this time.";
  $("layerNote").textContent = note;
  $("layerNote").hidden = !note;
}

const raster = {};
const tileImages = new Map();

function addRaster(name, before) {
  const bounds = state.meta.tile_bounds?.[name];
  if (!bounds) return;
  const canvas = document.createElement("canvas");
  canvas.width = bounds.width;
  canvas.height = bounds.height;
  raster[name] = { canvas, ctx: canvas.getContext("2d", { willReadFrequently: true }), revision: 0 };
  const { west, east, north, south } = bounds;
  state.map.addSource(name, { type: "canvas", canvas, animate: false,
    coordinates: [[west, north], [east, north], [east, south], [west, south]] });
  state.map.addLayer({ id: name, type: "raster", source: name, layout: { visibility: "none" },
    paint: { "raster-fade-duration": 0, "raster-resampling": name === "rain" ? "nearest" : "linear" } }, before);
}

const stem = index => new Date(frameTime(index)).toISOString().replace(/[-:]/g, "").slice(0, 13) + "Z";
const stemTime = scan => Date.parse(scan.replace(/(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)Z/, "$1-$2-$3T$4:$5Z"));

// The latest image at or before the frame, up to 2 h old, so a short gap keeps the last picture.
function tileFor(name, index) {
  if (name === "rain" && (state.latestObs == null || frameTime(index) > Date.parse(state.latestObs))) return null;
  const end = stem(index);
  const scan = state.meta.tile_times[name].findLast(time => time <= end);
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
  const scan = visible ? tileFor(name, state.frame) : null;
  const label = name === "rain" ? "raw rain image" : "cloud image";
  note.textContent = scan ? `No ${label} archived for this time. Showing the one from ${clockWib(stemTime(scan))}.`
    : `No ${label} archived for this time.`;
  note.hidden = !visible || (scan ? scan > stem(state.frame - 1) : name === "rain" && frameTime(state.frame) > Date.parse(state.latestObs));
  if (!scan) {
    state.map.setLayoutProperty(name, "visibility", "none");
    return;
  }
  const next = state.playing && state.frame < state.issueFrame && tileFor(name, state.frame + 1);
  if (next) tileBlob(name, next);
  const appearance = name === "rain" ? paletteColors().ob : paletteColors().cloud;
  const key = [scan, appearance, name === "cloud" ? state.settings.cloud : ""].join(":");
  if (target.key === key) {
    state.map.setLayoutProperty(name, "visibility", "visible");
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
  const colors = paletteColors().ob.map(hex => [
    parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16),
  ]);
  // One RGBA entry per grey level. Both arrays are read as 32-bit words, so byte order matches.
  const lut = new Uint8ClampedArray(256 * 4);
  for (let value = 0; value < 256; value++) {
    const a = Math.min(1, Math.max(0, (value / 255 - 0.28) / 0.55));
    lut.set(name === "cloud" ? [...appearance, Math.round(255 * state.settings.cloud * a * a * (3 - 2 * a))]
      : [...colors[rainClass(value / 10)], value ? 235 : 0], value * 4);
  }
  const entries = new Uint32Array(lut.buffer);
  const words = new Uint32Array(data.buffer);
  for (let i = 0; i < words.length; i++) words[i] = entries[data[i * 4]];
  ctx.putImageData(pixels, 0, 0);
  target.key = key;
  state.map.setLayoutProperty(name, "visibility", "visible");
  const source = state.map.getSource(name);
  source.play();
  state.map.once("render", () => source.pause());
}

export function paint(updateStyle = true) {
  drawLegend();
  if (!state.mapReady) return;
  const showRaw = raw && state.layer === "ob";
  if (updateStyle) {
    const p = paletteColors();
    const expression = ["match", ["coalesce", ["feature-state", "k"], -1]];
    p[state.layer].forEach((color, index) => expression.push(index, color));
    expression.push(p.nodata);
    state.map.setPaintProperty("bg", "background-color", p.sea);
    state.map.setPaintProperty("nb", "fill-color", p.land);
    state.map.setPaintProperty("kab-fill", "fill-color", expression);
    state.map.setPaintProperty("kab-fill", "fill-opacity", showRaw ? 0 : 1);
    state.map.setPaintProperty("kab-line", "line-color", showRaw ? p.fg : p.edge);
    state.map.setPaintProperty("kab-line", "line-opacity", showRaw ? 0.35 : 1);
    state.map.setPaintProperty("kab-sel", "line-color", p.sel);
    state.map.setPaintProperty("kab-hover", "line-color", p.hover);
    state.map.setPaintProperty("gauges", "circle-stroke-color", p.fg);
    state.map.setLayoutProperty("gauges", "visibility", state.settings.gauges ? "visible" : "none");
  }
  return Promise.all([drawRaster("rain", showRaw), drawRaster("cloud", state.cloudsOn)]);
}

export async function applyFrame(playbackStep = false) {
  const values = frameRow(layerData[state.layer], state.frame);
  const layerChanged = state.layer !== paintedLayer;
  if (state.mapReady) {
    if (layerChanged || values !== paintedValues) {
      for (const id of mapAreaIds) state.map.setFeatureState({ source: "kab", id }, { k: colorClass(values?.[id]) });
      paintedLayer = state.layer;
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

function showTip(id, point) {
  const area = state.mapAreas.get(id);
  const value = valueAt(layerData[state.layer], state.frame, id);
  let description = value == null ? "No data for this time" : state.layer === "fc"
    ? `${percent(value)} chance of rain ${forecastWindow(state.frame)}` : state.layer === "ob"
      ? `${(value / 10).toFixed(1)} mm/h` : legend[state.layer][1][colorClass(value)];
  if (state.layer === "bm") {
    const code = valueAt("bmkg_code", state.frame, id);
    const step = valueAt("bmkg_step", state.frame, id);
    const age = valueAt("bmkg_age", state.frame, id);
    description = "No data for this time";
    if (value != null && code != null && step != null && age != null) {
      const slot = frameTime(state.frame);
      const start = slot - step * 60000;
      const hour = 2 * halfHour;
      const run = Math.floor((slot - age * hour) / hour) * hour;
      const names = bmkgNames[code]?.join(" · ") ?? `BMKG code ${code}`;
      description = `${names}<br>BMKG ${clock(start)}–${clockWib(start + 3 * hour)}, run ${dayClockWib(run)}`;
    }
  }
  const cold = valueAt("cold", state.frame, id);
  const tip = $("tip");
  tip.innerHTML = `<b>${escape(area.label)}</b><br>${description}${state.cloudsOn && cold != null ? `<br><span class="note">Cold cloud ${percent(cold)}</span>` : ""}`;
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
  if (state.mapReady) state.map.fitBounds(zoomBounds[name], { padding: mapPadding(), duration: animate ? 700 : 0 });
  document.querySelectorAll("[data-z]").forEach(button => button.setAttribute("aria-pressed", button.dataset.z === name));
}

export async function initMap() {
  const [kab, neighbours] = await Promise.all([
    read("/geo/kabkota.geojson", "Area boundaries"), read("/geo/neighbours.geojson", "Neighbouring boundaries"),
  ]);
  const loading = $("loading");
  if (!window.maplibregl || !kab) {
    loading.textContent = !window.maplibregl ? "The map library could not load." : "Area boundaries are unavailable.";
    return;
  }
  state.mapAreas = new Map(kab.features.map(({ properties: p }) => [p.i,
    state.areaById.get(p.i) ?? { area_id: p.i, label: p.n, centroid_lon: p.x, centroid_lat: p.y },
  ]));
  mapAreaIds = [...state.mapAreas.keys()];

  try {
    state.map = new maplibregl.Map({
      container: "map", style: { version: 8, sources: {}, layers: [
        { id: "bg", type: "background", paint: { "background-color": paletteColors().sea } },
      ] },
      bounds: zoomBounds[state.settings.zoom], fitBoundsOptions: { padding: mapPadding() },
      attributionControl: false, dragRotate: false, pitchWithRotate: false, renderWorldCopies: false,
      minZoom: 1, maxZoom: 12,
    });
  } catch {
    loading.textContent = "The map could not start. The other pages are available.";
    return;
  }
  state.map.touchZoomRotate.disableRotation();
  await new Promise(resolve => state.map.once("load", resolve));
  state.map.on("error", () => {
    state.mapLayerError = true;
    renderDataStatus();
  });
  const features = state.meta.gauge_stations.map(station => ({
    type: "Feature", properties: { icao: station.icao },
    geometry: { type: "Point", coordinates: [station.lon, station.lat] },
  }));
  state.map.addSource("gauges", { type: "geojson", data: { type: "FeatureCollection", features } });
  state.map.addSource("nb", { type: "geojson", data: neighbours ?? { type: "FeatureCollection", features: [] } });
  state.map.addSource("kab", { type: "geojson", data: kab });
  state.map.addLayer({ id: "nb", type: "fill", source: "nb", paint: { "fill-color": paletteColors().land } });
  state.map.addLayer({ id: "kab-fill", type: "fill", source: "kab", paint: { "fill-color": paletteColors().nodata } });
  state.map.addLayer({ id: "kab-line", type: "line", source: "kab",
    paint: { "line-width": ["interpolate", ["linear"], ["zoom"], 4, 0.3, 7, 0.8, 9, 1.4] } });
  addRaster("rain", "kab-line");
  addRaster("cloud");
  for (const [id, width] of [["kab-hover", 1.6], ["kab-sel", 2.6]]) {
    state.map.addLayer({ id, type: "line", source: "kab", filter: ["==", ["get", "i"], -1], paint: { "line-width": width } });
  }
  state.map.addLayer({ id: "gauges", type: "circle", source: "gauges", paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 2.5, 8, 5],
    "circle-opacity": 0, "circle-stroke-width": 1.4,
  } });

  state.map.on("mousemove", "kab-fill", event => {
    const id = event.features[0].properties.i;
    state.map.getCanvas().style.cursor = "pointer";
    if (id !== hovered) state.map.setFilter("kab-hover", ["==", ["get", "i"], id]);
    hovered = id;
    hoverPoint = event.point;
    showTip(id, event.point);
  });
  state.map.on("mouseleave", "kab-fill", () => {
    state.map.getCanvas().style.cursor = "";
    hovered = null;
    state.map.setFilter("kab-hover", ["==", ["get", "i"], -1]);
    $("tip").hidden = true;
  });
  state.map.on("click", "kab-fill", event => openDrawer(event.features[0].properties.i, false, state.map.getCanvas()));
  state.mapReady = true;
  $("areaSearch").disabled = false;
  zoomTo(state.settings.zoom, false);
  loading.hidden = true;
}

export function renderScoreboard(scores) {
  const latest = scores?.findLast(row => row.lead_h === 3)?.day;
  $("national").querySelector(".phead .note").textContent = latest ? `${latest} UTC · +3 h` : "Latest verified day · +3 h";
  $("natBody").innerHTML = latest ? scores.filter(row => row.day === latest && row.lead_h === 3).map(row => {
    const [label, color] = scoreNames[row.source];
    return `<tr><td><span class="key" style="background:${color}"></span>${label}</td>
      <td class="num">${decimal(row.csi)}</td><td class="num">${pct(row.pod)}</td><td class="num">${pct(row.far)}</td></tr>`;
  }).join("") : emptyRow(4, scores ? "No verified national scores yet." : "National scores unavailable.");
}

export function renderMapStatus(data) {
  if (!state.meta) return;
  $("mapIssue").textContent = state.meta.latest_issue_slot ? fmtTime(state.meta.latest_issue_slot) : "";
  $("mapCloud").textContent = clockWib(state.meta.latest_slots.himawari);
  $("mapRain").textContent = clockWib(state.meta.latest_observed_slot);
  $("mapBmkg").textContent = clockWib(state.meta.latest_slots.bmkg);
  if (!data) return;
  const processed = data.processed_vs_stored;
  $("procN").textContent = compactFormat.format(processed.imerg.values_processed + processed.mergir.values_processed);
  $("mapDisk").textContent = size(data.storage_total_bytes);
}

export function stop() {
  clearInterval(state.playing);
  state.playing = null;
  $("play").textContent = "▶";
  $("play").setAttribute("aria-label", "Play");
}

export function setFrame(index, playbackStep = false) {
  state.frame = index;
  $("frame").value = state.frame;
  return applyFrame(playbackStep);
}

export function resetTimeline() {
  const atIssue = state.frame === state.issueFrame;
  const shown = frameTime(state.frame);
  setTimeline();
  return setFrame(atIssue ? state.issueFrame : Math.max(0, Math.min(state.issueFrame, Math.round((shown - state.timelineStart) / halfHour))));
}

async function updateMapFrames() {
  if (!state.meta) return;
  await loadMapFrames();
  if (state.view === "map") await applyFrame();
}

export function wireMap() {
  document.querySelectorAll("#layers input").forEach(input => input.addEventListener("change", () => {
    state.layer = input.value;
    document.querySelectorAll("#layers label").forEach(label => label.classList.toggle("on", label.contains(input)));
    $("raw").disabled = state.layer !== "ob";
    $("rawWrap").classList.toggle("disabled", state.layer !== "ob");
    setFrame(state.layer === "ob" && state.obsFrame >= 0 ? Math.min(state.frame, state.obsFrame) : state.frame);
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
    state.cloudsOn = event.target.checked;
    applyFrame();
    if (state.cloudsOn) updateMapFrames();
  });
  $("replay").value = state.settings.replay;
  $("replay").addEventListener("change", event => {
    stop();
    state.settings.replay = Number(event.target.value);
    store.set("replay", state.settings.replay);
    if (!state.meta) {
      state.frame = state.issueFrame = state.settings.replay * 2;
      return;
    }
    resetTimeline();
    updateMapFrames();
  });
  document.querySelectorAll('input[name="lead"]').forEach(input => {
    input.checked = Number(input.value) === state.settings.lead;
    input.addEventListener("change", () => {
      state.settings.lead = Number(input.value);
      store.set("lead", state.settings.lead);
      applyFrame();
      updateMapFrames();
    });
  });
  $("frame").addEventListener("input", event => setFrame(Number(event.target.value)));
  $("play").addEventListener("click", () => {
    if (!state.meta) return;
    if (state.playing) {
      stop();
      return;
    }
    const end = () => state.layer === "ob" && state.obsFrame >= 0 ? state.obsFrame : state.issueFrame;
    if (state.frame >= end()) setFrame(0);
    $("play").textContent = "❚❚";
    $("play").setAttribute("aria-label", "Pause");
    state.playing = setInterval(() => {
      if (pendingFrames) return;
      if (state.frame >= end()) stop();
      else setFrame(state.frame + 1, true);
    }, { 48: 260, 168: 90, 720: 32 }[state.settings.replay]);
  });
}
