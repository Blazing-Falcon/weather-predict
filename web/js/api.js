import { $ } from "./dom.js";
import { state } from "./state.js";

export function renderDataStatus() {
  const labels = [...state.failedLabels];
  $("dataStatusText").textContent = [
    labels.length ? `${labels.join(", ")} could not load.` : "",
    state.mapLayerError ? "Some map layers could not render." : "",
  ].filter(Boolean).join(" ");
  $("dataStatus").hidden = !labels.length && !state.mapLayerError;
}

export function read(url, label, cache = "default") {
  if (!state.requests.has(url)) {
    const request = fetch(url, { cache, signal: AbortSignal.timeout(20000) }).then(response => {
      if (!response.ok) throw new Error(label);
      return response.json();
    }).then(data => {
      if (state.failedLabels.delete(label)) renderDataStatus();
      return data;
    }).catch(() => {
      if (state.requests.get(url) === request) state.requests.delete(url);
      if (!state.failedLabels.has(label)) {
        state.failedLabels.add(label);
        renderDataStatus();
      }
      return null;
    });
    state.requests.set(url, request);
  }
  return state.requests.get(url);
}

const threshold = () => Math.round(state.settings.thr * 100);

export const alertUrl = (limit = threshold()) => `/api/alerts?threshold=${limit}`;
