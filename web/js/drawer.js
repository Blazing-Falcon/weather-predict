import { read } from "./api.js";
import { traces } from "./charts.js";
import { $, mapPadding, svgEl } from "./dom.js";
import { blank, bmkgText, decimal, percent, temp } from "./format.js";
import { paletteColors, rainClass } from "./palette.js";
import { chartFrames, halfHour, state, toggleWatch } from "./state.js";
import { archiveStamp, archivedFrame, forecastWindow, frameRow, frameTime, valueAt } from "./timeline.js";

let drawerDetailFailed = false;
let drawerOpener = null;

export async function openDrawer(id, zoom, opener) {
  const area = state.mapAreas.get(id);
  if (!area || !state.mapReady) return;
  state.selected = id;
  drawerDetailFailed = false;
  $("drawer").hidden = false;
  $("national").hidden = true;
  state.map.setFilter("kab-sel", ["==", ["get", "i"], id]);
  if (zoom) {
    const { centroid_lon: x, centroid_lat: y } = area;
    state.map.fitBounds([[x - 1.2, y - 0.8], [x + 1.2, y + 0.8]], { padding: mapPadding(), duration: 600 });
  }
  drawDrawer();
  if (opener) {
    drawerOpener = opener;
    const narrow = innerWidth <= 820;
    $("dName").focus({ preventScroll: narrow });
    if (narrow) $("drawer").scrollIntoView({ block: "start" });
  }
  if (!state.areaDetails.has(id)) {
    const stamp = archiveStamp(state.meta);
    const detail = await read(`/api/areas/${id}`, "Area details", "no-cache");
    if (stamp !== archiveStamp(state.meta)) return;
    if (detail) state.areaDetails.set(id, detail);
    if (state.selected === id) {
      drawerDetailFailed = !detail;
      drawDrawer();
    }
  }
}

export function drawDrawer(playbackStep = false) {
  if (state.selected == null) return;
  const area = state.mapAreas.get(state.selected);
  const data = state.areaDetails.get(state.selected);
  const cloud = new Map();
  for (const row of data?.cloud ?? []) {
    const index = Math.ceil(archivedFrame(row.slot));
    cloud.set(index, row);
  }
  const rain = new Map((data?.observed ?? []).map(row => [archivedFrame(row.slot), row.rain_mean_x10]));
  const bmkgSlots = new Map((data?.bmkg ?? []).map(row => [archivedFrame(row.slot), row]));
  const bmkg = bmkgSlots.get(state.frame);
  const predictions = new Map((data?.predictions ?? []).map(row => [row.lead_h + ":" + archivedFrame(row.slot), row.prob_pct]));
  const areaValue = (name, index) => valueAt(name, index, state.selected) ?? frameRow(name, index, 48)?.[state.selected];
  const obs = index => rain.get(index) ?? areaValue("obs", index);
  const forecast = (index, lead) => areaValue("prob" + lead, index) ?? predictions.get(lead + ":" + index);
  const model = forecast(state.frame, state.settings.lead);
  const modelLabel = `Our model, rain <span style="white-space:nowrap">${forecastWindow(state.frame)}</span>`;
  const measured = obs(state.frame);
  const bm = areaValue("bmkg", state.frame);
  const cloudRow = cloud.get(state.frame);
  const cold = cloudRow?.cold_pct ?? areaValue("cold", state.frame);
  $("dName").textContent = area.label;
  $("dIsland").textContent = area.island ?? "kab/kota";
  $("dNow").innerHTML = [
    [modelLabel, percent(model), "var(--blue)"],
    ["BMKG", bmkg?.weather != null ? bmkgText(bmkg) : bm == null ? blank : bm ? "Rain" : "No rain", "var(--orange)"],
    ["Coldest cloud top", temp(cloudRow?.tb_min), "var(--fg0)"],
    ["IMERG rain", measured != null ? `${(measured / 10).toFixed(1)} mm/h`
      : state.frame > state.obsFrame ? "not in yet" : blank, "var(--fg0)"],
  ].map(([label, value, color]) => `<div><span class="l">${label}</span><span class="v" style="color:${color}">${value}</span></div>`).join("");

  const svg = $("dChart");
  const first = state.issueFrame - chartFrames + 1;
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
        svg.append(svgEl("rect", { x: x(index) - 1, y: 94 - height, width: 2.2, height, fill: paletteColors().ob[rainClass(value)] }));
      }
      const archived = areaValue("bmkg", index);
      const areaRain = bmkgSlots.get(index)?.rain;
      const wet = archived ?? (areaRain == null ? null : Number(areaRain));
      if (wet != null) svg.append(svgEl("rect", { x: x(index) - 1.4, y: 172, width: 2.9, height: 11, fill: paletteColors().bm[Number(wet > 0)] }));
    }
    for (const [index, color, dash] of [
      [state.obsFrame, "var(--muted)", "3 3"], [state.issueFrame, "var(--fg0)", "0"],
    ]) {
      if (index >= first) svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: 5, y2: 187,
        stroke: color, "stroke-dasharray": dash }));
    }
    for (const index of [first, first + 24, first + 48, first + 72, state.issueFrame]) {
      svg.append(svgEl("text", { x: x(index), y: 207, "text-anchor": index === state.issueFrame ? "end" : "middle" },
        index === state.issueFrame ? "issue" : `−${(state.issueFrame - index) / 2}h`));
    }
    svg.append(svgEl("line", { id: "dCursor", y1: 5, y2: 187, stroke: "var(--yellow)", "stroke-width": 2 }));
  }
  const cursor = $("dCursor");
  cursor.setAttribute("visibility", state.frame < first ? "hidden" : "visible");
  cursor.setAttribute("x1", x(state.frame));
  cursor.setAttribute("x2", x(state.frame));

  const score = data?.scores.rows.findLast(row => row.source === "model" && row.lead_h === 3);
  const month = new Date(frameTime(state.frame) + ({ WIB: 7, WITA: 8, WIT: 9 }[area.tz] ?? 0) * 2 * halfHour);
  const clim = data?.climatology.find(row => row.month === month.getUTCMonth() + 1 && row.hour_local === month.getUTCHours());
  const details = data ? [cold != null ? `Cold cloud ${percent(cold)}` : null,
    score ? `${data.scores.scope === "area" ? "Area" : "Province"} CSI ${decimal(score.csi)}` : "No area or province scores yet",
    clim ? `Local usual rain chance ${percent(clim.p3)}` : null,
  ].filter(Boolean).join(" · ") : drawerDetailFailed ? "Area details unavailable." : "Loading area details…";
  $("dDetail").textContent = details;
  $("dWatch").setAttribute("aria-pressed", state.watch.has(state.selected));
  $("dWatch").textContent = state.watch.has(state.selected) ? "★ Watching" : "☆ Watch";
}

const areaSearch = $("areaSearch");

function findArea(event) {
  const area = state.areas.find(area => area.label === areaSearch.value);
  if (!area) return;
  event.preventDefault();
  areaSearch.value = "";
  openDrawer(area.area_id, true, areaSearch);
}

export function wireDrawer() {
  areaSearch.addEventListener("change", findArea);
  areaSearch.addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.isComposing) findArea(event);
  });
  function closeDrawer() {
    state.drawerClosed = true;
    state.selected = null;
    $("drawer").hidden = true;
    $("national").hidden = false;
    state.map.setFilter("kab-sel", ["==", ["get", "i"], -1]);
    const opener = drawerOpener;
    drawerOpener = null;
    (opener?.isConnected && !opener.closest("[hidden]") ? opener : areaSearch).focus();
  }
  $("dClose").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", event => {
    if (event.defaultPrevented || event.key !== "Escape" || state.view !== "map" || $("drawer").hidden) return;
    const target = event.target;
    if (target !== areaSearch && target !== state.map.getCanvas()
        && (!$("drawer").contains(target) || target.matches("input, textarea, [contenteditable]"))) return;
    event.preventDefault();
    closeDrawer();
  });
  $("dWatch").addEventListener("click", () => {
    toggleWatch(state.selected);
    drawDrawer();
  });
  $("dHist").addEventListener("click", () => {
    state.historyA = state.selected;
  });
}
