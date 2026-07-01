# stack-doctor

**A self-hosted health daemon that auto-detects and fixes the recurring problems in a
Sonarr/Radarr + decypharr + Plex media stack.**

If you run *arr apps against usenet (decypharr, SABnzbd, NZBGet) or torrents/debrid, you know
the failure modes: downloads that finish but never import, dead grabs stuck as
`downloadClientUnavailable`, incomplete files whose corrupt headers make ffprobe choke, a
hung decypharr FUSE mount that takes Plex down, memory/load pressure that OOMs your arrs.
You only notice when something's "missing" or the family complains.

stack-doctor runs a set of **modular checks** on an interval (or on Sonarr/Radarr webhooks),
detects these, and fixes the safe ones automatically. No third-party dependencies, one small
container, everything configured by env vars.

> Born out of a long night of hand-fixing exactly these problems on a usenet *arr stack.
> Now it's a daemon so you never have to do it by hand again.

**New here? Start with the [Deployment guide](DEPLOY.md).** It gets you running in a few copy-paste steps.

## Checks (toggle each with `ENABLE_*`)

| check | detects | fixes |
|---|---|---|
| **queue** | stuck/dead/blocked *arr download-queue items | per-condition fix action: `report`, `research` (remove + blocklist -> re-search), `remove` (no blocklist), or `force_import` (ManualImport files already on disk) |
| **providers** | failed indexers / download clients (sonarr/radarr/**prowlarr**) | runs the **Test** on them to re-validate + clear the failure |
| **decypharr** | hung FUSE mount (read-test) + API down | runs your restart hook (`DECYPHARR_RESTART_CMD`) |
| **plex** | Plex unresponsive | alerts (optional library refresh) |
| **resources** | host load / low memory / swap pressure | reports; optional `drop_caches` relief |
| **janitor** | permanently-dead usenet releases (from decypharr's log) | quarantines those library symlinks (reversible) |
| **scrubber** | **proactively** scans library files for bad parts that make Plex skip mid-play (dead NZB articles, torn containers, packet corruption) | quarantines the symlink (reversible) + deletes the *arr's `moviefile`/`episodefile` with `blocklist=true` so a clean release is re-searched |
| **watchlists** | new titles on Plex Home users' + non-Home friends' watchlists that aren't in your library | adds them directly to Sonarr/Radarr (4K instance first, 1080p fallback), bypassing Overseerr entirely; per-sweep rate-cap so a dumped 300-item watchlist doesn't flood |
| **holidays** | the calendar nearing a holiday (curated per-holiday definitions, e.g. Independence Day, Halloween, Christmas) | builds a themed movie collection a few days before and pins it to Plex Home (the recommended row), then takes it down a few days after |
| **backlog** | monitored episodes/movies that are still **missing** and old enough that RSS will never reach back for them (e.g. content added after a source migration) | gently trickles interactive searches for them: a small per-sweep cap, a minimum age gate, a per-item cooldown, a load gate, and a minimum interval between sweeps so it never floods the download path |
| **riven** | a [Riven](https://github.com/rivenmedia/riven) backend that is unhealthy / has a down service, plus items wedged in a working state (Scraped/Downloaded) or never resolved (Requested/Indexed/Failed) | reports health + down services every sweep; gently retries stuck/missing items through Riven's own state machine, throttled exactly like backlog (per-sweep cap, per-item cooldown, load gate, minimum interval) |
| **mediastorm** | a [mediastorm](https://github.com/godver3/mediastorm) server that is down / not answering `/health` | alerts (health-only: mediastorm has no import queue or monitored-missing list to drain) |
| **bazarr** | Bazarr unreachable | alerts |
| **seerr** | Overseerr/Jellyseerr/Seerr requests stuck **FAILED** (the arr add timed out under load) | re-drives them so a transient blip self-heals (attempt-capped) |
| **warmer** | what a viewer is about to watch (Plex On Deck + next episode) | precaches the file head so playback starts instantly |

Safe by design: risky actions (restart, drop_caches) are **opt-in**, the queue fixer only
acts after an item is stuck for several consecutive checks, and everything supports
`DOCTOR_DRY_RUN=true`.

## Two ways to run (multi-level)

stack-doctor scales to the access it's given:

- **Container** (limited): the network/mount checks, `queue`, `plex`, the `decypharr` mount
  read-test, and `resources`. It can't restart a *host* decypharr service or read host
  journald, so leave `DECYPHARR_RESTART_CMD` empty (alert-only) or point it at
  `docker restart <decypharr>` / an SSH hook. See [`docker-compose.example.yml`](docker-compose.example.yml).
- **Host service** (full power): run it **on the same host as decypharr** (see
  [`stack-doctor.service.example`](stack-doctor.service.example)). Now it restarts decypharr
  natively (`DECYPHARR_RESTART_CMD=systemctl restart decypharr`), reads its journal for the
  janitor (`JANITOR_LOG_CMD=journalctl -u decypharr ...`), and touches the library directly,
  no container-to-host bridge needed. The *arr/Plex instances are still reached over the LAN.

Same `doctor.py`, same env vars; you just enable more checks where it has more power.

---

## What the queue check fixes

Each is a named **condition** you can enable/disable via `DOCTOR_CONDITIONS`, and each condition
maps to a **fix action** you choose via `DOCTOR_CONDITION_ACTIONS` (so the remediation fits the
failure, instead of one blunt response for everything):

| condition | what it catches | default action |
|---|---|---|
| `downloadClientUnavailable` | the download client was unreachable for this grab | **`report`** |
| `importBlocked` | completed download the *arr refuses to import | **`force_import`** |
| `importPending_warning` | completed but stuck pending with a warning | **`force_import`** |
| `importFailed` | import attempted and failed | **`research`** |
| `failedPending` | failed download awaiting handling | **`research`** |
| `stalled` | download flagged with a stall / "no files" warning | **`research`** |

The four actions:

| action | what it does | use it for |
|---|---|---|
| `report` | log only, change nothing | client-side blips (`downloadClientUnavailable`) where blocklisting a good release would be wrong |
| `research` | `DELETE` the queue item (`removeFromClient=true`, `blocklist=DOCTOR_BLOCKLIST`) so the *arr searches a **different** release | genuinely dead/stalled/failed releases |
| `remove` | same delete but **never** blocklists, so the *arr can re-grab the **same** release | flaky-but-not-bad releases you want retried, not banned |
| `force_import` | calls the *arr's **ManualImport** on the files already on disk (`DOCTOR_IMPORT_MODE` = `auto`/`move`/`copy`) | imports that stalled even though the file is fine, no re-download |

`research` (with `autoRedownloadFailed` on in your *arr) is **self-limiting**: once every bad
release for an item is blocklisted there's nothing left to grab, so the churn stops. Anything
not listed in `DOCTOR_CONDITION_ACTIONS` falls back to `DOCTOR_DEFAULT_ACTION` (`research`).

Why `downloadClientUnavailable` defaults to `report`: when the client is briefly down, every
in-flight grab reports that status. Removing + blocklisting them would ban perfectly good
releases for a problem that wasn't theirs. The `providers` and `decypharr` checks handle an
actually-down client; the queue check just waits.

---

## Quick start

```yaml
# docker-compose.yml
services:
  stack-doctor:
    image: ghcr.io/neoo-blue/stack-doctor:latest
    container_name: stack-doctor
    restart: unless-stopped
    environment:
      DOCTOR_MODE: cron               # cron | event
      DOCTOR_INTERVAL: "900"
      DOCTOR_DRY_RUN: "true"          # start safe: log only, change nothing. flip to false when happy
      ENABLE_UI: "true"              # web dashboard on :12345 (status, per-service health, warmer, config, logs)

      # ---------- queue cleaner + providers ----------
      ENABLE_QUEUE: "true"
      ENABLE_PROVIDERS: "true"       # auto-Test failed indexers / download clients (needs a prowlarr instance)
      DOCTOR_MIN_STRIKES: "2"
      DOCTOR_BLOCKLIST: "true"
      DOCTOR_CHURN_LIMIT: "3"         # after 3 dead grabs of the SAME title, stop the churn (dead usenet releases)
      DOCTOR_CHURN_ACTION: backoff    # report | park | backoff (un-monitor, then retry on DOCTOR_CHURN_BACKOFF)

      # ---------- instances (number from 1; add prowlarr for the providers check) ----------
      INSTANCE_1_NAME: sonarr
      INSTANCE_1_TYPE: sonarr
      INSTANCE_1_URL: http://sonarr:8989
      INSTANCE_1_APIKEY: your_sonarr_key
      INSTANCE_2_NAME: radarr
      INSTANCE_2_TYPE: radarr
      INSTANCE_2_URL: http://radarr:7878
      INSTANCE_2_APIKEY: your_radarr_key
      INSTANCE_3_NAME: prowlarr
      INSTANCE_3_TYPE: prowlarr
      INSTANCE_3_URL: http://prowlarr:9696
      INSTANCE_3_APIKEY: your_prowlarr_key
      # Riven / mediastorm backends use the same INSTANCE_N_* slots (TYPE selects the client):
      # INSTANCE_4_NAME: riven
      # INSTANCE_4_TYPE: riven            # rivenmedia/riven (needs ENABLE_RIVEN + INSTANCE_4_APIKEY = its x-api-key)
      # INSTANCE_4_URL: http://riven:8080
      # INSTANCE_4_APIKEY: your_riven_key
      # INSTANCE_5_NAME: mediastorm
      # INSTANCE_5_TYPE: mediastorm       # godver3/mediastorm (needs ENABLE_MEDIASTORM; APIKEY optional, /health is unauth)
      # INSTANCE_5_URL: http://mediastorm:7777

      # ---------- warmer: precache likely-next media so playback starts instantly ----------
      ENABLE_WARMER: "true"
      PLEX_URL: http://plex:32400
      PLEX_TOKEN: your_plex_token
      WARMER_SOURCES: "ondeck,next"   # what's about to be watched (add "recent" for newly-added)
      WARMER_PRECACHE_MB: "24"        # head pulled per title (small = fast warm; decypharr/rclone read-ahead does the rest)
      WARMER_PARTS: "1"               # warm only the highest-res version, not the 4K AND 1080p
      WARMER_LOAD_MAX: "12"           # pause background warming above this host load (protect live playback)
      # warm the exact title a viewer opens (tail Plex's server log; needs vfs cache on the mount):
      # WARMER_PLEXLOG_FILE: "/plexlog/Plex Media Server.log"
    ports:
      - "12345:12345"               # web dashboard
    volumes:
      - ./data:/data                 # state + saved config
      # - /path/to/plex/logs:/plexlog:ro   # only for detail-page warming
```

```bash
docker compose up -d
docker compose logs -f stack-doctor      # or open the dashboard at http://<host>:12345
```

The exhaustive example (decypharr restart hook, janitor, bazarr, resources, event mode, every
`WARMER_*` knob) is in [`docker-compose.example.yml`](docker-compose.example.yml), and a step-by-step
walkthrough is in the [Deployment guide](DEPLOY.md). **Tip:** it starts in `DOCTOR_DRY_RUN` above so
you can watch the Logs tab and see what it *would* do before letting it act.

---

## Web dashboard

Set `ENABLE_UI=true` and open `http://<host>:12345` for a simple, dependency-free dashboard:

- **Dashboard**: which checks are on/off; the live up/down + version + health-warning count of every
  monitored service (each *arr, Prowlarr, decypharr, Plex, Bazarr); and warmer stats, total warmed
  plus a feed of *what* was warmed and *why* (`ondeck` / `next` / `detail-page`).
- **Scout**: a hand-drawn acquisition front-end. Search a title, pick a result, hit **Get**, and
  watch it move through `searching -> grabbed -> downloading -> importing -> verifying -> kaboom`,
  then click straight through to **Play in Plex**. See the [Scout](#scout-acquire-from-the-dashboard)
  section below.
- **Config**: edit the common tuning knobs and save. Changes write to `DOCTOR_CONFIG_FILE` and apply
  on restart (there's a "Save and Restart" button). Secrets (API keys, tokens) are never shown.
- **Setup**: a first-run onboarding wizard (see below).
- **Logs**: a live tail of `DOCTOR_LOG_FILE`.

The whole UI runs on a token-driven theme system with two independent axes: a **theme** dropdown and
a **light / dark** switch, both in the header. Each is saved to `localStorage` (`sd-theme`, `sd-mode`)
and applied before first paint, so the look sticks across tabs and reloads. Two themes ship today,
each with a light and a dark palette: **Pencil** (hand-drawn paper by day, chalkboard by night, the
default) and **Cyber** (neon on glass, dark or daylight). Theme and mode are independent, so any of
the four combinations is one click apart. Adding another theme is deliberately small (see
[Adding a theme](#adding-a-theme)).

It runs inside the daemon's own process (no extra container). Gate it with `DOCTOR_UI_TOKEN` if your
LAN isn't trusted. In event mode the webhook listener (`DOCTOR_PORT`) and the dashboard
(`DOCTOR_UI_PORT`) run side by side.

### Adding a theme

Every color, font, radius, shadow and background in the UI is a CSS custom property (a token). A
theme is two palette blocks (one per mode) plus one registry entry, all in the `UI_HTML` string:

1. In the `<style>` block, copy the two `html[data-theme=cyber][data-mode=light]{...}` and
   `html[data-theme=cyber][data-mode=dark]{...}` blocks, rename them to your id (for example
   `html[data-theme=blueprint][data-mode=light]{...}` and `...[data-mode=dark]{...}`), and set the
   tokens. Each block is a complete, self-contained palette, so define every token; the shared font
   families in `:root` are the only thing you inherit.
2. In the boot script, add `{id:'blueprint',name:'Blueprint'}` to the `THEMES` array.

That is the whole change. The dropdown, the light/dark switch, persistence, and app-wide application
are automatic, and every tab (Dashboard, Scout, Config, Logs, Setup) recolors from the same tokens.

---

## Onboarding (Setup wizard)

If you start the container with nothing configured (no instances and no `PLEX_URL`), stack-doctor
comes up in onboarding mode and the dashboard opens straight onto the **Setup** tab, drawn in the
same hand-sketch style as Scout. You do not have to hand-write env vars or the compose file first;
just set `ENABLE_UI=true`, start it, and open the dashboard.

- **Auto-detect**: probes the usual container names, `localhost`, and the docker host for Radarr,
  Sonarr, Prowlarr, Plex, decypharr, Riven, Overseerr/Jellyseerr and Bazarr. It fills in the URLs it
  finds. It is a short, targeted probe of well-known names and ports, not a subnet scan. API keys
  cannot be read from another container, so you paste those (each row has a **Test** button that
  validates the URL + key live).
- **Manual add** (Advanced mode): add any service by hand, pick its type, paste URL + key.
- **Easy vs Advanced**: Easy shows the essentials and picks sensible `ENABLE_*` defaults from what you
  filled in. Advanced exposes every service, the warmer mount, decypharr, and per-check toggles.
- **Warmer volume hint**: the warmer reads media files straight off disk, so it needs your library
  bind-mounted into the container. If no library mount is visible, the wizard shows exactly what to
  add to `docker-compose.yml` (and when to set `WARMER_PATH_MAP`).
- **Save & start**: writes everything (including `DOCTOR_ONBOARDED=true`) to `DOCTOR_CONFIG_FILE`,
  then offers a one-click restart to apply. Nothing is applied until you restart. The **Setup** tab
  stays available afterward so you can re-run it to add or change services.

Onboarding only writes to `DOCTOR_CONFIG_FILE`; it never edits your compose file. Make sure that path
is on a writable volume (the wizard warns you up front if it is not).

---

## Configuration (all via env vars)

### Behaviour

| var | default | meaning |
|---|---|---|
| `DOCTOR_MODE` | `cron` | `cron` (interval sweeps) or `event` (Sonarr/Radarr webhook) |
| `DOCTOR_INTERVAL` | `900` | cron: seconds between sweeps |
| `DOCTOR_MIN_STRIKES` | `2` | item must be stuck this many consecutive checks before action (ignores transient blips like a download-client restart) |
| `DOCTOR_MAX_ACTIONS` | `20` | max removals per sweep (rate limit, keeps re-searches gentle) |
| `DOCTOR_BLOCKLIST` | `true` | when a `research` action removes a grab, also blocklist it so a *different* release is fetched |
| `DOCTOR_CONDITION_ACTIONS` | *(safe defaults)* | per-condition fix map, e.g. `stalled=research,importBlocked=force_import,downloadClientUnavailable=report`. Unset conditions use their built-in default (see condition table), then `DOCTOR_DEFAULT_ACTION` |
| `DOCTOR_DEFAULT_ACTION` | `research` | fallback action for any condition not in the map: `report` / `research` / `remove` / `force_import` |
| `DOCTOR_IMPORT_MODE` | `auto` | how `force_import` brings files in: `auto` (let the *arr decide), `move`, or `copy` |
| `DOCTOR_CHURN_LIMIT` | `0` | churn brake: after this many dead grabs of the *same* episode/movie, stop the loop (`0` = off). Catches releases that re-grab despite blocklist, or titles where only dead releases exist |
| `DOCTOR_CHURN_ACTION` | `report` | what the brake does: `report` (log only), `park` (un-monitor), or `backoff` (un-monitor, then auto re-monitor on the schedule below for a fresh try) |
| `DOCTOR_CHURN_BACKOFF` | `10m,1h,24h` | `backoff`: escalating retry schedule (`s`/`m`/`h`/`d` units). Each park steps to the next delay; the last entry repeats. Default = retry 10m after the 1st park, 1h after the 2nd, every 24h after. (Legacy `DOCTOR_CHURN_COOLDOWN` still honored as a single fixed delay.) |
| `DOCTOR_REMOVE_FROM_CLIENT` | `true` | also remove from the download client |
| `DOCTOR_DRY_RUN` | `false` | `true` = log only, change nothing |
| `DOCTOR_CONDITIONS` | *all* | comma list of conditions to act on (see table above) |
| `DOCTOR_LOAD_MAX` | `0` | if > 0, skip a sweep when host 1-min load exceeds it (mount `/proc/loadavg:ro`) |
| `DOCTOR_HEALTH_REPORT` | `true` | log *arr `/health` warnings at debug level |
| `DOCTOR_STATE_FILE` | `/data/state.json` | where strike counts persist |
| `DOCTOR_PORT` | `8088` | webhook port (event mode) |
| `ENABLE_UI` | `false` | serve the web dashboard (status, per-service health, warmer stats, editable config, live logs) |
| `DOCTOR_UI_PORT` | `12345` | dashboard port |
| `DOCTOR_UI_TOKEN` | *(none)* | if set, require `?token=` or an `X-Doctor-Token` header to reach the dashboard |
| `DOCTOR_CONFIG_FILE` | `/data/config.json` | overlay the dashboard writes edited settings to (merged over env at startup; applies on restart) |
| `DOCTOR_TRIGGER_EVENTS` | `Download,ManualInteractionRequired,DownloadFailed,Grab` | webhook events that trigger a sweep |
| `DOCTOR_LOG_LEVEL` | `INFO` | `DEBUG` for verbose |

### Seerr (failed-request retry)

`seerr` watches Overseerr / Jellyseerr / Seerr for requests stuck in **FAILED**. seerr hands an
approved request to Radarr/Sonarr with a fixed ~10s timeout and never retries on its own, so when
the arr is briefly slow (heavy search load, host CPU/RAM contention) the add times out and the title
is silently marked failed and never lands. Each sweep this re-drives those failed requests so a
transient blip self-heals; a per-request attempt cap stops it looping on a request that fails for a
real reason (dead TMDB id, removed title).

| var | default | meaning |
|---|---|---|
| `ENABLE_SEERR` | `false` | turn the check on (needs `SEERR_URL` + `SEERR_APIKEY`) |
| `SEERR_URL` | *(none)* | e.g. `http://seerr:5055` (Overseerr / Jellyseerr / Seerr share this API) |
| `SEERR_APIKEY` | *(none)* | from *Settings → General → API Key* |
| `SEERR_RETRY_MAX` | `10` | max requests retried per sweep (rate-limit the re-adds) |
| `SEERR_MAX_ATTEMPTS` | `5` | give up on a request after this many auto-retries (`0` = never give up) |

Honors `DOCTOR_DRY_RUN` (logs what it would retry, changes nothing).

### Instances

Add as many as you want, numbered from 1:

| var | example |
|---|---|
| `INSTANCE_<n>_TYPE` | `sonarr` or `radarr` |
| `INSTANCE_<n>_URL` | `http://sonarr:8989` |
| `INSTANCE_<n>_APIKEY` | from *Settings → General* |
| `INSTANCE_<n>_NAME` | `sonarr4k` (optional label) |

---

## Cron vs Event mode

**Cron** (default): a daemon that sweeps every `DOCTOR_INTERVAL` seconds. Simple, reliable,
catches everything within ~`INTERVAL × MIN_STRIKES`.

**Event**: stack-doctor runs a tiny webhook server. Point each *arr at it
(*Settings → Connect → Webhook*, URL `http://stack-doctor:8088`, enable **On Grab / On Import / On Manual Interaction Required**) and it sweeps the moment the *arr reports trouble.
A slow safety-net sweep still runs in the background in case a webhook is missed. In event
mode you'll usually set `DOCTOR_MIN_STRIKES: "1"` to act immediately, the event already
confirms the item is stuck.

---

## How the strike system works

To avoid over-reacting, an item is only removed once it's been seen stuck on
`MIN_STRIKES` **consecutive** checks. Counts persist in `/data/state.json`. This is what
stops it from blocklisting items that are merely *temporarily* unavailable (for example while
your download client restarts). Anything that recovers on its own is left alone.

---

## Playback warmer (instant start)

On a usenet/debrid FUSE mount, the slow part of pressing **Play** is decypharr fetching the
first segments from the provider, the few seconds (or, for 4K, *many* seconds) of "buffering"
before it starts. The warmer pre-pays that cost: it asks Plex what a viewer is **about to
watch** and reads the head of those files through the mount ahead of time, pulling them into
decypharr's cache so playback starts instantly.

Measured on a live stack: an untouched 1080p file served its first 8 MB in **2.7 s**; once
warmed, **0.02 s**. A cold 4K head took **15 s** to fetch, paid in advance instead of at Play.

**What it warms** (`WARMER_SOURCES`, default `ondeck,next`):
- `next` , the next episode(s) of anything currently playing (great for binge sessions). Polled
  every `WARMER_INTERVAL`.
- `ondeck` , everything in Continue Watching / On Deck. Refreshed every `WARMER_ONDECK_EVERY`. Toggle
  it on its own with **`WARMER_ONDECK`** (`true`/`false`) without touching the rest, useful on small or
  RAM-backed caches where you only want just-in-time warming. (See also low-cache mode below.)
- `recent` , the N most-recently-added per library (`WARMER_RECENT_COUNT`).
- **detail-page** , the exact title a viewer **opens the page for**, warmed the instant they open it
  (see `WARMER_PLEXLOG_CMD`/`_FILE` below). This is the true pre-play signal, precise and light.

**Works with any caching mount, not just decypharr.** The warmer never talks to decypharr; it
just reads the head of the file at the path Plex reports, so the bytes land in whatever cache
backs that mount. The only requirement is that the mount actually *caches reads*:

- **decypharr** , its vfs + DFS disk cache keep the warmed head; nothing to configure.
- **rclone** , run the mount with **`--vfs-cache-mode full`** (the usual Plex-on-debrid setup).
  A head-read is then stored in rclone's on-disk vfs cache and serves Play instantly; how long
  it stays warm follows `--vfs-cache-max-age`. With `--vfs-cache-mode off` (pure passthrough,
  no cache) warming has little effect, the bytes aren't kept.
- **zurg / NFS / any other mount** , same rule: helps if it caches reads, no-op if it doesn't.

If stack-doctor runs where the path differs from Plex's, set `WARMER_PATH_MAP=plexPrefix:localPrefix`.

The warmer is self-contained: you can run it **on its own** with every other check disabled
(`ENABLE_WARMER=true`, all other `ENABLE_*=false`) , it needs only `PLEX_URL` + `PLEX_TOKEN`,
no *arr instances.

**Warming the exact title you open.** Plex's API and webhooks are playback-only, but its *server
log* records the `/extras` (and native-app `includeExtras=1`) request a client makes the moment you
open a title's detail page, so this works for the Plex app **and** third-party clients like Infuse.
Point `WARMER_PLEXLOG_CMD` (a streaming command, e.g. `tail -F`, or
`pct exec <ct> -- tail -n0 -F '<log>'` to reach Plex in a Proxmox container) or `WARMER_PLEXLOG_FILE`
(a readable log path) at that log and the warmer pre-warms precisely what you're looking at, off a
background thread so the tailer stays responsive. This is the most accurate, lowest-cost signal; the
`ondeck`/`next` cycle is the zero-interaction backstop (resume + binge).

It does **not** force-delete warmed bytes: the mount's cache is itself the speed win and already
evicts by age/LRU. Instead it keeps speculative cost low , a small head, a per-cycle cap, a re-warm
cooldown, a host-load guard, and a hard pause on background warming whenever **anyone is watching**,
so it never competes with a live stream. The title you actively open still warms instantly, in its
own concurrency lane, even during playback.

| var | default | meaning |
|---|---|---|
| `ENABLE_WARMER` | `false` | turn the warmer on (needs `PLEX_URL` + `PLEX_TOKEN`) |
| `WARMER_PRECACHE_MB` | `64` | how much of each file's head to pull into cache |
| `WARMER_TAIL_MB` | `8` | also pull the tail (mkv cues / Plex end-probe); `0` = off |
| `WARMER_SOURCES` | `ondeck,next` | background signals to warm from (`ondeck`, `next`, `recent`). Detail-page warming is separate, via the log vars below |
| `WARMER_ONDECK` | `true` | quick on/off for **Continue Watching** (On Deck) warming, without editing `WARMER_SOURCES` |
| `WARMER_PLEXLOG_CMD` | *(none)* | stream command for Plex's server log (e.g. `tail -n0 -F '<log>'`, or `pct exec <ct> -- tail -n0 -F '<log>'`). Enables detail-page warming |
| `WARMER_PLEXLOG_FILE` | *(none)* | a directly-readable path to Plex's log (alternative to `_CMD`) |
| `WARMER_INTERVAL` | `120` | seconds between session polls (next-episode prefetch) |
| `WARMER_ONDECK_EVERY` | `600` | seconds between On Deck / recent warms |
| `WARMER_NEXT_EPISODES` | `1` | how many upcoming episodes of an active show to warm |
| `WARMER_NEXT_REMAINING_MIN` | `0` | warm the next episode only when this many minutes (or fewer) are left in the current one (`0` = as soon as playback is seen) |
| `WARMER_LOW_CACHE` | `false` | **low-cache mode** for small / RAM-backed caches: skip On Deck warming entirely and warm the next episode only as the current one nears its end (defaults the threshold above to 10 min). Keeps almost nothing pre-warmed |
| `WARMER_RECENT_COUNT` | `0` | warm N most-recently-added per library (`0` = off) |
| `WARMER_MAX_PER_CYCLE` | `12` | cap warms per cycle (rate-limit the usenet fetch) |
| `WARMER_COOLDOWN` | `3600` | don't re-warm the same file within this many seconds |
| `WARMER_LOAD_MAX` | `0` | pause speculative (on-deck/recent) warming while host 1-min load is above this (`0` = off). A title you *actively open* tolerates 2x this before yielding. Set it to protect live playback |
| `WARMER_CONCURRENCY` | `2` | simultaneous **background** (on-deck/recent) warm reads. Kept low so background warming never starves live playback of usenet connections |
| `WARMER_OPEN_CONCURRENCY` | `4` | simultaneous **detail-page** warm reads, a separate lane so a title you actively open starts warming instantly and never queues behind background warming |
| `WARMER_PARTS` | `1` | how many versions per title to warm (`1` = highest-resolution only; `0` = all). Stops a 1080p you'll never play from warming alongside the 4K and clogging the lane |
| `WARMER_READ_TIMEOUT` | `60` | abandon a single warm read after this long (hung-mount guard) |
| `WARMER_PATH_MAP` | *(none)* | `plexPrefix:hostPrefix` if Plex's file path differs from this host's |

> Items with multiple versions (e.g. a 4K and a 1080p file on one movie) warm every version,
> since the warmer can't know which the client will pick. Lower `WARMER_MAX_PER_CYCLE` or
> `WARMER_PRECACHE_MB` if that's too much speculative fetching for your provider.

---

## Scrubber (proactive file integrity)

The janitor is *reactive*: it can only quarantine a dead file once Plex (or something else) has
actually tried to read through it and decypharr has logged the failure. By then a viewer
already saw "skip" or "buffering forever". The **scrubber** is the proactive counterpart, it
walks the library and verifies each file before anyone hits a bad spot.

It is **tiered**, cheapest-first:

| tier | what it does | what it catches | cost |
|---|---|---|---|
| 1 *(default)* | `ffprobe` parses the container header | **torn / incomplete containers** (the only failure mode that can be verified reliably without false positives on a stream-fetched library) | ~1 s |
| 2 *(opt-in)* | + `ffmpeg -v error` decodes a few seconds at N seek points (via `-map 0:v:0 -f null -`) | mid-file dead NZB articles + packet/codec corruption | ~`SKIM_POINTS × SKIM_SECS × bitrate` (a few hundred MB per file at 1080p) |
| 3 *(opt-in)* | + full `ffmpeg -v error -f null -` decode of the whole file | anything tier-2 missed | slow (whole file restreamed) |

**Why tier 1 is the default**: on a decypharr / rclone / zurg style FUSE mount the kernel
sees a regular file, but reads of uncached chunks return EOF (or 0 bytes) instead of blocking
to fetch them, and the actual fetch time varies wildly (1 s to 60 s+). Both raw byte
sampling and `ffmpeg -ss` skimming false-positive on cold chunks in that environment —
flagging healthy files as having dead segments. Tier 1 only reads the header (always cached
or trivially fetchable) so it's the only check that cannot be tricked by cold-cache
behavior. Tiers 2 and 3 stay available for libraries on local disk, or as opt-in slow scans
where you accept some false-positive risk in return for catching mid-file rot.

The complementary reactive piece on this stack is the [**janitor** check](#checks-toggle-each-with-enable_)
which tails decypharr's log for `ARTICLE_NOT_FOUND` / "still missing" errors and quarantines
the affected library symlinks once Plex (or anything else) has actually attempted the read.
Together, **tier-1 scrubber + janitor** covers torn containers proactively and confirmed
dead segments reactively.

A confirmed **bad** result quarantines the library symlink (reversible manifest under
`SCRUBBER_QUARANTINE_DIR`, same shape as the janitor's) and **deletes the owning arr's
`moviefile`/`episodefile` with `blocklist=true`** — the arr then re-searches and grabs a
clean release on its own. With `SCRUBBER_FULL_DECODE_ON_BAD=true`, a tier-2 BAD is verified
by a full decode before action.

State (`SCRUBBER_STATE_FILE`) caches `(path, size, mtime) -> result`, so the scan is
**incremental**: once a file is OK, it is not re-checked until it changes (or until
`SCRUBBER_REVERIFY_DAYS` has passed — usenet retention rots over time).

### Configuration

| var | default | meaning |
|---|---|---|
| `ENABLE_SCRUBBER` | `false` | turn on the scrubber check |
| `SCRUBBER_PATHS` | (falls back to `JANITOR_LIBRARY_PATHS`) | comma list of library roots to walk |
| `SCRUBBER_TIER` | `1` | maximum tier to apply (1 = header only, 2 = +ffmpeg skim, 3 = +full decode). Default tier 1 is the only one safe on a decypharr / rclone / zurg FUSE mount — see Scrubber section above. |
| `SCRUBBER_FULL_DECODE_ON_BAD` | `false` | final-confirm a tier-2 BAD with a full ffmpeg decode before action (slow; off by default) |
| `SCRUBBER_SKIM_POINTS` | `4` | tier 2: seek points across the duration |
| `SCRUBBER_SKIM_SECS` | `5` | tier 2: seconds decoded at each point |
| `SCRUBBER_MAX_FILES` | `50` | files scanned per sweep (rate limit) |
| `SCRUBBER_CONCURRENCY` | `1` | parallel scans (1 = kindest to decypharr) |
| `SCRUBBER_LOAD_MAX` | `12` | skip sweep if 1-min host load is above this (0 = off) |
| `SCRUBBER_STRIKES` | `2` | consecutive bad reads before action (transient mount blips do not cost re-grabs) |
| `SCRUBBER_STATE_FILE` | `/data/scrubber.json` | per-file result cache |
| `SCRUBBER_QUARANTINE_DIR` | `JANITOR_QUARANTINE_DIR` or `/data/quarantine` | where quarantined symlinks land (with `manifest.json` for undo) |
| `SCRUBBER_DELETE_ARR_FILE` | `true` | delete the arr's `moviefile`/`episodefile` (with `blocklist=true`) so it re-searches; `false` = quarantine only |
| `SCRUBBER_EXTENSIONS` | `.mkv,.mp4,.avi,.m4v,.ts` | which file extensions to scan |
| `SCRUBBER_MIN_AGE_HOURS` | `1` | skip files newer than this (don't fight the warmer / fresh imports) |
| `SCRUBBER_REVERIFY_DAYS` | `30` | re-check previously-OK files after N days; usenet retention rots (`0` = never re-check) |
| `SCRUBBER_HEADER_TIMEOUT` | `30` | per-file ffprobe timeout (tier 1) |
| `SCRUBBER_SKIM_TIMEOUT` | `180` | per-skim-point ffmpeg timeout (tier 2) |
| `SCRUBBER_FULL_TIMEOUT` | `1800` | full-decode timeout (tier 3) |
| `SCRUBBER_FFPROBE` / `SCRUBBER_FFMPEG` | `ffprobe` / `ffmpeg` | binaries (override to use a sandboxed build) |

The scrubber needs **direct read access** to the library, so it is best run as a **host
service on the same host as decypharr** (where `/mnt/library` is real). It honors
`DOCTOR_DRY_RUN=true` (logs what it would quarantine + which arr file it would delete,
changes nothing).

## Watchlists (Plex Home + friends -> arrs, no Overseerr)

For people you trust enough that you don't want them clicking through an Overseerr approval
flow. The check polls each watchlist on the configured interval, diffs against your current
Sonarr / Radarr library, and adds new titles directly. Plex Home users are enumerated
automatically from your owner `PLEX_TOKEN`; non-Home friends each give you their own
`X-Plex-Token` (Plex Web -> any item -> ... -> Get Info -> View XML -> URL has the token).

| var | default | meaning |
|---|---|---|
| `ENABLE_WATCHLISTS` | `false` | turn the check on |
| `WATCHLISTS_FRIENDS` | *(none)* | comma list of `label:token` pairs for non-Home Plex friends, e.g. `alice:xxxxxx,bob:yyyyyy` |
| `WATCHLISTS_INCLUDE_HOME` | `true` | also pull every Plex Home / managed-user watchlist via your owner token |
| `WATCHLISTS_HOME_PINS` | *(none)* | PINs for managed users that have one set, as `userUuid:1234,userUuid:5678` |
| `WATCHLISTS_QUALITY` | *(use default)* | per-source quality preference. Format: `label=quality,label=quality` with `*` as wildcard, e.g. `*=both,home/kids=1080p,alice=4k`. Quality must be `4k`, `1080p`, or `both`. Labels match what the source is logged as (`home/<title>` for Plex Home users, the friend's label for non-Home friends) |
| `WATCHLISTS_DEFAULT_QUALITY` | `both` | fallback quality when no explicit rule matches. `4k` / `1080p` / `both` |
| `WATCHLISTS_PREFER_4K` | `true` | (legacy) only used if `WATCHLISTS_QUALITY` and `WATCHLISTS_DEFAULT_QUALITY` are unset; kept for back-compat |
| `WATCHLISTS_PAGE_SIZE` | `100` | page size when crawling the Plex Discover watchlist endpoint (Plex caps `Container-Size` so anything over 100 returns 400) |
| `WATCHLISTS_MAX_ADDS_PER_SWEEP` | `25` | rate-cap so a friend with a 300-item watchlist doesn't all land at once |
| `WATCHLISTS_PROFILES` | *(auto-pick)* | override `qualityProfileId` per arr, e.g. `radarr=1,sonarr=4,radarr4k=5,sonarr4k=5`. When unset, picks the first profile each arr returns. |
| `WATCHLISTS_STATE_FILE` | `/data/watchlists.json` | per-title `(tmdb|tvdb):id -> {added_to, ts, from}` cache so the same item isn't re-attempted |
| `WATCHLISTS_HTTP_TIMEOUT` | `20` | HTTP timeout for plex.tv + arr lookups |

The check needs `PLEX_TOKEN` (owner) for the Home enumeration and the configured `INSTANCE_n_*`
arrs to actually add to. Honors `DOCTOR_DRY_RUN` (logs each WOULD-add, changes nothing). Adds
are idempotent at the arr level - if a title is already present it's skipped via the per-sweep
library index. A title successfully added is recorded in the state file so we don't re-poke
the arrs every sweep for the same items.

**Per-source quality preference** (`WATCHLISTS_QUALITY`) decides where each user's adds land:
`4k` only routes to the Sonarr4K/Radarr4K instance, `1080p` only to the standard one, and
`both` adds to BOTH (two arr records per title, so 4K plays first with the 1080p as a Plex
fallback version). Single-quality adds fall back to the other tier on failure (so an
unavailable 4K release still gets the 1080p drop). `both` runs each tier independently — 4K
failing doesn't block 1080p and vice versa.

## Holidays (pre-holiday themed Plex rows)

Builds a themed movie collection a few days before each holiday and pins it to Plex Home (the
recommended row your household sees on the home screen), then removes it a few days after. The
curation is a hardcoded per-holiday definition (overridable via JSON). Each holiday matches
films four ways, unioned:

- **`countries`** - every film whose Plex production-country tag matches (e.g. all Canadian
  films for Canada Day, all China / Hong Kong / Taiwan films for Spring Festival). This is the
  self-maintaining signal: national-cinema holidays grow automatically as the library grows, no
  hand-curation. Friendly names resolve to Plex's exact tags (`korea` -> Republic of Korea,
  `taiwan` -> Taiwan Province of China, `uk` -> United Kingdom)
- **`genre`** - every film in a Plex genre (e.g. all Horror for Halloween)
- **`keywords`** - substring match on the film title (catches the obvious ones automatically)
- **`titles`** - exact film titles (case-insensitive), a true hand-curated list

All matching is metadata-only (no file reads), so it is safe on a decypharr / FUSE library.

**Pick your country (or several).** `HOLIDAYS_COUNTRIES` (default `us`) selects which curated
sets to merge; shared holidays (New Year, Halloween, Christmas, ...) are deduped so only one
collection is built per name. When several holidays overlap (late December stacks Christmas +
Boxing Day + New Year), the one whose date is **nearest today** is shown, except holidays from
the **first country listed** outrank a nearer foreign one (so with `us,canada` Independence Day
stays pinned through Jul 4 rather than yielding to the closer Canada Day on Jul 1).

| country | sample holidays (themed collections) |
|---|---|
| `us` | New Year, Valentine's, St. Patrick's, Independence Day, Halloween, Thanksgiving (4th Thu Nov), Christmas |
| `canada` | Canada Day, Canadian Thanksgiving (2nd Mon Oct), Halloween, Christmas, Boxing Day |
| `uk` | Bonfire Night (Nov 5), Halloween, Christmas, Boxing Day |
| `australia` | Australia Day (Jan 26), ANZAC Day (Apr 25), Halloween, Christmas, Boxing Day |
| `china` | Spring Festival, Qingming, Dragon Boat, Mid-Autumn, National Day (Oct 1) |
| `japan` | New Year (Shogatsu), Tanabata, Obon, Halloween, Christmas |
| `korea` | Seollal, Chuseok, Liberation Day (Aug 15), Halloween, Christmas |

Lunar / solar-term holidays (Spring Festival, Mid-Autumn, Seollal, Chuseok, Dragon Boat,
Qingming) carry an explicit per-year date table (2026-2030 built in; extend in `doctor.py` or
override via `HOLIDAYS_DEFINITIONS`). National-cinema holidays (Canada Day, Spring Festival,
National Day, Shogatsu, Seollal/Chuseok/Liberation Day) match by Plex production country, so
they populate from the whole library regardless of language and need no per-title curation. The
purely themed rows (Christmas, Halloween, Valentine's, Independence Day, ...) still lean on
English title keywords + Plex genres; tune any of those with explicit `titles`.

| var | default | meaning |
|---|---|---|
| `ENABLE_HOLIDAYS` | `false` | turn the check on (needs `PLEX_URL` + `PLEX_TOKEN`) |
| `HOLIDAYS_COUNTRIES` | `us` | comma list of countries to merge: `us,canada,uk,australia,china,japan,korea` |
| `HOLIDAYS_MOVIE_SECTION` | *(auto)* | Plex movie library section id; blank auto-detects the first `movie`-type section |
| `HOLIDAYS_LEAD_DAYS` | `7` | default days **before** the date to show the row (a per-holiday `lead` in the definition overrides it) |
| `HOLIDAYS_POST_DAYS` | `3` | default days **after** the date to keep it before removing (per-holiday `post` overrides) |
| `HOLIDAYS_PIN_HOME` | `true` | pin the active collection to Plex Home (Recommended / Own Home / Shared Home); `false` just creates the collection |
| `HOLIDAYS_DEFINITIONS` | *(built-in)* | JSON list overriding the curated holidays, e.g. `[{"name":"Independence Day Movies","month":7,"day":4,"lead":12,"keywords":["independence day","patriot"],"titles":["Top Gun: Maverick"]}]` |
| `HOLIDAYS_STATE_FILE` | `/data/holidays.json` | records the last-active / built / removed collection per run |
| `HOLIDAYS_HTTP_TIMEOUT` | `40` | HTTP timeout for the Plex calls |

Each definition is `{"name", "month", "day"}` plus any of `lead` / `post` / `countries` /
`keywords` / `titles` / `genre`. Floating dates use either `"rule":"thanksgiving"` (4th Thursday of November),
`"rule":"nth_weekday"` with `"weekday"` (Mon=0..Sun=6) + `"n"` (e.g. Canadian Thanksgiving =
`month:10, weekday:0, n:2`), or a per-year `"dates":{"2026":"2026-02-17",...}` table for
lunar / solar-term holidays. Honors `DOCTOR_DRY_RUN` (logs each WOULD-create / WOULD-remove, changes nothing). The
collection is a fixed set of ratingKeys (`smart=0`), so it is rebuilt fresh each season rather
than tracking the library live. Out-of-season collections whose title matches one of the
definitions are taken down automatically, so only the in-season row is ever pinned.

## Backlog (drain monitored-but-missing, gently)

RSS only looks **forward**: when you add a series/movie (or migrate to a new source), anything
already aired/released in the past is monitored-missing but never searched again unless you do
it by hand. This check trickles those searches automatically without ever flooding the download
path, the *arr APIs, or the host.

Each sweep it pulls `wanted/missing` from the chosen instances, picks the oldest items not on
cooldown, and fires one interactive search command (`EpisodeSearch` / `MoviesSearch`). Five
gates keep it gentle:

- **`BACKLOG_PER_SWEEP`** - hard cap on searches triggered per sweep (shared across instances).
- **`BACKLOG_MIN_AGE_DAYS`** - only items whose air/release date is this many days in the past
  (younger ones are left to RSS / normal monitoring).
- **`BACKLOG_RETRY_DAYS`** - per-item cooldown; once searched, an item is not retried within
  this window even if it is still missing (so unavailable titles aren't re-hammered).
- **`BACKLOG_LOAD_MAX`** - skip the whole sweep while host load is above this, so a busy host
  (Plex playback, other downloads) is never piled onto.
- **`BACKLOG_INTERVAL`** - minimum seconds between real sweeps. In `event` mode the daemon
  sweeps on every webhook (each grab the backlog itself causes triggers more sweeps), so this
  throttles the true grab-rate to `BACKLOG_PER_SWEEP` per interval regardless of webhook volume.

| var | default | meaning |
|---|---|---|
| `ENABLE_BACKLOG` | `false` | turn the check on |
| `BACKLOG_INSTANCES` | `sonarr,radarr` | which instance names to drain (e.g. add `sonarr4k,radarr4k` later) |
| `BACKLOG_PER_SWEEP` | `5` | max searches per sweep |
| `BACKLOG_MIN_AGE_DAYS` | `7` | only search items aired/released at least this long ago |
| `BACKLOG_RETRY_DAYS` | `7` | per-item cooldown before a still-missing item is searched again |
| `BACKLOG_LOAD_MAX` | `12` | skip the sweep while host load exceeds this (`0` ignores load) |
| `BACKLOG_INTERVAL` | `900` | minimum seconds between real sweeps (throttles grab-rate in event mode) |
| `BACKLOG_MAX_FETCH` | `2000` | cap on missing records pulled per instance per sweep |
| `BACKLOG_STATE_FILE` | `/data/backlog.json` | records per-item cooldowns + last-sweep timestamp |

Honors `DOCTOR_DRY_RUN` (logs each WOULD-search, fires nothing). At the defaults it drains about
`BACKLOG_PER_SWEEP` items every `BACKLOG_INTERVAL`, so a large backlog fills over days rather
than in one flood, keeping Plex responsive throughout.

## Riven (health + retry stuck/missing)

If you run [Riven](https://github.com/rivenmedia/riven) as a media backend, this check watches it
the same way the *arr checks watch Sonarr/Radarr. Configure Riven as an instance with
`INSTANCE_N_TYPE: riven` (the `INSTANCE_N_APIKEY` is Riven's `x-api-key`), set `ENABLE_RIVEN: true`,
and stack-doctor does two things:

- **Health + services, every sweep (read-only):** hits Riven's `/health` and `/services`. An
  unhealthy backend or any service Riven reports as down (a dead scraper / downloader) is logged
  as a warning, and the instance shows up in the dashboard health row.
- **Gentle retries, throttled:** items wedged in a *working* state (`RIVEN_STUCK_STATES`, e.g.
  `Scraped`/`Downloaded`/`PartiallyCompleted`) or that *never resolved* (`RIVEN_MISSING_STATES`,
  e.g. `Requested`/`Indexed`/`Failed`) are re-run through Riven's own state machine via
  `POST /items/retry`. The retry path uses the exact same four gates as backlog so it can't
  self-amplify in event mode: a per-sweep cap, a per-item cooldown, a host-load gate, and a
  minimum interval between real retry sweeps. (Health/services reporting is **not** throttled.)

| var | default | meaning |
|---|---|---|
| `ENABLE_RIVEN` | `false` | turn the check on |
| `RIVEN_PER_SWEEP` | `5` | max item retries triggered per sweep (per instance) |
| `RIVEN_INTERVAL` | `900` | minimum seconds between real retry sweeps (health still runs every sweep) |
| `RIVEN_RETRY_DAYS` | `3` | per-item cooldown before a still-stuck item is retried again |
| `RIVEN_LOAD_MAX` | `12` | skip retries while host load exceeds this (`0` ignores load); health still runs |
| `RIVEN_MAX_FETCH` | `500` | cap on items pulled per state-group per sweep |
| `RIVEN_STUCK_STATES` | `Scraped,Downloaded,PartiallyCompleted` | working states to nudge along |
| `RIVEN_MISSING_STATES` | `Requested,Indexed,Failed` | unresolved states to re-drive |
| `RIVEN_STATE_FILE` | `/data/riven.json` | records per-item cooldowns + last-sweep timestamp |

Honors `DOCTOR_DRY_RUN` (logs each WOULD-retry, calls nothing). If the backend is unhealthy, the
retry pass is skipped entirely so a down Riven is never hammered.

## Mediastorm (health watch)

[mediastorm](https://github.com/godver3/mediastorm) is a streaming server with no Sonarr-style
import queue or monitored-missing list, so there is nothing to drain or retry. Support is
deliberately **health-only**: configure it with `INSTANCE_N_TYPE: mediastorm`, set
`ENABLE_MEDIASTORM: true`, and each sweep stack-doctor probes its `/health` endpoint and warns if
the server is down. The `INSTANCE_N_APIKEY` is optional (mediastorm's `/health` is unauthenticated;
supply a key only if you front it with auth, and it is sent as a bearer token).

| var | default | meaning |
|---|---|---|
| `ENABLE_MEDIASTORM` | `false` | turn the check on |
| `MEDIASTORM_TIMEOUT` | `8` | per-probe HTTP timeout (seconds) for `/health` |

## Scout (acquire from the dashboard)

Scout is a lightweight alternative front-end built into the dashboard. Instead of bouncing between
Overseerr, Sonarr, Radarr and Plex, you search a title, click **Get**, and watch the request move
through a live status track right there in the page, ending with a deep link that opens the finished
item in Plex. It is drawn in a deliberately hand-sketched style and is laid out for both phone and
desktop.

It does not add a new backend: it drives whatever you already run.

- If any `sonarr` / `radarr` instance is configured, Scout searches their lookup (TMDB/TVDB, no extra
  key needed), adds or kicks a search on the matching instance, then follows the queue
  (`downloading` with a percentage), import and on-disk state.
- Otherwise, if a `riven` instance is configured, Scout adds by IMDb id through Riven and tracks the
  item's Riven state.
- With no acquisition backend, the tab shows a clear "nothing to drive" banner.

**Search by title or by actor.** A `Title / Actor` toggle sits next to the search box. In **Actor**
mode you type a name and Scout returns that person's filmography as cards, most-popular first, each
with a **Get** button, so you can pull a whole body of work without knowing individual titles. The
arr `/lookup` endpoints are title-only, so actor search rides a separate metadata provider:

- If `SCOUT_TMDB_API_KEY` is set, Scout queries TMDB directly. This needs **no Overseerr/Jellyseerr**.
- Otherwise, if `SEERR_URL` + `SEERR_APIKEY` are set, Scout uses seerr's person API.
- If neither is configured, the Actor toggle stays hidden and the tab explains what to set.

Person cards carry a TMDB id; Scout resolves the TVDB id a show needs (and checks whether the item is
already in your library) at **Get** time, so search stays fast. Actor search is offered only in the
Sonarr/Radarr backend mode, since that is the path that can add a TMDB-identified pick.

Search hits that are already playable in Plex skip the queue entirely: instead of a **Get** button they
show a **Play in Plex** button straight away. Presence is resolved against Plex at search time (matching
by IMDb/TMDB/TVDB guid, then title and year), not against the arr's `hasFile`, because on a debrid/Riven
mount a title plays fine long before any arr reports a local file. So Scout doubles as a quick find-and-play.

The six stages are `searching -> grabbed -> downloading -> importing -> verifying -> available`.
The **Get** button itself becomes the live status: after you click it, the button turns into a small
progress pill on the card (current stage plus a fill bar and, while downloading, the percentage), and
a persistent "Acquiring" list below tracks every request through the full stepper. When the file
lands, Scout resolves it in Plex (matching by IMDb/TMDB/TVDB guid, then year) and the pill becomes a
**Play in Plex** button that opens `app.plex.tv` at that item. Set `PLEX_URL` + `PLEX_TOKEN` for the
play link; without them acquisition still works, just without the deep link.

A Scout pick is treated as top priority. While any request is in flight the background drains
([Backlog](#backlog-drain-monitored-but-missing-gently) and [Riven](#riven-health--retry-stuckmissing)
retries) yield their sweeps so they do not compete for the download client, and the grab itself is
pushed to the top of its download client queue (SABnzbd is forced; other clients are left as-is), so
the thing you asked for is fetched first. Items that got the bump show a `priority` tag.

**Built for speed.** A Scout pick typically reaches **available** with a working play link in well
under 30 seconds. Four things get it there:

- **It grabs a release the backend can actually fetch.** Left to auto-search, an arr grabs the
  highest-scored release first, which on a debrid mount is usually a 40-90GB full-disc or remux image
  the backend cannot resolve. It spends ~20s failing, blocklists it, tries the next, and so on, so
  the biggest cost is failed grabs, not the download. Instead Scout runs its own interactive search
  and grabs the best release under `SCOUT_MAX_GRAB_GB` (the filter is size, not the arr's parsed
  quality, since a fetchable encode is sometimes mis-tagged as a disc). If a pick does fail it walks
  to the next candidate (`SCOUT_GRAB_TRIES`); if nothing fetchable turns up it falls back to the
  arr's own search. This is Scout-only and does not change your automated grabs.
- **It imports the instant the grab finishes** by forcing `RefreshMonitoredDownloads` every
  `SCOUT_IMPORT_NUDGE_SEC`, rather than waiting out the arr's ~60s completed-download-handling interval.
- **It pokes a targeted Plex scan** of the new file's folder on import (`SCOUT_PLEX_SCAN`), so the
  **Play in Plex** link resolves in seconds instead of at the next full library sweep.
- **It drives the state machine server-side** on a fast tick (`SCOUT_PUMP_SEC`), so completion does
  not depend on the dashboard's poll timer, which a backgrounded browser tab throttles.

Search and status are read-only, so the tab is safe to leave open. **Get** is the only action that
writes, and it honours `DOCTOR_DRY_RUN`: in dry-run nothing is submitted and the request is marked
`dry-run`. Requests live in `SCOUT_STATE_FILE` and a finished one drops off the feed after
`SCOUT_TTL_HOURS`.

| var | default | meaning |
|---|---|---|
| `ENABLE_SCOUT` | `true` | show the Scout tab (it is inert unless a backend is configured) |
| `SCOUT_MOVIE_INSTANCE` | _(first radarr)_ | which radarr name to acquire movies through |
| `SCOUT_SHOW_INSTANCE` | _(first sonarr)_ | which sonarr name to acquire shows through |
| `SCOUT_QUALITY_PROFILE` | _(instance default)_ | quality profile name or id to add new items with |
| `SCOUT_ROOT_FOLDER` | _(instance default)_ | root folder path to add new items into |
| `SCOUT_MAX_RESULTS` | `20` | cap on title search results shown |
| `SCOUT_TMDB_API_KEY` | _(none)_ | optional TMDB v3 key; enables actor/actress search with no seerr. If blank, Scout falls back to seerr's person API; if neither is set the Actor toggle is hidden |
| `SCOUT_PERSON_MAX` | `40` | cap on filmography cards an actor search returns (most-popular first) |
| `SCOUT_RETAIN` | `40` | how many recent requests the activity feed keeps |
| `SCOUT_TTL_HOURS` | `48` | drop a finished request from the feed after this long |
| `SCOUT_STATE_FILE` | `/data/scout.json` | where in-flight requests are persisted |
| `SCOUT_IMPORT_NUDGE_SEC` | `5` | how often to force the arr to import a finished grab (`0` = off, wait for the arr's own interval) |
| `SCOUT_PLEX_SCAN` | `true` | on import, poke a targeted Plex scan of the new file's folder so the Play link resolves fast |
| `SCOUT_PUMP_SEC` | `3` | server-side tick that drives a live request to completion regardless of the dashboard poll timer (`0` = off) |
| `SCOUT_MAX_GRAB_GB` | `30` | Scout grabs the best release under this size; skips the big full-disc/remux images that fail to resolve on a debrid mount (`0` = defer to the arr's auto-pick) |
| `SCOUT_GRAB_TRIES` | `4` | how many fetchable releases to try (best first) before falling back to the arr's own search |
| `SCOUT_GRAB_WAIT` | `18` | seconds to watch a grabbed release for import/failure before moving to the next candidate |
| `SCOUT_SEARCH_TIMEOUT` | `90` | timeout for Scout's interactive release search against the indexers |

## Extending

Conditions are just predicates in `doctor.py` (`CONDITIONS` dict). Adding a new
detect/fix rule is a couple of lines. PRs welcome.

## License

MIT
