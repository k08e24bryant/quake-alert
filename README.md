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
