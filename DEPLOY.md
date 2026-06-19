# Deployment guide

stack-doctor is a small pure-Python package (standard library only, no third-party dependencies).
Pick the path that matches your setup:

- **Docker** is easiest. It runs the `queue` / `providers` / `plex` / `resources` checks and the
  `warmer`. It can't restart a *host* decypharr service or read its journal, so those stay
  alert-only. Start here if your stack is all containers.
- **Host service** is full power. Run it on the same machine as decypharr (e.g. your Proxmox host)
  so it can natively restart decypharr, read its journal (the `janitor`), and warm exactly what a
  viewer opens. Use this if decypharr runs as a host service.

You only need each app's API key. In Sonarr / Radarr / Prowlarr it's under
**Settings -> General -> API Key**.

---

## Option A: Docker (compose)

1. Create `docker-compose.yml`:

```yaml
services:
  stack-doctor:
    image: ghcr.io/neoo-blue/stack-doctor:latest
    container_name: stack-doctor
    restart: unless-stopped
    environment:
      DOCTOR_MODE: cron
      DOCTOR_DRY_RUN: "true"        # start safe: log only, change nothing
      ENABLE_UI: "true"             # web dashboard
      ENABLE_QUEUE: "true"
      INSTANCE_1_NAME: sonarr
      INSTANCE_1_TYPE: sonarr
      INSTANCE_1_URL: http://sonarr:8989
      INSTANCE_1_APIKEY: your_sonarr_key
      INSTANCE_2_NAME: radarr
      INSTANCE_2_TYPE: radarr
      INSTANCE_2_URL: http://radarr:7878
      INSTANCE_2_APIKEY: your_radarr_key
    ports:
      - "12345:12345"              # dashboard
    volumes:
      - ./data:/data               # state + saved config
```

2. Start it and watch:

```bash
docker compose up -d
docker compose logs -f stack-doctor
```

3. Open the dashboard at `http://<docker-host>:12345`. With dry-run on, the Logs tab shows what it
   *would* do without touching anything. When you're happy, set `DOCTOR_DRY_RUN: "false"` (or flip
   it in the dashboard's Config tab) and restart.

Add more apps by numbering them (`INSTANCE_3_*`, `INSTANCE_4_*`, ...). Adding a Prowlarr instance
turns on the providers check. The full set of options (decypharr, plex, warmer, janitor, bazarr)
is in [`docker-compose.example.yml`](docker-compose.example.yml).

---

## Option B: host systemd service (full power)

Run this on the machine that runs decypharr.

1. Install the script:

```bash
mkdir -p /opt/stack-doctor
cd /opt/stack-doctor
curl -fsSL https://github.com/Neoo-Blue/stack-doctor/archive/refs/heads/main.tar.gz \
  | tar -xz --strip-components=1 stack-doctor-main/doctor
```

2. Install the service template and edit the values (URLs, API keys, paths):

```bash
curl -fsSL https://raw.githubusercontent.com/Neoo-Blue/stack-doctor/main/stack-doctor.service.example \
  -o /etc/systemd/system/stack-doctor.service
nano /etc/systemd/system/stack-doctor.service
```

3. Enable and follow it:

```bash
systemctl daemon-reload
systemctl enable --now stack-doctor
journalctl -u stack-doctor -f
```

4. Open the dashboard at `http://<host>:12345`.

Full power adds: restart decypharr (`DECYPHARR_RESTART_CMD=systemctl restart decypharr`), the
janitor reading its journal (`JANITOR_LOG_CMD=journalctl -u decypharr -n 10000 --no-hostname`), and,
if Plex runs in a Proxmox container, warming what viewers open
(`WARMER_PLEXLOG_CMD=pct exec <ctid> -- tail -n0 -F '<plex log>'`).

---

## Recommended first run

1. Start with `DOCTOR_DRY_RUN=true` and just `ENABLE_QUEUE`. Watch the dashboard Logs for a bit.
2. Turn off dry-run once the "would remove" lines look right.
3. Enable more checks one at a time (Config tab): `providers`, then `decypharr` / `plex` /
   `resources`, then `warmer`.
4. Risky actions stay opt-in: `DECYPHARR_RESTART_CMD`, `RES_DROP_CACHES`, and the churn brake
   (`DOCTOR_CHURN_ACTION`) do nothing until you set them.

## Lock down the dashboard (optional)

The dashboard has no auth by default. If your LAN isn't trusted, set `DOCTOR_UI_TOKEN=something`
and open `http://<host>:12345/?token=something`.

## Updating

- **Docker**: `docker compose pull && docker compose up -d`
- **Host**: re-download/extract the `doctor` package (step 1 above) and `systemctl restart stack-doctor`

Your saved settings live in `DOCTOR_CONFIG_FILE` (`/data/config.json` by default) and survive updates.
