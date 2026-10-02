/** The backend's public read API (see the README, "Query API"). Times are ISO 8601 UTC
 * with an explicit "+00:00". */

export interface SourceAttribution {
  name: string;
  url: string;
  notice: string;
  data_as_of: string | null;
}

export interface Earthquake {
  id: string;
  occurred_at: string;
  magnitude: number;
  depth_km: number;
  latitude: number;
  longitude: number;
  region: string;
  /** BMKG's "Potensi" free text, verbatim. NOT tsunami information. */
  potential: string | null;
  felt: string | null;
  shakemap_url: string | null;
  source_feeds: string[];
  /** Only when the query had lat/lon. */
  distance_km: number | null;
}

export interface EarthquakeList {
  data: Earthquake[];
  next_cursor: string | null;
  source: SourceAttribution;
}

export interface FeedStatus {
  feed: string;
  last_success_at: string | null;
  last_run_status: "success" | "skipped" | "failed" | null;
  last_run_at: string | null;
}

export interface StatusResponse {
  ingestion_state: "ok" | "stale";
  stale_after_minutes: number;
  checked_at: string;
  feeds: FeedStatus[];
  source: SourceAttribution;
}

export interface EarthquakeQuery {
  minMag?: number;
  maxMag?: number;
  /** ISO 8601 with offset. */
  start?: string;
  end?: string;
  lat?: number;
  lon?: number;
  radiusKm?: number;
  limit?: number;
  cursor?: string;
}
