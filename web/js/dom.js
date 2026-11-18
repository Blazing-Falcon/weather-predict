
export const $ = id => document.getElementById(id);

export const svgEl = (tag, attrs, label) => {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  if (label != null) element.textContent = label;
  return element;
};

export const mapPadding = () => innerWidth > 820
  ? { top: 20, bottom: 100, left: 330, right: 390 }
  : { top: 10, bottom: 10, left: 10, right: 10 };

export function tiles(id, values) {
  $(id).innerHTML = values.map(([label, value, detail, color]) => `
    <div class="panel tile s3 t-${color}"><span class="label">${label}</span>
    <span class="v">${value}</span><span class="d">${detail}</span></div>`).join("");
}
