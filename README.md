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
| **queue** | stuck/dead/blocked *arr download-queue items | removes + blocklists -> *arr re-searches a different release |
| **providers** | failed indexers / download clients (sonarr/radarr/**prowlarr**) | runs the **Test** on them to re-validate + clear the failure |
| **decypharr** | hung FUSE mount (read-test) + API down | runs your restart hook (`DECYPHARR_RESTART_CMD`) |
| **plex** | Plex unresponsive | alerts (optional library refresh) |
| **resources** | host load / low memory / swap pressure | reports; optional `drop_caches` relief |
| **janitor** | permanently-dead usenet releases (from decypharr's log) | quarantines those library symlinks (reversible) |
| **bazarr** | Bazarr unreachable | alerts |
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

Each is a named **condition** you can enable/disable via `DOCTOR_CONDITIONS`:

| condition | what it catches |
|---|---|
| `downloadClientUnavailable` | dead grabs the download client rejected (orphans that never go away) |
| `importBlocked` | completed download the *arr refuses to import |
| `importFailed` | import attempted and failed |
| `importPending_warning` | completed but stuck pending with a warning, usually an **incomplete/corrupt file ffprobe can't parse** |
| `failedPending` | failed download awaiting handling |
| `stalled` | download flagged with a stall / "no files" warning |

The fix is always the same and safe: `DELETE` the queue item with `removeFromClient=true`
and (by default) `blocklist=true`. With `autoRedownloadFailed` on in your *arr (the default),
that triggers a fresh search for another release. It's **self-limiting**, once every bad
release for an item is blocklisted, there's nothing left to grab, so the churn stops.

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
- **Config**: edit the common tuning knobs and save. Changes write to `DOCTOR_CONFIG_FILE` and apply
  on restart (there's a "Save and Restart" button). Secrets (API keys, tokens) are never shown.
- **Logs**: a live tail of `DOCTOR_LOG_FILE`.

It runs inside the daemon's own process (no extra container). Gate it with `DOCTOR_UI_TOKEN` if your
LAN isn't trusted. In event mode the webhook listener (`DOCTOR_PORT`) and the dashboard
(`DOCTOR_UI_PORT`) run side by side.

---

## Configuration (all via env vars)

### Behaviour

| var | default | meaning |
|---|---|---|
| `DOCTOR_MODE` | `cron` | `cron` (interval sweeps) or `event` (Sonarr/Radarr webhook) |
| `DOCTOR_INTERVAL` | `900` | cron: seconds between sweeps |
| `DOCTOR_MIN_STRIKES` | `2` | item must be stuck this many consecutive checks before action (ignores transient blips like a download-client restart) |
| `DOCTOR_MAX_ACTIONS` | `20` | max removals per sweep (rate limit, keeps re-searches gentle) |
| `DOCTOR_BLOCKLIST` | `true` | blocklist removed grabs so a *different* release is fetched |
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

## Extending

Conditions are just predicates in `doctor.py` (`CONDITIONS` dict). Adding a new
detect/fix rule is a couple of lines. PRs welcome.

## License

MIT
