/** Marker style by magnitude. A visual scale only: no impact estimate, no wording. */

export interface MagnitudeBand {
  /** Inclusive lower bound; the band runs up to the next band's bound. */
  min: number;
  label: string;
  color: string;
}

/** Light-to-dark, blue to dark red. Each colour has at least 3:1 contrast against the
 * white OSM map background, and markers also get a dark outline. */
export const MAGNITUDE_BANDS: readonly MagnitudeBand[] = [
  { min: -Infinity, label: "< 3,0", color: "#2563eb" },
  { min: 3, label: "3,0 – 3,9", color: "#0f8a3c" },
  { min: 4, label: "4,0 – 4,9", color: "#b45309" },
  { min: 5, label: "5,0 – 5,9", color: "#dc2626" },
  { min: 6, label: "6,0 – 6,9", color: "#9f1239" },
  { min: 7, label: "≥ 7,0", color: "#4c0519" },
];

export const MIN_RADIUS_PX = 5;
export const MAX_RADIUS_PX = 24;

export function magnitudeBand(magnitude: number): MagnitudeBand {
  let band = MAGNITUDE_BANDS[0] as MagnitudeBand;
  for (const candidate of MAGNITUDE_BANDS) {
    if (magnitude >= candidate.min) band = candidate;
  }
  return band;
}

export function magnitudeColor(magnitude: number): string {
  return magnitudeBand(magnitude).color;
}

/** Circle radius in pixels: grows 3 px per magnitude unit from M2, clamped. */
export function magnitudeRadius(magnitude: number): number {
  if (!Number.isFinite(magnitude)) return MIN_RADIUS_PX;
  const radius = MIN_RADIUS_PX + (magnitude - 2) * 3;
  return Math.min(MAX_RADIUS_PX, Math.max(MIN_RADIUS_PX, radius));
}
