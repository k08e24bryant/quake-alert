import { describe, expect, it } from "vitest";

import {
  ApiError,
  type ApiErrorKind,
  createApiClient,
  earthquakeSearchParams,
  errorMessage,
  listUpTo,
} from "@/lib/api";
import type { Earthquake, EarthquakeList, StatusResponse } from "@/lib/types";

const BASE = "https://api.example.com";

const SOURCE = {
  name: "BMKG (Badan Meteorologi, Klimatologi, dan Geofisika)",
  url: "https://data.bmkg.go.id/",
  notice: "Sumber: BMKG",
  data_as_of: "2026-10-02T06:59:00+00:00",
};

function quake(id: string, overrides: Partial<Earthquake> = {}): Earthquake {
  return {
    id,
    occurred_at: "2026-10-01T06:24:52+00:00",
    magnitude: 5.2,
    depth_km: 25,
    latitude: -2.46,
    longitude: 140.38,
    region: "Pusat gempa berada di darat 15 km Barat Laut Sentani",
    potential: "Gempa ini dirasakan untuk diteruskan pada masyarakat",
    felt: null,
    shakemap_url: null,
    source_feeds: ["autogempa"],
    distance_km: null,
    ...overrides,
  };
}

const STATUS: StatusResponse = {
  ingestion_state: "ok",
  stale_after_minutes: 5,
  checked_at: "2026-10-02T07:00:00+00:00",
  feeds: [],
  source: SOURCE,
};

function json(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

/** A fetch that records requested URLs and answers from `respond`. */
function fakeFetch(respond: (url: string, init?: RequestInit) => Promise<Response> | Response) {
  const urls: string[] = [];
  const fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    urls.push(url);
    return Promise.resolve(respond(url, init));
  }) as typeof globalThis.fetch;
  return { fetch, urls };
}

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (error) {
    expect(error).toBeInstanceOf(ApiError);
    return error as ApiError;
  }
  throw new Error("expected the call to fail");
}

describe("successful calls", () => {
  it("gets the status", async () => {
    const { fetch, urls } = fakeFetch(() => json(STATUS));
    const client = createApiClient({ baseUrl: BASE, fetch });

    expect(await client.getStatus()).toEqual(STATUS);
    expect(urls).toEqual([`${BASE}/v1/status`]);
  });

  it("lists earthquakes with the filters as API query parameters", async () => {
    const page: EarthquakeList = { data: [quake("a")], next_cursor: null, source: SOURCE };
    const { fetch, urls } = fakeFetch(() => json(page));
    const client = createApiClient({ baseUrl: BASE, fetch });

    await client.listEarthquakes({
      minMag: 4.5,
      start: "2026-10-01T00:00:00+07:00",
      lat: -6.21,
      lon: 106.85,
      radiusKm: 200,
      limit: 25,
    });

    const url = new URL(urls[0] as string);
    expect(url.pathname).toBe("/v1/earthquakes");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      min_mag: "4.5",
      start: "2026-10-01T00:00:00+07:00", // "+" survives encoding
      lat: "-6.21",
      lon: "106.85",
      radius_km: "200",
      limit: "25",
    });
  });

  it("leaves unset filters out", () => {
    expect(earthquakeSearchParams({}).toString()).toBe("");
  });
});

describe("failures become ApiErrors the pages can explain", () => {
  it("config: no base URL, and nothing is fetched", async () => {
    const { fetch, urls } = fakeFetch(() => json(STATUS));
    const error = await failure(createApiClient({ baseUrl: null, fetch }).getStatus());
    expect(error.kind).toBe("config");
    expect(urls).toEqual([]);
  });

  it("unreachable: network errors, DNS, CORS refusals", async () => {
    const fetch = (() => Promise.reject(new TypeError("Failed to fetch"))) as typeof globalThis.fetch;
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).getStatus());
    expect(error.kind).toBe("unreachable");
  });

  it("timeout: no answer in time", async () => {
    const { fetch } = fakeFetch(
      (_, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const error = await failure(
      createApiClient({ baseUrl: BASE, fetch, timeoutMs: 10 }).getStatus(),
    );
    expect(error.kind).toBe("timeout");
  });

  it("rate_limited: 429 with Retry-After", async () => {
    const { fetch } = fakeFetch(() =>
      json({ detail: "Rate limit exceeded." }, { status: 429, headers: { "Retry-After": "30" } }),
    );
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).getStatus());
    expect([error.kind, error.status, error.retryAfterSeconds]).toEqual(["rate_limited", 429, 30]);
  });

  it.each([500, 503, 404, 422])("http: status %i", async (status) => {
    const { fetch } = fakeFetch(() => json({ detail: "x" }, { status }));
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).getStatus());
    expect([error.kind, error.status]).toEqual(["http", status]);
  });

  it("invalid: 200 that is not JSON (e.g. a proxy's HTML page)", async () => {
    const { fetch } = fakeFetch(() => new Response("<html>oops</html>", { status: 200 }));
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).getStatus());
    expect(error.kind).toBe("invalid");
  });

  it.each([
    ["no data array", { next_cursor: null, source: SOURCE }],
    ["a quake without magnitude", { data: [{ ...quake("a"), magnitude: "5.2" }], next_cursor: null, source: SOURCE }],
    ["no source", { data: [], next_cursor: null }],
  ])("invalid: earthquake list with %s", async (_, body) => {
    const { fetch } = fakeFetch(() => json(body));
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).listEarthquakes({}));
    expect(error.kind).toBe("invalid");
  });

  it("invalid: status with an unknown ingestion_state", async () => {
    const { fetch } = fakeFetch(() => json({ ...STATUS, ingestion_state: "unknown" }));
    const error = await failure(createApiClient({ baseUrl: BASE, fetch }).getStatus());
    expect(error.kind).toBe("invalid");
  });

  it("a cancelled request is not reported as an API failure", async () => {
    const { fetch } = fakeFetch(
      (_, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const controller = new AbortController();
    const pending = createApiClient({ baseUrl: BASE, fetch }).getStatus(controller.signal);
    controller.abort();
    await expect(pending).rejects.not.toBeInstanceOf(ApiError);
  });
});

describe("errorMessage", () => {
  const kinds: ApiErrorKind[] = ["config", "unreachable", "timeout", "rate_limited", "http", "invalid"];

  it.each(kinds)("says something plain and Indonesian for %s", (kind) => {
    const message = errorMessage(new ApiError(kind, "x", { status: 503 }));
    expect(message.length).toBeGreaterThan(20);
    expect(message).not.toMatch(/peringatan dini|early warning|bahaya|darurat/i);
  });

  it("tells the user to check BMKG when data can't be shown", () => {
    for (const kind of ["unreachable", "timeout", "invalid"] as const) {
      expect(errorMessage(new ApiError(kind, "x"))).toContain("bmkg.go.id");
    }
  });

  it("states that the server is unreachable", () => {
    expect(errorMessage(new ApiError("unreachable", "x"))).toContain("tidak dapat dihubungi");
  });

  it("includes the wait for 429", () => {
    expect(errorMessage(new ApiError("rate_limited", "x", { retryAfterSeconds: 30 }))).toContain(
      "30 detik",
    );
  });

  it("handles errors that are not ApiErrors", () => {
    expect(errorMessage(new Error("boom"))).toContain("bmkg.go.id");
  });
});

describe("listUpTo follows next_cursor", () => {
  function pagedFetch(pages: EarthquakeList[]) {
    return fakeFetch((url) => {
      const cursor = new URL(url).searchParams.get("cursor");
      const index = cursor === null ? 0 : Number(cursor);
      return json(pages[index]);
    });
  }

  const page = (ids: string[], next: string | null): EarthquakeList => ({
    data: ids.map((id) => quake(id)),
    next_cursor: next,
    source: SOURCE,
  });

  it("loads every page when there are few", async () => {
    const { fetch, urls } = pagedFetch([page(["a", "b"], "1"), page(["c"], null)]);
    const result = await listUpTo(createApiClient({ baseUrl: BASE, fetch }), {}, 500);

    expect(result.items.map((q) => q.id)).toEqual(["a", "b", "c"]);
    expect(result.truncated).toBe(false);
    expect(result.dataAsOf).toBe(SOURCE.data_as_of);
    expect(new URL(urls[1] as string).searchParams.get("cursor")).toBe("1");
  });

  it("stops at the maximum and says so", async () => {
    const { fetch, urls } = pagedFetch([page(["a", "b"], "1"), page(["c", "d"], "2")]);
    const result = await listUpTo(createApiClient({ baseUrl: BASE, fetch }), {}, 2);

    expect(result.items.map((q) => q.id)).toEqual(["a", "b"]);
    expect(result.truncated).toBe(true);
    expect(urls).toHaveLength(1);
    expect(new URL(urls[0] as string).searchParams.get("limit")).toBe("2");
  });
});
