import { loadAlerts } from "./alerts.js";
import { $ } from "./dom.js";
import { drawDrawer } from "./drawer.js";
import { renderHistory } from "./history.js";
import { paint } from "./map.js";
import { state, store } from "./state.js";

export function applyTheme() {
  if (state.settings.theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = state.settings.theme;
  document.querySelectorAll("[data-theme-set]").forEach(button => {
    button.setAttribute("aria-pressed", button.dataset.themeSet === state.settings.theme);
  });
  paint();
  drawDrawer();
  if (state.view === "history" && state.meta) renderHistory();
}

export function renderSettings() {
  $("setThr").value = state.settings.thr;
  $("setZoom").value = state.settings.zoom;
  $("setCloud").value = state.settings.cloud;
  $("setGauges").checked = state.settings.gauges;
  const labels = [...state.watch].map(id => state.areaById.get(id)?.label).filter(Boolean);
  $("setWatchD").textContent = labels.length ? labels.join(", ") : "No areas yet. Use ☆ in an area panel or on the Alerts page.";
  $("setGaugeD").textContent = state.meta?.gauge_stations.length ? `Show ${state.meta.gauge_stations.length} METAR airports as rings.`
    : "Show METAR airports as rings.";
}

export function wireSettings() {
  document.addEventListener("click", event => {
    const theme = event.target.closest("[data-theme-set]");
    if (theme) {
      state.settings.theme = theme.dataset.themeSet;
      store.set("theme", state.settings.theme);
      applyTheme();
    }
  });
  for (const [id, key, event, value] of [
    ["setThr", "thr", "change", input => Number(input.value)],
    ["setZoom", "zoom", "change", input => input.value],
    ["setCloud", "cloud", "input", input => Number(input.value)],
    ["setGauges", "gauges", "change", input => input.checked],
  ]) {
    $(id).addEventListener(event, async e => {
      state.settings[key] = value(e.target);
      store.set(key, state.settings[key]);
      if (key === "thr") await loadAlerts();
      else paint();
    });
  }
  $("setWatchClear").addEventListener("click", () => {
    state.watch.clear();
    store.set("watch", []);
    renderSettings();
  });
  state.prefersLight.addEventListener("change", () => {
    if (state.settings.theme === "system") applyTheme();
  });
}
