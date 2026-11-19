export const blank = "—";
export const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const dateFormat = new Intl.DateTimeFormat("en-GB", {
  weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23", timeZone: "Asia/Jakarta",
});

export const archiveDateFormat = new Intl.DateTimeFormat("en-GB", {
  year: "numeric", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
  hourCycle: "h23", timeZone: "Asia/Jakarta",
});

const clockFormat = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "Asia/Jakarta",
});

export const dayFormat = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "Asia/Jakarta" });
export const fmtTime = value => dateFormat.format(new Date(value)).replace(",", "") + " WIB";
export const clock = value => clockFormat.format(new Date(value));
export const clockWib = value => value == null ? blank : clock(value) + " WIB";
export const dayClockWib = value => `${dayFormat.format(new Date(value))} ${clockWib(value)}`;
export const pct = value => value == null ? blank : Math.round(value * 100) + "%";
export const percent = value => value == null ? blank : Math.round(value) + "%";
export const decimal = (value, digits = 2) => value == null ? blank : value.toFixed(digits);
export const temp = value => value == null ? blank : Math.round(value - 273.15) + " °C";
const countFormat = new Intl.NumberFormat("en-GB");
export const compactFormat = new Intl.NumberFormat("en-GB", { notation: "compact", maximumFractionDigits: 1 });
export const count = value => value == null ? blank : countFormat.format(value);
export const size = value => value >= 1e9 ? (value / 1e9).toFixed(2) + " GB"
  : value >= 1e6 ? (value / 1e6).toFixed(1) + " MB" : Math.round(value / 1e3) + " KB";

export const minutes = value => value == null ? blank
  : value < 60 ? Math.round(value) + " min"
    : value < 1440 ? (value / 60).toFixed(1) + " h" : Math.round(value / 1440) + " d";

export const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);

export const emptyRow = (columns, message) => `<tr><td colspan="${columns}" class="note">${message}</td></tr>`;
export const bmkgNames = {
  0: ["Cerah", "Sunny"],
  1: ["Cerah", "Sunny"],
  2: ["Cerah Berawan", "Partly Cloudy"],
  3: ["Berawan", "Mostly Cloudy"],
  10: ["Udara Kabur", "Mist/Haze"],
  17: ["Petir", "Thunder"],
  45: ["Kabut/Asap", "Fog/Smoke"],
  61: ["Hujan Ringan", "Light Rain"],
  63: ["Hujan Sedang", "Moderate Rain"],
  65: ["Hujan Lebat", "Heavy Rain"],
  95: ["Hujan Petir", "Thunderstorm"],
};

export function bmkgText(record, className = "") {
  if (record?.weather == null) return `<span class="${className}">${blank}</span>`;
  const code = `code ${escape(record.weather)}`;
  const names = bmkgNames[record.weather];
  const title = names ? `${names[1]} · ${code}` : code;
  return `<span class="${className}" title="${title}">${names?.[0] ?? code}</span>`;
}

export const areaLabel = area => area.kemendagri_name?.startsWith("Kota ")
  ? area.kemendagri_name : area.geo_name.startsWith("Kota ") ? area.geo_name
    : area.is_admin ? "Kab. " + area.geo_name : area.geo_name;

export const sources = {
  himawari: "Himawari-9 clouds", bmkg: "BMKG forecast", imerg: "IMERG Early rain",
  metar: "METAR gauges", model: "Our model", mergir: "MERGIR clouds", ghcnh: "GHCNh gauges",
};

export const scoreNames = {
  model: ["Our model", "var(--blue)"], motion: ["Cloud motion only", "var(--purple)"],
  persist: ["Same as IMERG now", "var(--gray)"], clim: ["Local climatology", "var(--aqua)"],
  bmkg: ["BMKG", "var(--orange)"], nomotion: ["LightGBM without cloud motion", "var(--gray)"],
};
