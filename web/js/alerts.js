import { alertUrl, read } from "./api.js";
import { $ } from "./dom.js";
import { bmkgText, count, dayClockWib, emptyRow, escape, fmtTime, percent, temp } from "./format.js";
import { leads, state, store } from "./state.js";

let alertLoad = 0;
let bmkgAlertData;
let bmkgAlertsRequest;
const alertSort = $("alSort");
const alertSortHeaders = document.querySelectorAll("[data-alert-sort]");
const alertLabel = row => state.areaById.get(row.area_id)?.label ?? row.name;

export function renderAlerts() {
  const bmkg = state.settings.alertSource === "bmkg";
  const data = bmkg ? bmkgAlertData : state.alertData;
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
      && (!q || label.toLowerCase().includes(q)) && (!onlyWatch || state.watch.has(row.area_id));
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
    return `<tr class="al-row"><td><button class="star" data-star="${row.area_id}" aria-pressed="${state.watch.has(row.area_id)}" aria-label="Watch ${label}">★</button></td>
      <td>${chip}</td><td class="al-name">${label}</td><td class="note al-detail">${escape(row.island)}</td>
      ${leads.map(lead => `<td class="num al-detail" data-lead="+${lead} h">${percent(row.prob_pct[lead])}</td>`).join("")}
      <td class="num al-detail al-cloud">${temp(row.tb_min)}</td><td class="num al-detail al-cold">${percent(row.cold_pct)}</td>
      <td class="al-detail al-bmkg">${bmkgText(row.bmkg, "chip")}</td><td class="al-map"><button class="linkbtn" data-open="${row.area_id}">map</button></td></tr>`;
  }).join("") || emptyRow(11, !data.issue_slot ? "No forecast issue archived."
    : bmkg ? "No areas match." : "No areas match. Lower the level or clear the search.");
}

export async function loadAlerts() {
  const request = ++alertLoad;
  const data = await read(alertUrl(), "Alerts");
  if (request !== alertLoad) return;
  state.alertData = data;
  $("alertN").textContent = data?.issue_slot ? count(data.alerts.length) : "";
  document.querySelector("[data-view][aria-current=page]").scrollIntoView({ block: "nearest", inline: "nearest" });
  if (state.view === "alerts") renderAlerts();
}

export async function loadBmkgAlerts() {
  const request = bmkgAlertsRequest = read("/api/alerts?source=bmkg", "BMKG alerts");
  const data = await request;
  if (request !== bmkgAlertsRequest) return;
  bmkgAlertData = data;
  if (state.view === "alerts") renderAlerts();
}

export function wireAlerts() {
  for (const id of ["alQ", "alIsland", "alProvince", "alLvl", "alWatch"]) {
    $(id).addEventListener("input", renderAlerts);
  }
  for (const input of document.querySelectorAll('input[name="alertSource"]')) {
    input.checked = input.value === state.settings.alertSource;
    input.addEventListener("change", () => {
      state.settings.alertSource = input.value;
      store.set("alertSource", state.settings.alertSource);
      renderAlerts();
      if (state.settings.alertSource === "bmkg") loadBmkgAlerts();
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
}
