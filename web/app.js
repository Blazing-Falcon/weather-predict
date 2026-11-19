import { loadAlerts, loadBmkgAlerts, renderAlerts, wireAlerts } from "./js/alerts.js";
import { alertUrl, read, renderDataStatus } from "./js/api.js";
import { $ } from "./js/dom.js";
import { openDrawer, wireDrawer } from "./js/drawer.js";
import { areaLabel } from "./js/format.js";
import { renderHistory, wireHistory } from "./js/history.js";
import {
  applyFrame, initMap, layerData, loadMapFrames, renderMapStatus, renderScoreboard, resetTimeline,
  setFrame, stop, updateWhen, wireMap,
} from "./js/map.js";
import { renderModel } from "./js/model.js";
import { renderFreshness, renderOverview } from "./js/overview.js";
import { renderPipeline, wirePipeline } from "./js/pipeline.js";
import { applyTheme, renderSettings, wireSettings } from "./js/settings.js";
import { state, toggleWatch, views } from "./js/state.js";
import {
  archiveStamp, frameRow, frameTime, loadFrames, setTimeline, useLatestProbabilities,
} from "./js/timeline.js";

let route = 0;
let mapSetup;
let pendingArea = null;

function initMeta(data) {
  state.meta = data;
  state.areas = state.meta.areas.map(area => ({ ...area, label: areaLabel(area) }));
  state.areaById = new Map(state.areas.map(area => [area.area_id, area]));
  state.islands = [...new Set(state.areas.map(area => area.island).filter(Boolean))];
  const malang = state.areas.find(area => area.label === "Kota Malang");
  state.malangId = malang?.area_id;
  state.historyA ??= malang?.area_id ?? state.areas[0]?.area_id ?? null;
  for (const area of state.areas.toSorted((a, b) => a.label.localeCompare(b.label))) {
    $("hA").add(new Option(area.label, area.area_id));
    $("hB").add(new Option(area.label, area.area_id));
    $("areaLabels").append(new Option("", area.label));
  }
  for (const island of state.islands) $("alIsland").add(new Option(island, island));
  const provinces = new Map(state.areas.filter(a => a.prov_code && a.prov_name).map(a => [a.prov_code, a.prov_name]));
  for (const [code, name] of [...provinces].sort((a, b) => a[1].localeCompare(b[1]))) {
    $("alProvince").add(new Option(name, code));
  }

  setTimeline();
  $("frame").value = state.frame;
  updateWhen();
  $("play").disabled = false;
}

async function renderView(current) {
  if (current === "settings") renderSettings();
  await metaRequest;
  if (state.view !== current) return;
  if (current === "alerts") {
    await (state.settings.alertSource === "bmkg" ? loadBmkgAlerts() : initialAlerts);
    renderAlerts();
  } else if (current === "model") {
    renderModel(await read("/api/model", "Model results"));
  } else if (!state.meta) {
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
    if (state.view !== current) return;
    useLatestProbabilities(ranking);
    renderMapStatus(pipeline);
    renderScoreboard(scores);
    if (state.mapReady) state.map.resize();
    const opener = pendingArea?.opener;
    const id = pendingArea?.id ?? state.selected ?? (!state.drawerClosed && innerWidth > 820 ? state.malangId : null);
    pendingArea = null;
    await Promise.all([
      applyFrame(),
      id != null && (opener || !state.areaDetails.has(id)) ? openDrawer(id, Boolean(opener), opener) : null,
    ]);
  } else if (current === "overview") {
    const [ranking] = await Promise.all([
      read(alertUrl(0), "Forecast ranking"), loadFrames(48, "prob1", "prob2", "prob3", "obs", "cold"), initialAlerts,
    ]);
    if (state.view === current) {
      useLatestProbabilities(ranking);
      renderOverview(ranking);
    }
  } else if (current === "history") {
    await renderHistory();
  } else if (current === "pipeline") {
    renderPipeline(await read("/api/pipeline", "Pipeline statistics"));
  } else if (current === "settings") renderSettings();
}

let metaRequest;
let initialAlerts;

async function refreshMeta() {
  const data = await read("/api/meta", "Archive metadata", "no-cache");
  if (!data) return false;
  if (!state.meta) {
    initMeta(data);
    return true;
  }
  const changed = archiveStamp(data) !== archiveStamp(state.meta);
  state.meta = data;
  renderMapStatus();
  if (state.view === "overview") renderFreshness();
  if (!changed) return false;
  state.areaDetails.clear();
  state.latestProbabilities.clear();
  for (const url of state.requests.keys()) {
    if (url.startsWith("/api/areas/")) state.requests.delete(url);
  }
  resetTimeline();
  if (state.selected != null) openDrawer(state.selected, false);
  return true;
}

async function show() {
  const requested = location.hash.slice(1).split("/")[0];
  const current = views.includes(requested) ? requested : "map";
  const nav = ++route;
  state.view = current;
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
  state.frame = state.issueFrame;
  $("frame").value = state.frame;
  location.hash = "map";
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

});

wireMap();

$("dataRetry").addEventListener("click", async () => {
  $("dataRetry").disabled = true;
  state.requests.clear();
  state.mapLayerError = false;
  renderDataStatus();
  if (!state.meta || state.failedLabels.has("Archive metadata")) metaRequest = refreshMeta();
  initialAlerts = loadAlerts();
  if (state.view === "map" && !state.mapReady) {
    await mapSetup;
    if (!state.mapReady) mapSetup = undefined;
  }
  await show();
  $("dataRetry").disabled = false;
});

wireDrawer();

wireAlerts();

wireHistory();

wireSettings();

addEventListener("hashchange", show);

let resizeTimer;

addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (["overview", "model", "history"].includes(state.view)) renderView(state.view);
  }, 200);
});

const tabs = document.querySelector("nav.tabs");
const updateTabCue = () => tabs.classList.toggle("more-tabs", tabs.scrollWidth - tabs.clientWidth - tabs.scrollLeft > 1);

tabs.addEventListener("scroll", updateTabCue);

const tabResize = new ResizeObserver(updateTabCue);

tabResize.observe(tabs);

for (const link of tabs.children) tabResize.observe(link);

wirePipeline();

applyTheme();

metaRequest = refreshMeta();

initialAlerts = loadAlerts();

show();

setInterval(async () => {
  if (state.playing || !state.meta || $("dataRetry").disabled) return;
  state.requests.clear();
  if (!await refreshMeta()) return;
  initialAlerts = loadAlerts();
  renderView(state.view);
}, 60000);

// Browser checks use this handle.
window.nowcast = { state, setFrame, applyTheme, layerData, frameRow, frameTime };
