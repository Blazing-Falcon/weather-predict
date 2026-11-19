import { barsH, chartNote, fitChart, lineChart } from "./charts.js";
import { $, svgEl, tiles } from "./dom.js";
import { blank, count, decimal, emptyRow, escape, fmtTime, pct, scoreNames } from "./format.js";
import { leads, state } from "./state.js";

export function renderModel(data) {
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
  const modelIslands = state.meta ? state.islands : [...new Set(regional.map(row => row.island))];
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
