import { describe, expect, it } from "vitest";

import {
  MAGNITUDE_BANDS,
  MAX_RADIUS_PX,
  MIN_RADIUS_PX,
  magnitudeBand,
  magnitudeColor,
  magnitudeRadius,
} from "@/lib/magnitude";

describe("magnitude colour", () => {
  it.each([
    [1.5, "< 3,0"],
    [2.9, "< 3,0"],
    [3.0, "3,0 – 3,9"],
    [4.9, "4,0 – 4,9"],
    [5.0, "5,0 – 5,9"],
    [6.5, "6,0 – 6,9"],
    [7.0, "≥ 7,0"],
    [9.1, "≥ 7,0"],
  ])("M%s is in band %s", (magnitude, label) => {
    expect(magnitudeBand(magnitude).label).toBe(label);
  });

  it("gives every band its own colour", () => {
    const colours = MAGNITUDE_BANDS.map((band) => band.color);
    expect(new Set(colours).size).toBe(colours.length);
    expect(magnitudeColor(5.4)).toBe(magnitudeBand(5.4).color);
  });

  // WCAG 2.1 non-text contrast: markers on the (white) OSM background need 3:1.
  it.each(MAGNITUDE_BANDS.map((band) => [band.label, band.color] as const))(
    "band %s has at least 3:1 contrast against white",
    (_, color) => {
      expect(contrastWithWhite(color)).toBeGreaterThanOrEqual(3);
    },
  );
});

describe("magnitude size", () => {
  it("grows with magnitude", () => {
    const sizes = [2, 3, 4, 5, 6, 7, 8].map(magnitudeRadius);
    for (let i = 1; i < sizes.length; i++) {
      expect(sizes[i]).toBeGreaterThan(sizes[i - 1] as number);
    }
  });

  it("is clamped so tiny quakes stay clickable and big ones don't cover the map", () => {
    expect(magnitudeRadius(0)).toBe(MIN_RADIUS_PX);
    expect(magnitudeRadius(-1)).toBe(MIN_RADIUS_PX);
    expect(magnitudeRadius(9.5)).toBe(MAX_RADIUS_PX);
    expect(magnitudeRadius(Number.NaN)).toBe(MIN_RADIUS_PX);
  });
});

function contrastWithWhite(hex: string): number {
  const channel = (offset: number) => {
    const value = parseInt(hex.slice(offset, offset + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const luminance = 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
  return 1.05 / (luminance + 0.05);
}
