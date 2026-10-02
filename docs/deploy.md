# Deploying to production

One Oracle Cloud **Always Free** Ampere A1 VM runs the backend; Vercel runs the frontend.

| Where | What |
|---|---|
| `https://gempasekitarsaya.my.id` (and `www`, redirected) | Frontend, Vercel |
| `https://api.gempasekitarsaya.my.id` | Caddy (HTTPS) → api, on the VM |
| VM, internal network only | worker, PostgreSQL + PostGIS, Redis |
| `t.me/infogempasekitarbot` | Production Telegram bot (webhook to the API) |

Files: `deploy/docker-compose.prod.yml`, `deploy/Caddyfile`, `deploy/.env.production.example`,
and the scripts in `deploy/` (`deploy.sh`, `set_telegram_webhook.sh`, `backup.sh`, `restore.sh`).
Every step below is something you run; nothing here deploys by itself.

> **Oracle Always Free limit: never exceed 2 OCPU / 12 GB RAM** for the A1 shape (this
> project's own ceiling, inside Oracle's free allowance), and stay on Always Free resources only.
> Anything bigger, a second paid shape, or extra block volumes beyond the free storage can
> start billing. This stack needs about 1 GB of RAM; 1 OCPU / 6 GB is plenty, 2 OCPU / 12 GB
> is the maximum.

## Checklist

- [ ] 1. Images are on GHCR and public
- [ ] 2. VM created (A1, Ubuntu, arm64, ≤ 2 OCPU / 12 GB)
- [ ] 3. SSH: key only, password login off
- [ ] 4. Firewall: Oracle security list **and** host iptables (22, 80, 443)
- [ ] 5. Docker installed
- [ ] 6. DNS: `api` A record resolves to the VM
- [ ] 7. Code checked out, `deploy/.env` filled in
- [ ] 8. First deploy: `./deploy.sh`
- [ ] 9. Telegram webhook set: `./set_telegram_webhook.sh`
- [ ] 10. Verified with curl
- [ ] 11. Frontend on Vercel (env vars, domains, CORS)
- [ ] 12. Backups in cron, first restore test passed
- [ ] 13. Uptime monitors (`/readyz`, `/v1/status`)

## 1. Images (GHCR)

**Recommended: CI builds them.** On every push to `main` that passes all checks, the
`images` job in `.github/workflows/ci.yml` builds for `linux/arm64` (the VM) and
`linux/amd64` and pushes:

- `ghcr.io/k08e24bryant/quake-alert-api` (backend: api, worker and migrations)
- `ghcr.io/k08e24bryant/quake-alert-postgis` (PostgreSQL 16 + PostGIS 3)

each tagged `latest` and `sha-<commit>`. The VM only pulls; it never compiles anything.

Why our own PostGIS image: `postgis/postgis` (used in development and CI) publishes amd64 only,
and Ampere is arm64. `deploy/postgis/Dockerfile` is the official `postgres:16` image plus
PostGIS from the PostgreSQL apt repository. It currently installs PostGIS 3.6, and dev uses
3.5. The whole backend test suite passes against the new image.

**Once, after the first successful run:** GHCR packages start private even for a public
repository. On GitHub: your profile → Packages → `quake-alert-api` → Package settings →
Change visibility → Public. Do the same for `quake-alert-postgis`. Then the VM can pull
without logging in.

> Alternative: build on the VM (`docker build -t ghcr.io/k08e24bryant/quake-alert-api:latest
> backend`, same for `deploy/postgis`, then `SKIP_PULL=1 ./deploy.sh`). It works, but every
> deploy compiles on the free VM and the result isn't the image CI tested.

## 2. Create the VM

Oracle Cloud console → Compute → Instances → Create instance:

- **Image:** Canonical Ubuntu 24.04 (or 22.04), the **aarch64** build.
- **Shape:** Ampere → `VM.Standard.A1.Flex`, **at most 2 OCPU and 12 GB** (see the warning above).
- **Networking:** a public subnet with a public IPv4 address. Optional: reserve the public IP
  (Networking → Reserved public IPs) so it survives re-creating the instance.
- **SSH keys:** upload your **public** key (`~/.ssh/id_ed25519.pub`; create one with
  `ssh-keygen -t ed25519` if needed). Oracle images have no password for the `ubuntu` user.
- **Boot volume:** the default (about 47 GB) is inside the free storage.

Then: `ssh ubuntu@<VM_IP>`, and `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.

## 3. SSH: key only

Oracle's Ubuntu images already accept keys only, but make it explicit and survive upgrades.
**Keep your current SSH session open** until a second login works.

```sh
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
sudo sshd -t && sudo systemctl reload ssh
```

From a **new** terminal: `ssh ubuntu@<VM_IP>` must still work, and
`ssh -o PubkeyAuthentication=no ubuntu@<VM_IP>` must answer `Permission denied (publickey)`.

Optional: `sudo apt install -y unattended-upgrades` (security updates; usually already on).

## 4. Firewall: both layers

Two independent firewalls, and **both** must allow only 22, 80 and 443.

**a) Oracle security list** (the outer perimeter). Networking → Virtual cloud networks →
your VCN → the subnet's security list → Ingress rules:

| Source | Protocol | Port | Why |
|---|---|---|---|
| `0.0.0.0/0` (or your own IP/32 for SSH) | TCP | 22 | SSH |
| `0.0.0.0/0` | TCP | 80 | Let's Encrypt HTTP challenge, redirect to HTTPS |
| `0.0.0.0/0` | TCP | 443 | HTTPS |

Delete any other ingress rule (the default one for ICMP type 3 is fine to keep).

**b) Host firewall (iptables).** Oracle's Ubuntu images ship their own iptables rules
(saved in `/etc/iptables/rules.v4`, loaded by `netfilter-persistent`) that allow SSH and
**reject everything else**. Use iptables, **not ufw**, on these images: ufw would manage a
second rule set on top of Oracle's, and the two are easy to get out of step.

```sh
sudo iptables -L INPUT --line-numbers -n      # find the line of the final REJECT rule
# Insert 80 and 443 just before it (here line 5; use the number you saw):
sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT
sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT
sudo netfilter-persistent save
sudo iptables -L INPUT --line-numbers -n      # 22, 80, 443 ACCEPT, then REJECT
```

> **Docker publishes ports through its own rules** (the FORWARD chain), not through INPUT.
> So the host rules above don't protect a port Docker publishes: only the Oracle security
> list does. That is why `docker-compose.prod.yml` publishes **only** Caddy's 80 and 443,
> and PostgreSQL and Redis sit on an `internal` network with no published port at all. Never
> add a `ports:` entry to `db` or `redis`.

On a non-Oracle Ubuntu image (no preinstalled iptables rules) the equivalent is
`sudo ufw default deny incoming && sudo ufw allow 22,80,443/tcp && sudo ufw enable`.

## 5. Docker

Docker's own apt repository (works on arm64 as is):

```sh
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu     # then log out and back in
docker run --rm hello-world        # prints "This message shows that your installation appears to be working correctly."
docker compose version             # v2.24 or newer
```

Docker starts on boot, and every service has `restart: unless-stopped`, so the stack comes
back by itself after a reboot.

## 6. DNS (do this before the first deploy)

The domain is **`gempasekitarsaya.my.id`**, with DNS at **Exabytes**.

**a) The API record.** In the Exabytes client area → your domain → DNS management, add:

| Type | Host | Value | TTL |
|---|---|---|---|
| A | `api` | the VM's public IPv4 | 300 |

**b) Confirm it resolves before starting Caddy for the first time.** On the first start,
Caddy asks Let's Encrypt for a certificate right away. If DNS doesn't point at the VM yet,
the challenge fails. Let's Encrypt limits failed validations (5 per hour per hostname), and
repeated attempts can lock you out for an hour or more. So wait until both of these print
the VM's IP:

```sh
dig +short api.gempasekitarsaya.my.id @1.1.1.1
dig +short api.gempasekitarsaya.my.id @8.8.8.8
```

(Without `dig`: `nslookup api.gempasekitarsaya.my.id 1.1.1.1`.)

**c) The frontend records** (apex `gempasekitarsaya.my.id` and `www`) point at Vercel. Add
them in step 11, with the exact values the **Vercel dashboard** shows for your project
(Project → Settings → Domains). Vercel's values can change, so copy them from there rather
than from any guide.

> Other domain options, for reference: a free subdomain service (for example DuckDNS or
> FreeDNS) works with the same steps (an A record for the API host). A `.my.id` domain (PANDI,
> Indonesia, cheap, registered with a KTP) is what this deployment uses, and it can also
> hold the frontend at the apex.

## 7. Code and configuration

```sh
sudo mkdir -p /opt/quake-alert && sudo chown ubuntu: /opt/quake-alert
git clone https://github.com/k08e24bryant/quake-alert.git /opt/quake-alert
cd /opt/quake-alert/deploy
cp .env.production.example .env
chmod 600 .env
```

Fill in every `CHANGE_ME` in `deploy/.env` (`nano .env`):

| Variable | How |
|---|---|
| `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `TELEGRAM_WEBHOOK_SECRET` | `openssl rand -hex 32`, one each |
| `WEBHOOK_SECRET_KEYS` | `docker run --rm ghcr.io/k08e24bryant/quake-alert-api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TELEGRAM_BOT_TOKEN` | From @BotFather for **@infogempasekitarbot** (never the `_dev_bot`) |

`API_DOMAIN`, `ACME_EMAIL` and `CORS_ALLOWED_ORIGINS` are already set for
`gempasekitarsaya.my.id`. Keep a copy of `.env` somewhere safe (a password manager): if the
VM is lost, `WEBHOOK_SECRET_KEYS` is the only way to read the stored webhook secrets in a backup.

**What refuses to start, on purpose:** `deploy.sh` refuses while any `CHANGE_ME` is left.
Compose refuses without `POSTGRES_*`, `REDIS_PASSWORD`, `API_DOMAIN` or `ACME_EMAIL`. And the
API and the worker refuse to start in production if `DATABASE_URL`, `REDIS_URL`,
`WEBHOOK_SECRET_KEYS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
`CORS_ALLOWED_ORIGINS` or `TRUST_PROXY_HEADERS` is missing, empty or a placeholder
(`app/core/startup.py`), naming the variables, never their values.

## 8. First deploy

Only after step 6b resolves:

```sh
cd /opt/quake-alert/deploy
./deploy.sh
```

It runs `git pull`, pulls the images, and starts PostgreSQL and Redis. Then it runs the
migrations (a one-off `migrate` container) and starts api, worker and Caddy, waiting until
every healthcheck passes. Last, it checks `/readyz` inside the stack and at
`https://api.gempasekitarsaya.my.id/readyz`. The first run waits a little for the
certificate. It ends with `deployed` and the `docker compose ps` table, every service
`(healthy)`.

Logs and status, from `deploy/` (`.env` sets `COMPOSE_FILE`, so plain `docker compose`
finds the production file): `docker compose ps`, `docker compose logs -f api worker caddy`.
Every container's log is rotated by Docker (10 MB × 5 files).

## 9. Telegram webhook

```sh
./set_telegram_webhook.sh
```

It checks the token belongs to a bot that is **not** a `*_dev_bot`, then calls `setWebhook` with
`https://api.gempasekitarsaya.my.id/v1/telegram/webhook` and `TELEGRAM_WEBHOOK_SECRET` as
`secret_token`. Last, it prints `getWebhookInfo`: `url` must be that address, and
`last_error_message` must stay `None`. The token never appears on a command line or in the
output.

Then in Telegram: open `t.me/infogempasekitarbot`, send `/start`; the bot answers with
the commands and the disclaimer. Share a location to subscribe.

The dev bot (`@infot_gempa_sekitar_dev_bot`) stays in polling mode for local development.
Never set a webhook on it, or local polling stops working.

## 10. Verify with curl

From your laptop:

```sh
API=https://api.gempasekitarsaya.my.id
curl -s $API/readyz                  # {"db":"ok","redis":"ok"}
curl -s $API/v1/status               # {"ingestion_state":"ok",...} within ~2 minutes of the first deploy
curl -s "$API/v1/earthquakes?limit=3" | head -c 300; echo
curl -sI http://api.gempasekitarsaya.my.id/readyz | head -1        # 308, redirect to HTTPS
curl -s -D - -o /dev/null -H "Origin: https://gempasekitarsaya.my.id" "$API/v1/status" | grep -i access-control-allow-origin
curl -s -D - -o /dev/null -H "Origin: https://example.com" "$API/v1/status" | grep -ci access-control   # 0
nc -vz -w 5 api.gempasekitarsaya.my.id 5432; nc -vz -w 5 api.gempasekitarsaya.my.id 6379   # both must fail
```

On the VM, the logs carry no location: neither Caddy's nor the API's log ever has a query
string (README, "Location privacy"):

```sh
curl -s "$API/v1/earthquakes?lat=-6.21&lon=106.85&radius_km=100" >/dev/null   # from the laptop
docker compose logs api caddy | grep -c "106.85"    # on the VM, in deploy/: 0
```

## 11. Frontend on Vercel

1. vercel.com → Add New → Project → import `k08e24bryant/quake-alert`.
2. **Root Directory:** `frontend`. Framework preset: Next.js (detected). Build settings: defaults.
3. **Environment variables** (Production, and Preview if you use preview deployments):

   | Name | Value |
   |---|---|
   | `NEXT_PUBLIC_API_BASE_URL` | `https://api.gempasekitarsaya.my.id` |
   | `NEXT_PUBLIC_TELEGRAM_BOT_URL` | `https://t.me/infogempasekitarbot` |
   | `NEXT_PUBLIC_REPO_URL` | `https://github.com/k08e24bryant/quake-alert` |

   They are inlined at build time: after changing one, **redeploy**.
4. Deploy.
5. **Domains** (Project → Settings → Domains): add `gempasekitarsaya.my.id` and
   `www.gempasekitarsaya.my.id`. Set `www` to **redirect** to `gempasekitarsaya.my.id`
   (308). Vercel then shows the DNS records to create. Add exactly those at Exabytes, next to
   the `api` A record, and wait until Vercel marks both domains valid.
6. Check: `https://gempasekitarsaya.my.id` shows the map with quakes and "Data terakhir
   diperbarui …". The browser console has no CORS error.

CORS: the API allows exactly the origins in `CORS_ALLOWED_ORIGINS`
(`https://gempasekitarsaya.my.id,https://www.gempasekitarsaya.my.id`). Vercel preview URLs
(`*.vercel.app`) are not in it, so previews show "Server data gempa tidak dapat dihubungi". That
is expected; add a preview origin to the list only if you need it.

## 12. Backups

`deploy/backup.sh` writes `pg_dump | gzip` to `/var/backups/quake-alert`, keeps 14 days,
and writes each file atomically (a backup is complete or absent). Add it to **root's**
crontab (`sudo crontab -e`):

```cron
15 2 * * * /opt/quake-alert/deploy/backup.sh >> /var/log/quake-alert-backup.log 2>&1
```

That is 02:15 UTC (09:15 WIB), before the app's 03:00 UTC prune. Run it once by hand:
`sudo /opt/quake-alert/deploy/backup.sh`.

**Copy backups off the VM.** They sit on the same disk as the database, so losing the VM
loses both. The simplest copy is a pull from your laptop, e.g. weekly:

```sh
rsync -av ubuntu@<VM_IP>:/var/backups/quake-alert/ ~/quake-alert-backups/
```

(The backup directory is root-only. Either run the cron job with `BACKUP_DIR` set to a
directory `ubuntu` can read, or copy with `sudo` on the VM first.) Redis isn't backed up: it
holds only caches, rate-limit counters and the job queue. Pending deliveries live in
PostgreSQL and are picked up again by the worker's sweep.

### Restore test (do it after the first backup, then monthly)

```sh
cd /opt/quake-alert/deploy
sudo ./restore.sh "$(ls -t /var/backups/quake-alert/quake_alert-*.sql.gz | head -1)"
```

It restores the newest backup into a scratch database (`restore_check`) next to the live one
and compares row counts and the Alembic revision with the live database. It checks that
PostGIS works in the copy, then drops it. The live data isn't touched. It must end with
`restore test passed`, and the counts must match or trail the live ones only by what arrived
after the backup. Note the date: a backup that has never been restored isn't a backup yet.

### Real restore (data loss)

```sh
sudo ./restore.sh /var/backups/quake-alert/quake_alert-<STAMP>.sql.gz --replace-live
```

It asks you to type the database name. Then it takes a safety backup of the current
database, stops api and worker, recreates the database from the file, runs the migrations
and starts api and worker again. Caddy keeps running and answers 502 for the minute this
takes.

## 13. Monitoring (free, external)

Use **UptimeRobot** (free plan: 50 monitors, 5-minute checks, e-mail alerts; Telegram alerts
via its integrations). Two monitors:

| Monitor | Type | URL | Alert when |
|---|---|---|---|
| API ready | HTTP(s) | `https://api.gempasekitarsaya.my.id/readyz` | not HTTP 200 (the API is down, or PostgreSQL is) |
| Data fresh | Keyword | `https://api.gempasekitarsaya.my.id/v1/status` | keyword `"ingestion_state":"ok"` **does not exist** |

The keyword monitor alerts both when ingestion is `stale` (no successful BMKG read for
`INGESTION_STALE_AFTER_MINUTES`, 5) and when the API can't answer at all. Type the keyword
exactly, with the quotes and no spaces: the API's JSON is compact. With 5-minute checks you
hear about a stale feed within about 10 minutes. The frontend shows the same state to
visitors as a banner.

`/readyz` stays 200 while only Redis is down (`"redis": "degraded"`): the API keeps serving
from PostgreSQL. A Redis outage stops the worker, though, so it shows up on the `/v1/status`
monitor a few minutes later.

(Better Stack's free plan works the same way, with a "keyword" check on `/v1/status`.)

## Updating and rolling back

- **Update:** push to `main`, wait for CI's `images` job, then on the VM: `cd
  /opt/quake-alert/deploy && ./deploy.sh`.
- **Roll back:** set `IMAGE_TAG=sha-<commit>` (any earlier tag on GHCR) in `deploy/.env` and
  run `./deploy.sh` again. Migrations only go forward. Rolling back across a migration needs
  a backup restore, or `alembic downgrade` run by hand in the `migrate` container.
- If the migrations fail, `deploy.sh` stops **before** touching api and worker, so the
  running version keeps serving.
- Never run `docker compose down -v`: `-v` deletes the database **and** Caddy's
  certificates.

## Troubleshooting

| Symptom | Look at |
|---|---|
| `deploy.sh` stops at "refusing to start in production" | The variables it names in `deploy/.env` |
| Caddy logs `challenge failed` / `NXDOMAIN` | DNS (step 6b), the security list and iptables for 80/443. Wait an hour if you hit the rate limit |
| `/readyz` → 502 | `docker compose ps`, `docker compose logs api` (in `deploy/`) |
| `/v1/status` stays `stale` | `docker compose logs worker` (BMKG reachable? Redis up?) |
| Bot doesn't answer | `./set_telegram_webhook.sh` output: `last_error_message`; `docker compose logs api` for 403s (wrong secret) |
| Frontend: "Server data gempa tidak dapat dihubungi" | Browser console (CORS?), `CORS_ALLOWED_ORIGINS`, `NEXT_PUBLIC_API_BASE_URL`, then redeploy on Vercel |
| `pull access denied` for ghcr.io | Step 1: make both packages public |
