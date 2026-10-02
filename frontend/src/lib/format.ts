/** Display formatting. Values are BMKG's as stored: these only format, never round or
 * change magnitude, depth, place or time. Times are shown in WIB (UTC+7, no DST), with
 * the "WIB" label, as BMKG does. */

const WIB_OFFSET_MS = 7 * 60 * 60 * 1000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];
const DATE_INPUT = /^(\d{4})-(\d{2})-(\d{2})$/;

const pad = (n: number): string => String(n).padStart(2, "0");

/** The wall clock in WIB as UTC fields of a shifted Date (independent of the browser's
 * time zone and ICU version). */
function wibClock(ms: number): Date {
  return new Date(ms + WIB_OFFSET_MS);
}

/** "02 Okt 2026 13:24:52 WIB" */
export function formatWib(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "Waktu tidak diketahui";
  const d = wibClock(ms);
  const date = `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  return `${date} ${time} WIB`;
}

/** "baru saja", "5 menit lalu", "3 jam lalu", "2 hari lalu". */
export function formatRelative(iso: string, now: Date): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return "waktu tidak diketahui";
  const minutes = Math.floor((now.getTime() - ms) / 60_000);
  if (minutes < 1) return "baru saja"; // includes a slightly fast server clock
  if (minutes < 60) return `${minutes} menit lalu`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} jam lalu`;
  return `${Math.floor(hours / 24)} hari lalu`;
}

/** Minutes between `iso` and `now`; null if unparseable. */
export function ageMinutes(iso: string, now: Date): number | null {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : (now.getTime() - ms) / 60_000;
}

/** BMKG publishes one decimal; this shows exactly that. */
export function formatMagnitude(magnitude: number): string {
  return magnitude.toFixed(1);
}

export function formatDepth(depthKm: number): string {
  return `${depthKm} km`;
}

/** Our PostGIS distance, not BMKG data, so it may be rounded for display. */
export function formatDistance(distanceKm: number): string {
  return `${Math.round(distanceKm)} km`;
}

/** Today's date in WIB as "YYYY-MM-DD" (for <input type="date">). */
export function wibDateString(at: Date): string {
  const d = wibClock(at.getTime());
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** "YYYY-MM-DD" days back from `date` (a WIB calendar date). */
export function shiftDate(date: string, days: number): string {
  const ms = parseDate(date);
  if (ms === null) throw new Error(`invalid date ${date}`);
  const d = new Date(ms + days * 86_400_000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** Start of a WIB calendar day, as ISO 8601 with offset: "2026-10-02T00:00:00+07:00".
 * Null for anything that isn't a real date. */
export function wibDayStart(date: string): string | null {
  return parseDate(date) === null ? null : `${date}T00:00:00+07:00`;
}

/** Start of the WIB day after `date`: the exclusive end of a range that includes `date`. */
export function wibDayAfter(date: string): string | null {
  return parseDate(date) === null ? null : `${shiftDate(date, 1)}T00:00:00+07:00`;
}

function parseDate(date: string): number | null {
  const match = DATE_INPUT.exec(date);
  if (!match) return null;
  const [, y, m, d] = match.map(Number) as [number, number, number, number];
  const ms = Date.UTC(y, m - 1, d);
  const check = new Date(ms);
  // Rejects 2026-02-30 and friends, which Date.UTC would silently roll over.
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== m - 1 || check.getUTCDate() !== d) {
    return null;
  }
  return ms;
}
