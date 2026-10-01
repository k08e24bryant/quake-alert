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
| `gempaterkini.json`   | latest 15 quakes, M5+  | `Potensi` (tsunami potential)                |
| `gempadirasakan.json` | latest 15 felt quakes  | `Dirasakan` (MMI felt report)                |

BMKG publishes no history endpoint, so the history is whatever this service collects.

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
   - a malformed item is logged and skipped.
4. **Deduplicate** (`app/ingestion/dedup.py`). BMKG has no quake ID, so a report is the
   same quake as an existing row when either:
   - its fingerprint matches: the UTC time to the second plus lat/lon rounded to 2
     decimals; or
   - it is within `DEDUP_MAX_TIME_DIFF_SECONDS` (60) **and** `DEDUP_MAX_DISTANCE_KM` (50)
     of that row.

   On a match the reports are merged: the latest measurements win, and felt info,
   tsunami potential and the shakemap are kept from whichever feed has them. `source_feeds`
   and `raw` record every feed the quake came from.
5. **Record** one `ingestion_runs` row per feed: `success`, `skipped` or `failed`, with
   counts and the error. Fetches run concurrently, but feeds are processed one after
   another so the same quake from two feeds can't be inserted twice. A feed's quakes and
   its run row commit together.

`tests/fixtures/bmkg/` holds real responses saved from the live API. The parser is built
and tested against them.

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
