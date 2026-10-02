import { describe, expect, it } from "vitest";

import { buildHistoryQuery, type HistoryForm } from "@/lib/historyFilters";

const FORM: HistoryForm = {
  minMag: "",
  maxMag: "",
  startDate: "2026-09-03",
  endDate: "2026-10-02",
  lat: "",
  lon: "",
  radiusKm: 200,
};

const build = (patch: Partial<HistoryForm>) => buildHistoryQuery({ ...FORM, ...patch });

describe("buildHistoryQuery", () => {
  it("turns WIB days into an inclusive start and exclusive end with offset", () => {
    expect(build({})).toEqual({
      ok: true,
      query: { start: "2026-09-03T00:00:00+07:00", end: "2026-10-03T00:00:00+07:00" },
    });
  });

  it("accepts a decimal comma", () => {
    const result = build({ minMag: "4,5", maxMag: "6.0" });
    expect(result.ok && [result.query.minMag, result.query.maxMag]).toEqual([4.5, 6]);
  });

  it("allows a single day", () => {
    expect(build({ startDate: "2026-10-02", endDate: "2026-10-02" }).ok).toBe(true);
  });

  it("adds a rounded point and the radius only when both coordinates are given", () => {
    const result = build({ lat: "-6.20876", lon: "106,84559", radiusKm: 100 });
    expect(result.ok && result.query).toMatchObject({ lat: -6.21, lon: 106.85, radiusKm: 100 });
    const without = build({});
    expect(without.ok && without.query.radiusKm).toBeUndefined();
  });

  it.each<[Partial<HistoryForm>, string]>([
    [{ minMag: "besar" }, "angka"],
    [{ minMag: "10" }, "angka"],
    [{ minMag: "6", maxMag: "5" }, "tidak boleh lebih besar"],
    [{ startDate: "" }, "tanggal yang valid"],
    [{ endDate: "2026-02-30" }, "tanggal yang valid"],
    [{ startDate: "2026-10-03", endDate: "2026-10-01" }, "sebelum tanggal awal"],
    [{ startDate: "2025-01-01", endDate: "2026-10-02" }, "366 hari"],
    [{ lat: "-6.2" }, "lintang"],
    [{ lat: "-91", lon: "106" }, "lintang"],
    [{ lat: "-6", lon: "181" }, "bujur"],
  ])("refuses %j with an Indonesian message", (patch, message) => {
    const result = build(patch);
    expect(result.ok).toBe(false);
    expect(!result.ok && result.error).toContain(message);
  });
});
