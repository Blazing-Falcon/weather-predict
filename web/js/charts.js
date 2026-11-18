import { svgEl } from "./dom.js";

const chartWidths = new WeakMap();

export function fitChart(svg) {
  const box = svg.viewBox.baseVal;
  const limit = chartWidths.get(svg) ?? box.width;
  chartWidths.set(svg, limit);
  box.width = Math.min(svg.clientWidth || limit, limit);
  return box.width;
}

export function traces(svg, series, x, y) {
  for (const { values, color, width = 2, dash = "0" } of series) {
    let path = "";
    values.forEach((value, index) => {
      if (value == null) return;
      const continuation = index > 0 && values[index - 1] != null;
      path += `${continuation ? "L" : "M"}${x(index)},${y(value)} `;
      if (!continuation && values[index + 1] == null) {
        svg.append(svgEl("circle", { cx: x(index), cy: y(value), r: 2.5, fill: color }));
      }
    });
    if (path) svg.append(svgEl("path", {
      d: path, fill: "none", stroke: color, "stroke-width": width,
      "stroke-dasharray": dash, "stroke-linejoin": "round",
    }));
  }
}

export function lineChart(svg, w, h, series, { n, hi, yt, format, xt = [], marks = [] }) {
  const left = 42, right = 26, top = 10, bottom = 24;
  const x = index => left + index * (w - left - right) / (n - 1);
  const y = value => top + (1 - value / hi) * (h - top - bottom);
  svg.replaceChildren();
  for (const value of yt) {
    svg.append(svgEl("line", { x1: left, x2: w - right, y1: y(value), y2: y(value), stroke: "var(--line)" }),
      svgEl("text", { x: left - 6, y: y(value) + 3.5, "text-anchor": "end" }, format(value)));
  }
  for (const [index, label] of xt) {
    svg.append(svgEl("text", { x: x(index), y: h - 6, "text-anchor": "middle" }, label));
  }
  marks.forEach(([index, label], i) => {
    svg.append(svgEl("line", { x1: x(index), x2: x(index), y1: top, y2: h - bottom,
      stroke: "var(--muted)", "stroke-dasharray": "3 3" }),
    svgEl("text", { x: x(index) - 4, y: top + 9 + (w < 500 ? i * 13 : 0), "text-anchor": "end" }, label));
  });
  traces(svg, series, x, y);
}

export function barsH(svg, w, h, rows, { max, format, left = 150, right = 52 }) {
  svg.replaceChildren();
  const rowHeight = (h - 12) / rows.length;
  rows.forEach(({ label, values }, index) => {
    const y = 6 + index * rowHeight;
    svg.append(svgEl("text", { x: left - 8, y: y + rowHeight / 2 + 3.5, "text-anchor": "end" }, label));
    const height = (rowHeight - 8) / values.length;
    values.forEach(([value, color], i) => {
      if (value == null) return;
      const width = Math.max(0, value / max * (w - left - right));
      svg.append(svgEl("rect", { x: left, y: y + 4 + i * height, width, height: height - 1, rx: 2, fill: color }),
        svgEl("text", { x: left + width + 5, y: y + 4 + (i + 0.5) * height + 3 }, format(value)));
    });
  });
}

export function chartNote(svg, message) {
  const { width, height } = svg.viewBox.baseVal;
  svg.replaceChildren(svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle" }, message));
}
