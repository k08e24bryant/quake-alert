/** Client for the backend's public read API. Every failure becomes an ApiError with a
 * kind, so pages can say plainly what went wrong instead of showing an empty result. */

import { config } from "@/lib/config";
import type { Earthquake, EarthquakeList, EarthquakeQuery, StatusResponse } from "@/lib/types";

export type ApiErrorKind =
  | "config" // NEXT_PUBLIC_API_BASE_URL is not set
  | "unreachable" // network error, DNS, CORS refusal, server down
  | "timeout"
  | "rate_limited" // 429
  | "http" // any other non-2xx
  | "invalid"; // 2xx, but not the JSON we expect

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly retryAfterSeconds: number | null;

  constructor(
    kind: ApiErrorKind,
    message: string,
    options: { status?: number; retryAfterSeconds?: number | null } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = options.status ?? null;
    this.retryAfterSeconds = options.retryAfterSeconds ?? null;
  }
}

const OFFICIAL = "Untuk informasi resmi, kunjungi bmkg.go.id.";

/** What to tell the user, in Indonesian. */
export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return `Terjadi kesalahan saat memuat data gempa. ${OFFICIAL}`;
  }
  switch (error.kind) {
    case "config":
      return "Alamat server data belum dikonfigurasi (NEXT_PUBLIC_API_BASE_URL).";
    case "unreachable":
      return `Server data gempa tidak dapat dihubungi, jadi data gempa tidak dapat ditampilkan saat ini. ${OFFICIAL}`;
    case "timeout":
      return `Server data gempa tidak menjawab tepat waktu, jadi data gempa tidak dapat ditampilkan saat ini. ${OFFICIAL}`;
    case "rate_limited":
      return error.retryAfterSeconds !== null
        ? `Terlalu banyak permintaan. Coba lagi dalam ${error.retryAfterSeconds} detik.`
        : "Terlalu banyak permintaan. Coba lagi sebentar lagi.";
    case "http":
      return error.status !== null && error.status >= 500
        ? `Server data gempa sedang bermasalah (HTTP ${error.status}). ${OFFICIAL}`
        : `Permintaan ditolak server (HTTP ${error.status ?? "?"}). Periksa kembali filter.`;
    case "invalid":
      return `Jawaban server data gempa tidak dapat dibaca. ${OFFICIAL}`;
  }
}

export interface ApiClientOptions {
  baseUrl: string | null;
  fetch?: typeof fetch;
  timeoutMs?: number;
}

export interface ApiClient {
  getStatus(signal?: AbortSignal): Promise<StatusResponse>;
  listEarthquakes(query: EarthquakeQuery, signal?: AbortSignal): Promise<EarthquakeList>;
}

export function earthquakeSearchParams(query: EarthquakeQuery): URLSearchParams {
  const params = new URLSearchParams();
  const set = (name: string, value: string | number | undefined) => {
    if (value !== undefined && value !== "") params.set(name, String(value));
  };
  set("min_mag", query.minMag);
  set("max_mag", query.maxMag);
  set("start", query.start);
  set("end", query.end);
  set("lat", query.lat);
  set("lon", query.lon);
  set("radius_km", query.radiusKm);
  set("limit", query.limit);
  set("cursor", query.cursor);
  return params;
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  const timeoutMs = options.timeoutMs ?? 10_000;

  async function getJson<T>(
    path: string,
    guard: (value: unknown) => value is T,
    params?: URLSearchParams,
    signal?: AbortSignal,
  ): Promise<T> {
    if (!options.baseUrl) throw new ApiError("config", "API base URL is not configured");
    const query = params && params.size > 0 ? `?${params}` : "";
    const timeout = AbortSignal.timeout(timeoutMs);
    const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;

    let response: Response;
    try {
      response = await fetchImpl(`${options.baseUrl}${path}${query}`, {
        headers: { Accept: "application/json" },
        signal: combined,
      });
    } catch (error) {
      if (signal?.aborted) throw error; // the caller cancelled: not an API failure
      if (timeout.aborted) throw new ApiError("timeout", `GET ${path} timed out`);
      throw new ApiError("unreachable", `GET ${path} failed: ${String(error)}`);
    }

    if (response.status === 429) {
      throw new ApiError("rate_limited", `GET ${path}: 429`, {
        status: 429,
        retryAfterSeconds: parseRetryAfter(response.headers.get("Retry-After")),
      });
    }
    if (!response.ok) {
      throw new ApiError("http", `GET ${path}: HTTP ${response.status}`, {
        status: response.status,
      });
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiError("invalid", `GET ${path}: response is not JSON`, {
        status: response.status,
      });
    }
    if (!guard(body)) {
      throw new ApiError("invalid", `GET ${path}: unexpected response shape`, {
        status: response.status,
      });
    }
    return body;
  }

  return {
    getStatus: (signal) => getJson("/v1/status", isStatusResponse, undefined, signal),
    listEarthquakes: (query, signal) =>
      getJson("/v1/earthquakes", isEarthquakeList, earthquakeSearchParams(query), signal),
  };
}

export const api = createApiClient({ baseUrl: config.apiBaseUrl });

function parseRetryAfter(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value.trim())) return null;
  return Number(value.trim());
}

// --- shape checks: enough to never render undefined as data -------------------------------

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
const isString = (v: unknown): v is string => typeof v === "string";
const isNullableString = (v: unknown): v is string | null => v === null || typeof v === "string";
const isNumber = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function isEarthquake(v: unknown): v is Earthquake {
  return (
    isObject(v) &&
    isString(v.id) &&
    isString(v.occurred_at) &&
    isNumber(v.magnitude) &&
    isNumber(v.depth_km) &&
    isNumber(v.latitude) &&
    isNumber(v.longitude) &&
    isString(v.region) &&
    isNullableString(v.potential) &&
    isNullableString(v.felt) &&
    isNullableString(v.shakemap_url) &&
    Array.isArray(v.source_feeds) &&
    v.source_feeds.every(isString) &&
    (v.distance_km === null || v.distance_km === undefined || isNumber(v.distance_km))
  );
}

function isSource(v: unknown): boolean {
  return isObject(v) && isNullableString(v.data_as_of);
}

export function isEarthquakeList(v: unknown): v is EarthquakeList {
  return (
    isObject(v) &&
    Array.isArray(v.data) &&
    v.data.every(isEarthquake) &&
    isNullableString(v.next_cursor) &&
    isSource(v.source)
  );
}

export function isStatusResponse(v: unknown): v is StatusResponse {
  return (
    isObject(v) &&
    (v.ingestion_state === "ok" || v.ingestion_state === "stale") &&
    isNumber(v.stale_after_minutes) &&
    isString(v.checked_at) &&
    Array.isArray(v.feeds) &&
    isSource(v.source)
  );
}

/** Follow next_cursor until `maxItems` are loaded or there are no more pages. */
export async function listUpTo(
  client: ApiClient,
  query: EarthquakeQuery,
  maxItems: number,
  signal?: AbortSignal,
): Promise<{ items: Earthquake[]; truncated: boolean; dataAsOf: string | null }> {
  const items: Earthquake[] = [];
  let cursor: string | undefined;
  let dataAsOf: string | null = null;
  for (;;) {
    const limit = Math.min(100, maxItems - items.length);
    const page = await client.listEarthquakes({ ...query, limit, cursor }, signal);
    items.push(...page.data);
    dataAsOf ??= page.source.data_as_of;
    if (!page.next_cursor) return { items, truncated: false, dataAsOf };
    if (items.length >= maxItems) return { items, truncated: true, dataAsOf };
    cursor = page.next_cursor;
  }
}
