import { read } from "./api.js";
import { $ } from "./dom.js";
import { clock, clockWib, dayFormat } from "./format.js";
import { halfHour, leads, state } from "./state.js";

export const frameTime = index => state.timelineStart + index * halfHour;

export const archivedFrame = stamp => (Date.parse(stamp) - state.timelineStart) / halfHour;

export function timelineTicks(start, hours) {
  const step = hours === 48 ? 24 : hours === 168 ? 48 : 5 * 48;
  const wibSlot = (start / halfHour + 14) % 48;
  const ticks = [];
  for (let index = (48 - wibSlot) % Math.min(step, 48); index <= hours * 2; index += step) {
    const time = start + index * halfHour;
    ticks.push([index, clock(time) === "00:00" ? dayFormat.format(time) : "12:00"]);
  }
  return ticks;
}

export function setTimeline() {
  state.modelIssue = state.meta.latest_issue_slot;
  state.timelineEnd = state.modelIssue ? Date.parse(state.modelIssue) : Math.floor(Date.parse(state.meta.as_of) / halfHour) * halfHour;
  state.issueFrame = state.settings.replay * 2;
  state.timelineStart = state.timelineEnd - state.issueFrame * halfHour;
  state.latestObs = state.meta.latest_observed_slot;
  state.obsFrame = state.latestObs == null ? -1 : Math.min(state.issueFrame, Math.floor(archivedFrame(state.latestObs)));
  $("frame").max = state.issueFrame;
  drawTrack();
}

export const archiveStamp = data => [
  data.latest_issue_slot, data.latest_observed_slot, data.latest_slots?.himawari, data.latest_slots?.bmkg,
  data.frames_updated,
].join("|");

export const frameKey = (name, hours) => name + ":" + hours;

export async function loadFrames(hours, ...layers) {
  const end = state.timelineEnd;
  const stamp = archiveStamp(state.meta);
  const window = new URLSearchParams({
    from: new Date(end - hours * 2 * halfHour).toISOString(), to: new Date(end).toISOString(),
  });
  await Promise.all(layers.map(async name => {
    const key = frameKey(name, hours);
    const cached = state.frameStamps.get(key);
    if (cached?.stamp === stamp && cached.end === end) return;
    const data = await read(`/api/frames?layer=${name}&${window}`, `${name} frames`);
    if (!data || stamp !== archiveStamp(state.meta) || end !== state.timelineEnd) return;
    const rows = new Map(data.slots.map((slot, index) => [Date.parse(slot), data.values[index]]));
    state.frameStamps.set(key, { stamp, end, rows });
  }));
}

export function useLatestProbabilities(ranking) {
  if (!ranking?.issue_slot || Date.parse(ranking.issue_slot) !== Date.parse(state.modelIssue)) return;
  state.latestProbabilities = new Map(leads.map(lead => {
    const row = [];
    for (const area of ranking.alerts) row[area.area_id] = area.prob_pct[lead];
    return [lead, row];
  }));
}

export function forecastWindow(index, comparison = false) {
  const start = frameTime(index);
  const lead = comparison ? 3 : state.settings.lead;
  return `${clock(start)}–${clockWib(start + lead * 2 * halfHour)}`;
}

export function frameRow(name, index, hours = state.settings.replay) {
  const row = state.frameStamps.get(frameKey(name, hours))?.rows.get(frameTime(index));
  return row ?? (index === state.issueFrame && name.startsWith("prob") ? state.latestProbabilities.get(Number(name.at(-1))) : null);
}

export function valueAt(name, index, id) {
  return frameRow(name, index)?.[id] ?? null;
}

function drawTrack() {
  const pos = index => index / state.issueFrame * 100;
  const start = pos(Math.max(0, state.obsFrame));
  $("deco").innerHTML = `
    <div class="gap" style="left:${start}%;right:0"></div>
    ${state.obsFrame >= 0 && state.obsFrame !== state.issueFrame ? `<div class="mk sat" style="left:${start}%"><span>IMERG</span></div>` : ""}
    <div class="mk now"><span>latest</span></div>`;
  $("ticks").innerHTML = timelineTicks(state.timelineStart, state.settings.replay)
    .map(([index, label]) => `<span style="left:${pos(index)}%">${label}</span>`).join("");
}
