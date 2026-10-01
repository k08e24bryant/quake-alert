# quake-alert

Earthquake alert service for Indonesia. A worker polls BMKG Open Data, stores quakes in
PostgreSQL + PostGIS, exposes a geospatial REST API, and notifies subscribers about quakes
near them.

Earthquake data: **BMKG (Badan Meteorologi, Klimatologi, dan Geofisika)** —
<https://data.bmkg.go.id/>.

## Ingestion

The worker runs `poll_bmkg_feeds` every minute, on the minute, and once at startup. It
reads three BMKG feeds from `https://data.bmkg.go.id/DataMKG/TEWS/`:

| Feed                  | Contents               | Extra fields                                 |
|-----------------------|------------------------|----------------------------------------------|
| `autogempa.json`      | latest single quake    | `Potensi`, `Dirasakan`, `Shakemap` image     |
| `gempaterkini.json`   | latest 15 quakes, M5+  | `Potensi`                                    |
| `gempadirasakan.json` | latest 15 felt quakes  | `Dirasakan` (MMI felt report)                |

BMKG publishes no history endpoint, so the history is whatever this service collects.

`Potensi` is BMKG's free text, stored verbatim as `potential`. It is usually a tsunami
statement, but not always: autogempa may say "Gempa ini dirasakan untuk diteruskan pada
masyarakat" ("this quake was felt; pass it on to the public"). It must never be presented as
tsunami information.

Each poll works like this:

1. **Fetch** all feeds concurrently (`app/ingestion/bmkg_client.py`). 5xx responses,
   network errors and timeouts are retried with exponential backoff. A 4xx response, or a
   body that isn't JSON (e.g. a Cloudflare error page), fails immediately.
2. **Skip unchanged feeds.** If the sha256 of a feed's canonical JSON equals the hash of
   that feed's last *successful* run, the feed isn't processed again.
3. **Parse** into typed `QuakeReport`s (`app/ingestion/parser.py`):
   - string numbers (`"5.2"`, `"10 km"`) become numbers;
   - `"lat,lon"` becomes coordinates;
   - time comes from the UTC `DateTime` field, never the local `Tanggal`/`Jam`;
   - a malformed item is logged, skipped and counted in the run's `skipped_count`.
4. **Deduplicate** (`app/ingestion/dedup.py`). BMKG has no quake ID. A report from feed F
   resolves to an existing row by the first rule that matches:
   1. **Same-feed revision:** the row already holds an F payload with the identical
      `DateTime`, **and** that payload's coordinates are within `SAME_FEED_REVISION_MAX_KM`
      (100) of the report. It is the same quake, even if coordinates or magnitude moved.
   2. **Exact fingerprint** (UTC time to the second plus lat/lon rounded to 2 decimals),
      from another feed.
   3. **Fuzzy**, from another feed: within `DEDUP_MAX_TIME_DIFF_SECONDS` (60) **and**
      `DEDUP_MAX_DISTANCE_KM` (50).

   Rules 2 and 3 never match a row that already holds an F payload, and no two items of
   one snapshot may resolve to the same row. Otherwise the report becomes a new row. See
   [Design decisions](#design-decisions) for why.

   On a match the report replaces its own feed's payload in `raw` (one entry per feed).
   All other columns are then re-derived from `raw` by feed precedence,
   **autogempa > gempaterkini > gempadirasakan**:
   - time, magnitude, location, depth and region come from the highest-precedence feed
     present;
   - `felt`, `potential` and `shakemap_url` come from the highest-precedence feed that has a
     value.

   A row therefore depends only on which payloads it holds, never on poll order. The
   fingerprint is set by the first report and never changes.

   If a row's stored payload no longer parses, that row is left as it is, the error is
   logged with the row id, and the item counts toward `skipped_count`. The rest of the run
   goes on.
5. **Record** one `ingestion_runs` row per feed: `success`, `skipped` or `failed`, with
   counts and the error. Fetches run concurrently, but feeds are processed one after
   another so the same quake from two feeds can't be inserted twice. A feed's quakes and
   its run row commit together.

A second cron job, `prune_old_ingestion_runs`, runs daily at 03:00 UTC. It deletes
`success`/`skipped` runs older than `INGESTION_RUNS_RETENTION_DAYS` (14) and `failed` runs
older than `INGESTION_RUNS_FAILED_RETENTION_DAYS` (90). It never deletes a feed's latest
successful run, because the content-hash skip compares against it.

`tests/fixtures/bmkg/` holds real responses saved from the live API. The parser is built
and tested against them.
A regression test checks that every saved fixture still parses with the current parser,
because stored payloads are re-parsed on every merge.

## Query API

Interactive docs are at `/docs` (OpenAPI at `/openapi.json`). Every response includes a
`source` object attributing the data to BMKG.

| Endpoint | Returns |
|---|---|
| `GET /v1/earthquakes` | A page of earthquakes, newest first, plus `next_cursor` |
| `GET /v1/earthquakes/latest` | The most recent earthquake (404 if none yet) |
| `GET /v1/earthquakes/{id}` | One earthquake (404 if unknown) |

Filters for `GET /v1/earthquakes` (unknown parameters are rejected with 422):

| Parameter | Rule |
|---|---|
| `lat`, `lon` | Given together. Adds `distance_km` to each result, computed by PostGIS `ST_Distance` on `geography` (geodesic, in meters, divided by 1000). |
| `radius_km` | Requires `lat` and `lon`; `0 < radius_km ≤ 1000`. Uses `ST_DWithin` on `geography`. |
| `min_mag`, `max_mag` | Inclusive; `min_mag ≤ max_mag`. |
| `start`, `end` | ISO 8601 **with offset** (naive times are rejected). `start` is inclusive, `end` exclusive. The range is at most 366 days; if only `start` is given, `end` counts as now. |
| `limit` | 1–100, default 20. |
| `cursor` | The previous page's `next_cursor`. |

```sh
curl 'localhost:8000/v1/earthquakes?lat=-2.53&lon=140.72&radius_km=300&min_mag=4'
```

Response fields: `id`, `occurred_at` (UTC, e.g. `2026-10-01T06:24:52+00:00`), `magnitude`,
`depth_km`, `latitude`, `longitude`, `region`, `potential`, `felt`, `shakemap_url`,
`source_feeds`, `distance_km`. `potential` is BMKG's verbatim `Potensi` text and is **not**
tsunami information. Internal fields (`raw`, `fingerprint`) are never exposed.

**Pagination** is keyset-based on `(occurred_at DESC, id DESC)`. The cursor encodes the last
row of the page, and the next page holds rows strictly after it. Rows that share an
`occurred_at` are split deterministically by `id`, and quakes ingested while you page never
shift or repeat later pages.

**Caching** (Redis):
- `latest` is cached and deleted by the worker whenever ingestion inserts or updates a row.
  `CACHE_LATEST_TTL_SECONDS` (60) bounds staleness if an invalidation is ever missed.
- List queries are cached for `CACHE_LIST_TTL_SECONDS` (30). The key comes from the
  *validated* parameters, so parameter order, number spelling (`-6.2` vs `-6.20`) and
  timezone offsets of the same instant all share one entry.
- The `X-Cache` header is `HIT`, `MISS` or `BYPASS`.
- **If Redis is down**, the API answers from the database (`X-Cache: BYPASS`) and rate
  limiting is skipped. `/readyz` reports Redis as failing, but requests keep succeeding.
  Redis calls time out after `REDIS_SOCKET_TIMEOUT_SECONDS` (0.5).
- **Circuit breaker:** all request-path Redis calls (cache and rate limit) go through an
  in-process breaker (`app/core/circuit_breaker.py`).
  - After `REDIS_BREAKER_FAILURE_THRESHOLD` (3) consecutive failures or timeouts, it opens
    for `REDIS_BREAKER_OPEN_SECONDS` (30). While open, Redis is not contacted at all, so a
    hung Redis costs a few timeouts in total instead of a timeout on every request.
  - After the open period, exactly one trial call goes through: success closes the
    breaker, failure reopens it for another full period.
  - Only state changes are logged.
  - `/readyz` bypasses the breaker on purpose: it has to probe Redis for real.

**Rate limiting** applies to `/v1/*` only, per client IP, with a fixed window of
`RATE_LIMIT_PER_MINUTE` (60) requests per minute.
- Every response carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and
  `X-RateLimit-Reset` (seconds until the window resets).
- Over the limit, the response is `429` with `Retry-After`.
- The client IP is the socket peer. With `TRUST_PROXY_HEADERS=true` (only behind Caddy), it
  is the **last** `X-Forwarded-For` entry, the one our proxy appended. Earlier entries are
  client-controlled and ignored.

### Query plans

Measured with `EXPLAIN ANALYZE` on PostgreSQL 16 / PostGIS 3.5, using 100,000 synthetic
quakes spread over Indonesia across one year (query point Jakarta, `limit=20`):

| Query | Plan | Execution |
|---|---|---|
| `radius_km=100` (342 matches) | Bitmap Index Scan on **`ix_earthquakes_location`** (GIST) → top-N sort | 6.9 ms |
| `radius_km=1000` (~27k matches) | Bitmap Index Scan on **`ix_earthquakes_location`** (GIST), parallel heap scan → top-N sort | 151 ms |
| no filters | Index Scan on `ix_earthquakes_occurred_at_id` | 0.05 ms |
| next page (`cursor`) | Index Scan on `ix_earthquakes_occurred_at_id`, `Index Cond: ROW(occurred_at, id) < ROW(...)` | 6.4 ms |

The GIST index is used through `ST_DWithin`'s `&&` bounding-box condition, and the exact
distance check runs on the candidates it returns. Large radii cost more, because every
matching row is sorted by time before the page is cut. That is one reason `radius_km` is
capped at 1000. `tests/integration/test_query_plans.py` guards the index usage: it seeds
20k rows, runs `ANALYZE`, and asserts that the radius query plan uses
`ix_earthquakes_location` with no sequential scan.

## Design decisions

### Dedup prefers duplicates over wrong merges

Getting dedup wrong has two possible costs, and they are not equal:

| Mistake | What a subscriber experiences |
|---|---|
| Two distinct quakes merged into one row | The second quake disappears: a **missed alert** |
| One quake stored as two rows | At most a **duplicate alert** |

A missed alert is the worse failure for an alert service, so whenever the data is
ambiguous, dedup creates a new row:

- **Items in one feed snapshot are always distinct.** A feed never lists the same quake
  twice, so two items from one response never resolve to the same row, however close they
  are. Aftershock sequences are the case this protects, e.g. M5.8 then M5.1 thirty seconds
  later and 10 km away.
- **Within a feed, `DateTime` (plus a distance guard) is identity.** The same feed reporting
  the same second again, within `SAME_FEED_REVISION_MAX_KM` (100 km) of its previous
  position, is a revision and gets merged, even if the coordinates moved. The distance is
  measured from that feed's own previous coordinates, not the row's location, which may come
  from a higher-precedence feed. A different second from the same feed is a different
  quake, even within 60 s and 50 km. So is the same second more than 100 km away, since two
  unrelated quakes can share a second. A `DateTime` revision inside one feed therefore
  produces a duplicate row. That cost is accepted.
- **Fuzzy matching is only for reconciling different feeds.** Feeds describe the same quake
  with slightly different numbers. A row that already holds a payload from the incoming feed
  with a different `DateTime` is excluded, because that feed has already told us it is a
  different quake.
- **`DateTime` is compared as BMKG's own string.** If BMKG ever changed the format, the
  comparison would fail toward a duplicate, never a merge.
- **Fingerprints stay unique.** When a forced-distinct row's natural fingerprint is already
  taken (e.g. two items of one snapshot at the same second and same rounded position), the
  new row gets a salted fingerprint.

Remaining duplicate risk: one quake reported at different seconds by the same feed over
time, or reported by two feeds outside the fuzzy thresholds. Notification code (Phase 3)
should therefore make a duplicate alert recognisable rather than assume rows are unique
quakes.

### Fixed-window rate limiting allows bursts at window edges

The limiter counts requests per client per clock-aligned minute: one Redis `INCR` plus an
`EXPIRE` per request, a single key per client, and nothing to clean up. The cost is that the
window boundary is visible. A client can spend its whole allowance in the last second of one
window and again in the first second of the next, so up to **2 × `RATE_LIMIT_PER_MINUTE`
requests can land within about one second**. The average over any longer period still stays
at the limit.

That is acceptable here. The limit exists to keep one client from monopolising a small
read-only API, and every endpoint is served from cache or by indexed queries, so a short
2× burst is cheap. If exact smoothing ever matters (e.g. for an expensive endpoint), a
sliding-window log (a sorted set per client) or a token bucket (a small Lua script) removes
the edge burst. Both cost more Redis work per request.

### Redis is an optimisation, never a dependency

Redis holds only things the API can live without: cached responses and rate-limit
counters. Every Redis failure degrades instead of erroring: the cache is bypassed and the
rate limit fails open. The circuit breaker covers the slow failure mode as well. A Redis
that accepts connections but never answers would otherwise add a timeout to every request,
and after a few failures the breaker stops calling it at all. Failing open on rate limiting
during a Redis outage is a deliberate choice: availability of quake data matters more
than enforcing a per-client quota for a few minutes.

## Development

Requirements: Docker, [uv](https://docs.astral.sh/uv/).

### Run the stack

```sh
cp .env.example .env                  # optional: compose ports/credentials
cp backend/.env.example backend/.env  # app settings for running outside Docker
docker compose up --build
```

| Service  | What it does                                                      |
|----------|-------------------------------------------------------------------|
| `db`     | PostgreSQL 16 + PostGIS 3.5 (`postgis/postgis:16-3.5`)            |
| `redis`  | Redis 7: arq job queue                                            |
| `api`    | Runs `alembic upgrade head`, then FastAPI with auto-reload on :8000 |
| `worker` | arq worker (`worker.settings.WorkerSettings`)                     |

- `GET /healthz`: liveness. The process is up; no dependencies are checked.
- `GET /readyz`: readiness. 200 when PostgreSQL and Redis are reachable, 503 with the
  failing check(s) otherwise.

If ports 5432/6379/8000 are taken on your machine, set `POSTGRES_PORT`, `REDIS_PORT` or
`API_PORT` in the root `.env`, and update the URLs in `backend/.env` to match.

### Backend checks

From `backend/`, with `db` and `redis` running:

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest                     # unit + integration
uv run pytest -m "not integration"  # unit only, no services needed
```

Integration tests run against a real PostGIS database. `TEST_DATABASE_URL` (default
`quake_alert_test`) is **dropped and recreated** each run and migrated through Alembic.
Each test runs inside a transaction that is rolled back afterwards.

### Migrations

```sh
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy and pytest against PostGIS and Redis
service containers.
