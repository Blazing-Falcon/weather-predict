import { read } from "./api.js";
import { chartNote, fitChart } from "./charts.js";
import { $, svgEl } from "./dom.js";
import { count, decimal, emptyRow, escape, months, percent } from "./format.js";
import { paletteColors } from "./palette.js";
import { state } from "./state.js";

let historyB = null;
let historySelection = 0;

export async function renderHistory() {
  const request = ++historySelection;
  $("hA").value = state.historyA ?? "";
  $("hB").value = historyB ?? "";
  const ids = [state.historyA, historyB].filter(id => id != null);
  const history = (await Promise.all(ids.map(id => read(`/api/history/${id}`, "Rain history")))).filter(Boolean);
  if (request !== historySelection) return;
  const primary = history.find(data => data.area_id === state.historyA);
  const a = state.areaById.get(state.historyA);
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
        fill: value == null ? "var(--panel2)" : paletteColors().fc[Math.min(4, Math.floor(value / 20))] });
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
  const historyColor = data => data.area_id === state.historyA ? "var(--blue)" : "var(--orange)";
  $("hYearLeg").innerHTML = history.map(data =>
    `<span><i style="background:${historyColor(data)}"></i>${escape(state.areaById.get(data.area_id).label)}</span>`).join("")
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
    <tr><td>${escape(state.areaById.get(data.area_id).label)}</td><td>${months[row.month - 1]} ${row.year}</td>
    <td class="num">${decimal(row.rain_mm, 1)}</td><td class="num">${count(row.slots)}</td></tr>`
  )).join("") || emptyRow(4, "No monthly totals archived.");
}

export function wireHistory() {
  $("hA").addEventListener("change", event => {
    state.historyA = Number(event.target.value);
    if (state.historyA === historyB) historyB = null;
    renderHistory();
  });
  $("hB").addEventListener("change", event => {
    const id = event.target.value;
    historyB = id === "" || Number(id) === state.historyA ? null : Number(id);
    renderHistory();
  });
}
