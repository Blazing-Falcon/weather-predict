import { barsH, chartNote, fitChart, lineChart } from "./charts.js";
import { $, tiles } from "./dom.js";
import {
  blank, bmkgText, clock, clockWib, count, emptyRow, escape, fmtTime, minutes, pct, percent, 
  sources, temp,
} from "./format.js";
import { chartFrames, state } from "./state.js";
import { archiveStamp, frameKey, frameRow, frameTime, timelineTicks } from "./timeline.js";

function getShare(values, predicate) {
  const present = values?.filter(value => value != null);
  return present?.length ? present.reduce((sum, value) => sum + predicate(value), 0) / present.length : null;
}

export function renderFreshness() {
  $("freshTime").textContent = state.meta ? clockWib(state.meta.as_of) : "";
  $("fresh").innerHTML = state.meta ? ["himawari", "bmkg", "imerg", "metar", "model"].map(key => `
    <span>${sources[key]}</span><span class="mono note">${clockWib(state.meta.latest_slots[key])}</span>
    <span class="${key === "imerg" ? "lag" : "note"}">${minutes(state.meta.freshness_minutes[key])}</span>`).join("")
    : '<span class="note">Data freshness unavailable.</span>';
}

export function renderOverview(ranking) {
  renderFreshness();
  const share = $("ovShare"), island = $("ovIsland");
  const shareWidth = fitChart(share), islandWidth = fitChart(island);
  if (!state.meta) {
    $("ovTime").textContent = "Archive metadata unavailable.";
    chartNote(share, "Rain share unavailable.");
    chartNote(island, "Island forecasts unavailable.");
    $("ovTop").innerHTML = emptyRow(5, "Forecast ranking unavailable.");
    return;
  }
  const obs = frameRow("obs", state.obsFrame, 48);
  const coldRows = state.frameStamps.get(frameKey("cold", 48))?.rows;
  const coldTime = [...(coldRows?.keys() ?? [])].findLast(time => time <= state.timelineEnd);
  const cold = coldRows?.get(coldTime);
  const forecast = frameRow("prob3", state.issueFrame, 48);
  const tally = (values, limit) => values?.some(value => value != null)
    ? count(values.filter(value => value != null && value >= limit).length) : blank;
  const alertCounts = [1, 2, 3].map(rank => state.alertData?.alerts.filter(row => row.rank === rank).length);
  $("ovTime").textContent = state.modelIssue ? fmtTime(state.modelIssue) + " · latest model issue" : "No model issue archived";
  tiles("ovTiles", [
    ["Under cold cloud", tally(cold, 25), cold ? `of ${count(cold.filter(value => value != null).length)} reported · ${clockWib(coldTime)}` : "No cold-cloud frames archived", "yellow"],
    ["Rain likely next 3 h", tally(forecast, 60), "our model ≥ 60%", "blue"],
    ["Alerts", count(state.alertData?.alerts.length), state.alertData ? `${alertCounts[2]} Awas · ${alertCounts[1]} Siaga · ${alertCounts[0]} Waspada` : "Alert data unavailable", "red"],
    [`Raining${state.obsFrame >= 0 ? " at " + clock(frameTime(state.obsFrame)) : ""}`, tally(obs, 5), "IMERG, the latest slot in", "green"],
  ]);
  const first = state.issueFrame - chartFrames + 1;
  const indices = Array.from({ length: chartFrames }, (_, index) => first + index);
  const series = [
    { values: indices.map(index => getShare(frameRow("prob3", index, 48), value => value / 100)), color: "var(--blue)" },
    { values: indices.map(index => getShare(frameRow("cold", index, 48), value => value >= 25)), color: "var(--fg)", width: 1.5 },
    { values: indices.map(index => getShare(frameRow("obs", index, 48), value => value >= 5)), color: "var(--aqua)", width: 2.2 },
  ];
  const stamp = archiveStamp(state.meta);
  const shareFailed = ["prob3", "obs", "cold"].some(name => state.frameStamps.get(frameKey(name, 48))?.stamp !== stamp);
  const shareValues = series.flatMap(line => line.values);
  if (shareFailed && shareValues.every(value => value == null)) {
    chartNote(share, "Rain share unavailable.");
  } else {
    const hi = Math.max(0.3, Math.ceil(Math.max(...shareValues) * 10) / 10);
    const ticks = timelineTicks(frameTime(first), 48);
    lineChart(share, shareWidth, 230, series, {
      n: chartFrames, hi, yt: Array.from({ length: Math.round(hi * 10) + 1 }, (_, i) => i / 10), format: pct,
      xt: shareWidth < 500 ? ticks.filter(([, label]) => label !== "12:00") : ticks,
      marks: [[state.obsFrame - first, "IMERG"], [chartFrames - 1, "issue"]].filter(([index]) => index >= 0),
    });
  }
  const rows = state.islands.map(island => ({
    label: island, values: [[getShare(state.areas.filter(area => area.island === island)
      .map(area => forecast?.[area.area_id]), value => value >= 60), "var(--blue)"]],
  }));
  if (rows.some(row => row.values[0][0] != null)) {
    barsH(island, islandWidth, 230, rows, { max: Math.max(0.2, ...rows.map(row => row.values[0][0])), format: pct });
  } else chartNote(island, !ranking && state.frameStamps.get(frameKey("prob3", 48))?.stamp !== stamp
    ? "Island forecasts unavailable." : "No island forecasts archived.");
  const top = (ranking?.alerts ?? []).filter(row => row.prob_pct[3] != null)
    .sort((a, b) => b.prob_pct[3] - a.prob_pct[3]).slice(0, 10);
  $("ovTop").innerHTML = top.map(row => `
    <tr><td><button class="linkbtn" data-open="${row.area_id}">${escape(state.areaById.get(row.area_id)?.label ?? row.name)}</button></td>
    <td class="note">${escape(row.island)}</td><td class="mono"><span class="bar" style="width:${row.prob_pct[3] / 2}px"></span>${percent(row.prob_pct[3])}</td>
    <td class="num">${temp(row.tb_min)}</td><td>${bmkgText(row.bmkg, "chip")}</td></tr>`
  ).join("") || emptyRow(5, ranking ? "No ranked forecasts available." : "Forecast ranking unavailable.");
}
