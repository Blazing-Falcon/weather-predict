import { state } from "./state.js";

const palette = {
  dark: {
    sea: "#1d2021", land: "#282828", edge: "#282828", fg: "#ebdbb2", sel: "#fabd2f",
    hover: "#fbf1c7", nodata: "#3c3836", cloud: [251, 241, 199],
    fc: ["#504945", "#076678", "#458588", "#83a598", "#d3869b"],
    ob: ["#504945", "#076678", "#458588", "#83a598", "#d3869b"],
    bm: ["#665c54", "#458588"], ds: ["#504945", "#fe8019"],
  },
  light: {
    sea: "#f9f5d7", land: "#f2e5bc", edge: "#fbf1c7", fg: "#3c3836", sel: "#af3a03",
    hover: "#282828", nodata: "#ebdbb2", cloud: [80, 73, 69],
    fc: ["#d5c4a1", "#83a598", "#458588", "#076678", "#8f3f71"],
    ob: ["#d5c4a1", "#83a598", "#458588", "#076678", "#8f3f71"],
    bm: ["#bdae93", "#83a598"], ds: ["#d5c4a1", "#af3a03"],
  },
};

export const paletteColors = () => palette[state.settings.theme === "system" ? (state.prefersLight.matches ? "light" : "dark") : state.settings.theme];
export const rainClass = value => value < 0.1 ? 0 : value < 1 ? 1 : value < 3 ? 2 : value < 8 ? 3 : 4;
