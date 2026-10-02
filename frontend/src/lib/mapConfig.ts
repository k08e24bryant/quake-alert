import type { LatLngBoundsExpression } from "leaflet";

/** Indonesia, roughly Sabang to Merauke. */
export const INDONESIA_BOUNDS: LatLngBoundsExpression = [
  [-11.5, 94.5],
  [6.5, 141.5],
];

export const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
export const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">kontributor OpenStreetMap</a>';
/** Leaflet's default attribution prefix includes a flag icon; keep the credit, plain. */
export const LEAFLET_PREFIX = '<a href="https://leafletjs.com">Leaflet</a>';

export const AREA_STYLE = { color: "#1d4ed8", weight: 2, dashArray: "6 6", fillOpacity: 0.05 };
export const POINT_STYLE = { color: "#ffffff", weight: 2, fillColor: "#1d4ed8", fillOpacity: 1 };
