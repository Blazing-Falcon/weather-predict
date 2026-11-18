
export const store = {
  get(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem("rn." + key)) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem("rn." + key, JSON.stringify(value));
    } catch {
      // Preferences still work for the current page when storage is unavailable.
    }
  },
};

export const views = ["map", "overview", "alerts", "model", "history", "pipeline", "settings"];

export const halfHour = 30 * 60 * 1000;

export const chartFrames = 48 * 2 + 1;

export const leads = [1, 2, 3];

export function toggleWatch(id) {
  if (state.watch.has(id)) state.watch.delete(id);
  else state.watch.add(id);
  store.set("watch", [...state.watch]);
}

export const state = {
  settings: {
    theme: store.get("theme", "dark"),
    thr: store.get("thr", 0.5),
    zoom: store.get("zoom", "id"),
    cloud: store.get("cloud", 0.65),
    gauges: store.get("gauges", true),
    lead: store.get("lead", 3),
    replay: store.get("replay", 48),
    alertSource: store.get("alertSource", "model"),
  },
  watch: new Set(store.get("watch", [])),
  prefersLight: matchMedia("(prefers-color-scheme: light)"),
  issueFrame: 0,
  view: "map",
  meta: undefined,
  areas: [],
  areaById: new Map(),
  islands: [],
  modelIssue: undefined,
  timelineStart: undefined,
  timelineEnd: undefined,
  latestObs: undefined,
  obsFrame: undefined,
  frame: 0,
  selected: null,
  historyA: null,
  alertData: undefined,
  latestProbabilities: new Map(),
  map: undefined,
  mapReady: false,
  layer: "fc",
  cloudsOn: true,
  playing: undefined,
  areaDetails: new Map(),
  frameStamps: new Map(),
  requests: new Map(),
  failedLabels: new Set(),
  mapLayerError: false,
  mapAreas: new Map(),
  drawerClosed: false,
  malangId: undefined,
};
state.issueFrame = state.settings.replay * 2;
state.frame = state.issueFrame;
