import { barsH, chartNote } from "./charts.js";
import { $ } from "./dom.js";
import {
  archiveDateFormat, blank, clockWib, count, dayFormat, decimal, emptyRow, escape, fmtTime,
  minutes, size, sources,
} from "./format.js";
import { PIPELINE } from "../pipeline-content.js";
import { state, views } from "./state.js";

export function renderPipeline(data) {
  plStorage = data?.storage || {};
  renderPipelineSelection();
  if (!data) return;
  $("runBody").innerHTML = data.runs.map(row => `
    <tr><td class="mono">${escape(row.job)}</td><td class="mono">${row.started == null ? blank : clockWib(row.started)}</td>
    <td class="num">${row.seconds == null ? blank : decimal(row.seconds, 1) + " s"}</td>
    <td><span class="chip" style="color:${row.status === "success" ? "var(--green)" : row.status === "failure" ? "var(--red)" : "var(--muted)"}">${escape(row.status.toLowerCase())}</span></td></tr>`
  ).join("") || emptyRow(4, "No runs archived.");
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

export function wirePipeline() {
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
