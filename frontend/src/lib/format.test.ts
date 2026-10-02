import { describe, expect, it } from "vitest";

import {
  ageMinutes,
  formatDepth,
  formatDistance,
  formatMagnitude,
  formatRelative,
  formatWib,
  shiftDate,
  wibDateString,
  wibDayAfter,
  wibDayStart,
} from "@/lib/format";

describe("formatWib", () => {
  it("shows UTC times in WIB (UTC+7) with the WIB label", () => {
    expect(formatWib("2026-10-01T06:24:52+00:00")).toBe("01 Okt 2026 13:24:52 WIB");
  });

  it("rolls over to the next WIB day, month and year", () => {
    expect(formatWib("2026-10-01T17:00:00+00:00")).toBe("02 Okt 2026 00:00:00 WIB");
    expect(formatWib("2026-12-31T20:30:05+00:00")).toBe("01 Jan 2027 03:30:05 WIB");
  });

  it("accepts any offset and Z", () => {
    expect(formatWib("2026-10-01T13:24:52+07:00")).toBe("01 Okt 2026 13:24:52 WIB");
    expect(formatWib("2026-05-09T00:00:00Z")).toBe("09 Mei 2026 07:00:00 WIB");
  });

  it("uses Indonesian month abbreviations, like BMKG", () => {
    const months = Array.from({ length: 12 }, (_, m) =>
      formatWib(new Date(Date.UTC(2026, m, 15)).toISOString()).split(" ")[1],
    );
    expect(months).toEqual([
      "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
    ]);
  });

  it("never shows a bogus date for garbage", () => {
    expect(formatWib("kemarin")).toBe("Waktu tidak diketahui");
  });
});

describe("formatRelative", () => {
  const now = new Date("2026-10-02T07:00:00Z");
  const ago = (seconds: number) => new Date(now.getTime() - seconds * 1000).toISOString();

  it.each([
    [0, "baru saja"],
    [59, "baru saja"],
    [60, "1 menit lalu"],
    [5 * 60 + 30, "5 menit lalu"],
    [59 * 60 + 59, "59 menit lalu"],
    [60 * 60, "1 jam lalu"],
    [23 * 3600 + 3599, "23 jam lalu"],
    [24 * 3600, "1 hari lalu"],
    [3 * 86_400 + 5, "3 hari lalu"],
  ])("%i seconds ago -> %s", (seconds, expected) => {
    expect(formatRelative(ago(seconds), now)).toBe(expected);
  });

  it("treats a timestamp slightly in the future (clock skew) as just now", () => {
    expect(formatRelative(ago(-30), now)).toBe("baru saja");
  });

  it("handles garbage", () => {
    expect(formatRelative("nope", now)).toBe("waktu tidak diketahui");
    expect(ageMinutes("nope", now)).toBeNull();
  });
});

describe("BMKG values are formatted, never changed", () => {
  it("shows magnitude with BMKG's one decimal", () => {
    expect(formatMagnitude(5)).toBe("5.0");
    expect(formatMagnitude(5.2)).toBe("5.2");
  });

  it("shows depth as km", () => {
    expect(formatDepth(10)).toBe("10 km");
  });

  it("rounds our own distance only", () => {
    expect(formatDistance(118.44)).toBe("118 km");
  });
});

describe("WIB calendar days for the date filters", () => {
  it("gives today's date in WIB, not UTC", () => {
    expect(wibDateString(new Date("2026-10-01T16:59:59Z"))).toBe("2026-10-01");
    expect(wibDateString(new Date("2026-10-01T17:00:00Z"))).toBe("2026-10-02");
  });

  it("turns a day into its WIB start and the next day's start", () => {
    expect(wibDayStart("2026-10-02")).toBe("2026-10-02T00:00:00+07:00");
    expect(wibDayAfter("2026-10-02")).toBe("2026-10-03T00:00:00+07:00");
    expect(wibDayAfter("2026-12-31")).toBe("2027-01-01T00:00:00+07:00");
    expect(wibDayAfter("2028-02-28")).toBe("2028-02-29T00:00:00+07:00");
  });

  it("shifts dates across months", () => {
    expect(shiftDate("2026-10-02", -29)).toBe("2026-09-03");
  });

  it.each(["", "2026-02-30", "2026-13-01", "2026-1-1", "02-10-2026"])(
    "rejects %j",
    (value) => {
      expect(wibDayStart(value)).toBeNull();
      expect(wibDayAfter(value)).toBeNull();
    },
  );
});
