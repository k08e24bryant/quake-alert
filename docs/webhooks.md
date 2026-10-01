# Webhooks

A webhook subscription receives a signed JSON `POST` for every quake near a point. It uses
the same rules as Telegram alerts:
- the quake must be at least the subscription's magnitude and within its radius;
- it must be at most `NOTIFY_MAX_AGE_MINUTES` (30) old, checked before every attempt
  including retries;
- each subscription is alerted at most once per stored quake.

This is a **post-event information relay, not an early-warning system**: an alert can only
leave after BMKG has published the quake. Every payload carries BMKG's values unmodified,
`"Sumber: BMKG"` with a link, and this disclaimer verbatim:

> Layanan ini tidak resmi dan hanya meneruskan data dari BMKG. Notifikasi bisa terlambat atau tidak terkirim. Untuk informasi resmi dan arahan keselamatan, ikuti BMKG (bmkg.go.id / aplikasi InfoBMKG) dan BPBD setempat.

## Subscribe

```sh
curl -X POST https://<api>/v1/subscriptions/webhook \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://hooks.example.com/quake", "lat": -6.2088, "lon": 106.8456,
       "radius_km": 200, "min_magnitude": 4.5}'
```

| Field | Rule |
|---|---|
| `url` | `https`, publicly resolvable (see [Target rules](#target-rules)). `http` is accepted only when the server runs with `ENVIRONMENT=development`. |
| `lat`, `lon` | Rounded to 2 decimals (about 1 km) before they are stored. |
| `radius_km` | Whole km, 10–1000. |
| `min_magnitude` | 2.0–9.0, at most one decimal. |

The `201` response is the **only** time you see the two credentials:

```json
{
  "id": "0b6f2a4e-3c1d-4f7e-9a51-2d8c6e0f4b13",
  "url": "https://hooks.example.com/quake",
  "latitude": -6.21, "longitude": 106.85, "radius_km": 200, "min_magnitude": 4.5,
  "is_active": true,
  "signing_secret": "whsec_…",
  "manage_token": "qamt_…"
}
```

- `signing_secret` verifies every request we send you. We store it encrypted.
- `manage_token` deletes or tests the subscription. We store only its hash, so a lost
  token can't be recovered: subscribe again.

Creating subscriptions and sending test payloads share a limit of
`SUBSCRIPTION_WRITE_RATE_LIMIT_PER_HOUR` (5) per IP per hour. That is on top of the
general `/v1` limit.

## Test and delete

```sh
# Sends one signed "webhook.test" payload now and reports what your receiver answered.
curl -X POST https://<api>/v1/subscriptions/webhook/<id>/test \
  -H 'Authorization: Bearer <manage_token>'
# -> {"delivered": true, "status_code": 200, "error": null}

curl -X DELETE https://<api>/v1/subscriptions/webhook/<id> \
  -H 'Authorization: Bearer <manage_token>'
# -> 204; the subscription and its delivery history are deleted
```

An unknown id and a missing or wrong token both answer **404**. This is deliberate: the
API never confirms that an id exists to someone who can't prove they own it.

## The request

```http
POST /quake HTTP/1.1
Host: hooks.example.com
Content-Type: application/json
User-Agent: quake-alert-webhook/1
X-Quake-Delivery-Id: 6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48
X-Quake-Timestamp: 1790851492
X-Quake-Signature: sha256=5f0c…
```

| Header | Meaning |
|---|---|
| `X-Quake-Delivery-Id` | One per (subscription, quake). **Identical on every retry**: deduplicate on it. |
| `X-Quake-Timestamp` | Unix seconds when this attempt was signed. It changes on every retry. |
| `X-Quake-Signature` | `sha256=` + hex `HMAC_SHA256(signing_secret, f"{timestamp}.{body}")` over the exact raw body bytes. |

### Payload (`schema_version` 1)

```json
{
  "schema_version": 1,
  "event": "earthquake.alert",
  "delivery_id": "6c1d9e2a-8f3b-4a77-b1c0-5e2f7d9a3b48",
  "test": false,
  "synthetic": false,
  "possible_duplicate": false,
  "earthquake": {
    "id": "724a3801-0aac-4382-9b02-37d518091d01",
    "occurred_at": "2026-10-01T06:24:52+00:00",
    "magnitude": 5.2,
    "depth_km": 25,
    "latitude": -2.46,
    "longitude": 140.38,
    "region": "Pusat gempa berada di darat 15 km Barat Laut Sentani",
    "potential": "Tidak berpotensi tsunami",
    "potential_label": "Potensi (BMKG)",
    "felt": "II Kab. Jayapura",
    "shakemap_url": "https://data.bmkg.go.id/DataMKG/TEWS/20261001132452.mmi.jpg",
    "source_feeds": ["autogempa", "gempadirasakan"],
    "distance_km": 118.4
  },
  "source": {"notice": "Sumber: BMKG", "url": "https://www.bmkg.go.id"},
  "disclaimer": "Layanan ini tidak resmi dan hanya meneruskan data dari BMKG. Notifikasi bisa terlambat atau tidak terkirim. Untuk informasi resmi dan arahan keselamatan, ikuti BMKG (bmkg.go.id / aplikasi InfoBMKG) dan BPBD setempat."
}
```

- `earthquake` has the same fields and formats as `GET /v1/earthquakes/{id}`. Times are
  UTC with an explicit `+00:00`. `distance_km` is measured from your (rounded) point by
  PostGIS.
- `potential` is BMKG's free-text "Potensi", verbatim. Show it under `potential_label`
  ("Potensi (BMKG)"). **It is not tsunami information**: it can be any message, e.g. that
  the quake was felt.
- `possible_duplicate: true` means you were already sent an alert for another stored quake
  within 120 s and 100 km. BMKG has no quake id, and this service prefers a duplicate alert
  to a missed one, so it may be the same event reported by another BMKG feed.
- `event` is one of:

| `event` | When | `test` | `synthetic` | `earthquake` |
|---|---|---|---|---|
| `earthquake.alert` | A real quake from BMKG | `false` | `false` | the quake |
| `earthquake.test` | A synthetic quake from `scripts/dev_fake_quake.py`. Impossible outside `ENVIRONMENT=development`. | `false` | `true` | the synthetic quake |
| `webhook.test` | `POST /v1/subscriptions/webhook/{id}/test` | `true` | `false` | `null` |

Never act on a payload with `"synthetic": true` or `"test": true` as if a real quake
happened.

## Verify every request

Reject a request unless its signature matches **and** its timestamp is recent. The
signature proves the request came from us; the timestamp window stops an old, correctly
signed request from being replayed later. Then answer `2xx` quickly and do the work
afterwards. Deduplicate on `X-Quake-Delivery-Id`, because the same alert can arrive twice
(see [Delivery and retries](#delivery-and-retries)).

```python
import hashlib
import hmac
import time

MAX_AGE_SECONDS = 5 * 60


def verify_quake_webhook(
    secret: str, headers: dict[str, str], body: bytes, now: float | None = None
) -> bool:
    """True only if `body` was signed with `secret` within the last 5 minutes.

    `headers` must be the received headers with lower-case names, `body` the raw bytes
    exactly as received (never re-serialised JSON).
    """
    timestamp = headers.get("x-quake-timestamp", "")
    signature = headers.get("x-quake-signature", "")
    if not timestamp.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > MAX_AGE_SECONDS:
        return False  # too old (a replay?) or too far in the future
    expected = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return hmac.compare_digest("sha256=" + expected.hexdigest(), signature)


if __name__ == "__main__":
    # A minimal receiver with the standard library only.
    import json
    import os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    SECRET = os.environ["QUAKE_WEBHOOK_SECRET"]
    seen_deliveries: set[str] = set()  # use a database or cache in real life

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            headers = {name.lower(): value for name, value in self.headers.items()}
            if not verify_quake_webhook(SECRET, headers, body):
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(204)  # acknowledge first; a slow answer counts as a failure
            self.end_headers()
            delivery_id = headers["x-quake-delivery-id"]
            if delivery_id in seen_deliveries:
                return  # a retry of something already handled
            seen_deliveries.add(delivery_id)
            event = json.loads(body)
            if event["test"] or event["synthetic"]:
                print("test payload received:", event["event"])
                return
            quake = event["earthquake"]
            print(f"M{quake['magnitude']} {quake['region']} ({event['source']['notice']})")

    HTTPServer(("127.0.0.1", 8080), Receiver).serve_forever()
```

## Delivery and retries

| Your answer | Result |
|---|---|
| `2xx` | Sent. Resets the subscription's consecutive failure count. |
| `410 Gone` | The subscription is **deactivated**. No retry. |
| Any other `4xx` (including `429`), or a `3xx` | Failed. No retry. Redirects are never followed. |
| `5xx`, timeout, connection error, DNS failure | Retried with exponential backoff (5 s, 10 s, 20 s, 40 s), at most `NOTIFY_MAX_ATTEMPTS` (5) attempts in total. |

- Before **every** attempt, the first and each retry, the quake must still be at most
  `NOTIFY_MAX_AGE_MINUTES` (30) old. Otherwise the delivery is dropped, never sent late.
- Delivery is **at-least-once**. If you answered `2xx` but we failed to record it, the
  retry sends the alert again with the same `X-Quake-Delivery-Id`.
- After `WEBHOOK_MAX_CONSECUTIVE_FAILURES` (10) deliveries in a row end failed, the
  subscription is deactivated, and this is logged once on our side. A deactivated
  subscription receives nothing, so create a new one.
- Each attempt has a 5 s timeout for connecting and for each read. At most 64 KB of your
  response is read; the rest is ignored.

## Target rules

These run when you subscribe and **again before every request**. The check before each
request is the one that counts, because DNS can change in between.

- `https` only. `http` is allowed only when the server runs with
  `ENVIRONMENT=development`. No credentials in the URL.
- The host is resolved right before each request, and **every** address it resolves to
  must be public. Requests are refused to:
  - loopback, private, link-local and carrier-grade NAT ranges;
  - multicast, reserved, unspecified and other non-global ranges;
  - cloud metadata services (`169.254.169.254` and friends);
  - the same addresses hidden in IPv6 forms (`::ffff:127.0.0.1`, 6to4, Teredo, NAT64).
- We then connect to **that exact address**, while sending your hostname as `Host` and as
  TLS SNI, so your certificate is verified as usual. A second DNS answer can't redirect
  the request (no DNS rebinding).
- There is no allowlist and no bypass, in any environment. That includes development:
  `localhost` receivers are refused too.

## Trying it by hand

You can't point a webhook at `localhost`, so use a **public request-inspection service**:
any site that gives you a unique HTTPS URL and shows the requests it receives.

> **Only send synthetic payloads to a third-party inspector.** Whoever runs it can read
> everything sent to it. Keep the subscription for the duration of the test, give it a
> threshold no real quake will meet, and delete it afterwards.

1. Run the stack with `ENVIRONMENT=development`. In `backend/.env`, set
   `WEBHOOK_SECRET_KEYS` (see the README) and a `*_dev_bot` `TELEGRAM_BOT_TOKEN`; the
   synthetic quake script refuses to run without the dev bot.
2. Subscribe the inspector URL with `min_magnitude` 9.0, so no real quake matches:
   ```sh
   curl -X POST http://localhost:8000/v1/subscriptions/webhook -H 'Content-Type: application/json' \
     -d '{"url": "https://<inspector>/<your-id>", "lat": -6.21, "lon": 106.85, "radius_km": 50, "min_magnitude": 9.0}'
   ```
   Save `signing_secret`, `manage_token` and `id`.
3. Optional: `POST .../<id>/test` sends a `webhook.test` payload, which contains no quake
   data.
4. Send a synthetic M9.0 quake near the same point; the worker delivers it:
   ```sh
   cd backend
   uv run python -m scripts.dev_fake_quake --lat -6.21 --lon 106.85 --magnitude 9.0
   ```
   The inspector shows an `earthquake.test` payload with `"synthetic": true`. Check the
   signature with `verify_quake_webhook` above, using the saved `signing_secret`.
5. Delete the subscription: `curl -X DELETE .../<id> -H 'Authorization: Bearer <manage_token>'`.
