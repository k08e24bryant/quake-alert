/** The Riwayat filter form -> an API query, with the API's own rules checked first so the
 * user gets an Indonesian message instead of a 422. */

import { wibDayAfter, wibDayStart } from "@/lib/format";
import { isLatitude, isLongitude, roundCoordinate } from "@/lib/geo";
import type { EarthquakeQuery } from "@/lib/types";

export const MAX_RANGE_DAYS = 366;
export const RADIUS_OPTIONS = [50, 100, 200, 300, 500, 1000] as const;

export interface HistoryForm {
  minMag: string;
  maxMag: string;
  /** "YYYY-MM-DD", WIB calendar days, both inclusive. */
  startDate: string;
  endDate: string;
  lat: string;
  lon: string;
  radiusKm: number;
}

export type HistoryQueryResult =
  | { ok: true; query: EarthquakeQuery }
  | { ok: false; error: string };

function parseMagnitude(value: string): number | null | "invalid" {
  const trimmed = value.trim().replace(",", ".");
  if (trimmed === "") return null;
  const number = Number(trimmed);
  return Number.isFinite(number) && number >= 0 && number < 10 ? number : "invalid";
}

export function buildHistoryQuery(form: HistoryForm): HistoryQueryResult {
  const minMag = parseMagnitude(form.minMag);
  const maxMag = parseMagnitude(form.maxMag);
  if (minMag === "invalid" || maxMag === "invalid") {
    return { ok: false, error: "Magnitudo harus berupa angka 0 sampai 9,9." };
  }
  if (minMag !== null && maxMag !== null && minMag > maxMag) {
    return { ok: false, error: "Magnitudo minimal tidak boleh lebih besar dari maksimal." };
  }

  const start = wibDayStart(form.startDate);
  const end = wibDayAfter(form.endDate);
  if (start === null || end === null) {
    return { ok: false, error: "Isi tanggal awal dan akhir dengan tanggal yang valid." };
  }
  const days = (Date.parse(end) - Date.parse(start)) / 86_400_000;
  if (days <= 0) {
    return { ok: false, error: "Tanggal akhir tidak boleh sebelum tanggal awal." };
  }
  if (days > MAX_RANGE_DAYS) {
    return { ok: false, error: `Rentang tanggal paling lama ${MAX_RANGE_DAYS} hari.` };
  }

  const query: EarthquakeQuery = { start, end };
  if (minMag !== null) query.minMag = minMag;
  if (maxMag !== null) query.maxMag = maxMag;

  const hasLat = form.lat.trim() !== "";
  const hasLon = form.lon.trim() !== "";
  if (hasLat || hasLon) {
    const lat = Number(form.lat.trim().replace(",", "."));
    const lon = Number(form.lon.trim().replace(",", "."));
    if (!hasLat || !hasLon || !isLatitude(lat) || !isLongitude(lon)) {
      return {
        ok: false,
        error: "Titik pusat butuh lintang (-90 sampai 90) dan bujur (-180 sampai 180).",
      };
    }
    query.lat = roundCoordinate(lat);
    query.lon = roundCoordinate(lon);
    query.radiusKm = form.radiusKm;
  }
  return { ok: true, query };
}
