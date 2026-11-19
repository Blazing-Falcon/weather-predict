import { loadAlerts, loadBmkgAlerts, renderAlerts, wireAlerts } from "./js/alerts.js";
import { alertUrl, read, renderDataStatus } from "./js/api.js";
import { barsH, chartNote, fitChart } from "./js/charts.js";
import { $, svgEl } from "./js/dom.js";
import { drawDrawer, openDrawer, wireDrawer } from "./js/drawer.js";
import {
  archiveDateFormat, areaLabel, blank, clockWib, count, dayFormat, decimal, emptyRow, escape, 
  fmtTime, minutes, months, percent, size, sources,
} from "./js/format.js";
import {
  applyFrame, initMap, layerData, loadMapFrames, paint, renderMapStatus, renderScoreboard, 
  resetTimeline, setFrame, stop, updateWhen, wireMap,
} from "./js/map.js";
import { renderModel } from "./js/model.js";
import { renderFreshness, renderOverview } from "./js/overview.js";
import { paletteColors } from "./js/palette.js";
import { state, store, toggleWatch, views } from "./js/state.js";
import {
  archiveStamp, frameRow, frameTime, loadFrames, setTimeline, useLatestProbabilities,
} from "./js/timeline.js";

let historyB = null;
let historySelection = 0;

async function renderHistory() {
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

function wireHistory() {
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

function applyTheme() {
  if (state.settings.theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = state.settings.theme;
  document.querySelectorAll("[data-theme-set]").forEach(button => {
    button.setAttribute("aria-pressed", button.dataset.themeSet === state.settings.theme);
  });
  paint();
  drawDrawer();
  if (state.view === "history" && state.meta) renderHistory();
}

function renderSettings() {
  $("setThr").value = state.settings.thr;
  $("setZoom").value = state.settings.zoom;
  $("setCloud").value = state.settings.cloud;
  $("setGauges").checked = state.settings.gauges;
  const labels = [...state.watch].map(id => state.areaById.get(id)?.label).filter(Boolean);
  $("setWatchD").textContent = labels.length ? labels.join(", ") : "No areas yet. Use ☆ in an area panel or on the Alerts page.";
  $("setGaugeD").textContent = state.meta?.gauge_stations.length ? `Show ${state.meta.gauge_stations.length} METAR airports as rings.`
    : "Show METAR airports as rings.";
}

function wireSettings() {
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
    const stamp = state.meta.latest_slots[source];
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
    const area = c.name === 'area_id' && state.areaById.get(Number(value));
    return escape(String(value) + (area ? ` · ${area.geo_name}` : ''));
  }))) + '</div>';
  if (data?.files) current = '<div class="pl-files">' + plTable(['Name', 'Bytes', 'Time WIB'], data.files.map(f => [escape(f.name), count(f.bytes), escape(plTime(f.modified))])) + `</div><p>${count(data.file_count)} files</p>`;
  if (data?.json_preview) current += `<pre>${escape(JSON.stringify(plValue(data.json_preview, '', state.areaById.get(data.json_preview.area_id)?.tz), null, 2))}</pre>`;
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

function wirePipeline() {
  drawPipeline();
  $('view-pipeline').addEventListener('click', event => {
    const target = event.target.closest('[data-pl]');
    if (target) location.hash = 'pipeline' + (target.dataset.pl ? '/' + target.dataset.pl : '');
  });
  $('view-pipeline').addEventListener('keydown', event => {
    const target = event.target.closest('[data-pl]');
    if (target && (event.key === 'Enter' || event.key === ' ')) { event.preventDefault(); target.dispatchEvent(new MouseEvent('click', {bubbles: true})); }
  });
  addEventListener('keydown', event => { if (event.key === 'Escape' && state.view === 'pipeline') location.hash = 'pipeline'; });
  $('plJump').addEventListener('change', event => { location.hash = 'pipeline' + (event.target.value ? '/' + event.target.value : ''); });
}

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
