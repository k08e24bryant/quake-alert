/** How fresh the data is, from GET /v1/status. Staleness is always shown openly. */

import { ageMinutes, formatRelative, formatWib } from "@/lib/format";
import type { StatusResponse } from "@/lib/types";

export interface Freshness {
  /** Show the "data may be outdated" banner. */
  stale: boolean;
  /** "Data terakhir diperbarui 3 menit lalu", or why there is no such time. */
  updatedText: string;
  /** The exact time in WIB, for a tooltip; null if unknown. */
  updatedAtWib: string | null;
}

export const STALE_BANNER =
  "Data mungkin belum terbaru: pembaruan dari BMKG tertunda. " +
  "Untuk informasi terkini, periksa langsung bmkg.go.id.";

export const UNREACHABLE_BANNER =
  "Server data gempa tidak dapat dihubungi. Status dan data gempa tidak dapat dimuat. " +
  "Untuk informasi resmi, periksa langsung bmkg.go.id.";

/**
 * Stale when the server says so, when BMKG was never read, or when data_as_of has aged
 * past stale_after_minutes since the status was fetched (a tab left open, or a status
 * that was itself late). Never "ok" without a data_as_of to show.
 */
export function assessFreshness(status: StatusResponse, now: Date): Freshness {
  const dataAsOf = status.source.data_as_of;
  if (dataAsOf === null) {
    return {
      stale: true,
      updatedText: "Belum ada data dari BMKG yang berhasil dibaca.",
      updatedAtWib: null,
    };
  }
  const age = ageMinutes(dataAsOf, now);
  const tooOld = age === null || age > status.stale_after_minutes;
  return {
    stale: status.ingestion_state === "stale" || tooOld,
    updatedText: `Data terakhir diperbarui ${formatRelative(dataAsOf, now)}`,
    updatedAtWib: formatWib(dataAsOf),
  };
}
