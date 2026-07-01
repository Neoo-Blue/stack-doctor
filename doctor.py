#!/usr/bin/env python3
"""
stack-doctor - auto-detect and fix recurring issues across a Sonarr/Radarr +
decypharr + Plex media stack.

Modular checks, each toggled and configured by environment variables:

  queue      *arr download queues       - clear stuck/dead/blocked items -> re-search
  providers  *arr/prowlarr providers    - auto-Test failed indexers/download clients to clear them
  decypharr  decypharr mount + API      - detect a hung FUSE mount -> run a restart hook
  plex       Plex Media Server          - detect unresponsive Plex (+ optional library scan)
  resources  host load / memory / swap  - report pressure, optional drop_caches relief
  janitor    usenet dead files          - quarantine library symlinks for permanently-dead
                                           releases (reversible) from a decypharr log file
  bazarr     Bazarr                     - reachability check
  seerr      Overseerr/Jellyseerr/Seerr - auto-retry FAILED requests (arr add timed out under load)
  warmer     Plex-driven precache       - read the head of likely-next media so playback starts
                                           instantly (next episode + On Deck); thread, not a sweep

Runs as a cron-style interval loop OR reacts to Sonarr/Radarr webhook events.
Pure Python standard library, no dependencies.
"""
import calendar
import datetime
import json
import logging
import logging.handlers
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

VERSION = "0.3"

# --------------------------------------------------------------------------- #
# config helpers
# --------------------------------------------------------------------------- #

def _b(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _i(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _dur(tok, default=0):
    """Parse a duration token: 30s / 10m / 2h / 1d, or a bare number of seconds."""
    t = str(tok).strip().lower()
    if not t:
        return default
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(float(t[:-1]) * mult[t[-1]]) if t[-1] in mult else int(float(t))
    except (ValueError, KeyError):
        return default

def _human(sec):
    sec = int(sec)
    for size, suf in ((86400, "d"), (3600, "h"), (60, "m")):
        if sec >= size and sec % size == 0:
            return "%d%s" % (sec // size, suf)
    return "%ds" % sec

# UI-saved overrides: merge a JSON overlay over the inherited env BEFORE config is read, so edits win.
CONFIG_FILE = os.environ.get("DOCTOR_CONFIG_FILE", "/data/config.json")

def _load_overrides():
    try:
        with open(CONFIG_FILE) as f:
            for k, v in json.load(f).items():
                if v is not None:
                    os.environ[str(k)] = str(v)
    except Exception:
        pass

_load_overrides()

MODE        = os.environ.get("DOCTOR_MODE", "cron").strip().lower()   # cron | event
INTERVAL    = _i("DOCTOR_INTERVAL", 900)
PORT        = _i("DOCTOR_PORT", 8088)                                 # webhook port (event mode)
UI_PORT     = _i("DOCTOR_UI_PORT", 12345)                            # web dashboard port
EN_UI       = _b("ENABLE_UI", False)
UI_TOKEN    = os.environ.get("DOCTOR_UI_TOKEN", "")                   # optional ?token= / X-Doctor-Token gate
LOG_LEVEL   = os.environ.get("DOCTOR_LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("DOCTOR_LOG_FILE", "")
TIMEOUT     = _i("DOCTOR_HTTP_TIMEOUT", 60)
DRY_RUN     = _b("DOCTOR_DRY_RUN", False)

# which checks are on
EN_QUEUE      = _b("ENABLE_QUEUE", True)
EN_DECYPHARR  = _b("ENABLE_DECYPHARR", False)
EN_PLEX       = _b("ENABLE_PLEX", False)
EN_RESOURCES  = _b("ENABLE_RESOURCES", False)
EN_JANITOR    = _b("ENABLE_JANITOR", False)
EN_PROVIDERS  = _b("ENABLE_PROVIDERS", False)   # auto-test failed indexers/download clients (sonarr/radarr/prowlarr)
EN_BAZARR     = _b("ENABLE_BAZARR", False)      # Bazarr reachability
EN_SEERR      = _b("ENABLE_SEERR", False)       # Overseerr/Jellyseerr/Seerr: auto-retry FAILED requests
EN_WESTREPAIR = _b("ENABLE_WESTREPAIR", False)  # symlink repair via repair.py subprocess
EN_SCRUBBER   = _b("ENABLE_SCRUBBER", False)    # proactive file integrity scan (catches mid-file dead segments before playback)
EN_WATCHLISTS = _b("ENABLE_WATCHLISTS", False)  # pull Plex Home/friends watchlists, add directly to *arr (bypasses Overseerr)
EN_HOLIDAYS   = _b("ENABLE_HOLIDAYS", False)    # auto-build + pin pre-holiday themed Plex collections (curated per holiday)
EN_BACKLOG    = _b("ENABLE_BACKLOG", False)     # trickle-search monitored-but-missing items that no backlog search ever found
EN_RIVEN      = _b("ENABLE_RIVEN", False)       # Riven (rivenmedia/riven): health + services watch, retry stuck/missing items
EN_MEDIASTORM = _b("ENABLE_MEDIASTORM", False)  # mediastorm (godver3/mediastorm): up/health watch (no import queue to manage)
EN_SCOUT      = _b("ENABLE_SCOUT", True)        # dashboard Scout tab: search a title -> Get -> watch it acquire -> play in Plex (uses whatever backend is enabled)

# westrepair config
WR_SCRIPT          = os.environ.get("WESTREPAIR_SCRIPT", "/app/westrepair/repair.py")
WR_RUN_INTERVAL    = os.environ.get("WESTREPAIR_RUN_INTERVAL", "6h")
WR_REPAIR_INTERVAL = os.environ.get("WESTREPAIR_REPAIR_INTERVAL", "1m")

BAZARR_URL    = os.environ.get("BAZARR_URL", "")
BAZARR_APIKEY = os.environ.get("BAZARR_APIKEY", "")

# seerr (Overseerr / Jellyseerr / Seerr) failed-request auto-retry.
# When the arr API is briefly slow (e.g. under a heavy search load), seerr's add call times out and
# it marks the request FAILED - it never auto-retries, so the title silently never reaches the arr.
# We periodically re-drive those FAILED requests so a transient blip self-heals, with an attempt cap
# so a genuinely-bad request (dead tmdb id, etc.) doesn't get retried forever.
SEERR_URL       = os.environ.get("SEERR_URL", "")
SEERR_APIKEY    = os.environ.get("SEERR_APIKEY", "")
SEERR_MAX       = _i("SEERR_RETRY_MAX", 10)      # max requests retried per sweep (rate-limit the re-adds)
SEERR_MAX_TRIES = _i("SEERR_MAX_ATTEMPTS", 5)    # give up on a request after this many auto-retries (0 = never give up)

# queue check
MIN_STRIKES   = _i("DOCTOR_MIN_STRIKES", 2)
MAX_ACTIONS   = _i("DOCTOR_MAX_ACTIONS", 20)
BLOCKLIST     = _b("DOCTOR_BLOCKLIST", True)
REMOVE_CLIENT = _b("DOCTOR_REMOVE_FROM_CLIENT", True)
STATE_FILE    = os.environ.get("DOCTOR_STATE_FILE", "/data/state.json")
# churn brake: a title that keeps grabbing dead releases (re-grabbed despite blocklist, or only
# dead releases exist) never imports and just burns cycles. After CHURN_LIMIT failed grabs of the
# SAME episode/movie, stop the loop. action: report (log only) | park (un-monitor) | backoff
# (un-monitor, then auto re-monitor on an escalating schedule for a fresh attempt).
CHURN_LIMIT    = _i("DOCTOR_CHURN_LIMIT", 0)              # 0 = brake off
CHURN_ACTION   = os.environ.get("DOCTOR_CHURN_ACTION", "report").strip().lower()
# backoff retry schedule: each park steps to the next delay; the last entry repeats forever.
# default "10m,1h,24h" = retry 10m after the 1st park, 1h after the 2nd, every 24h thereafter.
CHURN_BACKOFF  = [_dur(x) for x in os.environ.get("DOCTOR_CHURN_BACKOFF", "").split(",") if x.strip()]
if not CHURN_BACKOFF:
    _legacy = os.environ.get("DOCTOR_CHURN_COOLDOWN")    # back-compat with the old single fixed cooldown
    CHURN_BACKOFF = [_dur(_legacy)] if _legacy else [600, 3600, 86400]
DEFAULT_CONDITIONS = "downloadClientUnavailable,importBlocked,importFailed,importPending_warning,failedPending,stalled"
ENABLED_CONDITIONS = [c.strip() for c in os.environ.get("DOCTOR_CONDITIONS", DEFAULT_CONDITIONS).split(",") if c.strip()]
# per-condition remediation: each detected condition maps to a fix action.
#   report       - log only, change nothing (e.g. client-unavailable: don't blocklist a good release)
#   research     - remove + blocklist (honors DOCTOR_BLOCKLIST) so the arr re-searches a fresh release
#   remove       - remove + re-search but never blocklist (give the same release another shot)
#   force_import - call the arr's ManualImport on already-downloaded files (no re-download)
_VALID_ACTIONS = ("report", "research", "remove", "force_import")
_DEFAULT_ACTIONS = {
    "downloadClientUnavailable": "report",        # client is down, not the release's fault -> never blocklist
    "importBlocked":             "force_import",
    "importPending_warning":     "force_import",
    "importFailed":              "research",
    "failedPending":             "research",
    "stalled":                   "research",
}
DEFAULT_ACTION = os.environ.get("DOCTOR_DEFAULT_ACTION", "research").strip().lower()
if DEFAULT_ACTION not in _VALID_ACTIONS:
    DEFAULT_ACTION = "research"
CONDITION_ACTIONS = dict(_DEFAULT_ACTIONS)
for _kv in os.environ.get("DOCTOR_CONDITION_ACTIONS", "").split(","):
    if "=" in _kv:
        _c, _a = _kv.split("=", 1)
        if _c.strip() and _a.strip().lower() in _VALID_ACTIONS:
            CONDITION_ACTIONS[_c.strip()] = _a.strip().lower()
IMPORT_MODE = os.environ.get("DOCTOR_IMPORT_MODE", "auto").strip().lower()   # auto|move|copy

# resource thresholds (host load uses /proc/loadavg if mounted)
LOAD_MAX        = _f("DOCTOR_LOAD_MAX", 0)         # queue check pauses above this (0=off)
RES_LOAD_WARN   = _f("RES_LOAD_WARN", 40)
RES_SWAP_WARN   = _i("RES_SWAP_WARN_MB", 7000)
RES_MEM_MIN     = _i("RES_MEM_MIN_MB", 800)
RES_DROP_CACHES = _b("RES_DROP_CACHES", False)       # echo 1 > drop_caches on memory pressure (needs privilege)

# decypharr
DECY_URL          = os.environ.get("DECYPHARR_URL", "")             # e.g. http://192.168.50.202:8282
DECY_MOUNT_TEST   = os.environ.get("DECYPHARR_MOUNT_TEST", "")      # a dir on the FUSE mount to read-test
DECY_READ_TIMEOUT = _i("DECYPHARR_READ_TIMEOUT", 25)
DECY_RESTART_CMD  = os.environ.get("DECYPHARR_RESTART_CMD", "")     # shell cmd to recover a hung mount

# plex
PLEX_URL   = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_SCAN  = _b("PLEX_SCAN_ON_CHECK", False)

# warmer (Plex-driven precache of the heads of likely-next media -> instant playback start)
EN_WARMER         = _b("ENABLE_WARMER", False)
WARM_HEAD_MB      = _i("WARMER_PRECACHE_MB", 64)        # how much of the file head to pull into cache
WARM_TAIL_MB      = _i("WARMER_TAIL_MB", 8)             # also pull the tail (mkv cues / Plex end-probe); 0=off
WARM_INTERVAL     = _i("WARMER_INTERVAL", 120)          # seconds between session polls (next-episode prefetch)
WARM_ONDECK_EVERY = _i("WARMER_ONDECK_EVERY", 600)      # seconds between on-deck / recent warms
WARM_NEXT_EPS     = _i("WARMER_NEXT_EPISODES", 1)       # warm this many upcoming episodes of an active show
WARM_RECENT_COUNT = _i("WARMER_RECENT_COUNT", 0)        # warm N most-recently-added per library (0=off)
WARM_MAX_CYCLE    = _i("WARMER_MAX_PER_CYCLE", 12)      # cap warms per cycle (rate-limit the usenet fetch)
WARM_COOLDOWN     = _i("WARMER_COOLDOWN", 3600)         # do not re-warm the same file within this many seconds
WARM_LOAD_MAX     = _f("WARMER_LOAD_MAX", 0)            # skip warming if host 1-min load above this (protect Plex); 0=off
WARM_READ_TIMEOUT = _i("WARMER_READ_TIMEOUT", 60)       # abandon a single warm read after this long (hung mount guard)
WARM_CONCURRENCY  = _i("WARMER_CONCURRENCY", 2)         # simultaneous BACKGROUND (on-deck/recent) warm reads
WARM_OPEN_CONC    = _i("WARMER_OPEN_CONCURRENCY", 4)    # dedicated lane for the title you OPEN, so it starts instantly and never queues behind background warming
WARM_PARTS        = _i("WARMER_PARTS", 1)              # how many versions per title to warm (1 = highest-res only; 0 = all). Avoids warming a 1080p you'll never play next to the 4K
# low-cache mode: for small / RAM-backed caches. Skips On Deck (Continue Watching) warming entirely and
# only warms the NEXT episode as the current one nears its end, so almost nothing sits in cache early.
WARM_LOW_CACHE    = _b("WARMER_LOW_CACHE", False)
WARM_NEXT_REMAIN  = _i("WARMER_NEXT_REMAINING_MIN", 0)  # warm the next episode only when <= this many minutes remain (0 = as soon as playback is seen)
WARM_NEXT_NEAR_END = WARM_NEXT_REMAIN if WARM_NEXT_REMAIN > 0 else (10 if WARM_LOW_CACHE else 0)
WARM_SOURCES      = [s.strip().lower() for s in os.environ.get("WARMER_SOURCES", "ondeck,next").split(",") if s.strip()]
WARM_ONDECK       = _b("WARMER_ONDECK", True)          # quick on/off for Continue Watching (On Deck) warming
WARM_PATH_MAP     = os.environ.get("WARMER_PATH_MAP", "")   # "plexPrefix:hostPrefix" if Plex's file path != this host's
# detail-page warming: tail Plex's server log and warm the exact title a viewer opens (the one true
# pre-play signal Plex emits). Give it a streaming command (tail -F, or `pct exec ... tail -F`) OR a file.
WARM_PLEXLOG_CMD  = os.environ.get("WARMER_PLEXLOG_CMD", "")
WARM_PLEXLOG_FILE = os.environ.get("WARMER_PLEXLOG_FILE", "")

# janitor (give it decypharr's error log via a file OR a command, e.g. journalctl when on-host)
JAN_LIBS      = [p.strip() for p in os.environ.get("JANITOR_LIBRARY_PATHS", "").split(",") if p.strip()]
JAN_LOG       = os.environ.get("JANITOR_DECYPHARR_LOG", "")         # log file path
JAN_LOG_CMD   = os.environ.get("JANITOR_LOG_CMD", "")               # cmd printing the log, e.g. "journalctl -u decypharr -n 10000 --no-hostname"
JAN_QUAR      = os.environ.get("JANITOR_QUARANTINE_DIR", "/data/quarantine")
JAN_PATTERNS  = os.environ.get("JANITOR_DEAD_PATTERNS", "ARTICLE_NOT_FOUND,still missing").split(",")

# scrubber (proactive file integrity scan)
# Tiered, cheapest-first:
#   1 = ffprobe header parse           (catches torn containers, ~1s)
#   2 = ffmpeg null-muxer skim at N seek points (catches mid-file dead NZB articles + packet corruption;
#       ffmpeg blocks on FUSE cold-cache misses so it does NOT false-positive on uncached chunks the way
#       raw byte reads do)
#   3 = full ffmpeg -v error decode    (opt-in / used to final-confirm a tier-2 BAD before action)
# Default tier=2 is the sweet spot for usenet/decypharr stacks.
SCRUB_PATHS        = [p.strip() for p in os.environ.get("SCRUBBER_PATHS", os.environ.get("JANITOR_LIBRARY_PATHS", "")).split(",") if p.strip()]
SCRUB_STATE        = os.environ.get("SCRUBBER_STATE_FILE", "/data/scrubber.json")
# Default = 1 (ffprobe header only) because byte-level / ffmpeg-seek checks false-positive on
# decypharr-style FUSE mounts that return EOF for uncached chunks. Tier 1 catches the truly torn
# containers (the only failure mode we can verify reliably without a deeper decypharr-native API).
# Tiers 2-3 stay available for libraries on local disk OR for opt-in slow-but-thorough scans;
# they will misclassify cold-cache misses as bad on a stream-fetched library.
SCRUB_TIER         = _i("SCRUBBER_TIER", 1)              # 1..3
SCRUB_FULL_ON_BAD  = _b("SCRUBBER_FULL_DECODE_ON_BAD", False) # final-confirm a BAD with a full decode before quarantining (slow; off by default)
SCRUB_SKIM_POINTS  = _i("SCRUBBER_SKIM_POINTS", 4)       # seek points for tier 2 ffmpeg skim
SCRUB_SKIM_SECS    = _i("SCRUBBER_SKIM_SECS", 5)         # seconds decoded at each skim point
SCRUB_MAX_FILES    = _i("SCRUBBER_MAX_FILES", 50)        # files scanned per sweep
SCRUB_CONC         = _i("SCRUBBER_CONCURRENCY", 1)       # parallel scans (1 = single stream, kindest to decypharr)
SCRUB_LOAD_MAX     = _f("SCRUBBER_LOAD_MAX", 12)         # skip sweep if 1-min load above this (0=off)
SCRUB_STRIKES      = _i("SCRUBBER_STRIKES", 2)           # consecutive bad reads before action (transient mount blips don't cost re-grabs)
SCRUB_FFPROBE      = os.environ.get("SCRUBBER_FFPROBE", "ffprobe")
SCRUB_FFMPEG       = os.environ.get("SCRUBBER_FFMPEG", "ffmpeg")
SCRUB_HEADER_TO    = _i("SCRUBBER_HEADER_TIMEOUT", 30)
SCRUB_SKIM_TO      = _i("SCRUBBER_SKIM_TIMEOUT", 180)    # per skim point timeout (tier 2)
SCRUB_FULL_TO      = _i("SCRUBBER_FULL_TIMEOUT", 1800)   # full-decode timeout (tier 3)
SCRUB_QUAR         = os.environ.get("SCRUBBER_QUARANTINE_DIR", os.environ.get("JANITOR_QUARANTINE_DIR", "/data/quarantine"))
SCRUB_DEL_ARR      = _b("SCRUBBER_DELETE_ARR_FILE", True)  # DELETE arr movieFile/episodeFile so it re-searches; false = quarantine only
SCRUB_EXTS         = tuple(x.strip().lower() for x in os.environ.get("SCRUBBER_EXTENSIONS", ".mkv,.mp4,.avi,.m4v,.ts").split(",") if x.strip())
SCRUB_MIN_AGE      = _i("SCRUBBER_MIN_AGE_HOURS", 1)     # skip files newer than this (don't fight the warmer / fresh imports)
SCRUB_REVERIFY_DAYS = _i("SCRUBBER_REVERIFY_DAYS", 30)   # re-check previously-OK files after N days (0=never)

# watchlists (pull Plex Home users + non-Home friends watchlists, add directly to the arrs)
# Sources:
#   - Plex Home / managed users: enumerated automatically from PLEX_TOKEN (owner) via plex.tv API.
#     PINs (per managed user) optional: WATCHLISTS_HOME_PINS="userUuid1:1234,userUuid2:5678".
#   - Non-Home friends: each gives their X-Plex-Token; list as label:token pairs in
#     WATCHLISTS_FRIENDS="alice:xxxxxxx,bob:yyyyyyy".
# Policy: 4K instance first (Sonarr4K/Radarr4K), fall back to 1080p (Sonarr/Radarr) if 4K add fails.
# State (tmdb:id / tvdb:id -> {added_to, ts}) persists so the same item isn't re-attempted.
WL_FRIENDS         = os.environ.get("WATCHLISTS_FRIENDS", "")        # "alice:xxx,bob:yyy"
WL_HOME_INCLUDE    = _b("WATCHLISTS_INCLUDE_HOME", True)             # also pull Plex Home users via owner token
WL_HOME_PINS       = os.environ.get("WATCHLISTS_HOME_PINS", "")      # "uuid1:1234,uuid2:5678"
WL_PREFER_4K       = _b("WATCHLISTS_PREFER_4K", True)                # FALLBACK preference when WATCHLISTS_QUALITY has no rule for a source
# Per-source quality preference: 4k | 1080p | both. "both" = add to BOTH 4K and 1080p instances.
# Format: comma list of "label=quality" pairs, with "*" as wildcard default.
#   WATCHLISTS_QUALITY="*=both,home/kids=1080p,alice=4k,bob=1080p"
# Labels match what the source is logged as ("home/<title>" for Plex Home users, the friend's
# label for non-Home friends). Unknown sources fall back to WATCHLISTS_DEFAULT_QUALITY.
WL_QUALITY_MAP     = os.environ.get("WATCHLISTS_QUALITY", "")
WL_DEFAULT_QUALITY = os.environ.get("WATCHLISTS_DEFAULT_QUALITY", "both")  # 4k | 1080p | both
WL_MAX_ADDS        = _i("WATCHLISTS_MAX_ADDS_PER_SWEEP", 25)         # rate-cap so a friend dumping 300 titles doesn't flood
WL_STATE           = os.environ.get("WATCHLISTS_STATE_FILE", "/data/watchlists.json")
WL_PROFILES        = os.environ.get("WATCHLISTS_PROFILES", "")       # override per-arr quality profile id, e.g. "radarr=1,sonarr=4,radarr4k=5,sonarr4k=5"
WL_HTTP_TO         = _i("WATCHLISTS_HTTP_TIMEOUT", 20)
WL_PAGE_SIZE       = _i("WATCHLISTS_PAGE_SIZE", 100)                 # Plex Discover caps Container-Size; 100 is safe

# holidays (pre-holiday themed Plex collections, auto-built then pinned to Plex Home)
# Each holiday is a curated definition: match films by exact title list, by keyword-in-title,
# and/or by genre, then create a collection a few days before the date and remove it a few days
# after. The default set is baked in (HOLIDAYS_DEFINITIONS overrides it with JSON). Titles only:
# metadata-only Plex calls, safe on a decypharr/FUSE library (no file reads).
HOL_COUNTRIES  = [c.strip().lower() for c in os.environ.get("HOLIDAYS_COUNTRIES", "us").split(",") if c.strip()]  # us,canada,uk,china,japan,korea,australia,...
HOL_SECTION    = os.environ.get("HOLIDAYS_MOVIE_SECTION", "")        # movie library section id; blank = auto-detect first movie section
HOL_LEAD_DAYS  = _i("HOLIDAYS_LEAD_DAYS", 7)                         # default days before the date to show the row (per-holiday "lead" overrides)
HOL_POST_DAYS  = _i("HOLIDAYS_POST_DAYS", 3)                         # default days after the date to keep it (per-holiday "post" overrides)
HOL_PIN_HOME   = _b("HOLIDAYS_PIN_HOME", True)                       # pin the active collection to Plex Home (the recommended row)
HOL_STATE      = os.environ.get("HOLIDAYS_STATE_FILE", "/data/holidays.json")
HOL_HTTP_TO    = _i("HOLIDAYS_HTTP_TIMEOUT", 40)
HOL_MIN_INTERVAL = _i("HOLIDAYS_MIN_INTERVAL_HOURS", 12) * 3600       # holidays change daily; skip the Plex work between runs unless the active holiday changes (0=run every sweep)
HOL_DEFS_JSON  = os.environ.get("HOLIDAYS_DEFINITIONS", "")          # JSON list to override the baked-in curated holidays

# backlog: monitored-but-missing items that no search ever ran for (content that aired/released
# before the indexers were wired up - RSS only looks forward, so these sit empty forever). Trickle
# a few searches per sweep, gated on host load, with a per-item cooldown so genuinely-unavailable
# titles are not re-hammered every sweep. Default scope is the 1080p instances (add 4k names later).
BACKLOG_INSTANCES    = [s.strip() for s in os.environ.get("BACKLOG_INSTANCES", "sonarr,radarr").split(",") if s.strip()]
BACKLOG_PER_SWEEP    = _i("BACKLOG_PER_SWEEP", 5)                    # max searches triggered per sweep
BACKLOG_MIN_AGE_DAYS = _i("BACKLOG_MIN_AGE_DAYS", 7)                 # only items aired/released >= this many days ago (younger = leave to RSS)
BACKLOG_RETRY_DAYS   = _i("BACKLOG_RETRY_DAYS", 7)                   # per-item cooldown: do not re-search within this window
BACKLOG_LOAD_MAX     = _f("BACKLOG_LOAD_MAX", 12)                    # skip the whole check while host load is above this (0=ignore load)
BACKLOG_MAX_FETCH    = _i("BACKLOG_MAX_FETCH", 2000)                 # cap on missing records pulled per instance per sweep
BACKLOG_STATE        = os.environ.get("BACKLOG_STATE_FILE", "/data/backlog.json")
BACKLOG_INTERVAL     = _i("BACKLOG_INTERVAL", 900)                   # min seconds between real backlog sweeps; event mode fires many sweeps/min, this throttles grab-rate + arr API load

# riven (rivenmedia/riven): symlink-library manager with its own state machine. We watch /health and
# /services every sweep (cheap, read-only) and gently retry items wedged in a working state or never
# resolved. Retries are throttled like backlog (interval guard + load gate + per-item cooldown) so the
# event-mode feedback loop cannot self-amplify. Stuck = items that started but stalled; missing = items
# requested/indexed/failed that never produced a file.
RIVEN_PER_SWEEP    = _i("RIVEN_PER_SWEEP", 5)                        # max item retries triggered per sweep
RIVEN_INTERVAL     = _i("RIVEN_INTERVAL", 900)                       # min seconds between real retry sweeps (health/services still reported every sweep)
RIVEN_RETRY_DAYS   = _i("RIVEN_RETRY_DAYS", 3)                       # per-item cooldown: do not re-retry the same item within this window
RIVEN_LOAD_MAX     = _f("RIVEN_LOAD_MAX", 12)                        # skip retries while host load is above this (0=ignore load); health still runs
RIVEN_MAX_FETCH    = _i("RIVEN_MAX_FETCH", 500)                      # cap on items pulled per state-group per sweep
RIVEN_STUCK_STATES   = [s.strip() for s in os.environ.get("RIVEN_STUCK_STATES", "Scraped,Downloaded,PartiallyCompleted").split(",") if s.strip()]
RIVEN_MISSING_STATES = [s.strip() for s in os.environ.get("RIVEN_MISSING_STATES", "Requested,Indexed,Failed").split(",") if s.strip()]
RIVEN_STATE        = os.environ.get("RIVEN_STATE_FILE", "/data/riven.json")

# mediastorm (godver3/mediastorm): Go streaming server. Architecturally it has no Sonarr-style import
# queue or monitored-missing list, so there is nothing to drain/retry - we only watch that it is up.
MEDIASTORM_TIMEOUT = _i("MEDIASTORM_TIMEOUT", 8)                     # per-probe HTTP timeout for /health

# scout: a request-and-watch acquire frontend on the dashboard. You search a title, pick a result,
# hit Get; scout adds it to whatever acquisition backend is enabled (Sonarr/Radarr if present, else
# Riven) with search-on-add, then the tab polls the backend and shows it move searching -> downloading
# -> importing -> verifying -> available, ending in a deep link that plays it in Plex. Search + status
# are read-only; only Get writes, and Get honors DOCTOR_DRY_RUN (logs a would-add, submits nothing).
SCOUT_MOVIE_INSTANCE = os.environ.get("SCOUT_MOVIE_INSTANCE", "")    # which radarr name to acquire movies through (blank = first radarr)
SCOUT_SHOW_INSTANCE  = os.environ.get("SCOUT_SHOW_INSTANCE", "")     # which sonarr name to acquire shows through (blank = first sonarr)
SCOUT_QUALITY_PROFILE = os.environ.get("SCOUT_QUALITY_PROFILE", "")  # quality profile name or id to add with (blank = the instance's first profile)
SCOUT_ROOT_FOLDER    = os.environ.get("SCOUT_ROOT_FOLDER", "")       # root folder path to add into (blank = the instance's first root folder)
SCOUT_MAX_RESULTS    = _i("SCOUT_MAX_RESULTS", 20)                   # cap on search results returned to the UI
SCOUT_RETAIN         = _i("SCOUT_RETAIN", 40)                       # how many recent requests the activity feed keeps
SCOUT_TTL_HOURS      = _i("SCOUT_TTL_HOURS", 48)                    # drop a finished (available) request from the feed after this long
SCOUT_STATE          = os.environ.get("SCOUT_STATE_FILE", "/data/scout.json")

TRIGGER_EVENTS = set(e.strip() for e in os.environ.get(
    "DOCTOR_TRIGGER_EVENTS", "Download,ManualInteractionRequired,DownloadFailed,Grab").split(",") if e.strip())

# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3))
    except Exception:
        pass
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S", handlers=handlers)
log = logging.getLogger("doctor")

# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def http_code(url, headers=None, t=10):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=t)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def run_cmd(cmd):
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        return (p.returncode, (p.stdout + p.stderr).strip()[:300])
    except Exception as e:
        return (1, "cmd error: " + str(e)[:120])

def run_output(cmd, t=120):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.stdout
    except Exception as e:
        log.warning("log cmd failed: %s", str(e)[:80])
        return ""

def host_load():
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0

# =========================================================================== #
# CHECK: queue
# =========================================================================== #

def _msgs(rec):
    out = []
    for sm in (rec.get("statusMessages") or []):
        out += [m for m in (sm.get("messages") or [])]
    if rec.get("errorMessage"):
        out.append(rec["errorMessage"])
    return out

CONDITIONS = {
    "downloadClientUnavailable": lambda r: r.get("status") == "downloadClientUnavailable",
    "importBlocked":            lambda r: r.get("trackedDownloadState") == "importBlocked",
    "importFailed":             lambda r: r.get("trackedDownloadState") == "importFailed",
    "importPending_warning":    lambda r: r.get("trackedDownloadState") == "importPending"
                                          and r.get("trackedDownloadStatus") in ("warning", "error"),
    "failedPending":            lambda r: r.get("trackedDownloadState") == "failedPending",
    "stalled":                  lambda r: r.get("trackedDownloadStatus") == "warning"
                                          and any("stall" in m.lower() or "no files" in m.lower() for m in _msgs(r)),
}

def stuck_reason(rec):
    for name in ENABLED_CONDITIONS:
        pred = CONDITIONS.get(name)
        if pred and pred(rec):
            return name
    return None

def _action_for(cond):
    return CONDITION_ACTIONS.get(cond, DEFAULT_ACTION)

def _force_import(arr, rec):
    """Ask the arr to ManualImport the files already on disk for this download (no re-download).
    Returns the number of files queued for import (0 = nothing importable)."""
    did = rec.get("downloadId")
    if not did:
        return 0
    cands = arr.get_json("/manualimport?downloadId=%s&filterExistingFiles=true"
                         % urllib.parse.quote(str(did)))
    if not isinstance(cands, list):
        return 0
    files = []
    for it in cands:
        if it.get("rejections"):                 # arr found a blocking reason (sample, unknown, etc.)
            continue
        f = {"path": it.get("path"), "folderName": it.get("folderName", ""),
             "quality": it.get("quality"), "languages": it.get("languages"),
             "releaseGroup": it.get("releaseGroup", ""), "indexerFlags": it.get("indexerFlags", 0),
             "downloadId": did}
        if arr.kind == "sonarr":
            ser = it.get("series") or {}
            eps = [e.get("id") for e in (it.get("episodes") or []) if e.get("id")]
            if not ser.get("id") or not eps:
                continue
            f["seriesId"] = ser["id"]; f["episodeIds"] = eps
        else:
            mov = it.get("movie") or {}
            if not mov.get("id"):
                continue
            f["movieId"] = mov["id"]
        files.append(f)
    if not files:
        return 0
    res = arr.command({"name": "ManualImport", "importMode": IMPORT_MODE, "files": files})
    return len(files) if res is not None else 0

class Arr:
    def __init__(self, name, kind, url, apikey):
        self.name, self.kind = name, kind                       # sonarr | radarr | prowlarr
        self.base = url.rstrip("/") + ("/api/v1" if kind == "prowlarr" else "/api/v3")
        self.apikey = apikey
        self.unknown = "includeUnknownSeriesItems=true" if kind == "sonarr" else "includeUnknownMovieItems=true"

    def _req(self, method, path, data=None, t=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def queue(self):
        if self.kind == "prowlarr":
            return []                                            # prowlarr has no download queue
        try:
            return json.load(self._req("GET", "/queue?page=1&pageSize=1000&" + self.unknown)).get("records", [])
        except Exception as e:
            log.warning("[%s] queue fetch failed: %s", self.name, e); return None

    def health(self):
        try:
            return json.load(self._req("GET", "/health"))
        except Exception:
            return []

    def remove(self, item_id, blocklist=None):
        bl = BLOCKLIST if blocklist is None else blocklist
        q = "removeFromClient=%s&blocklist=%s" % (str(REMOVE_CLIENT).lower(), str(bl).lower())
        self._req("DELETE", "/queue/%d?%s" % (item_id, q))

    def post(self, path, t=150):
        """POST with empty body (used for /indexer/testall, /downloadclient/testall). Returns parsed JSON or []."""
        try:
            body = self._req("POST", path, data=b"", t=t).read()
            return json.loads(body) if body else []
        except urllib.error.HTTPError as e:
            try: return json.loads(e.read())
            except Exception: return []
        except Exception as ex:
            log.debug("[%s] POST %s err %s", self.name, path, str(ex)[:50]); return []

    def get_json(self, path, t=None):
        try:
            return json.load(self._req("GET", path, t=t))
        except Exception as e:
            log.debug("[%s] GET %s err %s", self.name, path, str(e)[:60]); return None

    def command(self, body, t=120):
        """POST /command with a JSON body (e.g. ManualImport). Returns parsed JSON or None."""
        try:
            return json.load(self._req("POST", "/command", data=json.dumps(body).encode(), t=t))
        except Exception as e:
            log.warning("[%s] command %s failed: %s", self.name, body.get("name"), str(e)[:90]); return None

    def set_monitored(self, ids, monitored):
        """Bulk toggle monitoring for episodes (sonarr) / movies (radarr). Used by the churn brake."""
        if self.kind == "sonarr":
            path, body = "/episode/monitor", {"episodeIds": list(ids), "monitored": monitored}
        elif self.kind == "radarr":
            path, body = "/movie/editor", {"movieIds": list(ids), "monitored": monitored}
        else:
            return False
        try:
            self._req("PUT", path, data=json.dumps(body).encode()); return True
        except Exception as e:
            log.warning("[churn:%s] monitor %s failed: %s", self.name, "on" if monitored else "off", str(e)[:70])
            return False

    def queue_target_id(self, rec):
        """Stable id of what a queue record is FOR (episode for sonarr, movie for radarr)."""
        return rec.get("episodeId") if self.kind == "sonarr" else rec.get("movieId") if self.kind == "radarr" else None

class Riven:
    """rivenmedia/riven client (REST /api/v1, auth header x-api-key). Read-mostly: health/services
    watch plus item retry. We deliberately keep this OUT of the Arr list so the *arr sweeps never
    call Riven-only methods."""
    kind = "riven"

    def __init__(self, name, url, apikey):
        self.name = name
        self.base = url.rstrip("/") + "/api/v1"
        self.apikey = apikey

    def _req(self, method, path, t=None):
        req = urllib.request.Request(self.base + path, method=method, headers={"x-api-key": self.apikey})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def health(self):
        """(ok, detail). Riven returns {"message":"True"} when healthy."""
        try:
            d = json.load(self._req("GET", "/health", t=8))
            msg = str(d.get("message", "")).strip().lower()
            return (msg in ("true", "running", "ok", "initialized", "")), (msg or "ok")
        except Exception as e:
            return False, str(e)[:60]

    def services_down(self):
        """List of service names Riven reports as not-connected (e.g. a dead scraper/downloader)."""
        try:
            d = json.load(self._req("GET", "/services", t=8))
            return sorted([k for k, v in d.items() if not v]) if isinstance(d, dict) else []
        except Exception:
            return []

    def items(self, states, limit):
        """Items in any of `states` (oldest first), movies + shows."""
        q = "/items?limit=%d&page=1&sort=date_asc&type=movie&type=show" % limit
        for s in states:
            q += "&states=" + urllib.parse.quote(s)
        try:
            d = json.load(self._req("GET", q, t=20))
            return d.get("items", []) if isinstance(d, dict) else []
        except Exception as e:
            log.debug("[riven:%s] items fetch failed: %s", self.name, str(e)[:60]); return []

    def retry(self, ids):
        """Re-run the state machine for the given item ids (re-scrape/re-download)."""
        try:
            self._req("POST", "/items/retry?ids=" + ",".join(str(i) for i in ids), t=60); return True
        except Exception as e:
            log.warning("[riven:%s] retry failed: %s", self.name, str(e)[:80]); return False

class Mediastorm:
    """godver3/mediastorm client. Only /health is unauthenticated and there is no import queue to
    manage, so support is health-only."""
    kind = "mediastorm"

    def __init__(self, name, url, apikey=""):
        self.name = name
        self.url = url.rstrip("/")
        self.apikey = apikey

    def health(self):
        try:
            h = {"Authorization": "Bearer " + self.apikey} if self.apikey else None
            code = http_code(self.url + "/health", headers=h, t=MEDIASTORM_TIMEOUT)
            return code == 200, "HTTP %d" % code
        except Exception as e:
            return False, str(e)[:60]

def load_instances():
    """Build the *arr list (INSTANCES) and populate the isolated RIVENS / MEDIASTORMS globals.
    Riven and mediastorm are branched off BEFORE the sonarr/radarr fallback so they are never
    mis-typed as an *arr."""
    global RIVENS, MEDIASTORMS
    RIVENS, MEDIASTORMS = [], []
    out = []
    for n in range(1, 51):
        url = os.environ.get("INSTANCE_%d_URL" % n)
        if not url:
            continue
        key = os.environ.get("INSTANCE_%d_APIKEY" % n, "")
        kind = os.environ.get("INSTANCE_%d_TYPE" % n, "").strip().lower()
        if kind == "riven":
            name = os.environ.get("INSTANCE_%d_NAME" % n, "riven-%d" % n)
            if not key:
                log.warning("INSTANCE_%d (riven) has no APIKEY, skipping", n); continue
            RIVENS.append(Riven(name, url, key)); continue
        if kind == "mediastorm":
            name = os.environ.get("INSTANCE_%d_NAME" % n, "mediastorm-%d" % n)
            MEDIASTORMS.append(Mediastorm(name, url, key)); continue   # health-only, apikey optional
        if kind not in ("sonarr", "radarr", "prowlarr"):
            kind = ("radarr" if "radarr" in url.lower() else
                    "prowlarr" if "prowlarr" in url.lower() else "sonarr")
        name = os.environ.get("INSTANCE_%d_NAME" % n, "%s-%d" % (kind, n))
        if not key:
            log.warning("INSTANCE_%d has no APIKEY, skipping", n); continue
        out.append(Arr(name, kind, url, key))
    return out

INSTANCES = []
RIVENS = []
MEDIASTORMS = []

def _load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}

def _save_state(s):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        json.dump(s, open(STATE_FILE, "w"))
    except Exception:
        pass

def _offenders(state):
    return state.setdefault("__offenders__", {})

def _churn_record(state, arr, rec, title):
    """Count a dead grab for this episode/movie; brake if it's over the limit.
    Returns True if it un-monitored the target (so the caller knows the blocklist-remove won't re-search)."""
    if CHURN_LIMIT <= 0:
        return False
    tid = arr.queue_target_id(rec)
    if not tid:
        return False
    off = _offenders(state).setdefault(arr.name, {})
    o = off.setdefault(str(tid), {"fails": 0, "until": 0, "level": 0, "title": title})
    o["fails"] += 1; o["title"] = title
    if o["fails"] < CHURN_LIMIT or o["until"] != 0:        # below limit, or already parked/reported
        return False
    if CHURN_ACTION == "report":
        log.warning("[churn:%s] REPEAT-OFFENDER (%d dead grabs, still retrying): %s", arr.name, o["fails"], title)
        o["until"] = -1
        return False
    if CHURN_ACTION in ("park", "backoff") and arr.set_monitored([int(tid)], False):
        o["fails"] = 0
        if CHURN_ACTION == "backoff":
            lvl = o.get("level", 0)
            delay = CHURN_BACKOFF[min(lvl, len(CHURN_BACKOFF) - 1)]
            o["until"] = time.time() + delay; o["level"] = lvl + 1
            log.warning("[churn:%s] REPEAT-OFFENDER parked (retry #%d in %s) -> un-monitored: %s",
                        arr.name, lvl + 1, _human(delay), title)
        else:  # park: no auto-retry
            o["until"] = -1
            log.warning("[churn:%s] REPEAT-OFFENDER parked (un-monitored, manual re-monitor): %s", arr.name, title)
        return True
    return False

def _churn_remonitor(state):
    """Re-monitor parked titles whose backoff delay has elapsed, giving them a fresh attempt."""
    if CHURN_LIMIT <= 0 or CHURN_ACTION != "backoff":
        return
    now = time.time(); off_all = state.get("__offenders__", {})
    for arr in INSTANCES:
        for tid, o in list(off_all.get(arr.name, {}).items()):
            until = o.get("until", 0)
            if isinstance(until, (int, float)) and until > 0 and now >= until:
                if arr.set_monitored([int(tid)], True):
                    log.info("[churn:%s] backoff #%d elapsed, re-monitoring for a fresh attempt: %s",
                             arr.name, o.get("level", 0), o.get("title", ""))
                    o["fails"] = 0; o["until"] = 0           # keep level so the next park escalates

def check_queue(only=None):
    if LOAD_MAX > 0 and host_load() > LOAD_MAX:
        log.info("[queue] host load > %.0f -> skipping", LOAD_MAX); return
    state = _load_state(); actions = 0
    _churn_remonitor(state)
    for arr in INSTANCES:
        if only and arr.name.lower() != only.lower():
            continue
        recs = arr.queue()
        if recs is None:
            continue
        strikes = state.get(arr.name, {}); new = {}; stuck = 0
        for r in recs:
            reason = stuck_reason(r)
            if not reason:
                continue
            action = _action_for(reason)
            title = (r.get("title") or "")[:70]
            if action == "report":
                log.info("[queue:%s] %s (report-only, no change): %s", arr.name, reason, title)
                continue
            stuck += 1; iid = str(r.get("id")); cnt = strikes.get(iid, 0) + 1; new[iid] = cnt
            if cnt < MIN_STRIKES or actions >= MAX_ACTIONS:
                continue
            if DRY_RUN:
                log.info("[queue:%s] WOULD %s (%s strike %d): %s", arr.name, action, reason, cnt, title)
                continue
            if action == "force_import":
                try:
                    n = _force_import(arr, r)
                except Exception as e:
                    log.warning("[queue:%s] force_import failed: %s", arr.name, str(e)[:90]); n = 0
                if n:
                    actions += 1; new.pop(iid, None)
                    log.info("[queue:%s] force-imported %d file(s) (%s): %s", arr.name, n, reason, title)
                else:
                    log.info("[queue:%s] %s: nothing importable yet, leaving (strike %d): %s",
                             arr.name, reason, cnt, title)
            else:   # research (remove + blocklist) | remove (remove, never blocklist)
                bl = BLOCKLIST if action == "research" else False
                parked = _churn_record(state, arr, r, title)   # un-monitor first so the remove can't re-search
                try:
                    arr.remove(r["id"], blocklist=bl); actions += 1; new.pop(iid, None)
                    log.info("[queue:%s] removed (%s, action=%s, blocklist=%s)%s: %s",
                             arr.name, reason, action, str(bl).lower(),
                             " [parked, no re-search]" if parked else " -> re-search", title)
                except Exception as e:
                    log.warning("[queue:%s] remove failed: %s", arr.name, e)
        state[arr.name] = new
        if stuck:
            log.info("[queue:%s] %d stuck tracked, %d acted", arr.name, stuck, actions)
        for h in arr.health():
            if h.get("type") in ("error", "warning"):
                log.debug("[queue:%s] health %s: %s", arr.name, h.get("type"), (h.get("message") or "")[:90])
    _save_state(state)

# =========================================================================== #
# CHECK: decypharr (mount hang -> restart hook)
# =========================================================================== #

def _read_test(path, timeout):
    """Return True if a file under path read its first bytes within timeout, else False (hung/failed)."""
    result = {"ok": False}
    target = {"f": None}
    try:
        for root, _, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith((".mkv", ".mp4", ".avi", ".m4v", ".ts")):
                    target["f"] = os.path.join(root, fn); break
            if target["f"]:
                break
    except Exception:
        return None  # cannot even list -> unknown
    if not target["f"]:
        return None
    def _do():
        try:
            with open(target["f"], "rb") as fh:
                fh.read(65536)
            result["ok"] = True
        except Exception:
            result["ok"] = False
    th = threading.Thread(target=_do, daemon=True); th.start(); th.join(timeout)
    if th.is_alive():
        return False  # hung
    return result["ok"]

_decy_last_restart = [0.0]

def check_decypharr():
    if DECY_URL:
        c = http_code(DECY_URL, t=10)
        log.info("[decypharr] api %s -> %s", DECY_URL, c if c else "DOWN")
    if not DECY_MOUNT_TEST:
        return
    ok = _read_test(DECY_MOUNT_TEST, DECY_READ_TIMEOUT)
    if ok is None:
        log.warning("[decypharr] mount %s: no test file found / unlistable", DECY_MOUNT_TEST); return
    if ok:
        log.info("[decypharr] mount %s read OK", DECY_MOUNT_TEST); return
    log.error("[decypharr] mount %s READ HUNG (FUSE stall)", DECY_MOUNT_TEST)
    if DRY_RUN or not DECY_RESTART_CMD:
        log.error("[decypharr] no restart cmd set (or dry-run) -> alert only"); return
    if time.time() - _decy_last_restart[0] < 300:
        log.warning("[decypharr] restarted <5m ago, holding off"); return
    log.error("[decypharr] running restart hook: %s", DECY_RESTART_CMD)
    rc = run_cmd(DECY_RESTART_CMD); _decy_last_restart[0] = time.time()
    log.error("[decypharr] restart hook rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")

# =========================================================================== #
# CHECK: plex
# =========================================================================== #

def check_plex():
    if not PLEX_URL:
        return
    sep = "&" if "?" in PLEX_URL else "?"
    url = PLEX_URL.rstrip("/") + "/identity"
    c = http_code(url + (sep + "X-Plex-Token=" + PLEX_TOKEN if PLEX_TOKEN else ""), t=10)
    if c == 200:
        log.info("[plex] %s -> 200 OK", PLEX_URL)
    else:
        log.error("[plex] %s -> %s (unresponsive)", PLEX_URL, c if c else "DOWN")
    if PLEX_SCAN and PLEX_TOKEN and c == 200:
        try:
            urllib.request.urlopen(PLEX_URL.rstrip("/") + "/library/sections/all/refresh?X-Plex-Token=" + PLEX_TOKEN, timeout=10)
            log.info("[plex] triggered library refresh")
        except Exception as e:
            log.debug("[plex] refresh failed: %s", e)

# =========================================================================== #
# CHECK: resources
# =========================================================================== #

def _meminfo():
    d = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            d[k.strip()] = int(v.split()[0]) // 1024  # MB
    except Exception:
        pass
    return d

def check_resources():
    l1 = host_load()
    mi = _meminfo()
    avail = mi.get("MemAvailable", -1)
    swap_used = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
    msg = "[resources] load=%.1f memAvail=%sMB swapUsed=%sMB" % (l1, avail, swap_used)
    crit = (l1 >= RES_LOAD_WARN) or (0 <= avail < RES_MEM_MIN) or (swap_used >= RES_SWAP_WARN)
    (log.warning if crit else log.info)(msg + (" <-- PRESSURE" if crit else ""))
    if crit and RES_DROP_CACHES and not DRY_RUN:
        rc = run_cmd("sync; echo 1 > /proc/sys/vm/drop_caches")
        log.warning("[resources] dropped page cache rc=%s", rc[0] if rc else "?")

# =========================================================================== #
# CHECK: janitor (usenet dead-file quarantine, from a decypharr log file)
# =========================================================================== #

def check_janitor():
    has_log = JAN_LOG_CMD or (JAN_LOG and os.path.exists(JAN_LOG))
    if not (JAN_LIBS and has_log):
        log.debug("[janitor] need JANITOR_LIBRARY_PATHS + (JANITOR_LOG_CMD or a readable JANITOR_DECYPHARR_LOG)")
        return
    bad = set()
    try:
        if JAN_LOG_CMD:
            data = run_output(JAN_LOG_CMD)                       # e.g. journalctl when running on-host
        else:
            data = open(JAN_LOG, errors="ignore").read()[-2_000_000:]
    except Exception as e:
        log.warning("[janitor] cannot read log: %s", e); return
    pat = re.compile(r"Error streaming file: (.+?) error=\"([^\"]*)\"")
    for m in pat.finditer(data):
        path, err = m.group(1), m.group(2)
        if any(p.strip() and p.strip() in err for p in JAN_PATTERNS):
            bad.add(path.strip().split("/")[0])
    if not bad:
        log.debug("[janitor] no dead releases in log tail"); return
    moved = 0
    qroot = os.path.join(JAN_QUAR, time.strftime("%Y%m%d-%H%M%S"))
    manifest = []
    for libp in JAN_LIBS:
        for root, _, files in os.walk(libp):
            for fn in files:
                fp = os.path.join(root, fn)
                if not os.path.islink(fp):
                    continue
                try:
                    tgt = os.readlink(fp)
                except Exception:
                    continue
                mm = re.search(r"/__all__/([^/]+)(?:/|$)", tgt)
                if mm and mm.group(1) in bad:
                    if DRY_RUN:
                        log.info("[janitor] WOULD quarantine: %s", fp); continue
                    try:
                        dst = os.path.join(qroot, os.path.relpath(fp, "/"))
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.symlink(tgt, dst); os.unlink(fp)
                        manifest.append({"orig": fp, "target": tgt}); moved += 1
                    except Exception as e:
                        log.warning("[janitor] move failed %s: %s", fp, e)
    if manifest:
        try:
            os.makedirs(qroot, exist_ok=True); json.dump(manifest, open(qroot + "/manifest.json", "w"), indent=1)
        except Exception:
            pass
    if moved:
        log.info("[janitor] quarantined %d dead-file symlink(s) across %d release(s) -> %s", moved, len(bad), qroot)

# =========================================================================== #
# CHECK: scrubber (proactive file integrity scan)
#
# Walks library paths and verifies each file isn't going to make Plex skip
# mid-play. Most common failure mode on a usenet/decypharr stack: a file
# imported clean but has dead NZB articles partway through (cached header
# survived, mid-stream article rotted off retention) or slipped through with
# availability_sample_percent<100. Plex hits the dead segment, the FUSE read
# stalls, the family complains.
#
# Tiered, cheapest -> deepest. Default tier=2 (header + sampled chunks) catches
# the article-missing failure mode cheaply through the FUSE mount without
# restreaming whole files from Newshosting. Tier 3/4 only run on suspects
# (when SCRUBBER_PROMOTE_ON_SUSPECT) or when explicitly enabled.
#
#   tier 1: ffprobe header               (~1s per file; catches torn containers)
#   tier 2: + N sampled MB-sized chunks  (catches dead articles mid-file)
#   tier 3: + ffmpeg -v error stream-skim at N seek points
#                                        (catches packet/codec corruption)
#   tier 4: + full ffmpeg -v error -f null - decode of the whole file (slow; opt-in)
#
# State (path -> last-known result keyed on size+mtime) is persisted, so
# unchanged-OK files are skipped on subsequent sweeps - the scan is
# incremental. STRIKES count consecutive bad reads so a transient mount blip
# does not cost a re-grab. Confirmed BAD => quarantine the library symlink
# (reversible manifest, same shape as the janitor) + DELETE the owning arr's
# moviefile/episodefile with blocklist=true so the arr re-searches a clean
# release.
# =========================================================================== #

_SCRUB_ARR_INDEX_CACHE = {"sweep": 0, "data": None}
_SCRUB_SWEEP_COUNTER   = [0]

def _scrub_load_state():
    try:
        return json.load(open(SCRUB_STATE))
    except Exception:
        return {"files": {}, "manifest_dir": None}

def _scrub_save_state(s):
    try:
        os.makedirs(os.path.dirname(SCRUB_STATE) or ".", exist_ok=True)
        json.dump(s, open(SCRUB_STATE, "w"))
    except Exception as e:
        log.debug("[scrubber] state save failed: %s", e)

def _scrub_walk(paths):
    """Yield (real_path_for_io, lib_symlink_path) for every video file under any of `paths`.
    For symlinks we use the realpath for ffprobe/ffmpeg (so they read straight through the FUSE mount)
    and remember the original symlink so we can quarantine it cleanly."""
    seen = set()
    for libp in paths:
        try:
            for root, _, files in os.walk(libp):
                for fn in files:
                    if not fn.lower().endswith(SCRUB_EXTS):
                        continue
                    fp = os.path.join(root, fn)
                    try:
                        rp = os.path.realpath(fp)
                    except Exception:
                        rp = fp
                    if rp in seen:
                        continue
                    seen.add(rp)
                    yield rp, fp
        except Exception as e:
            log.warning("[scrubber] walk %s failed: %s", libp, e)

def _scrub_arr_index():
    """Build {realpath: (arr, fileId, kind)} once per sweep across all sonarr/radarr instances.
    kind = 'movie' for radarr, 'episode' for sonarr."""
    sweep = _SCRUB_SWEEP_COUNTER[0]
    if _SCRUB_ARR_INDEX_CACHE["sweep"] == sweep and _SCRUB_ARR_INDEX_CACHE["data"] is not None:
        return _SCRUB_ARR_INDEX_CACHE["data"]
    idx = {}
    for arr in INSTANCES:
        if arr.kind == "radarr":
            try:
                ms = json.load(arr._req("GET", "/movie"))
                for m in ms:
                    mf = m.get("movieFile") or {}
                    p = mf.get("path")
                    if p and mf.get("id"):
                        idx[os.path.realpath(p)] = (arr, mf["id"], "movie")
                log.debug("[scrubber] indexed %d radarr files from %s", sum(1 for v in idx.values() if v[0] is arr), arr.name)
            except Exception as e:
                log.warning("[scrubber] radarr index %s failed: %s", arr.name, str(e)[:80])
        elif arr.kind == "sonarr":
            try:
                series = json.load(arr._req("GET", "/series"))
                n0 = len(idx)
                for s in series:
                    sid = s.get("id")
                    if not sid:
                        continue
                    try:
                        efs = json.load(arr._req("GET", "/episodefile?seriesId=%d" % sid))
                        for ef in efs:
                            p = ef.get("path")
                            if p and ef.get("id"):
                                idx[os.path.realpath(p)] = (arr, ef["id"], "episode")
                    except Exception:
                        continue
                log.debug("[scrubber] indexed %d sonarr files from %s", len(idx) - n0, arr.name)
            except Exception as e:
                log.warning("[scrubber] sonarr index %s failed: %s", arr.name, str(e)[:80])
    _SCRUB_ARR_INDEX_CACHE["sweep"] = sweep
    _SCRUB_ARR_INDEX_CACHE["data"]  = idx
    return idx

def _scrub_run(cmd, timeout):
    """Run cmd with a hard timeout. Returns (rc, stderr_text). Empty stderr = clean."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=timeout)
        return p.returncode, p.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ds" % timeout
    except FileNotFoundError as e:
        return 127, "binary not found: %s" % e

def _scrub_t1_header(path):
    """Tier 1: ffprobe parses the container header. Returns (ok, detail)."""
    cmd = [SCRUB_FFPROBE, "-v", "error", "-hide_banner",
           "-show_entries", "format=duration,bit_rate",
           "-of", "default=nw=1", path]
    rc, err = _scrub_run(cmd, SCRUB_HEADER_TO)
    if rc == 0 and not err.strip():
        return True, ""
    return False, ("ffprobe rc=%d %s" % (rc, err.strip()[:200])) or "header_fail"

def _scrub_t2_skim(path):
    """Tier 2: decode SCRUB_SKIM_SECS at SCRUB_SKIM_POINTS seek points with ffmpeg's null muxer.
    This is the right primitive for a decypharr FUSE mount: ffmpeg BLOCKS waiting for FUSE I/O
    until bytes arrive, so a cold-cache miss (the mount returns 0 bytes for an unfetched chunk)
    doesn't false-positive the way raw byte reads do. A dead NZB article makes ffmpeg log a real
    decode/demux error (or the timeout fires). Catches both 'mid-file dead segment' and
    'packet/codec corruption' in one pass, at the cost of pulling ~SECS*bitrate bytes per seek
    point (a few hundred MB per file at 1080p), all via the FUSE mount."""
    # get duration; ffprobe with stderr=error never prints it to stderr -> capture stdout
    try:
        p = subprocess.run([SCRUB_FFPROBE, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=SCRUB_HEADER_TO)
        dur = float((p.stdout or b"0").decode("utf-8", "replace").strip() or "0")
    except Exception:
        dur = 0.0
    n = max(2, SCRUB_SKIM_POINTS)
    sec = max(1, SCRUB_SKIM_SECS)
    pts = [int(dur * i / (n + 1)) for i in range(1, n + 1)] if dur > 0 else [0]
    for off in pts:
        # decode video-only (skip audio) to keep bandwidth modest; if a corrupt audio packet
        # is what actually skips Plex playback ffmpeg still surfaces the demux error.
        cmd = [SCRUB_FFMPEG, "-v", "error", "-hide_banner",
               "-ss", str(off), "-t", str(sec),
               "-i", path, "-map", "0:v:0", "-f", "null", "-"]
        rc, err = _scrub_run(cmd, SCRUB_SKIM_TO)
        if rc != 0 or err.strip():
            return False, "ffmpeg @%ds rc=%d %s" % (off, rc, (err or "").strip()[:200])
    return True, ""

def _scrub_t3_full(path):
    """Tier 3: full -v error decode of the whole file. Slow. Opt-in or used as final-confirm
    before action when SCRUBBER_FULL_DECODE_ON_BAD is set."""
    cmd = [SCRUB_FFMPEG, "-v", "error", "-hide_banner", "-i", path, "-f", "null", "-"]
    rc, err = _scrub_run(cmd, SCRUB_FULL_TO)
    if rc != 0 or err.strip():
        return False, "ffmpeg full rc=%d %s" % (rc, (err or "").strip()[:300])
    return True, ""

def _scrub_act_on_bad(real_path, lib_symlink, reason, state, manifest):
    """Quarantine the library symlink + delete the owning arr's file with blocklist=true.
    Mirrors the janitor's quarantine shape so 'undo' is the same drill."""
    if DRY_RUN:
        log.warning("[scrubber] WOULD quarantine + re-grab %s (%s)", lib_symlink, reason)
        return False
    qroot = state.get("manifest_dir") or os.path.join(SCRUB_QUAR, time.strftime("scrubber-%Y%m%d-%H%M%S"))
    state["manifest_dir"] = qroot
    try:
        os.makedirs(qroot, exist_ok=True)
    except Exception as e:
        log.warning("[scrubber] cannot create quar dir %s: %s", qroot, e); return False
    # 1) move the library symlink (preserve its target so an undo is just `mv` back)
    moved = False
    try:
        if os.path.islink(lib_symlink):
            tgt = os.readlink(lib_symlink)
            dst = os.path.join(qroot, os.path.relpath(lib_symlink, "/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(tgt, dst)
            os.unlink(lib_symlink)
            moved = True
        elif os.path.exists(lib_symlink):
            # not a symlink (a flat file under the library) - move the file itself
            dst = os.path.join(qroot, os.path.relpath(lib_symlink, "/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(lib_symlink, dst)
            moved = True
    except Exception as e:
        log.warning("[scrubber] quarantine %s failed: %s", lib_symlink, e)
    # 2) ask the arr to delete the file record + blocklist so a different release is searched
    arr_acted = False
    if SCRUB_DEL_ARR:
        idx = _scrub_arr_index()
        ent = idx.get(real_path) or idx.get(os.path.realpath(lib_symlink))
        if ent:
            arr, file_id, kind = ent
            path = "/moviefile/%d" % file_id if kind == "movie" else "/episodefile/%d" % file_id
            try:
                arr._req("DELETE", path)
                arr_acted = True
                log.warning("[scrubber] [%s:%s] deleted %s id=%d -> arr will re-search (%s)",
                            arr.name, kind, kind+"File", file_id, reason)
            except Exception as e:
                log.warning("[scrubber] arr delete failed (%s id=%d): %s", kind, file_id, str(e)[:120])
        else:
            log.info("[scrubber] no arr record matched for %s (quarantine only)", real_path)
    manifest.append({"real": real_path, "symlink": lib_symlink, "reason": reason,
                     "moved": moved, "arr_acted": arr_acted, "ts": int(time.time())})
    return moved or arr_acted

def check_scrubber():
    if not SCRUB_PATHS:
        log.debug("[scrubber] no SCRUBBER_PATHS (or JANITOR_LIBRARY_PATHS) configured"); return
    # plex-safe gate
    load1 = host_load()
    if SCRUB_LOAD_MAX > 0 and load1 > SCRUB_LOAD_MAX:
        log.info("[scrubber] load %.1f > %.1f, skipping this sweep", load1, SCRUB_LOAD_MAX); return
    state = _scrub_load_state()
    files_state = state.setdefault("files", {})
    _SCRUB_SWEEP_COUNTER[0] += 1
    now = time.time()
    reverify_after = SCRUB_REVERIFY_DAYS * 86400 if SCRUB_REVERIFY_DAYS > 0 else 0
    # pick candidates: never-scanned OR changed (size/mtime) OR overdue for reverify OR previously suspect (strikes>0)
    candidates = []
    for real_path, lib_symlink in _scrub_walk(SCRUB_PATHS):
        try:
            st = os.stat(real_path)
        except Exception:
            continue
        if (now - st.st_mtime) < SCRUB_MIN_AGE * 3600:
            continue   # don't fight fresh imports / the warmer
        rec = files_state.get(real_path) or {}
        if rec.get("size") == st.st_size and rec.get("mtime") == int(st.st_mtime):
            if rec.get("status") == "ok" and reverify_after > 0 and (now - rec.get("ts", 0)) < reverify_after:
                continue
            if rec.get("status") == "bad":
                continue   # already actioned; will reappear as a new path once arr re-grabs
        candidates.append((real_path, lib_symlink, st))
        if len(candidates) >= SCRUB_MAX_FILES * 4:
            break
    # Priority: SUSPECTS first (so a 1-strike file gets its 2nd strike next sweep instead of
    # waiting for the whole library to be scanned once), then never-scanned, then due-for-reverify.
    # Within each tier, oldest-tested first so the queue cycles evenly.
    def _prio(real_path):
        rec = files_state.get(real_path, {})
        if rec.get("status") == "suspect":
            return (0, rec.get("ts", 0))
        return (1, rec.get("ts", 0))
    candidates.sort(key=lambda t: _prio(t[0]))
    candidates = candidates[:SCRUB_MAX_FILES]
    if not candidates:
        log.debug("[scrubber] nothing due (all cached-OK or below min-age)"); return
    log.info("[scrubber] scanning %d file(s), tier=%d", len(candidates), SCRUB_TIER)
    manifest = []
    bad = 0; suspect = 0; ok_n = 0
    for real_path, lib_symlink, st in candidates:
        # re-check load mid-sweep; bail early if we've started crowding decypharr
        if SCRUB_LOAD_MAX > 0 and host_load() > SCRUB_LOAD_MAX:
            log.info("[scrubber] load climbed >%.1f, pausing mid-sweep", SCRUB_LOAD_MAX)
            break
        rec = files_state.setdefault(real_path, {})
        # ----- tier 1: ffprobe header -----
        ok, why = _scrub_t1_header(real_path)
        cur_tier = 1
        # ----- tier 2: ffmpeg skim at N seek points (FUSE-safe; blocks on cold chunks) -----
        if ok and SCRUB_TIER >= 2:
            ok, why = _scrub_t2_skim(real_path); cur_tier = 2
        # ----- tier 3: full ffmpeg decode (opt-in, or used to final-confirm a tier-2 BAD) -----
        if (not ok and SCRUB_FULL_ON_BAD) or (ok and SCRUB_TIER >= 3):
            ok3, why3 = _scrub_t3_full(real_path); cur_tier = 3
            if not ok and not ok3:
                why = "tier2+3 BAD: %s | %s" % (why, why3)
            elif not ok and ok3:
                ok, why = True, "tier2 hiccup cleared by full decode"
            elif ok and not ok3:
                ok, why = False, "tier3 full: %s" % why3
        # ----- record result -----
        size = st.st_size; mtime = int(st.st_mtime)
        prev_strikes = rec.get("strikes", 0)
        if ok:
            rec.update({"status": "ok", "size": size, "mtime": mtime, "ts": int(now),
                        "tier": cur_tier, "strikes": 0})
            ok_n += 1
            log.debug("[scrubber] OK  t%d %s", cur_tier, real_path)
        else:
            strikes = prev_strikes + 1
            rec.update({"status": "suspect" if strikes < SCRUB_STRIKES else "bad",
                        "size": size, "mtime": mtime, "ts": int(now),
                        "tier": cur_tier, "strikes": strikes, "why": why[:240]})
            if strikes < SCRUB_STRIKES:
                suspect += 1
                log.warning("[scrubber] SUSPECT t%d (%d/%d) %s :: %s",
                            cur_tier, strikes, SCRUB_STRIKES, real_path, why[:160])
            else:
                bad += 1
                log.error("[scrubber] BAD t%d %s :: %s", cur_tier, real_path, why[:200])
                _scrub_act_on_bad(real_path, lib_symlink, why, state, manifest)
    # persist manifest snapshot for reversibility
    if manifest and state.get("manifest_dir"):
        try:
            mf = os.path.join(state["manifest_dir"], "manifest.json")
            existing = []
            if os.path.exists(mf):
                try: existing = json.load(open(mf))
                except Exception: pass
            json.dump(existing + manifest, open(mf, "w"), indent=1)
        except Exception:
            pass
    _scrub_save_state(state)
    log.info("[scrubber] done: %d ok, %d suspect, %d bad (action)", ok_n, suspect, bad)

# =========================================================================== #
# CHECK: watchlists - pull Plex Home users + non-Home friends watchlists and
# add new titles directly to Sonarr/Radarr, bypassing Overseerr.
#
# Why bypass seerr: seerr's add-to-arr call has a fixed ~10s timeout and no
# retry; under load it silently drops requests (the existing seerr check
# re-drives failed ones, but the user wanted to skip the approval step
# entirely for people they trust). Watchlist = implicit "I want this" signal,
# no approval UI needed.
#
# Sources of watchlist tokens:
#   - Plex Home users: enumerated from the owner's PLEX_TOKEN via plex.tv's
#     /home/users API, then /home/users/{uuid}/switch (with PIN if set) returns
#     each managed user's token. WATCHLISTS_INCLUDE_HOME=true (default) turns
#     this on.
#   - Non-Home Plex friends: each gives their own X-Plex-Token; configured as
#     "label:token,label:token" in WATCHLISTS_FRIENDS.
#
# Each token then hits discover.provider.plex.tv to fetch the watchlist, which
# embeds tmdb:/tvdb:/imdb: GUIDs on each item. We index the current arrs once
# per sweep to skip titles already in the library, try the 4K instance first
# and fall back to 1080p if the 4K add fails (no 4K release, no matching
# profile, etc.). Confirmed adds are cached in WATCHLISTS_STATE_FILE so the
# same title isn't re-attempted next sweep.
# =========================================================================== #

_WL_ARR_INDEX_CACHE = {"sweep": 0, "data": None}

def _wl_http(url, headers=None, t=None):
    """GET url, return (status, bytes). Doesn't raise on HTTP errors."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=t or WL_HTTP_TO) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        try: return e.code, e.read()
        except Exception: return e.code, b""
    except Exception as e:
        log.debug("[watchlists] GET %s err: %s", url, str(e)[:120])
        return 0, b""

def _wl_post(url, headers=None, data=None, t=None):
    try:
        req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST")
        with urllib.request.urlopen(req, timeout=t or WL_HTTP_TO) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        try: return e.code, e.read()
        except Exception: return e.code, b""
    except Exception as e:
        log.debug("[watchlists] POST %s err: %s", url, str(e)[:120])
        return 0, b""

def _wl_collect_tokens():
    """Return list of (label, token) for every watchlist source we'll poll."""
    tokens = []
    # Non-Home friends from env
    for entry in (WL_FRIENDS or "").split(","):
        entry = entry.strip()
        if not entry or ":" not in entry: continue
        lab, tok = entry.split(":", 1)
        tokens.append((lab.strip() or "friend", tok.strip()))
    # Plex Home users via the owner's PLEX_TOKEN
    if WL_HOME_INCLUDE and PLEX_TOKEN:
        pins = {}
        for entry in (WL_HOME_PINS or "").split(","):
            if ":" in entry:
                u, p = entry.split(":", 1); pins[u.strip()] = p.strip()
        code, body = _wl_http("https://plex.tv/api/v2/home/users",
                              headers={"X-Plex-Token": PLEX_TOKEN,
                                       "X-Plex-Client-Identifier": "stack-doctor",
                                       "Accept": "application/json"})
        if code == 200:
            try:
                users = json.loads(body)
                users_list = users.get("users") if isinstance(users, dict) else (users if isinstance(users, list) else [])
                for u in users_list or []:
                    title = u.get("title") or u.get("friendlyName") or u.get("username") or "home-user"
                    uuid  = u.get("uuid") or u.get("id")
                    if u.get("admin"):
                        # owner: use PLEX_TOKEN directly, no switch needed
                        tokens.append(("home/%s" % title, PLEX_TOKEN)); continue
                    sw_url = "https://plex.tv/api/v2/home/users/%s/switch" % uuid
                    if pins.get(str(uuid)): sw_url += "?pin=" + pins[str(uuid)]
                    sc, sb = _wl_post(sw_url,
                                      headers={"X-Plex-Token": PLEX_TOKEN,
                                               "X-Plex-Client-Identifier": "stack-doctor",
                                               "Accept": "application/json"})
                    if sc in (200, 201):
                        try:
                            sub = json.loads(sb); sub_tok = sub.get("authToken")
                            if sub_tok: tokens.append(("home/%s" % title, sub_tok))
                            else: log.debug("[watchlists] home %s: switch returned no token", title)
                        except Exception:
                            log.debug("[watchlists] home %s: switch body parse failed", title)
                    else:
                        log.info("[watchlists] home %s: switch failed (HTTP %s) - PIN required?", title, sc)
            except Exception as e:
                log.warning("[watchlists] /home/users parse failed: %s", str(e)[:120])
        else:
            log.warning("[watchlists] /home/users HTTP %s", code)
    return tokens

def _wl_fetch(token):
    """Return list of {plex_id, type, tmdb, tvdb, title, year} for one user's watchlist.
    Plex Discover caps Container-Size (>100 returns 400), so we paginate. Auth via QUERY PARAM
    (X-Plex-Token: header gets 403 on Discover even though it works on a local PMS).
    NOTE: Discover's listing endpoint does NOT include external GUIDs (tmdb/tvdb) on items;
    those have to be fetched from metadata.provider.plex.tv per item - done lazily in
    _wl_resolve_ids() so we only do it for items not already in the library or in our cache."""
    items = []; seen_pg = set(); start = 0
    safe_size = max(20, min(int(WL_PAGE_SIZE), 100))
    while True:
        url = ("https://discover.provider.plex.tv/library/sections/watchlist/all"
               "?includeCollections=1&includeExternalMedia=1"
               "&X-Plex-Container-Start=%d&X-Plex-Container-Size=%d"
               "&X-Plex-Token=%s") % (start, safe_size, urllib.parse.quote(token))
        code, body = _wl_http(url, headers={"Accept": "application/json"})
        if code != 200:
            log.warning("[watchlists] discover HTTP %s (start=%d)", code, start); break
        try:
            mc = json.loads(body).get("MediaContainer", {})
            md = mc.get("Metadata", []) or []
            total = int(mc.get("totalSize", 0) or 0)
        except Exception as e:
            log.warning("[watchlists] discover parse failed: %s", str(e)[:120]); break
        for v in md:
            pg = v.get("guid") or ""
            if pg and pg in seen_pg: continue
            seen_pg.add(pg)
            plex_id = v.get("ratingKey")
            items.append({"plex_id": plex_id, "type": v.get("type") or "",
                          "tmdb": None, "tvdb": None,
                          "title": v.get("title") or "", "year": v.get("year")})
        if not md or len(items) >= total or len(md) < safe_size:
            break
        start += len(md)
    return items

def _wl_resolve_ids(plex_id, token, cache):
    """Fetch tmdb / tvdb GUIDs for a single Plex Discover item; cache the answer forever
    (Plex ids are immutable). Returns (tmdb, tvdb) or (None, None) on failure."""
    if not plex_id: return None, None
    if plex_id in cache:
        c = cache[plex_id]; return c.get("tmdb"), c.get("tvdb")
    url = ("https://metadata.provider.plex.tv/library/metadata/%s?X-Plex-Token=%s"
           % (urllib.parse.quote(str(plex_id)), urllib.parse.quote(token)))
    code, body = _wl_http(url, headers={"Accept": "application/json"})
    if code != 200:
        log.debug("[watchlists] resolve %s -> HTTP %s", plex_id, code)
        return None, None
    tmdb = tvdb = None
    try:
        mc = json.loads(body).get("MediaContainer", {})
        for v in mc.get("Metadata", []) or []:
            for g in v.get("Guid", []) or []:
                gid = g.get("id") or ""
                if gid.startswith("tmdb://"): tmdb = gid.split("//",1)[1]
                elif gid.startswith("tvdb://"): tvdb = gid.split("//",1)[1]
    except Exception as e:
        log.debug("[watchlists] resolve parse %s: %s", plex_id, str(e)[:80])
    cache[plex_id] = {"tmdb": tmdb, "tvdb": tvdb}
    return tmdb, tvdb

def _wl_arr_index():
    """{ 'tmdb:NNN', 'tvdb:NNN', ... } across all arrs - skip-set for already-in-library."""
    sweep = _SCRUB_SWEEP_COUNTER[0]   # reuse the same per-sweep counter
    if _WL_ARR_INDEX_CACHE["sweep"] == sweep and _WL_ARR_INDEX_CACHE["data"] is not None:
        return _WL_ARR_INDEX_CACHE["data"]
    idx = set()
    for arr in INSTANCES:
        try:
            if arr.kind == "radarr":
                for m in json.load(arr._req("GET", "/movie")):
                    if m.get("tmdbId"): idx.add("tmdb:%s" % m["tmdbId"])
            elif arr.kind == "sonarr":
                for s in json.load(arr._req("GET", "/series")):
                    if s.get("tvdbId"): idx.add("tvdb:%s" % s["tvdbId"])
        except Exception as e:
            log.debug("[watchlists] %s index failed: %s", arr.name, str(e)[:80])
    _WL_ARR_INDEX_CACHE["sweep"] = sweep
    _WL_ARR_INDEX_CACHE["data"]  = idx
    return idx

def _wl_quality_for(label):
    """Resolve quality preference for a source label. Returns one of '4k' | '1080p' | 'both'.
    WATCHLISTS_QUALITY format: '*=both,home/kids=1080p,alice=4k,bob=1080p' (exact-match wins
    over wildcard). Unknown label -> WATCHLISTS_DEFAULT_QUALITY."""
    rules = {}
    for entry in (WL_QUALITY_MAP or "").split(","):
        if "=" not in entry: continue
        k, v = entry.split("=", 1)
        rules[k.strip().lower()] = v.strip().lower()
    lab = (label or "").strip().lower()
    if lab in rules: q = rules[lab]
    elif "*" in rules: q = rules["*"]
    else: q = (WL_DEFAULT_QUALITY or "both").lower()
    if q not in ("4k", "1080p", "both"): q = "both"
    return q

def _wl_arr_for(kind, quality):
    """Return arr instances of `kind` to try (in order) for the given quality preference.
    quality='4k'    -> [arr_4k only]
    quality='1080p' -> [arr_1080p only]
    quality='both'  -> [arr_4k, arr_1080p]  (added to BOTH instances)
    If only one tier exists for `kind`, the other tier silently degrades to that one.
    A None entry is dropped."""
    fourk = None; std = None
    for arr in INSTANCES:
        if arr.kind != kind: continue
        if "4k" in arr.name.lower() or "uhd" in arr.name.lower():
            fourk = arr
        else:
            std = arr
    if quality == "4k":
        return [a for a in (fourk,) if a]
    if quality == "1080p":
        return [a for a in (std,) if a]
    # both
    return [a for a in (fourk, std) if a]

def _wl_profile_for(arr):
    """qualityProfileId for this arr: respect WATCHLISTS_PROFILES override, else first available."""
    for entry in (WL_PROFILES or "").split(","):
        if "=" in entry:
            k, v = entry.split("=", 1)
            if k.strip().lower() == arr.name.lower():
                try: return int(v.strip())
                except Exception: pass
    try:
        profs = json.load(arr._req("GET", "/qualityprofile"))
        if profs: return profs[0]["id"]
    except Exception: pass
    return 1

def _wl_root_for(arr):
    try:
        rfs = json.load(arr._req("GET", "/rootfolder"))
        if rfs: return rfs[0]["path"]
    except Exception: pass
    return None

def _wl_add(item, arr):
    """Try to add `item` to `arr`. Returns (ok, message)."""
    qp   = _wl_profile_for(arr)
    root = _wl_root_for(arr)
    if not root: return False, "no rootFolder"
    if arr.kind == "radarr" and item.get("tmdb"):
        code, body = _wl_http("%s/movie/lookup/tmdb?tmdbId=%s" % (arr.base, item["tmdb"]),
                              headers={"X-Api-Key": arr.apikey})
        if code != 200: return False, "lookup HTTP %s" % code
        try: m = json.loads(body)
        except Exception: return False, "lookup parse failed"
        if isinstance(m, list):
            if not m: return False, "lookup empty"
            m = m[0]
        payload = {**m, "qualityProfileId": qp, "rootFolderPath": root, "monitored": True,
                   "minimumAvailability": "released",
                   "addOptions": {"searchForMovie": True}}
        try:
            arr._req("POST", "/movie", data=json.dumps(payload).encode())
            return True, "added"
        except urllib.error.HTTPError as e:
            msg = ""
            try: msg = e.read().decode("utf-8","replace")[:200]
            except Exception: pass
            return False, "POST /movie HTTP %s %s" % (e.code, msg[:120])
        except Exception as e:
            return False, "POST /movie err %s" % str(e)[:120]
    elif arr.kind == "sonarr" and item.get("tvdb"):
        code, body = _wl_http("%s/series/lookup?term=tvdb:%s" % (arr.base, item["tvdb"]),
                              headers={"X-Api-Key": arr.apikey})
        if code != 200: return False, "lookup HTTP %s" % code
        try: arr_list = json.loads(body)
        except Exception: return False, "lookup parse failed"
        if not arr_list: return False, "lookup empty"
        s = arr_list[0]
        payload = {**s, "qualityProfileId": qp, "rootFolderPath": root, "monitored": True,
                   "seasonFolder": True, "seriesType": s.get("seriesType") or "standard",
                   "addOptions": {"monitor": "all", "searchForMissingEpisodes": True,
                                  "searchForCutoffUnmetEpisodes": False}}
        try:
            arr._req("POST", "/series", data=json.dumps(payload).encode())
            return True, "added"
        except urllib.error.HTTPError as e:
            msg = ""
            try: msg = e.read().decode("utf-8","replace")[:200]
            except Exception: pass
            return False, "POST /series HTTP %s %s" % (e.code, msg[:120])
        except Exception as e:
            return False, "POST /series err %s" % str(e)[:120]
    return False, "no usable id (tmdb/tvdb) for type=%s" % item.get("type")

def _wl_load_state():
    try: return json.load(open(WL_STATE))
    except Exception: return {"added": {}}

def _wl_save_state(s):
    try:
        os.makedirs(os.path.dirname(WL_STATE) or ".", exist_ok=True)
        json.dump(s, open(WL_STATE, "w"))
    except Exception as e:
        log.debug("[watchlists] state save failed: %s", e)

def check_watchlists():
    tokens = _wl_collect_tokens()
    if not tokens:
        log.debug("[watchlists] no tokens (set WATCHLISTS_FRIENDS or PLEX_TOKEN+WATCHLISTS_INCLUDE_HOME=true)")
        return
    state = _wl_load_state()
    added = state.setdefault("added", {})
    id_cache = state.setdefault("plex_id_cache", {})  # plex ratingKey -> {tmdb,tvdb}
    arr_idx = _wl_arr_index()
    log.info("[watchlists] polling %d source(s); library skip-set: %d titles", len(tokens), len(arr_idx))
    acts = 0; skipped_in_lib = skipped_cached = skipped_noid = 0
    seen = set()
    for label, tok in tokens:
        wl = _wl_fetch(tok)
        log.debug("[watchlists] %s: %d items", label, len(wl))
        for it in wl:
            # Discover's listing doesn't include external IDs; resolve via metadata endpoint
            # (cached forever — Plex ratingKeys are immutable).
            if not (it.get("tmdb") or it.get("tvdb")):
                tmdb, tvdb = _wl_resolve_ids(it.get("plex_id"), tok, id_cache)
                it["tmdb"], it["tvdb"] = tmdb, tvdb
            # Pick the id that matches the title kind (radarr needs tmdb, sonarr needs tvdb).
            # If the matching id isn't on the metadata response, the title cannot be auto-added.
            t = it.get("type")
            if t == "movie":
                key = ("tmdb:%s" % it["tmdb"]) if it.get("tmdb") else None
            elif t in ("show", "series"):
                key = ("tvdb:%s" % it["tvdb"]) if it.get("tvdb") else None
            else:
                key = None
            if not key:
                log.debug("[watchlists] no usable %s id for %s (plex_id=%s)", t, it.get("title"), it.get("plex_id"))
                skipped_noid += 1; continue
            if key in seen: continue
            seen.add(key)
            if key in arr_idx:
                skipped_in_lib += 1; continue
            if key in added:
                skipped_cached += 1; continue
            if acts >= WL_MAX_ADDS:
                log.info("[watchlists] hit per-sweep cap %d, deferring rest", WL_MAX_ADDS); break
            kind = "radarr" if it["type"] == "movie" else "sonarr" if it["type"] in ("show", "series") else None
            if not kind:
                log.debug("[watchlists] unknown type %s for %s", it["type"], it["title"]); continue
            qpref = _wl_quality_for(label)
            arrs  = _wl_arr_for(kind, qpref)
            if not arrs:
                log.warning("[watchlists] %s wants %s but no matching %s instance",
                            label, qpref, kind); continue
            if DRY_RUN:
                log.info("[watchlists] WOULD add (%s, q=%s, -> %s) %s (%s) from %s",
                         kind, qpref, ",".join(a.name for a in arrs), it["title"], key, label)
                added[key] = {"added_to": "DRY_RUN", "ts": int(time.time()), "from": label,
                              "quality": qpref}
                acts += 1; continue
            # 'both' = add to every arr in the list; '4k' / '1080p' = single target.
            # Fallback semantics: if quality=4k and the 4K add fails, fall back to 1080p (the
            # title is still wanted, just at lower quality). For quality=both, each tier is
            # independent (4K failing doesn't block 1080p and vice versa).
            placed_any = False; placed_to = []
            if qpref == "both":
                for arr in arrs:
                    ok, msg = _wl_add(it, arr)
                    if ok:
                        placed_any = True; placed_to.append(arr.name)
                    else:
                        log.info("[watchlists] %s -> %s failed: %s", it["title"], arr.name, msg)
            else:
                # single-quality with one fallback to the OTHER tier on failure
                primary = arrs[0]
                ok, msg = _wl_add(it, primary)
                if ok:
                    placed_any = True; placed_to = [primary.name]
                else:
                    log.info("[watchlists] %s -> %s failed: %s (trying fallback)",
                             it["title"], primary.name, msg)
                    other = _wl_arr_for(kind, "1080p" if qpref == "4k" else "4k")
                    if other:
                        ok2, msg2 = _wl_add(it, other[0])
                        if ok2: placed_any = True; placed_to = [other[0].name]
                        else: log.info("[watchlists] %s -> %s failed: %s",
                                       it["title"], other[0].name, msg2)
            if placed_any:
                log.warning("[watchlists] added (%s, q=%s) %s (%s) -> %s -- from %s",
                            kind, qpref, it["title"], key, "+".join(placed_to), label)
                added[key] = {"added_to": placed_to, "ts": int(time.time()),
                              "from": label, "quality": qpref}
                acts += 1; arr_idx.add(key)
            else:
                log.warning("[watchlists] all %s instances failed for %s (%s) (q=%s, from %s)",
                            kind, it["title"], key, qpref, label)
        if acts >= WL_MAX_ADDS: break
    _wl_save_state(state)
    log.info("[watchlists] done: added=%d, already-in-library=%d, already-attempted=%d, no-external-id=%d",
             acts, skipped_in_lib, skipped_cached, skipped_noid)


# =========================================================================== #
# holidays: build a themed movie collection a few days before each holiday and
# pin it to Plex Home (the recommended row), then take it down a few days after.
#
# Curation is a hardcoded per-holiday definition (overridable via JSON). Each
# holiday matches films three ways, unioned:
#   - "titles":   exact film titles (case-insensitive) -> a true curated list
#   - "keywords": substring match on the film title     -> catches the obvious ones
#   - "genre":    every film in a Plex genre            -> e.g. all Horror for Halloween
# All matching is metadata-only (no file reads), so it is safe on a
# decypharr/FUSE library. The collection is a fixed set of ratingKeys (smart=0).
# =========================================================================== #

# Shared holidays celebrated across many of the countries below. Defined once and reused; when
# multiple selected countries include the same-named holiday the definitions are merged (keywords
# unioned) so only one collection is ever built per name.
_H_NEWYEAR   = {"name": "New Year Movies",   "month": 1,  "day": 1,  "lead": 7,
                "keywords": ["new year", "new year's", "new years"]}
_H_VALENTINE = {"name": "Valentine's Movies", "month": 2, "day": 14, "lead": 14,
                "genre": "Romance", "keywords": ["valentine"]}
_H_HALLOWEEN = {"name": "Halloween Movies",  "month": 10, "day": 31, "lead": 21,
                "genre": "Horror", "keywords": ["halloween"]}
_H_XMAS      = {"name": "Christmas Movies",   "month": 12, "day": 25, "lead": 35,
                "keywords": ["christmas", "xmas", "santa", "noel", "elf", "grinch", "scrooge",
                             "jingle", "reindeer", "frosty", "krampus", "nativity", "nutcracker",
                             "polar express", "home alone", "klaus", "miracle on 34", "holiday inn",
                             "love actually", "die hard"]}
_H_BOXING    = {"name": "Boxing Day Movies",  "month": 12, "day": 26, "lead": 2,
                "keywords": ["boxing day"]}

# Lunar / solar-term holidays have no fixed Gregorian date, so they carry an explicit per-year
# date table (extend as needed; a year missing from the table is simply skipped that year).
_D_LUNAR_NY   = {"2026": "2026-02-17", "2027": "2027-02-06", "2028": "2028-01-26",
                 "2029": "2029-02-13", "2030": "2030-02-03"}   # Chinese/Korean Lunar New Year
_D_MIDAUTUMN  = {"2026": "2026-09-25", "2027": "2027-09-15", "2028": "2028-10-03",
                 "2029": "2029-09-22", "2030": "2030-09-12"}   # Mid-Autumn / Chuseok
_D_DRAGONBOAT = {"2026": "2026-06-19", "2027": "2027-06-09", "2028": "2028-05-28",
                 "2029": "2029-06-16", "2030": "2030-06-05"}   # Duanwu / Dragon Boat
_D_QINGMING   = {"2026": "2026-04-05", "2027": "2027-04-05", "2028": "2028-04-04",
                 "2029": "2029-04-04", "2030": "2030-04-05"}   # Qingming / Tomb-Sweeping

# Curated per-country holiday sets. HOLIDAYS_COUNTRIES selects which to merge (default "us").
# Themed matching leans on English title keywords + Plex genres, so non-English libraries may
# match sparsely; tune any holiday with explicit "titles"/"keywords" via HOLIDAYS_DEFINITIONS.
_HOLIDAY_SETS = {
    "us": [
        _H_NEWYEAR, _H_VALENTINE,
        {"name": "St. Patrick's Movies", "month": 3, "day": 17, "lead": 10,
         "keywords": ["leprechaun", "irish", "st patrick", "st. patrick"]},
        {"name": "Independence Day Movies", "month": 7, "day": 4, "lead": 12,
         "keywords": ["independence day", "patriot", "american sniper", "top gun", "born on the fourth"]},
        _H_HALLOWEEN,
        {"name": "Thanksgiving Movies", "month": 11, "day": 1, "lead": 14, "rule": "thanksgiving",
         "keywords": ["thanksgiving", "turkey", "planes trains"]},
        _H_XMAS,
    ],
    "canada": [
        _H_NEWYEAR, _H_VALENTINE,
        {"name": "Canada Day Movies", "month": 7, "day": 1, "lead": 10,
         "countries": ["Canada"], "keywords": ["canadian", "mountie"]},
        {"name": "Canadian Thanksgiving Movies", "month": 10, "day": 1, "lead": 10,
         "rule": "nth_weekday", "weekday": 0, "n": 2, "keywords": ["thanksgiving", "turkey", "harvest"]},
        _H_HALLOWEEN, _H_XMAS, _H_BOXING,
    ],
    "uk": [
        _H_NEWYEAR, _H_VALENTINE,
        {"name": "Bonfire Night Movies", "month": 11, "day": 5, "lead": 7,
         "keywords": ["v for vendetta", "guy fawkes", "gunpowder"]},
        _H_HALLOWEEN, _H_XMAS, _H_BOXING,
    ],
    "australia": [
        _H_NEWYEAR,
        {"name": "Australia Day Movies", "month": 1, "day": 26, "lead": 10,
         "keywords": ["australia", "australian", "aussie", "outback", "crocodile", "mad max"]},
        {"name": "ANZAC Day Movies", "month": 4, "day": 25, "lead": 7,
         "keywords": ["gallipoli", "anzac", "war", "kokoda"]},
        _H_HALLOWEEN, _H_XMAS, _H_BOXING,
    ],
    "china": [
        {"name": "Spring Festival Movies", "dates": _D_LUNAR_NY, "lead": 14, "post": 7,
         "countries": ["China", "Hong Kong", "Taiwan"], "keywords": ["spring festival"]},
        {"name": "Qingming Movies", "dates": _D_QINGMING, "lead": 5, "post": 3,
         "countries": ["China", "Hong Kong", "Taiwan"], "keywords": ["qingming", "tomb sweeping"]},
        {"name": "Dragon Boat Movies", "dates": _D_DRAGONBOAT, "lead": 5, "post": 3,
         "countries": ["China", "Hong Kong", "Taiwan"], "keywords": ["dragon boat"]},
        {"name": "Mid-Autumn Movies", "dates": _D_MIDAUTUMN, "lead": 7, "post": 3,
         "countries": ["China", "Hong Kong", "Taiwan"], "keywords": ["mid-autumn", "mooncake"]},
        {"name": "National Day Movies", "month": 10, "day": 1, "lead": 10, "post": 7,
         "countries": ["China", "Hong Kong"], "keywords": ["national day"]},
    ],
    "japan": [
        {"name": "New Year (Shogatsu) Movies", "month": 1, "day": 1, "lead": 7,
         "countries": ["Japan"], "keywords": ["shogatsu"]},
        {"name": "Tanabata Movies", "month": 7, "day": 7, "lead": 7,
         "genre": "Romance", "keywords": ["tanabata", "star-crossed", "your name"]},
        {"name": "Obon Movies", "month": 8, "day": 13, "lead": 7, "post": 4,
         "genre": "Horror", "keywords": ["ghost", "spirit", "yokai", "ju-on", "ringu"]},
        _H_HALLOWEEN,
        {"name": "Christmas Movies", "month": 12, "day": 25, "lead": 21,
         "genre": "Romance", "keywords": ["christmas", "xmas", "tokyo godfathers"]},
    ],
    "korea": [
        {"name": "Seollal Movies", "dates": _D_LUNAR_NY, "lead": 10, "post": 5,
         "countries": ["Republic of Korea"], "keywords": ["seollal"]},
        {"name": "Chuseok Movies", "dates": _D_MIDAUTUMN, "lead": 10, "post": 5,
         "countries": ["Republic of Korea"], "keywords": ["chuseok"]},
        {"name": "Liberation Day Movies", "month": 8, "day": 15, "lead": 7,
         "countries": ["Republic of Korea"], "keywords": ["liberation"]},
        _H_HALLOWEEN, _H_XMAS,
    ],
}

_HOL_MACHINE = [None]

def _hol_req(method, path, t=None):
    """Plex request with token header. Raises on HTTP/network error (caller guards)."""
    url = PLEX_URL.rstrip("/") + path
    data = b"" if method in ("POST", "PUT") else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": "application/json", "X-Plex-Token": PLEX_TOKEN})
    return urllib.request.urlopen(req, timeout=t or HOL_HTTP_TO)

def _hol_getj(path):
    return json.load(_hol_req("GET", path))

def _hol_machine():
    if not _HOL_MACHINE[0]:
        _HOL_MACHINE[0] = _hol_getj("/")["MediaContainer"]["machineIdentifier"]
    return _HOL_MACHINE[0]

def _hol_defs():
    # explicit JSON override wins outright
    if HOL_DEFS_JSON.strip():
        try:
            d = json.loads(HOL_DEFS_JSON)
            if isinstance(d, list) and d:
                return d
            log.warning("[holidays] HOLIDAYS_DEFINITIONS not a non-empty list; using country sets")
        except Exception as e:
            log.warning("[holidays] bad HOLIDAYS_DEFINITIONS JSON (%s); using country sets", str(e)[:80])
    # merge the selected countries' curated sets, deduping by collection name (unioning keywords/titles)
    merged, order = {}, []
    for c in (HOL_COUNTRIES or ["us"]):
        if c not in _HOLIDAY_SETS:
            log.warning("[holidays] unknown country '%s' (known: %s)", c, ",".join(sorted(_HOLIDAY_SETS)))
            continue
        for h in _HOLIDAY_SETS[c]:
            n = h.get("name")
            if not n:
                continue
            if n in merged:
                ex = merged[n]
                ex["keywords"] = sorted(set(ex.get("keywords", [])) | set(h.get("keywords", [])))
                if h.get("titles"):
                    ex["titles"] = sorted(set(ex.get("titles", [])) | set(h.get("titles", [])))
                if h.get("countries"):
                    ex["countries"] = sorted(set(ex.get("countries", [])) | set(h.get("countries", [])))
            else:
                merged[n] = dict(h); merged[n]["_country"] = c; order.append(n)
    if not merged:
        return list(_HOLIDAY_SETS["us"])
    return [merged[n] for n in order]

def _hol_section():
    if HOL_SECTION.strip():
        return HOL_SECTION.strip()
    d = _hol_getj("/library/sections")
    for s in d.get("MediaContainer", {}).get("Directory", []):
        if s.get("type") == "movie":
            return s.get("key")
    return None

def _nth_weekday(year, month, weekday, n):
    """nth occurrence of a weekday (Mon=0..Sun=6) in a month, e.g. 4th Thursday of November."""
    days = [d for d in calendar.Calendar().itermonthdates(year, month)
            if d.month == month and d.weekday() == weekday]
    return days[n - 1]

def _hol_date(h, year):
    # explicit per-year table (lunar / solar-term holidays); year absent -> no date this year
    table = h.get("dates")
    if table:
        v = table.get(str(year)) or table.get(year)
        if not v:
            return None
        y, m, d = (int(x) for x in str(v).split("-"))
        return datetime.date(y, m, d)
    rule = h.get("rule") or h.get("date")
    if rule == "thanksgiving":
        return _nth_weekday(year, 11, 3, 4)             # 4th Thursday of November
    if rule == "nth_weekday":
        return _nth_weekday(year, int(h["month"]), int(h["weekday"]), int(h["n"]))
    return datetime.date(year, int(h["month"]), int(h["day"]))

def _hol_in_window(defs, today):
    """Holidays whose lead/post window contains today, nearest-date first: [(h, dist), ...].
    With several countries merged, windows overlap (late December stacks Christmas + Boxing Day +
    New Year); the caller walks these nearest-first and pins the closest one that has films."""
    out = []
    for h in defs:
        lead = int(h.get("lead", HOL_LEAD_DAYS))
        post = int(h.get("post", HOL_POST_DAYS))
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                hd = _hol_date(h, y)
            except Exception:
                continue
            if hd is None:
                continue
            if hd - datetime.timedelta(days=lead) <= today <= hd + datetime.timedelta(days=post):
                out.append((h, abs((hd - today).days)))
                break
    out.sort(key=lambda x: x[1])
    return out

def _hol_genre_id(section, name):
    d = _hol_getj("/library/sections/%s/genre" % section)
    for g in d.get("MediaContainer", {}).get("Directory", []):
        if (g.get("title") or "").lower() == name.lower():
            return str(g.get("key")).split("=")[-1]
    return None

# Plex stores production country as a first-class tag (like genre); map friendly
# names to Plex's exact titles so "korea"/"taiwan"/"uk" resolve.
_HOL_COUNTRY_ALIASES = {
    "us": "united states of america", "usa": "united states of america",
    "united states": "united states of america", "america": "united states of america",
    "uk": "united kingdom", "britain": "united kingdom", "great britain": "united kingdom",
    "south korea": "republic of korea", "korea": "republic of korea",
    "taiwan": "taiwan, province of china",
}
_HOL_COUNTRY_CACHE = {}

def _hol_country_map(section):
    if section not in _HOL_COUNTRY_CACHE:
        d = _hol_getj("/library/sections/%s/country" % section)
        m = {}
        for c in d.get("MediaContainer", {}).get("Directory", []):
            title = (c.get("title") or "").lower()
            if title:
                m[title] = str(c.get("key")).split("=")[-1]
        _HOL_COUNTRY_CACHE[section] = m
    return _HOL_COUNTRY_CACHE[section]

def _hol_country_ids(section, names):
    m = _hol_country_map(section)
    ids = []
    for raw in names:
        n = _HOL_COUNTRY_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        cid = m.get(n)
        if cid is None:
            for title, tid in m.items():
                if title.split(",")[0] == n:        # "taiwan, province of china" -> "taiwan"
                    cid = tid
                    break
        if cid and cid not in ids:
            ids.append(cid)
    return ids

def _hol_all_titles(section):
    d = _hol_getj("/library/sections/%s/all?X-Plex-Container-Start=0&X-Plex-Container-Size=8000" % section)
    return d.get("MediaContainer", {}).get("Metadata", [])

def _hol_match_keys(section, h, all_meta):
    keys = set()
    if h.get("genre"):
        gid = _hol_genre_id(section, h["genre"])
        if gid is not None:
            d = _hol_getj("/library/sections/%s/all?genre=%s&X-Plex-Container-Size=5000"
                          % (section, urllib.parse.quote(str(gid))))
            for m in d.get("MediaContainer", {}).get("Metadata", []):
                keys.add(m["ratingKey"])
    for cid in _hol_country_ids(section, h.get("countries", [])):
        d = _hol_getj("/library/sections/%s/all?country=%s&X-Plex-Container-Size=8000"
                      % (section, urllib.parse.quote(str(cid))))
        for m in d.get("MediaContainer", {}).get("Metadata", []):
            keys.add(m["ratingKey"])
    kws = [k.lower() for k in (list(h.get("keywords", [])) + list(h.get("extra", [])))]
    titles = set(t.lower() for t in h.get("titles", []))
    if kws or titles:
        for m in all_meta:
            t = (m.get("title") or "").lower()
            if (kws and any(k in t for k in kws)) or (titles and t in titles):
                keys.add(m["ratingKey"])
    return keys

def _hol_find_coll(section, title):
    d = _hol_getj("/library/sections/%s/collections" % section)
    for c in d.get("MediaContainer", {}).get("Metadata", []):
        if c.get("title") == title:
            return c.get("ratingKey")
    return None

def _hol_create(section, title, keys):
    kl = ",".join(sorted(keys, key=lambda x: int(x)))
    uri = "server://%s/com.plexapp.plugins.library/library/metadata/%s" % (_hol_machine(), kl)
    params = urllib.parse.urlencode({"type": 1, "title": title, "smart": 0,
                                     "sectionId": section, "uri": uri})
    meta = json.load(_hol_req("POST", "/library/collections?" + params))["MediaContainer"]["Metadata"][0]
    return meta["ratingKey"]

def _hol_pin(section, rk):
    pp = urllib.parse.urlencode({"metadataItemId": rk, "promotedToRecommended": 1,
                                 "promotedToOwnHome": 1, "promotedToSharedHome": 1})
    _hol_req("POST", "/hubs/sections/%s/manage?%s" % (section, pp))

def _hol_load_state():
    try: return json.load(open(HOL_STATE))
    except Exception: return {}

def _hol_save_state(s):
    try:
        os.makedirs(os.path.dirname(HOL_STATE) or ".", exist_ok=True)
        json.dump(s, open(HOL_STATE, "w"))
    except Exception as e:
        log.debug("[holidays] state save failed: %s", e)

def check_holidays():
    if not (PLEX_URL and PLEX_TOKEN):
        log.debug("[holidays] PLEX_URL/PLEX_TOKEN not set"); return
    defs = _hol_defs()
    names = [h.get("name") for h in defs if h.get("name")]
    today = datetime.date.today()
    inwin = _hol_in_window(defs, today)                      # nearest-date first
    # home-country preference: the first country in HOLIDAYS_COUNTRIES wins an overlapping window
    # even if a foreign holiday is calendar-nearer (e.g. keep Independence Day over Canada Day).
    home = (HOL_COUNTRIES or ["us"])[0]
    inwin.sort(key=lambda hd: (0 if hd[0].get("_country") == home else 1, hd[1]))
    sig = ",".join(sorted(h.get("name", "") for h, _ in inwin))

    # daily cadence: the set of in-window holidays only changes day-to-day, so between runs
    # (event/sweep can fire often) skip the Plex round-trips unless a window just opened/closed.
    state = _hol_load_state()
    now = int(time.time())
    if (HOL_MIN_INTERVAL and state.get("ts") and state.get("sig") == sig
            and now - int(state.get("ts", 0)) < HOL_MIN_INTERVAL):
        log.debug("[holidays] no window change (%s); skipping until next daily run", sig or "none")
        return

    try:
        section = _hol_section()
    except Exception as e:
        log.error("[holidays] cannot reach Plex: %s", str(e)[:120]); return
    if not section:
        log.warning("[holidays] no movie library section found (set HOLIDAYS_MOVIE_SECTION)"); return

    # Pick the row to show: the nearest in-window holiday that is either already built or has
    # matching films. This stops an empty holiday (e.g. Canada Day with 0 themed films) from
    # shadowing a nearby one that does have films (e.g. Independence Day).
    chosen = chosen_keys = None
    all_meta = None
    for h, _dist in inwin:
        try:
            if _hol_find_coll(section, h["name"]):
                chosen = h; chosen_keys = None; break        # already built -> keep it
        except Exception as e:
            log.debug("[holidays] lookup %s failed: %s", h.get("name"), str(e)[:80]); continue
        if all_meta is None:
            all_meta = _hol_all_titles(section)
        keys = _hol_match_keys(section, h, all_meta)
        if keys:
            chosen = h; chosen_keys = keys; break
        log.debug("[holidays] '%s' in window but 0 matching films; trying next", h.get("name"))
    chosen_name = chosen["name"] if chosen else None

    # take down managed collections that are not the chosen row (out of season, or empty/overlapped)
    removed = []
    for name in names:
        if name == chosen_name:
            continue
        try:
            rk = _hol_find_coll(section, name)
        except Exception as e:
            log.debug("[holidays] lookup %s failed: %s", name, str(e)[:80]); continue
        if not rk:
            continue
        if DRY_RUN:
            log.info("[holidays] WOULD remove collection: %s", name); removed.append(name); continue
        try:
            _hol_req("DELETE", "/library/collections/%s" % rk); removed.append(name)
            log.info("[holidays] removed collection: %s", name)
        except Exception as e:
            log.warning("[holidays] remove %s failed: %s", name, str(e)[:80])

    built = None
    if chosen:
        existing = _hol_find_coll(section, chosen_name)
        if existing:
            log.info("[holidays] active collection present: %s", chosen_name); built = chosen_name
            if HOL_PIN_HOME and not DRY_RUN:
                try: _hol_pin(section, existing)
                except Exception: pass
        elif DRY_RUN:
            log.info("[holidays] WOULD create+pin '%s' (%d films)", chosen_name, len(chosen_keys or [])); built = chosen_name
        else:
            try:
                rk = _hol_create(section, chosen_name, chosen_keys)
                if HOL_PIN_HOME:
                    _hol_pin(section, rk)
                log.warning("[holidays] created%s collection '%s' (%d films)",
                            " + pinned to Home" if HOL_PIN_HOME else "", chosen_name, len(chosen_keys))
                built = chosen_name
            except Exception as e:
                log.error("[holidays] create '%s' failed: %s", chosen_name, str(e)[:120])
    elif inwin:
        log.info("[holidays] in window (%s) but none have matching films", sig)
    else:
        log.info("[holidays] no holiday active today (%s)", today.isoformat())

    state.update({"active": chosen_name, "built": built, "removed": removed,
                  "sig": sig, "ts": int(time.time()), "date": today.isoformat()})
    _hol_save_state(state)


# =========================================================================== #
# CHECK: backlog  (search monitored-but-missing items that RSS never grabbed)
# =========================================================================== #

def _backlog_load_state():
    try: return json.load(open(BACKLOG_STATE))
    except Exception: return {}

def _backlog_save_state(s):
    try:
        os.makedirs(os.path.dirname(BACKLOG_STATE) or ".", exist_ok=True)
        json.dump(s, open(BACKLOG_STATE, "w"))
    except Exception as e:
        log.debug("[backlog] state save failed: %s", e)

def _backlog_age_days(rec, kind, now):
    """Days since the item became available; None if it has no past air/release date (so skip it)."""
    if kind == "sonarr":
        cands = [rec.get("airDateUtc")]
    else:
        cands = [rec.get("digitalRelease"), rec.get("physicalRelease"), rec.get("inCinemas")]
    best = None
    for c in cands:
        if not c: continue
        try:
            dt = datetime.datetime.fromisoformat(str(c).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt.tzinfo is None: dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = (now - dt).total_seconds() / 86400.0
        if age < 0: continue                                  # not yet aired/released -> leave it
        best = age if best is None else min(best, age)        # most-recent past date = smallest age
    return best

def check_backlog():
    if not INSTANCES:
        log.debug("[backlog] no arr instances"); return
    _act = _scout_active()
    if _act:
        log.info("[backlog] yielding to %d active Scout request(s) - skipping this sweep so the explicit pick lands first", _act); return
    if BACKLOG_LOAD_MAX and host_load() > BACKLOG_LOAD_MAX:
        log.info("[backlog] host load over %.1f - skipping this sweep to keep Plex responsive", BACKLOG_LOAD_MAX); return
    targets = [a for a in INSTANCES if a.kind in ("sonarr", "radarr") and a.name in BACKLOG_INSTANCES]
    if not targets:
        log.debug("[backlog] no enabled instances match BACKLOG_INSTANCES=%s", ",".join(BACKLOG_INSTANCES)); return
    state = _backlog_load_state()
    nowsec = time.time()
    if BACKLOG_INTERVAL and not DRY_RUN:
        last = float(state.get("_last_run", 0) or 0)
        if nowsec - last < BACKLOG_INTERVAL:
            log.debug("[backlog] last sweep %ds ago (< %ds) - throttled", int(nowsec - last), BACKLOG_INTERVAL); return
        state["_last_run"] = nowsec
        _backlog_save_state(state)                                  # claim the slot before any work so concurrent event-sweeps don't double-fire
    now = datetime.datetime.now(datetime.timezone.utc)
    cooldown_cut = time.time() - BACKLOG_RETRY_DAYS * 86400
    budget = max(0, BACKLOG_PER_SWEEP)
    searched = 0
    for arr in targets:
        if budget <= 0: break
        path = "/wanted/missing?monitored=true&pageSize=%d" % BACKLOG_MAX_FETCH
        if arr.kind == "sonarr":
            path += "&includeSeries=true"
        data = arr.get_json(path)
        recs = (data or {}).get("records", []) if isinstance(data, dict) else []
        if not recs:
            log.debug("[backlog:%s] no missing records", arr.name); continue
        seen = state.setdefault(arr.name, {})
        picked = []
        for r in recs:
            if len(picked) >= budget: break
            iid = r.get("id")                                 # episodeId (sonarr) / movieId (radarr)
            if not iid: continue
            if seen.get(str(iid), 0) > cooldown_cut: continue # on cooldown
            age = _backlog_age_days(r, arr.kind, now)
            if age is None or age < BACKLOG_MIN_AGE_DAYS: continue   # too new (RSS will get it) or undated
            picked.append(r)
        if not picked:
            continue
        ids = [r["id"] for r in picked]
        if arr.kind == "sonarr":
            label = ", ".join("%s S%02dE%02d" % ((r.get("series", {}) or {}).get("title", "?"),
                              r.get("seasonNumber") or 0, r.get("episodeNumber") or 0) for r in picked[:6])
            body = {"name": "EpisodeSearch", "episodeIds": ids}
        else:
            label = ", ".join("%s (%s)" % (r.get("title", "?"), r.get("year", "")) for r in picked[:6])
            body = {"name": "MoviesSearch", "movieIds": ids}
        if DRY_RUN:
            log.info("[backlog:%s] WOULD search %d missing: %s", arr.name, len(ids), label)
            budget -= len(ids); searched += len(ids)
            continue
        res = arr.command(body)
        if res is None:
            log.warning("[backlog:%s] search command failed for %d items", arr.name, len(ids)); continue
        nowts = time.time()
        for i in ids: seen[str(i)] = nowts
        budget -= len(ids); searched += len(ids)
        log.info("[backlog:%s] searching %d missing: %s", arr.name, len(ids), label)
    if not DRY_RUN:
        _backlog_save_state(state)
    if searched:
        log.info("[backlog] triggered %d search(es) this sweep (cap %d, aged>=%dd)", searched, BACKLOG_PER_SWEEP, BACKLOG_MIN_AGE_DAYS)
    else:
        log.debug("[backlog] nothing eligible this sweep")


def _riven_load_state():
    try: return json.load(open(RIVEN_STATE))
    except Exception: return {}

def _riven_save_state(s):
    try:
        os.makedirs(os.path.dirname(RIVEN_STATE) or ".", exist_ok=True)
        json.dump(s, open(RIVEN_STATE, "w"))
    except Exception as e:
        log.debug("[riven] state save failed: %s", e)

def check_riven():
    """Per Riven instance: report health + any down services every sweep (cheap, read-only), then
    gently retry items wedged in a working state (stuck) or never resolved (missing). Retries are
    throttled by RIVEN_INTERVAL + host load + a per-item cooldown so event-mode sweeps cannot
    self-amplify - exactly like check_backlog."""
    if not RIVENS:
        log.debug("[riven] no riven instances"); return
    state = _riven_load_state()
    # --- health + services: every sweep ---
    for rv in RIVENS:
        ok, detail = rv.health()
        if not ok:
            log.warning("[riven:%s] unhealthy: %s", rv.name, detail); continue
        down = rv.services_down()
        if down:
            log.warning("[riven:%s] services down: %s", rv.name, ", ".join(down))
        else:
            log.debug("[riven:%s] healthy (%s)", rv.name, detail)
    # --- retries: throttled ---
    if not (RIVEN_STUCK_STATES or RIVEN_MISSING_STATES):
        return
    _act = _scout_active()
    if _act:
        log.info("[riven] yielding to %d active Scout request(s) - skipping retries this sweep", _act); return
    nowsec = time.time()
    if RIVEN_INTERVAL and not DRY_RUN:
        last = float(state.get("_last_run", 0) or 0)
        if nowsec - last < RIVEN_INTERVAL:
            log.debug("[riven] last retry sweep %ds ago (< %ds) - throttled", int(nowsec - last), RIVEN_INTERVAL); return
        state["_last_run"] = nowsec
        _riven_save_state(state)                                     # claim the slot before any work so concurrent event-sweeps don't double-fire
    if RIVEN_LOAD_MAX and host_load() > RIVEN_LOAD_MAX:
        log.info("[riven] host load over %.1f - skipping retries this sweep to keep Plex responsive", RIVEN_LOAD_MAX); return
    cooldown_cut = nowsec - RIVEN_RETRY_DAYS * 86400
    retried_total = 0
    for rv in RIVENS:
        ok, _ = rv.health()
        if not ok:
            continue                                                # already warned above; don't hammer a dead backend
        seen = state.setdefault(rv.name, {})
        budget = max(0, RIVEN_PER_SWEEP)
        for group in (RIVEN_STUCK_STATES, RIVEN_MISSING_STATES):
            if budget <= 0 or not group: break
            items = rv.items(group, RIVEN_MAX_FETCH)
            picked = []
            for it in items:
                if len(picked) >= budget: break
                iid = it.get("id")
                if iid is None: continue
                if seen.get(str(iid), 0) > cooldown_cut: continue   # on cooldown
                picked.append(it)
            if not picked:
                continue
            ids = [it["id"] for it in picked]
            label = ", ".join("%s [%s]" % (it.get("title") or it.get("log_string") or "?", it.get("state", "?")) for it in picked[:6])
            if DRY_RUN:
                log.info("[riven:%s] WOULD retry %d item(s): %s", rv.name, len(ids), label)
                budget -= len(ids); retried_total += len(ids)
                continue
            if not rv.retry(ids):
                continue
            for i in ids: seen[str(i)] = nowsec
            budget -= len(ids); retried_total += len(ids)
            log.info("[riven:%s] retrying %d item(s): %s", rv.name, len(ids), label)
    if not DRY_RUN:
        _riven_save_state(state)
    if retried_total:
        log.info("[riven] retried %d item(s) this sweep (cap %d/instance, cooldown %dd)", retried_total, RIVEN_PER_SWEEP, RIVEN_RETRY_DAYS)
    else:
        log.debug("[riven] nothing eligible to retry this sweep")


def check_mediastorm():
    """mediastorm has no import queue or monitored-missing list, so there is nothing to drain. We
    only watch that the server is up and answering /health."""
    if not MEDIASTORMS:
        log.debug("[mediastorm] no mediastorm instances"); return
    for ms in MEDIASTORMS:
        ok, detail = ms.health()
        if ok:
            log.debug("[mediastorm:%s] up (%s)", ms.name, detail)
        else:
            log.warning("[mediastorm:%s] down: %s", ms.name, detail)


_PROVIDER_KEYWORDS = ("indexer", "download client", "applications unavailable", "applications are unavailable")

def check_providers():
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr", "prowlarr"):
            continue
        issues = [h for h in arr.health()
                  if h.get("type") in ("warning", "error")
                  and any(k in (h.get("message") or "").lower() for k in _PROVIDER_KEYWORDS)]
        if not issues:
            continue
        log.warning("[providers:%s] %d provider issue(s): %s", arr.name, len(issues),
                    " | ".join((h.get("message") or "")[:60] for h in issues[:2]))
        if DRY_RUN:
            continue
        # re-test everything; a passing test clears the failure status and re-enables recovered ones
        for ep, label in (("/indexer/testall", "indexers"), ("/downloadclient/testall", "download-clients")):
            res = arr.post(ep)
            if isinstance(res, list) and res:
                ok = sum(1 for r in res if r.get("isValid"))
                still = [r.get("id") for r in res if not r.get("isValid")]
                log.info("[providers:%s] tested %s: %d ok, %d still failing %s",
                         arr.name, label, ok, len(still), still or "")

# =========================================================================== #
# CHECK: bazarr (reachability)
# =========================================================================== #

def check_bazarr():
    if not BAZARR_URL:
        return
    c = http_code(BAZARR_URL.rstrip("/") + "/api/system/status",
                  headers={"X-API-KEY": BAZARR_APIKEY} if BAZARR_APIKEY else None, t=10)
    (log.info if c == 200 else log.error)("[bazarr] %s -> %s", BAZARR_URL, c if c else "DOWN")

# =========================================================================== #
# CHECK: seerr (Overseerr / Jellyseerr / Seerr) - auto-retry FAILED requests
#
# seerr hands an approved request to Radarr/Sonarr with a fixed ~10s API timeout
# and NO retry of its own. If the arr is briefly slow (heavy search load, host
# contention) the add times out, the request is marked FAILED, and the title
# silently never lands in the arr. We re-drive those FAILED requests each sweep
# so a transient blip self-heals; an attempt cap stops us looping on a request
# that fails for a real reason (dead tmdb id, removed title).
# =========================================================================== #

class Seerr:
    def __init__(self, url, apikey):
        self.base = url.rstrip("/") + "/api/v1"
        self.apikey = apikey

    def _req(self, method, path, data=None, t=None):
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=t or TIMEOUT)

    def failed(self):
        """Requests currently in the FAILED state (seerr could not hand them to the arr)."""
        try:
            d = json.load(self._req("GET", "/request?take=100&skip=0&filter=failed&sort=added", t=15))
            return d.get("results", [])
        except Exception as e:
            log.warning("[seerr] failed-list fetch error: %s", str(e)[:80]); return None

    def retry(self, rid):
        self._req("POST", "/request/%d/retry" % int(rid), data=b"", t=30)

def check_seerr():
    if not SEERR_URL or not SEERR_APIKEY:
        return
    s = Seerr(SEERR_URL, SEERR_APIKEY)
    reqs = s.failed()
    if reqs is None:                                          # fetch errored -> seerr down/unreachable
        log.error("[seerr] %s unreachable", SEERR_URL); return
    if not reqs:
        log.info("[seerr] no failed requests"); return
    state = _load_state()
    tries = state.setdefault("__seerr__", {})
    log.warning("[seerr] %d failed request(s)", len(reqs))
    acted = 0
    for r in reqs:
        if acted >= SEERR_MAX:
            break
        rid = r.get("id")
        if rid is None:
            continue
        md = r.get("media") or {}
        label = "%s tmdb=%s req#%s" % (md.get("mediaType", "?"), md.get("tmdbId", "?"), rid)
        n = int(tries.get(str(rid), 0))
        if SEERR_MAX_TRIES and n >= SEERR_MAX_TRIES:          # keeps failing -> stop, leave it for a human
            log.error("[seerr] giving up on %s after %d retries (persistent failure)", label, n)
            continue
        if DRY_RUN:
            log.info("[seerr] DRY-RUN would retry %s", label); acted += 1; continue
        try:
            s.retry(rid)
            tries[str(rid)] = n + 1
            acted += 1
            log.info("[seerr] retried %s (attempt %d)", label, n + 1)
        except Exception as e:
            log.warning("[seerr] retry %s failed: %s", label, str(e)[:80])
    # a recovered request drops off the failed list; forget its counter so a future fresh fail starts clean
    live = set(str(r.get("id")) for r in reqs)
    for k in [k for k in tries if k not in live]:
        tries.pop(k, None)
    _save_state(state)
    if acted:
        log.info("[seerr] re-drove %d failed request(s)", acted)

# =========================================================================== #
# WARMER: precache the head of likely-next media so playback starts instantly
#
# On a usenet/debrid FUSE mount the slow part of pressing Play is decypharr
# fetching the first segments from the provider. We ask Plex what a viewer is
# about to watch (the next episode of whatever is playing, plus everything in
# their On Deck / Continue Watching row) and read the first WARMER_PRECACHE_MB
# of each through the mount, which pulls those bytes into decypharr's on-disk
# cache. By the time Play is pressed, the head is already warm.
#
# Plex exposes no "user opened the detail page" event, so we approximate intent
# with the high-hit-rate signals it DOES expose (active sessions + On Deck).
# We do not force-delete warmed bytes: decypharr's cache is itself the speed
# win and it already evicts by age/LRU; instead we keep speculative cost low
# (small head, a per-cycle cap, a re-warm cooldown, and a host-load guard).
# =========================================================================== #

class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/"); self.token = token

    def _get(self, path):
        sep = "&" if "?" in path else "?"
        with urllib.request.urlopen(self.url + path + sep + "X-Plex-Token=" + self.token, timeout=15) as r:
            return ET.fromstring(r.read())

    def sessions(self):
        try: return list(self._get("/status/sessions").iter("Video"))
        except Exception: return []

    def ondeck(self):
        try: return list(self._get("/library/onDeck").iter("Video"))
        except Exception: return []

    def leaves(self, show_rk):
        try: return list(self._get("/library/metadata/%s/allLeaves" % show_rk).iter("Video"))
        except Exception: return []

    def parts(self, rk):
        """File paths for this item, highest-resolution version first (so we can warm just the top one)."""
        out = []
        try:
            for m in self._get("/library/metadata/%s" % rk).iter("Media"):
                try: res = int(m.get("height") or 0) * 1000000 + int(m.get("bitrate") or 0)
                except Exception: res = 0
                for p in m.iter("Part"):
                    if p.get("file"):
                        out.append((res, p.get("file")))
            out.sort(key=lambda x: x[0], reverse=True)
        except Exception:
            return []
        return [f for _, f in out]

    def recent(self, n):
        out = []
        try:
            for d in self._get("/library/sections").iter("Directory"):
                if d.get("type") in ("movie", "show"):
                    ra = self._get("/library/sections/%s/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=%d" % (d.get("key"), n))
                    out += list(ra.iter("Video"))[:n]
        except Exception: pass
        return out

_warm_state = {}            # host_path -> last_warm_ts
_warm_lock = threading.Lock()
_warm_sem = threading.Semaphore(max(1, WARM_CONCURRENCY))        # background warming lane
_warm_sem_open = threading.Semaphore(max(1, WARM_OPEN_CONC))     # detail-page (you opened it) lane - separate so opens never wait
_warm_last_ondeck = [0.0]
_warm_count = [0]           # total warms since start (for the UI)
_warm_recent = []           # recent warms for the UI: [{"ts","title","why"}]

def _warm_record(title, why):
    _warm_count[0] += 1
    _warm_recent.append({"ts": time.time(), "title": title, "why": why})
    if len(_warm_recent) > 80:
        del _warm_recent[:len(_warm_recent) - 80]

def _limit_parts(files):
    return files if WARM_PARTS <= 0 else files[:WARM_PARTS]

def _host_path(f):
    if WARM_PATH_MAP and ":" in WARM_PATH_MAP:
        a, b = WARM_PATH_MAP.split(":", 1)
        if f.startswith(a):
            return b + f[len(a):]
    return f

def _warm_file(path, reason="cycle"):
    p = _host_path(path)
    # a title you actively opened tolerates more load (2x) than speculative background warming, but
    # both still yield before meltdown; concurrency stays capped either way so a burst can't flood.
    guard = (WARM_LOAD_MAX * 2) if reason == "detail-page" else WARM_LOAD_MAX
    if guard > 0 and host_load() > guard:
        return False
    with _warm_lock:                                    # atomic claim: one warm per file per cooldown
        if time.time() - _warm_state.get(p, 0) < WARM_COOLDOWN:
            return False
        _warm_state[p] = time.time()
    try:
        sz = os.path.getsize(p)
    except Exception as e:
        _warm_state.pop(p, None)                         # release so it can be retried
        log.debug("[warmer] stat fail %s: %s", p, str(e)[:60]); return False
    head = min(WARM_HEAD_MB << 20, sz)
    tail = WARM_TAIL_MB > 0 and sz > head + (WARM_TAIL_MB << 20)
    res = {"got": 0, "err": None}
    def _do():
        try:
            with open(p, "rb", buffering=0) as fh:
                while res["got"] < head:
                    b = fh.read(min(4 << 20, head - res["got"]))
                    if not b: break
                    res["got"] += len(b)
                if tail:
                    fh.seek(sz - (WARM_TAIL_MB << 20))
                    while fh.read(4 << 20):
                        pass
        except Exception as e:
            res["err"] = str(e)[:60]
    t0 = time.time()
    sem = _warm_sem_open if reason == "detail-page" else _warm_sem   # opens get their own lane (instant)
    with sem:                                           # cap concurrent usenet pulls so warming never floods decypharr
        th = threading.Thread(target=_do, daemon=True); th.start(); th.join(WARM_READ_TIMEOUT)
    if th.is_alive():
        _warm_state.pop(p, None)
        log.warning("[warmer] read timed out (%ds, mount slow/hung?): %s", WARM_READ_TIMEOUT, os.path.basename(p))
        return False
    if res["err"]:
        _warm_state.pop(p, None)
        log.warning("[warmer] read fail %s: %s", os.path.basename(p), res["err"]); return False
    _warm_record(os.path.basename(p), reason)
    log.info("[warmer] warmed %dMB head%s in %.1fs: %s",
             res["got"] >> 20, "+%dMB tail" % WARM_TAIL_MB if tail else "",
             time.time() - t0, os.path.basename(p))
    return True

def _warm_targets(plex):
    """Ordered, de-duped list of (reason, plex_file_path) to warm this cycle."""
    targets, seen = [], set()
    def add(reason, path):
        if path and path not in seen:
            seen.add(path); targets.append((reason, path))
    sessions = plex.sessions()
    if "next" in WARM_SOURCES:                              # next episode(s) of anything playing
        for v in sessions:
            if v.get("type") != "episode" or not v.get("grandparentRatingKey"):
                continue
            if WARM_NEXT_NEAR_END > 0:                       # only warm the next ep once the current one nears the end
                try:
                    remain_min = (int(v.get("duration", 0)) - int(v.get("viewOffset", 0))) / 60000.0
                except Exception:
                    remain_min = 0
                if remain_min > WARM_NEXT_NEAR_END:
                    continue
            eps = plex.leaves(v.get("grandparentRatingKey"))
            idx = next((i for i, e in enumerate(eps) if e.get("ratingKey") == v.get("ratingKey")), -1)
            if idx >= 0:
                for e in eps[idx + 1: idx + 1 + WARM_NEXT_EPS]:
                    for f in _limit_parts(plex.parts(e.get("ratingKey"))):
                        add("next-ep", f)
    # Plex-first: speculative On Deck / recent warming pauses while ANYONE is watching (never competes
    # with a live stream), and is skipped entirely in low-cache mode (keep almost nothing pre-warmed).
    if not WARM_LOW_CACHE and not sessions and time.time() - _warm_last_ondeck[0] >= WARM_ONDECK_EVERY:
        _warm_last_ondeck[0] = time.time()
        if WARM_ONDECK and "ondeck" in WARM_SOURCES:        # Continue Watching / Up Next (WARMER_ONDECK is the on/off)
            for v in plex.ondeck():
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("ondeck", f)
        if "recent" in WARM_SOURCES and WARM_RECENT_COUNT > 0:
            for v in plex.recent(WARM_RECENT_COUNT):
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("recent", f)
    return targets

def warm_cycle():
    if WARM_LOAD_MAX > 0 and host_load() > WARM_LOAD_MAX:
        log.info("[warmer] host load > %.0f -> skip cycle", WARM_LOAD_MAX); return
    targets = _warm_targets(Plex(PLEX_URL, PLEX_TOKEN))
    done = 0
    for reason, path in targets:
        if done >= WARM_MAX_CYCLE:
            break
        if _warm_file(path, reason):
            done += 1
    if done:
        log.info("[warmer] cycle warmed %d (of %d candidate paths)", done, len(targets))

def warmer_loop(stop):
    mode = (" | LOW-CACHE: no On Deck, next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_LOW_CACHE \
        else ((" | next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_NEXT_NEAR_END else "")
    log.info("[warmer] started: head=%dMB tail=%dMB sources=%s poll=%ds ondeck-every=%ds%s",
             WARM_HEAD_MB, WARM_TAIL_MB, ",".join(WARM_SOURCES) or "-", WARM_INTERVAL, WARM_ONDECK_EVERY, mode)
    while not stop.is_set():
        try:
            warm_cycle()
        except Exception as e:
            log.error("[warmer] cycle error: %s", e)
        if stop.wait(WARM_INTERVAL):
            break

# opening a title's detail page fetches its extras (/extras, every client incl. Infuse) and, on the
# native Plex app, a rich includeExtras=1 metadata request. Match either -> works for Plex + Infuse.
_PLEXLOG_RE = re.compile(r"/library/metadata/(\d+)(?:/extras|\?[^\s]*includeExtras=1)")

_playing = {"ts": 0.0, "rks": set()}

def _playing_rks(plex):
    """ratingKeys with an active Plex session, cached ~10s (Plex sends the same metadata query while
    you browse a title AND while you play it, so this tells the two apart)."""
    if time.time() - _playing["ts"] > 10:
        try: _playing["rks"] = set(v.get("ratingKey") for v in plex.sessions())
        except Exception: pass
        _playing["ts"] = time.time()
    return _playing["rks"]

def _warm_opened(plex, rk):
    if rk in _playing_rks(plex):                                # already playing (so already cached) -> not a new open
        return
    for f in _limit_parts(plex.parts(rk)):                      # warm just the top version(s) you'd actually play
        if _warm_file(f, "detail-page"):
            log.info("[warmer] you opened rk=%s -> warmed: %s", rk, os.path.basename(_host_path(f)))

def plexlog_loop(stop):
    """Tail Plex's server log; warm the exact title a viewer opens (true pre-play intent)."""
    cmd = WARM_PLEXLOG_CMD or ("tail -n0 -F %r" % WARM_PLEXLOG_FILE if WARM_PLEXLOG_FILE else "")
    if not cmd:
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    seen = {}                                                   # ratingKey -> last-handled ts
    log.info("[warmer] detail-page warming enabled (tailing Plex log)")
    while not stop.is_set():
        proc = None
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            for line in proc.stdout:
                if stop.is_set():
                    break
                m = _PLEXLOG_RE.search(line)
                if not m:
                    continue
                rk = m.group(1); now = time.time()
                if now - seen.get(rk, 0) < 300:                 # a detail page is polled repeatedly while open -> react once per item / 5 min
                    continue
                seen[rk] = now                                  # warm off-thread so the tailer stays responsive
                threading.Thread(target=_warm_opened, args=(plex, rk), daemon=True).start()
        except Exception as e:
            log.warning("[warmer] plexlog tail error: %s", str(e)[:80])
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass
        if stop.wait(10):                                       # tail died/rotated -> reconnect
            break

# =========================================================================== #
# sweep / loop
# =========================================================================== #

# =========================================================================== #
# westrepair - symlink repair subprocess + background monitor thread
# =========================================================================== #

_wr_lock  = threading.Lock()
_wr_state = {
    "running": False, "pid": None,
    "current_item": None, "current_mode": None,
    "items_processed": 0, "items_broken": 0, "items_fixed": 0,
    "last_action": None, "last_run_start": None, "next_run_in": None,
    "recent_log": [],
    "exit_code": None,
}
_wr_proc = None

_RE_WR_PROCESSING = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(\w+)\] \[DEBUG\] Processing: (.+)')
_RE_WR_BROKEN     = re.compile(r'\[DEBUG\] .*(broken|missing|not found|unreachable)', re.IGNORECASE)
_RE_WR_FIXED      = re.compile(r'\[(INFO|SUCCESS)\] .*(search|trigger|fix|repair|restor)', re.IGNORECASE)
_RE_WR_SLEEPING   = re.compile(r'[Ss]leeping for ([^\n]+)')
_RE_WR_START      = re.compile(r'Running repair')


def _wr_parse_line(line):
    s = _wr_state
    s["recent_log"].append(line.rstrip())
    if len(s["recent_log"]) > 20:
        s["recent_log"].pop(0)
    m = _RE_WR_PROCESSING.search(line)
    if m:
        s["current_item"] = m.group(3).strip()
        s["current_mode"] = m.group(2)
        s["items_processed"] += 1
        return
    if _RE_WR_BROKEN.search(line):
        s["items_broken"] += 1; s["last_action"] = line.strip(); return
    if _RE_WR_FIXED.search(line):
        s["items_fixed"] += 1; s["last_action"] = line.strip(); return
    m2 = _RE_WR_SLEEPING.search(line)
    if m2:
        s["next_run_in"] = m2.group(1).strip(); s["current_item"] = None; return
    if _RE_WR_START.search(line):
        s["last_run_start"] = line.strip()
        s["items_processed"] = s["items_broken"] = s["items_fixed"] = 0


def westrepair_loop(stop):
    """Run repair.py as a long-lived subprocess; restart on unexpected exit."""
    global _wr_proc
    if not os.path.exists(WR_SCRIPT):
        log.error("[westrepair] script not found: %s", WR_SCRIPT)
        return
    log.info("[westrepair] starting %s | run_interval=%s repair_interval=%s",
             WR_SCRIPT, WR_RUN_INTERVAL, WR_REPAIR_INTERVAL)
    while not stop.is_set():
        cmd = ["python", "-u", WR_SCRIPT, "--no-confirm",
               "--run-interval", WR_RUN_INTERVAL,
               "--repair-interval", WR_REPAIR_INTERVAL]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1, cwd=os.path.dirname(WR_SCRIPT))
            _wr_proc = proc
            with _wr_lock:
                _wr_state.update({"running": True, "pid": proc.pid, "exit_code": None})
            for line in proc.stdout:
                log.info("[westrepair] %s", line.rstrip())
                with _wr_lock:
                    _wr_parse_line(line)
                if stop.is_set():
                    break
            proc.wait()
            with _wr_lock:
                _wr_state.update({"running": False, "exit_code": proc.returncode})
            if stop.is_set():
                break
            log.warning("[westrepair] exited (code %d), restarting in 30s", proc.returncode)
            stop.wait(30)
        except Exception as e:
            log.error("[westrepair] error: %s", e)
            stop.wait(30)
    if _wr_proc and _wr_proc.poll() is None:
        try: _wr_proc.terminate()
        except Exception: pass
    log.info("[westrepair] stopped")


def check_westrepair():
    """No-op periodic check — westrepair runs continuously in its own thread."""
    with _wr_lock:
        s = dict(_wr_state)
    if s["running"]:
        log.debug("[westrepair] running pid=%s processed=%d broken=%d fixed=%d",
                  s["pid"], s["items_processed"], s["items_broken"], s["items_fixed"])
    else:
        log.warning("[westrepair] repair.py not running (exit_code=%s)", s["exit_code"])


def _wr_plex_rescan():
    """Trigger a Plex library refresh for all sections. Returns (ok, message)."""
    plex_url   = os.environ.get("PLEX_URL", "").rstrip("/")
    plex_token = os.environ.get("PLEX_TOKEN", "")
    if not plex_url or not plex_token:
        return False, "PLEX_URL or PLEX_TOKEN not set"
    # Get library sections
    sections_url = "%s/library/sections?X-Plex-Token=%s" % (plex_url, plex_token)
    try:
        with urllib.request.urlopen(urllib.request.Request(sections_url), timeout=10) as r:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(r.read())
    except Exception as e:
        return False, "could not fetch sections: %s" % str(e)[:80]
    keys = [d.get("key") for d in root.findall(".//Directory") if d.get("key")]
    if not keys:
        return False, "no library sections found"
    triggered = []
    for key in keys:
        scan_url = "%s/library/sections/%s/refresh?X-Plex-Token=%s" % (plex_url, key, plex_token)
        try:
            urllib.request.urlopen(urllib.request.Request(scan_url), timeout=10)
            triggered.append(key)
        except Exception as e:
            log.warning("[westrepair] plex scan section %s failed: %s", key, e)
    log.info("[westrepair] triggered Plex rescan for %d section(s): %s", len(triggered), triggered)
    return True, "triggered %d section(s)" % len(triggered)


CHECKS = [("queue", EN_QUEUE, check_queue), ("providers", EN_PROVIDERS, check_providers),
          ("decypharr", EN_DECYPHARR, check_decypharr), ("plex", EN_PLEX, check_plex),
          ("resources", EN_RESOURCES, check_resources), ("janitor", EN_JANITOR, check_janitor),
          ("scrubber", EN_SCRUBBER, check_scrubber),
          ("watchlists", EN_WATCHLISTS, check_watchlists),
          ("holidays", EN_HOLIDAYS, check_holidays),
          ("backlog", EN_BACKLOG, check_backlog),
          ("riven", EN_RIVEN, check_riven),
          ("mediastorm", EN_MEDIASTORM, check_mediastorm),
          ("bazarr", EN_BAZARR, check_bazarr), ("seerr", EN_SEERR, check_seerr),
          ("westrepair", EN_WESTREPAIR, check_westrepair)]

_lock = threading.Lock()

def sweep(only=None):
    if not _lock.acquire(blocking=False):
        log.debug("sweep already running"); return
    try:
        for cid, en, fn in CHECKS:
            if not en:
                continue
            try:
                fn(only) if cid == "queue" else fn()
            except Exception as e:
                log.error("[%s] check error: %s", cid, e)
    finally:
        _lock.release()

# =========================================================================== #
# web dashboard (optional, no dependencies): status + per-service health +
# warmer stats + editable tuning config + live logs. Secrets stay masked.
# =========================================================================== #

_SECRET_HINT = ("APIKEY", "API_KEY", "TOKEN", "PASSWORD", "PASS", "SECRET")

UI_SCHEMA = [
    ("Mode", [("DOCTOR_MODE", "cron|event"), ("DOCTOR_INTERVAL", "900"),
              ("DOCTOR_DRY_RUN", "false"), ("DOCTOR_LOG_LEVEL", "INFO")]),
    ("Checks (on/off)", [("ENABLE_QUEUE", ""), ("ENABLE_PROVIDERS", ""), ("ENABLE_DECYPHARR", ""),
              ("ENABLE_PLEX", ""), ("ENABLE_RESOURCES", ""), ("ENABLE_JANITOR", ""), ("ENABLE_SCRUBBER", ""),
              ("ENABLE_WATCHLISTS", ""), ("ENABLE_HOLIDAYS", ""), ("ENABLE_BACKLOG", ""),
              ("ENABLE_RIVEN", ""), ("ENABLE_MEDIASTORM", ""),
              ("ENABLE_BAZARR", ""), ("ENABLE_SEERR", ""), ("ENABLE_WARMER", ""), ("ENABLE_WESTREPAIR", "")]),
    ("Watchlists (Plex Home + friends -> arrs)", [
              ("WATCHLISTS_FRIENDS", "alice:xxxx,bob:yyyy"),
              ("WATCHLISTS_INCLUDE_HOME", "true|false"),
              ("WATCHLISTS_HOME_PINS", "uuid1:1234,uuid2:5678"),
              ("WATCHLISTS_QUALITY", "*=both,home/kids=1080p,alice=4k"),
              ("WATCHLISTS_DEFAULT_QUALITY", "both"),
              ("WATCHLISTS_MAX_ADDS_PER_SWEEP", "25"),
              ("WATCHLISTS_PROFILES", "radarr=1,sonarr=4,radarr4k=5,sonarr4k=5")]),
    ("Holidays (pre-holiday themed Plex rows)", [
              ("HOLIDAYS_COUNTRIES", "us,canada,uk,china,japan,korea,australia"),
              ("HOLIDAYS_MOVIE_SECTION", "5"),
              ("HOLIDAYS_LEAD_DAYS", "7"), ("HOLIDAYS_POST_DAYS", "3"),
              ("HOLIDAYS_PIN_HOME", "true|false"), ("HOLIDAYS_MIN_INTERVAL_HOURS", "12"),
              ("HOLIDAYS_DEFINITIONS", '[{"name":"...","month":7,"day":4,"lead":12,"keywords":[...]}]')]),
    ("Scrubber (file integrity)", [
              ("SCRUBBER_PATHS", "/mnt/library/movies,/mnt/library/movies-4k,/mnt/library/tv,/mnt/library/tv-4k"),
              ("SCRUBBER_TIER", "2"), ("SCRUBBER_FULL_DECODE_ON_BAD", "false"),
              ("SCRUBBER_SKIM_POINTS", "4"), ("SCRUBBER_SKIM_SECS", "5"),
              ("SCRUBBER_MAX_FILES", "50"), ("SCRUBBER_CONCURRENCY", "1"),
              ("SCRUBBER_LOAD_MAX", "12"), ("SCRUBBER_STRIKES", "2"),
              ("SCRUBBER_REVERIFY_DAYS", "30"), ("SCRUBBER_DELETE_ARR_FILE", "true")]),
    ("Westrepair", [("WESTREPAIR_SCRIPT", "/app/westrepair/repair.py"),
              ("WESTREPAIR_RUN_INTERVAL", "6h"), ("WESTREPAIR_REPAIR_INTERVAL", "1m")]),
    ("Backlog (search monitored-missing)", [
              ("BACKLOG_INSTANCES", "sonarr,radarr,sonarr4k,radarr4k"),
              ("BACKLOG_PER_SWEEP", "5"), ("BACKLOG_MIN_AGE_DAYS", "7"),
              ("BACKLOG_RETRY_DAYS", "7"), ("BACKLOG_LOAD_MAX", "12"),
              ("BACKLOG_INTERVAL", "900"), ("BACKLOG_MAX_FETCH", "2000")]),
    ("Riven (health + retry stuck/missing)", [
              ("RIVEN_PER_SWEEP", "5"), ("RIVEN_INTERVAL", "900"),
              ("RIVEN_RETRY_DAYS", "3"), ("RIVEN_LOAD_MAX", "12"),
              ("RIVEN_MAX_FETCH", "500"),
              ("RIVEN_STUCK_STATES", "Scraped,Downloaded,PartiallyCompleted"),
              ("RIVEN_MISSING_STATES", "Requested,Indexed,Failed")]),
    ("Mediastorm (health watch)", [("MEDIASTORM_TIMEOUT", "8")]),
    ("Queue / churn brake", [("DOCTOR_MIN_STRIKES", "2"), ("DOCTOR_MAX_ACTIONS", "20"), ("DOCTOR_BLOCKLIST", "true"),
              ("DOCTOR_CONDITIONS", "downloadClientUnavailable,importBlocked,importFailed,importPending_warning,failedPending,stalled"),
              ("DOCTOR_CONDITION_ACTIONS", "stalled=research,importBlocked=force_import,downloadClientUnavailable=report"),
              ("DOCTOR_DEFAULT_ACTION", "report|research|remove|force_import"), ("DOCTOR_IMPORT_MODE", "auto|move|copy"),
              ("DOCTOR_CHURN_LIMIT", "0"), ("DOCTOR_CHURN_ACTION", "report|park|backoff"), ("DOCTOR_CHURN_BACKOFF", "10m,1h,24h")]),
    ("Warmer", [("WARMER_PRECACHE_MB", "64"), ("WARMER_TAIL_MB", "8"), ("WARMER_SOURCES", "ondeck,next"),
              ("WARMER_ONDECK", "true|false"), ("WARMER_MAX_PER_CYCLE", "40"), ("WARMER_NEXT_EPISODES", "1"),
              ("WARMER_COOLDOWN", "3600"), ("WARMER_LOAD_MAX", "0")]),
    ("Resources", [("RES_LOAD_WARN", "40"), ("RES_SWAP_WARN_MB", "7000"), ("RES_MEM_MIN_MB", "800")]),
    ("Seerr (failed-request retry)", [("SEERR_URL", "http://seerr:5055"), ("SEERR_RETRY_MAX", "10"), ("SEERR_MAX_ATTEMPTS", "5")]),
]
UI_KEYS = set(k for _, items in UI_SCHEMA for k, _ in items)

def _is_secret(k):
    ku = k.upper()
    return any(h in ku for h in _SECRET_HINT)

def _ui_health():
    """Quick reachability of every monitored service, probed in parallel (short timeouts)."""
    def arr_probe(a):
        def f():
            st = json.load(a._req("GET", "/system/status", t=5))
            warns = [h for h in a.health() if h.get("type") in ("warning", "error")]
            return True, ("v%s" % st.get("version", "?")) + (", %d health warn" % len(warns) if warns else "")
        return f
    jobs = [(a.name, a.kind, arr_probe(a)) for a in INSTANCES]
    if DECY_URL:
        jobs.append(("decypharr", "mount", lambda: (http_code(DECY_URL, t=5) == 200, DECY_URL)))
    if PLEX_URL:
        jobs.append(("plex", "plex", lambda: (
            http_code(PLEX_URL.rstrip("/") + "/identity" + ("?X-Plex-Token=" + PLEX_TOKEN if PLEX_TOKEN else ""), t=5) == 200, "")))
    if BAZARR_URL:
        jobs.append(("bazarr", "bazarr", lambda: (http_code(BAZARR_URL.rstrip("/") + "/api/system/status",
            headers={"X-API-KEY": BAZARR_APIKEY} if BAZARR_APIKEY else None, t=5) == 200, "")))
    if SEERR_URL:
        jobs.append(("seerr", "seerr", lambda: (http_code(SEERR_URL.rstrip("/") + "/api/v1/status",
            headers={"X-Api-Key": SEERR_APIKEY} if SEERR_APIKEY else None, t=5) == 200, "")))
    for rv in RIVENS:
        jobs.append((rv.name, "riven", (lambda r: lambda: (http_code(r.base + "/health",
            headers={"x-api-key": r.apikey}, t=5) == 200, ", ".join(r.services_down()[:3])))(rv)))
    for ms in MEDIASTORMS:
        jobs.append((ms.name, "mediastorm", (lambda m: lambda: (http_code(m.url + "/health", t=5) == 200, m.url))(ms)))
    out = [None] * len(jobs)
    def run(i, name, kind, fn):
        try:
            up, detail = fn()
        except Exception as e:
            up, detail = False, str(e)[:46]
        out[i] = {"name": name, "kind": kind, "up": up, "detail": detail}
    ths = [threading.Thread(target=run, args=(i, n, k, fn), daemon=True) for i, (n, k, fn) in enumerate(jobs)]
    for t in ths: t.start()
    for t in ths: t.join(7)
    return [r for r in out if r]

def _ui_status():
    checks = [{"name": n, "on": bool(e)} for n, e, _ in CHECKS]
    checks.append({"name": "warmer", "on": _b("ENABLE_WARMER", False) and bool(PLEX_URL)})
    checks.append({"name": "detail-page warm", "on": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE)})
    return {"version": VERSION, "mode": MODE, "dry_run": DRY_RUN, "load": round(host_load(), 2), "checks": checks}

def _ui_warmer():
    rec = [{"title": r["title"], "why": r["why"], "ago": int(time.time() - r["ts"])} for r in reversed(_warm_recent)]
    return {"enabled": _b("ENABLE_WARMER", False) and bool(PLEX_URL),
            "detail_page": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE),
            "total": _warm_count[0], "recent": rec[:40]}

def _ui_westrepair():
    with _wr_lock:
        s = dict(_wr_state)
        s["recent_log"] = list(_wr_state["recent_log"])
    s["enabled"] = EN_WESTREPAIR
    return s

# --------------------------------------------------------------------------- #
# scout: request-and-watch acquire frontend (search -> Get -> track -> play in Plex)
# --------------------------------------------------------------------------- #
_scout_lock = threading.Lock()
_scout_pcache = {}
_scout_rcache = {}
_plex_mid = [None]
_RIVEN_STAGE = {"Requested": "searching", "Indexed": "searching", "Unreleased": "searching",
                "Ongoing": "searching", "Scraped": "grabbed", "Downloaded": "downloading",
                "Symlinked": "verifying", "PartiallyCompleted": "verifying",
                "Completed": "available", "Failed": "no source", "Paused": "no source"}

def _scout_load():
    try: return json.load(open(SCOUT_STATE))
    except Exception: return {"reqs": {}}

def _scout_save(s):
    try:
        os.makedirs(os.path.dirname(SCOUT_STATE) or ".", exist_ok=True)
        json.dump(s, open(SCOUT_STATE, "w"))
    except Exception as e:
        log.debug("[scout] state save failed: %s", e)

def _scout_mode():
    if any(a.kind in ("sonarr", "radarr") for a in INSTANCES): return "arr"
    if RIVENS: return "riven"
    return "none"

def _scout_arr(kind):
    target = "radarr" if kind == "movie" else "sonarr"
    want = SCOUT_MOVIE_INSTANCE if kind == "movie" else SCOUT_SHOW_INSTANCE
    cands = [a for a in INSTANCES if a.kind == target]
    if not cands: return None
    if want:
        for a in cands:
            if a.name == want: return a
    return cands[0]

def _scout_meta():
    mode = _scout_mode()
    label = {"arr": "Sonarr / Radarr", "riven": "Riven", "none": "no acquisition backend"}[mode]
    caps = {"movie": bool(_scout_arr("movie")) if mode == "arr" else (mode == "riven"),
            "show":  bool(_scout_arr("show"))  if mode == "arr" else (mode == "riven")}
    return {"enabled": EN_SCOUT, "available": EN_SCOUT and mode != "none", "mode": mode,
            "backend": label, "caps": caps, "dry_run": DRY_RUN, "plex": bool(PLEX_URL)}

def _scout_profile(arr):
    if arr.name in _scout_pcache: return _scout_pcache[arr.name]
    pid, profs = None, (arr.get_json("/qualityprofile") or [])
    if SCOUT_QUALITY_PROFILE:
        for p in profs:
            if str(p.get("id")) == SCOUT_QUALITY_PROFILE or (p.get("name", "").lower() == SCOUT_QUALITY_PROFILE.lower()):
                pid = p.get("id"); break
    if pid is None and profs: pid = profs[0].get("id")
    _scout_pcache[arr.name] = pid
    return pid

def _scout_root(arr):
    if arr.name in _scout_rcache: return _scout_rcache[arr.name]
    root, rfs = "", (arr.get_json("/rootfolder") or [])
    if SCOUT_ROOT_FOLDER:
        root = SCOUT_ROOT_FOLDER
    elif rfs:
        root = rfs[0].get("path", "")
    _scout_rcache[arr.name] = root
    return root

def _scout_norm_arr(it, kind, arr):
    poster = ""
    for im in (it.get("images") or []):
        if im.get("coverType") == "poster":
            poster = im.get("remoteUrl") or im.get("url") or ""; break
    hasfile = bool(it.get("hasFile")) if kind == "movie" else ((it.get("statistics") or {}).get("episodeFileCount", 0) > 0)
    key = it.get("tmdbId") or it.get("tvdbId") or it.get("imdbId") or it.get("title")
    return {"uid": kind + ":" + str(key), "kind": kind, "title": it.get("title") or "?",
            "year": it.get("year") or "", "overview": (it.get("overview") or "")[:240], "poster": poster,
            "tmdbId": it.get("tmdbId"), "tvdbId": it.get("tvdbId"), "imdbId": it.get("imdbId") or "",
            "arr": arr.name, "inLibrary": bool(it.get("id")), "hasFile": hasfile, "arr_id": it.get("id") or 0}

def _scout_search(qstr, kind):
    qstr = (qstr or "").strip()
    mode = _scout_mode()
    if not qstr or not EN_SCOUT or mode == "none":
        return {"mode": mode, "results": []}
    res = []
    if mode == "arr":
        kinds = ["movie", "show"] if kind in ("both", "", None) else [kind]
        for k in kinds:
            arr = _scout_arr(k)
            if not arr: continue
            path = ("/movie/lookup?term=" if k == "movie" else "/series/lookup?term=") + urllib.parse.quote(qstr)
            for it in (arr.get_json(path, t=20) or []):
                res.append(_scout_norm_arr(it, k, arr))
                if len(res) >= SCOUT_MAX_RESULTS * 2: break
    elif mode == "riven":
        m = re.match(r"(tt\d{6,9})", qstr)
        if m:
            res.append({"uid": "movie:" + m.group(1), "kind": "movie", "title": qstr, "year": "",
                        "overview": "Add by IMDb id via Riven", "poster": "", "tmdbId": None, "tvdbId": None,
                        "imdbId": m.group(1), "arr": "", "inLibrary": False, "hasFile": False, "arr_id": 0})
    seen, out = set(), []
    for r in res:
        if r["uid"] in seen: continue
        seen.add(r["uid"]); out.append(r)
    return {"mode": mode, "results": out[:SCOUT_MAX_RESULTS]}

def _scout_add_movie(arr, req):
    prof, root = _scout_profile(arr), _scout_root(arr)
    if prof is None or not root: return None, "no quality profile / root folder on %s" % arr.name
    payload = {"title": req["title"], "tmdbId": req.get("tmdbId"), "year": req.get("year") or 0,
               "qualityProfileId": prof, "rootFolderPath": root, "monitored": True,
               "minimumAvailability": "released", "addOptions": {"searchForMovie": True}}
    try:
        return json.load(arr._req("POST", "/movie", data=json.dumps(payload).encode(), t=40)).get("id"), None
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read())
        except Exception: msg = e.reason
        return None, "radarr add %s: %s" % (e.code, str(msg)[:140])
    except Exception as ex:
        return None, str(ex)[:140]

def _scout_add_show(arr, req):
    prof, root = _scout_profile(arr), _scout_root(arr)
    if prof is None or not root: return None, "no quality profile / root folder on %s" % arr.name
    payload = {"title": req["title"], "tvdbId": req.get("tvdbId"), "qualityProfileId": prof,
               "rootFolderPath": root, "monitored": True, "seasonFolder": True,
               "addOptions": {"searchForMissingEpisodes": True, "monitor": "all"}}
    try:
        return json.load(arr._req("POST", "/series", data=json.dumps(payload).encode(), t=40)).get("id"), None
    except urllib.error.HTTPError as e:
        try: msg = json.loads(e.read())
        except Exception: msg = e.reason
        return None, "sonarr add %s: %s" % (e.code, str(msg)[:140])
    except Exception as ex:
        return None, str(ex)[:140]

def _scout_store(req):
    with _scout_lock:
        s = _scout_load(); reqs = s.setdefault("reqs", {}); reqs[req["id"]] = req
        if len(reqs) > SCOUT_RETAIN * 2:
            for k in sorted(reqs, key=lambda k: reqs[k].get("created", 0))[:len(reqs) - SCOUT_RETAIN * 2]:
                reqs.pop(k, None)
        _scout_save(s)

def _scout_get(body):
    try: p = json.loads(body or b"{}")
    except Exception: return False, {"error": "bad request"}
    if not EN_SCOUT: return False, {"error": "scout disabled"}
    kind = p.get("kind") or "movie"
    mode = _scout_mode()
    rid = "%s-%s-%d" % (kind, (p.get("tmdbId") or p.get("tvdbId") or p.get("imdbId") or "x"), int(time.time()))
    uid = p.get("uid") or (kind + ":" + str(p.get("tmdbId") or p.get("tvdbId") or p.get("imdbId") or p.get("title") or ""))
    req = {"id": rid, "uid": uid, "kind": kind, "title": p.get("title") or "?", "year": p.get("year") or "",
           "imdbId": p.get("imdbId") or "", "tmdbId": p.get("tmdbId"), "tvdbId": p.get("tvdbId"),
           "backend": mode, "created": time.time(), "stage": "queued", "play": "", "detail": ""}
    if DRY_RUN:
        req["stage"] = "dry-run"; req["detail"] = "DRY_RUN: nothing submitted"
        _scout_store(req); log.info("[scout] DRY_RUN would acquire %s (%s)", req["title"], kind)
        return True, {"id": rid, "stage": "dry-run"}
    if mode == "arr":
        arr = _scout_arr(kind)
        if not arr: return False, {"error": "no %s instance" % ("radarr" if kind == "movie" else "sonarr")}
        req["arr"] = arr.name
        arr_id = int(p.get("arr_id") or 0)
        if arr_id > 0:
            req["target_id"] = arr_id
            if not p.get("hasFile"):
                arr.command({"name": "MoviesSearch", "movieIds": [arr_id]} if kind == "movie"
                            else {"name": "SeriesSearch", "seriesId": arr_id})
        else:
            nid, err = (_scout_add_movie(arr, req) if kind == "movie" else _scout_add_show(arr, req))
            if err: log.warning("[scout] add failed: %s", err); return False, {"error": err}
            req["target_id"] = nid
        req["stage"] = "searching"; _scout_store(req)
        log.info("[scout] acquiring %s (%s) via %s id=%s", req["title"], kind, arr.name, req.get("target_id"))
        return True, {"id": rid, "stage": "searching"}
    if mode == "riven":
        if not RIVENS: return False, {"error": "no riven instance"}
        if not req["imdbId"]: return False, {"error": "riven needs an imdb id"}
        rv = RIVENS[0]
        try: rv._req("POST", "/items?imdb_ids=" + urllib.parse.quote(req["imdbId"]), t=30)
        except Exception as e: return False, {"error": str(e)[:120]}
        req["riven"] = rv.name; req["stage"] = "searching"; _scout_store(req)
        log.info("[scout] acquiring %s via Riven (%s)", req["title"], req["imdbId"])
        return True, {"id": rid, "stage": "searching"}
    return False, {"error": "no acquisition backend enabled"}

def _scout_clear(body):
    try: p = json.loads(body or b"{}")
    except Exception: p = {}
    with _scout_lock:
        s = _scout_load()
        if p.get("id"): s.get("reqs", {}).pop(p["id"], None)
        else: s["reqs"] = {}
        _scout_save(s)
    return True

def _scout_queue_rec(arr, field, tid):
    for r in (arr.queue() or []):
        if r.get(field) == tid: return r
    return None

def _scout_stage_from_rec(rec):
    status = (rec.get("status") or "").lower()
    tds = (rec.get("trackedDownloadState") or "").lower()
    size, left = rec.get("size") or 0, rec.get("sizeleft")
    pct = None
    if size and left is not None:
        try: pct = max(0, min(100, int(100 * (size - float(left)) / size)))
        except Exception: pct = None
    if tds in ("importpending", "importing") or status == "completed":
        return "importing", {"pct": 100}
    return "downloading", {"pct": pct}

def _scout_search_or_timeout(req):
    if time.time() - req.get("created", 0) < 900:
        return "searching", {}
    return "no source", {"detail": "no release found yet"}

def _scout_riven_item(rv, imdb):
    if not imdb: return None
    try:
        d = json.load(rv._req("GET", "/items?limit=200&page=1&sort=date_desc&type=movie&type=show", t=15))
        for it in (d.get("items") or []):
            if str(it.get("imdb_id") or "") == imdb: return it
    except Exception: pass
    return None

def _plex_json(path, t=8):
    if not PLEX_URL: return None
    hdr = {"Accept": "application/json"}
    if PLEX_TOKEN: hdr["X-Plex-Token"] = PLEX_TOKEN
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(PLEX_URL.rstrip("/") + path, headers=hdr), timeout=t))
    except Exception as e:
        log.debug("[scout] plex %s err %s", path, str(e)[:60]); return None

def _plex_machine_id():
    if _plex_mid[0] is not None: return _plex_mid[0]
    d = _plex_json("/")
    _plex_mid[0] = (d or {}).get("MediaContainer", {}).get("machineIdentifier", "") or ""
    return _plex_mid[0]

def _plex_collect(d):
    items, mc = [], (d or {}).get("MediaContainer", {})
    if mc.get("Metadata"): items += mc["Metadata"]
    for hub in (mc.get("Hub") or []):
        if hub.get("Metadata"): items += hub["Metadata"]
    return items

def _guid_match(it, imdb, tmdb, tvdb):
    ids = set()
    for g in (it.get("Guid") or []):
        gid = g.get("id") if isinstance(g, dict) else str(g)
        if gid: ids.add(gid)
    return bool((imdb and "imdb://%s" % imdb in ids) or (tmdb and "tmdb://%s" % tmdb in ids) or (tvdb and "tvdb://%s" % tvdb in ids))

def _plex_resolve(title, year, imdb, tmdb, tvdb, kind):
    mid = _plex_machine_id()
    if not mid: return ""
    items = _plex_collect(_plex_json("/search?query=" + urllib.parse.quote(title or "") + "&limit=30"))
    want = "movie" if kind == "movie" else "show"
    cand = [it for it in items if it.get("type") == want]
    best = None
    for it in cand:
        if _guid_match(it, imdb, str(tmdb or ""), str(tvdb or "")): best = it; break
    if not best:
        for it in cand:
            if year and str(it.get("year")) == str(year): best = it; break
    if not best and cand: best = cand[0]
    rk = (best or {}).get("ratingKey")
    if not rk: return ""
    return "https://app.plex.tv/desktop/#!/server/%s/details?key=%s" % (mid, urllib.parse.quote("/library/metadata/" + str(rk), safe=""))

def _scout_play(req):
    if req.get("play"): return req["play"]
    return _plex_resolve(req.get("title"), req.get("year"), req.get("imdbId"), req.get("tmdbId"), req.get("tvdbId"), req.get("kind"))

_SCOUT_LIVE_STAGES = ("queued", "searching", "grabbed", "downloading", "importing", "verifying")

def _scout_active():
    """How many Scout requests are still in flight. Used by the background drains (backlog / Riven)
    to yield so an explicit user request is fetched first. Refreshes stages first so we do not yield
    forever on a stale state when nobody has the dashboard open."""
    if not EN_SCOUT: return 0
    try:
        with _scout_lock:
            have = bool(_scout_load().get("reqs"))
    except Exception:
        return 0
    if not have: return 0
    try: _scout_status()                                          # refresh stages (probes the arrs; cheap)
    except Exception: pass
    now = time.time()
    with _scout_lock:
        reqs = list(_scout_load().get("reqs", {}).values())
    return sum(1 for r in reqs
               if r.get("stage") in _SCOUT_LIVE_STAGES and now - r.get("created", 0) < SCOUT_TTL_HOURS * 3600)

def _scout_dlclient(arr, name):
    for c in (arr.get_json("/downloadclient") or []):
        if not name or c.get("name") == name:
            return c
    return None

def _sab_force_top(fields, nzo):
    apikey = fields.get("apiKey") or ""
    if not apikey: return False, "no sab apikey"
    host = fields.get("host") or "127.0.0.1"
    port = fields.get("port") or 8080
    scheme = "https" if fields.get("useSsl") in (True, "true", "True", 1) else "http"
    base = (fields.get("urlBase") or "").strip("/")
    root = "%s://%s:%s%s" % (scheme, host, port, ("/" + base) if base else "")
    url = root + "/api?mode=queue&name=priority&value=%s&value2=2&output=json&apikey=%s" % (
        urllib.parse.quote(str(nzo)), urllib.parse.quote(str(apikey)))
    try:
        urllib.request.urlopen(url, timeout=10).read(); return True, "forced"
    except Exception as e:
        return False, str(e)[:80]

def _scout_prioritize(arr, rec, req):
    """Best-effort: shove a Scout grab to the top of its download client so it finishes first.
    Runs once per request. Only SABnzbd is force-able today; other clients are marked done so we
    do not retry every poll."""
    if req.get("prioritized"): return
    dlid, cname = rec.get("downloadId"), rec.get("downloadClient")
    if not dlid: return
    c = _scout_dlclient(arr, cname) or {}
    impl = (c.get("implementation") or "").lower()
    fields = {f.get("name"): f.get("value") for f in (c.get("fields") or [])}
    if "sab" in impl:
        ok, detail = _sab_force_top(fields, dlid)
        if ok:
            req["prioritized"] = True
            log.info("[scout] forced '%s' to top of %s queue", req.get("title"), cname)
        elif "apikey" in detail:
            req["prioritized"] = True                            # config gap, do not retry
            log.debug("[scout] cannot force priority (%s): %s", cname, detail)
        else:
            log.debug("[scout] force priority failed (%s): %s", cname, detail)   # transient, retry next poll
    else:
        req["prioritized"] = True
        log.debug("[scout] no priority lever for client '%s' (impl=%s)", cname, impl or "?")

def _scout_probe(req):
    if req.get("stage") == "dry-run": return "dry-run", {}
    backend, kind = req.get("backend"), req.get("kind")
    if backend == "arr":
        arr = next((a for a in INSTANCES if a.name == req.get("arr")), None)
        tid = req.get("target_id")
        if not arr or not tid: return "error", {"detail": "instance/target gone"}
        if kind == "movie":
            m = arr.get_json("/movie/%d" % tid)
            if m is None: return req.get("stage", "searching"), {}
            if m.get("hasFile"): return "available", {"play": _scout_play(req)}
            rec = _scout_queue_rec(arr, "movieId", tid)
            if rec and not DRY_RUN:
                try: _scout_prioritize(arr, rec, req)
                except Exception as e: log.debug("[scout] prioritize err: %s", str(e)[:80])
            return _scout_stage_from_rec(rec) if rec else _scout_search_or_timeout(req)
        s = arr.get_json("/series/%d" % tid)
        if s is None: return req.get("stage", "searching"), {}
        stt = s.get("statistics") or {}
        if stt.get("episodeFileCount", 0) > 0:
            return "available", {"play": _scout_play(req), "detail": "%d/%d episodes" % (stt.get("episodeFileCount", 0), stt.get("episodeCount", 0) or 0)}
        rec = _scout_queue_rec(arr, "seriesId", tid)
        if rec and not DRY_RUN:
            try: _scout_prioritize(arr, rec, req)
            except Exception as e: log.debug("[scout] prioritize err: %s", str(e)[:80])
        return _scout_stage_from_rec(rec) if rec else _scout_search_or_timeout(req)
    if backend == "riven":
        rv = next((r for r in RIVENS if r.name == req.get("riven")), RIVENS[0] if RIVENS else None)
        if not rv: return "error", {"detail": "riven gone"}
        it = _scout_riven_item(rv, req.get("imdbId"))
        if not it: return _scout_search_or_timeout(req)
        stg = _RIVEN_STAGE.get(it.get("state"), "searching")
        return stg, ({"play": _scout_play(req)} if stg == "available" else {})
    return req.get("stage", "queued"), {}

def _scout_status():
    mode = _scout_mode()
    with _scout_lock:
        items = list(_scout_load().get("reqs", {}).values())
    now, changed, out = time.time(), False, []
    for req in items:
        if req.get("stage") in ("available", "no source", "error", "dry-run") and req.get("done_ts") and now - req["done_ts"] > SCOUT_TTL_HOURS * 3600:
            with _scout_lock:
                s = _scout_load(); s.get("reqs", {}).pop(req["id"], None); _scout_save(s)
            continue
        pri0 = req.get("prioritized")
        try: stg, extra = _scout_probe(req)
        except Exception as e: stg, extra = "error", {"detail": str(e)[:80]}
        if req.get("prioritized") != pri0: changed = True
        if stg != req.get("stage"):
            req["stage"] = stg; changed = True
            if stg in ("available", "no source", "error"): req["done_ts"] = now
        if extra.get("play") and not req.get("play"):
            req["play"] = extra["play"]; changed = True
        req["_pct"] = extra.get("pct"); req["_detail"] = extra.get("detail") or req.get("detail") or ""
        out.append(req)
    if changed:
        with _scout_lock:
            s = _scout_load()
            for req in items:
                if req["id"] in s.get("reqs", {}):
                    s["reqs"][req["id"]].update({k: req[k] for k in ("stage", "play", "done_ts", "prioritized") if k in req})
            _scout_save(s)
    out.sort(key=lambda r: r.get("created", 0), reverse=True)
    view = [{"id": r["id"], "uid": r.get("uid", ""), "title": r.get("title"), "year": r.get("year"), "kind": r.get("kind"),
             "backend": r.get("backend"), "stage": r.get("stage"), "pct": r.get("_pct"), "prioritized": bool(r.get("prioritized")),
             "detail": r.get("_detail"), "play": r.get("play", ""), "ago": int(now - r.get("created", now))} for r in out[:SCOUT_RETAIN]]
    return {"mode": mode, "backend": _scout_meta()["backend"], "requests": view}

_UI_MULTI = {
    "HOLIDAYS_COUNTRIES": ["us", "canada", "uk", "china", "japan", "korea", "australia"],
    "DOCTOR_CONDITIONS": ["downloadClientUnavailable", "importBlocked", "importFailed",
                          "importPending_warning", "failedPending", "stalled"],
    "WARMER_SOURCES": ["ondeck", "next"],
    "BACKLOG_INSTANCES": ["sonarr", "radarr", "sonarr4k", "radarr4k"],
    "RIVEN_STUCK_STATES": ["Unreleased", "Ongoing", "Requested", "Indexed", "Scraped",
                           "Downloaded", "Symlinked", "Completed", "PartiallyCompleted", "Failed", "Paused"],
    "RIVEN_MISSING_STATES": ["Unreleased", "Ongoing", "Requested", "Indexed", "Scraped",
                             "Downloaded", "Symlinked", "Completed", "PartiallyCompleted", "Failed", "Paused"],
}
_UI_BOOL = set([
    "DOCTOR_DRY_RUN", "WATCHLISTS_INCLUDE_HOME", "HOLIDAYS_PIN_HOME",
    "SCRUBBER_FULL_DECODE_ON_BAD", "SCRUBBER_DELETE_ARR_FILE",
    "DOCTOR_BLOCKLIST", "WARMER_ONDECK",
])

def _ui_control(k, ph):
    """Pick a dashboard control kind for a config key: multi-checkbox, dropdown, or text."""
    if k in _UI_MULTI:
        return "multi", _UI_MULTI[k]
    if k.startswith("ENABLE_") or k in _UI_BOOL:
        return "bool", ["true", "false"]
    if "|" in ph:
        return "select", [o.strip() for o in ph.split("|") if o.strip()]
    return "text", []

def _ui_config():
    groups = []
    for g, items in UI_SCHEMA:
        rows = []
        for k, ph in items:
            ct, opts = _ui_control(k, ph)
            rows.append({"key": k, "val": ("" if _is_secret(k) else os.environ.get(k, "")),
                         "ph": ph, "secret": _is_secret(k), "type": ct, "options": opts})
        groups.append({"group": g, "rows": rows})
    return {"groups": groups, "file": CONFIG_FILE}

def _ui_save(body):
    try:
        incoming = json.loads(body or b"{}")
    except Exception:
        return False, "bad json"
    try:
        ov = json.load(open(CONFIG_FILE))
    except Exception:
        ov = {}
    n = 0
    for k, v in incoming.items():
        if k in UI_KEYS and not _is_secret(k):
            ov[k] = v; os.environ[str(k)] = str(v); n += 1
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
        json.dump(ov, open(CONFIG_FILE, "w"), indent=1)
    except Exception as e:
        return False, str(e)[:80]
    return True, "saved %d (restart to apply)" % n

def _ui_logs(n):
    if not LOG_FILE:
        return "(set DOCTOR_LOG_FILE to view logs here)"
    try:
        return "".join(open(LOG_FILE, errors="ignore").readlines()[-n:])
    except Exception as e:
        return "log read error: " + str(e)[:80]

UI_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>stack-doctor</title>
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
<link href="https://fonts.googleapis.com/css2?family=Patrick+Hand&family=Caveat:wght@600;700&display=swap" rel=stylesheet>
<style>
:root{--bg:#05070f;--card:rgba(18,26,46,.55);--card2:rgba(12,18,34,.72);--bd:rgba(120,160,255,.16);--bd2:rgba(120,160,255,.3);--fg:#dbe4ff;--mut:#7e8cb8;--ok:#34d399;--off:#5b6788;--bad:#fb7185;--ac:#22d3ee;--ac2:#a855f7;--warn:#fbbf24;--glow:0 0 18px rgba(34,211,238,.35);--sans:'Inter',system-ui,Segoe UI,sans-serif;--mono:'JetBrains Mono',ui-monospace,monospace}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font:14px/1.55 var(--sans);background:var(--bg);color:var(--fg);-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-2;background:radial-gradient(900px 500px at 12% -10%,rgba(34,211,238,.16),transparent 60%),radial-gradient(800px 500px at 100% 0,rgba(168,85,247,.16),transparent 55%),radial-gradient(700px 600px at 50% 120%,rgba(56,189,248,.10),transparent 60%),var(--bg)}
body::after{content:"";position:fixed;inset:0;z-index:-1;background-image:linear-gradient(rgba(120,160,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(120,160,255,.05) 1px,transparent 1px);background-size:42px 42px;-webkit-mask-image:radial-gradient(ellipse at 50% 0,#000,transparent 80%);mask-image:radial-gradient(ellipse at 50% 0,#000,transparent 80%)}
header{padding:16px 22px;display:flex;gap:14px;align-items:baseline;border-bottom:1px solid var(--bd);background:linear-gradient(180deg,rgba(10,15,31,.7),transparent);backdrop-filter:blur(8px);position:sticky;top:0;z-index:5}
h1{font-size:17px;margin:0;font-weight:700;letter-spacing:.04em;background:linear-gradient(90deg,var(--ac),var(--ac2));-webkit-background-clip:text;background-clip:text;color:transparent}
h1::before{content:"\25C8 ";-webkit-text-fill-color:var(--ac);color:var(--ac)}
.mut{color:var(--mut);font-size:12px}
nav{display:flex;gap:8px;padding:14px 22px 0;flex-wrap:wrap}
nav button{background:var(--card2);color:var(--mut);border:1px solid var(--bd);border-radius:9px;padding:8px 16px;cursor:pointer;font:600 13px var(--sans);letter-spacing:.02em;transition:.18s;backdrop-filter:blur(6px)}
nav button:hover{color:var(--fg);border-color:var(--bd2)}
nav button.active{color:#04121a;background:linear-gradient(90deg,var(--ac),#67e8f9);border-color:transparent;box-shadow:var(--glow)}
main{padding:18px 22px 56px;max-width:1240px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px;margin:0 0 16px;backdrop-filter:blur(10px);box-shadow:0 1px 0 rgba(255,255,255,.03) inset,0 10px 30px rgba(0,0,0,.35)}
h3{margin:0 0 12px;font-size:11px;color:var(--ac);text-transform:uppercase;letter-spacing:.16em;font-weight:600}
.badge{display:inline-block;padding:3px 11px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid transparent}
.b-on{background:rgba(52,211,153,.12);color:var(--ok);border-color:rgba(52,211,153,.35);box-shadow:0 0 12px rgba(52,211,153,.18)}
.b-off{background:rgba(91,103,136,.12);color:var(--off);border-color:rgba(91,103,136,.3)}
.b-bad{background:rgba(251,113,133,.12);color:var(--bad);border-color:rgba(251,113,133,.4);box-shadow:0 0 12px rgba(251,113,133,.18)}
.row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--bd)}.row:last-child{border:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px}
.chip{display:flex;justify-content:space-between;align-items:center;background:var(--card2);border:1px solid var(--bd);border-radius:10px;padding:9px 12px;transition:.18s}
.chip:hover{border-color:var(--bd2);transform:translateY(-1px)}
.big{font-size:28px;font-weight:700;color:#fff;text-shadow:0 0 18px rgba(34,211,238,.3)}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px;border-bottom:1px solid var(--bd)}td.why{color:var(--mut)}td.ago{color:var(--mut);text-align:right;white-space:nowrap}
label{display:block;color:var(--mut);font-size:11px;margin:11px 0 4px;letter-spacing:.02em;font-family:var(--mono)}
input,select{width:100%;background:rgba(5,9,18,.8);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:8px 10px;font:13px var(--mono);transition:.15s}
input:focus,select:focus{outline:0;border-color:var(--ac);box-shadow:var(--glow)}
input:disabled{color:var(--mut);opacity:.7}
select{appearance:none;-webkit-appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--ac) 50%),linear-gradient(135deg,var(--ac) 50%,transparent 50%);background-position:calc(100% - 16px) 17px,calc(100% - 11px) 17px;background-size:5px 5px;background-repeat:no-repeat;cursor:pointer}
select option{background:#0a0f1f;color:var(--fg)}
.cfg{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px 16px}
.multi{display:flex;flex-wrap:wrap;gap:6px;padding:2px 0}
.multi label{display:inline-flex;align-items:center;gap:6px;margin:0;padding:5px 10px;background:rgba(5,9,18,.8);border:1px solid var(--bd);border-radius:999px;color:var(--fg);font-size:12px;font-family:var(--mono);cursor:pointer;transition:.15s}
.multi label:hover{border-color:var(--bd2)}
.multi label.on{border-color:var(--ac);color:var(--ac);box-shadow:0 0 10px rgba(34,211,238,.2)}
.multi input{width:auto;accent-color:var(--ac)}
button.act{background:linear-gradient(90deg,var(--ac),#3b82f6);color:#04121a;border:0;border-radius:9px;padding:10px 18px;cursor:pointer;font:600 13px var(--sans);margin-right:8px;box-shadow:var(--glow);transition:.18s}
button.act:hover{filter:brightness(1.08);transform:translateY(-1px)}
button.warn{background:linear-gradient(90deg,var(--warn),#f59e0b);color:#1a1304;box-shadow:0 0 16px rgba(251,191,36,.3)}
pre{background:rgba(2,5,12,.85);border:1px solid var(--bd);border-radius:12px;padding:14px;margin:0;max-height:66vh;overflow:auto;white-space:pre-wrap;word-break:break-word;font:12px/1.5 var(--mono);color:#aeb9e6}
details summary{color:var(--ac)!important}
#toast{position:fixed;right:18px;bottom:18px;background:var(--card2);border:1px solid var(--ac);color:var(--fg);padding:11px 16px;border-radius:10px;opacity:0;transition:.3s;pointer-events:none;backdrop-filter:blur(8px);box-shadow:var(--glow)}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:rgba(120,160,255,.25);border-radius:6px}::-webkit-scrollbar-track{background:transparent}
/* ---- Scout: hand-drawn pencil-sketch tab (paper page inside the app) ---- */
#scout{font-family:'Patrick Hand','Inter',sans-serif}
#scout *{font-family:'Patrick Hand','Inter',sans-serif}
.sk-wrap{position:relative;background:#f7f4ec;color:#2c2a26;border:2.5px solid #2c2a26;border-radius:12px;padding:18px 20px 24px;box-shadow:4px 5px 0 rgba(44,42,38,.16);background-image:repeating-linear-gradient(0deg,transparent 0,transparent 30px,rgba(44,42,38,.05) 31px)}
.sk-wrap::after{content:"";position:absolute;inset:5px;border:1.5px solid rgba(44,42,38,.3);border-radius:9px;pointer-events:none}
.sk-head{display:flex;align-items:baseline;gap:12px;margin:0 0 6px;flex-wrap:wrap}
.sk-title{font-family:'Caveat',cursive;font-weight:700;font-size:32px;line-height:1}
.sk-sub{font-size:15px;color:#6a6358}
.sk-searchbar{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin-top:8px}
.sk-input{flex:1 1 240px;width:auto;min-width:0;background:#fffdf7;color:#2c2a26;border:2px solid #2c2a26;border-radius:150px 9px 150px 9px/9px 130px 9px 130px;padding:10px 15px;font:18px 'Patrick Hand';box-shadow:2px 2px 0 rgba(44,42,38,.12)}
.sk-input:focus{outline:0;box-shadow:2px 2px 0 rgba(44,42,38,.32)}
.sk-input:disabled{opacity:.5}
.sk-seg{display:inline-flex;border:2px solid #2c2a26;border-radius:9px;overflow:hidden}
.sk-seg button{background:#fffdf7;color:#2c2a26;border:0;border-right:2px solid #2c2a26;padding:8px 15px;font:16px 'Patrick Hand';cursor:pointer}
.sk-seg button:last-child{border-right:0}
.sk-seg button.on{background:#2c2a26;color:#f7f4ec}
.sk-btn{background:#ffe7a3;color:#2c2a26;border:2.5px solid #2c2a26;border-radius:150px 11px 150px 11px/11px 130px 11px 130px;padding:9px 20px;font:18px 'Patrick Hand';cursor:pointer;box-shadow:2px 3px 0 rgba(44,42,38,.25);transition:transform .1s,box-shadow .1s;text-decoration:none;display:inline-block;white-space:nowrap}
.sk-btn:hover{transform:translate(-1px,-1px);box-shadow:3px 4px 0 rgba(44,42,38,.3)}
.sk-btn:active{transform:translate(1px,1px);box-shadow:1px 1px 0 rgba(44,42,38,.25)}
.sk-results{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;margin-top:16px}
.sk-card{display:flex;flex-direction:column;background:#fffdf7;border:2px solid #2c2a26;border-radius:13px;padding:10px;box-shadow:3px 4px 0 rgba(44,42,38,.14);transform:rotate(-.35deg)}
.sk-card:nth-child(2n){transform:rotate(.4deg)}
.sk-card:nth-child(3n){transform:rotate(-.15deg)}
.sk-poster{height:150px;border:2px solid #2c2a26;border-radius:8px;background:#efe9da center/cover no-repeat;margin-bottom:8px;filter:grayscale(.3) contrast(1.05)}
.sk-noposter{display:flex;align-items:center;justify-content:center;font-size:44px}
.sk-cardtitle{font-size:18px;font-weight:700;line-height:1.12}
.sk-year{color:#6a6358}
.sk-kindtag{display:inline-block;align-self:flex-start;font-size:13px;border:1.5px solid #2c2a26;border-radius:6px;padding:0 8px;margin:5px 0;text-transform:capitalize}
.sk-ov{font-size:14px;color:#5a5348;line-height:1.3;max-height:74px;overflow:hidden;margin-bottom:8px}
.sk-noimg{display:flex;align-items:center;justify-content:center;font:16px 'Caveat';color:#9a9384;filter:none}
.sk-have{display:inline-block;align-self:flex-start;font-size:13px;color:#3f7a2e;margin-bottom:6px}
.sk-get{align-self:flex-start;margin-top:auto;font-size:16px;padding:7px 16px}
.sk-get:disabled{opacity:.5;cursor:default;background:#efe9da;box-shadow:none}
.sk-act{margin-top:auto;padding-top:8px;align-self:stretch}
.sk-status{border:2px solid #2c2a26;border-radius:120px 8px 120px 8px/8px 110px 8px 110px;padding:6px 10px;background:#fffdf7;box-shadow:2px 3px 0 rgba(44,42,38,.14)}
.sk-statlab{font:16px 'Patrick Hand';text-transform:capitalize;display:flex;align-items:center;gap:6px}
.sk-statlab .sk-spark{font-size:13px;color:#b06a00}
.sk-bar{margin-top:6px;height:9px;border:2px solid #2c2a26;border-radius:6px;background:repeating-linear-gradient(45deg,#f0ead9,#f0ead9 4px,#e7dfca 4px,#e7dfca 8px);overflow:hidden}
.sk-bar span{display:block;height:100%;background:#ffd34d;border-right:2px solid #2c2a26;transition:width .5s ease}
.sk-status.sk-st-bad{border-color:#b3261e}
.sk-status.sk-st-bad .sk-statlab{color:#b3261e}
.sk-status.sk-st-go{background:#bfe3b0}
.sk-prio{border-color:#b06a00;color:#b06a00}
.sk-feed{margin-top:12px;display:flex;flex-direction:column;gap:12px}
.sk-req{background:#fffdf7;border:2px solid #2c2a26;border-radius:12px;padding:12px 14px;box-shadow:3px 4px 0 rgba(44,42,38,.13)}
.sk-reqhead{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px}
.sk-reqtitle{font-size:19px;font-weight:700}
.sk-x{background:none;border:0;font-size:20px;cursor:pointer;color:#8a8276;line-height:1;padding:0 4px}
.sk-x:hover{color:#b3261e}
.sk-steps{display:flex;align-items:center;flex-wrap:wrap;gap:2px}
.sk-step{display:flex;align-items:center;gap:6px;opacity:.4}
.sk-step.done,.sk-step.cur{opacity:1}
.sk-dot{width:14px;height:14px;border:2px solid #2c2a26;border-radius:50%;background:#fffdf7}
.sk-step.done .sk-dot{background:#2c2a26}
.sk-step.cur .sk-dot{background:#ffd34d;animation:skpulse 1.1s infinite}
@keyframes skpulse{0%,100%{box-shadow:0 0 0 3px rgba(255,211,77,.45)}50%{box-shadow:0 0 0 6px rgba(255,211,77,.12)}}
.sk-steplab{font-size:14px}
.sk-line{flex:1 1 14px;min-width:12px;height:0;border-top:2px dashed #b8b0a0;margin:0 2px}
.sk-line.done{border-top-color:#2c2a26;border-top-style:solid}
.sk-play{background:#bfe3b0;margin-top:10px;font-size:16px}
.sk-bad{color:#b3261e;margin-top:8px;font-size:15px}
.sk-detail{color:#6a6358;margin-top:6px;font-size:14px}
.sk-note{color:#6a6358;font-size:16px;padding:8px 2px}
@media(max-width:560px){
 .sk-wrap{padding:14px 13px 20px}
 .sk-title{font-size:27px}
 .sk-input{flex:1 1 100%}
 .sk-seg{flex:1 1 auto}.sk-seg button{flex:1}
 #sk-go{width:100%;text-align:center}
 .sk-results{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px}
 .sk-poster{height:118px}
 .sk-step:not(.cur) .sk-steplab{display:none}
}
</style></head><body>
<header><h1>stack-doctor</h1><span class=mut id=sub>loading</span></header>
<nav><button data-t=dash class=active>Dashboard</button><button data-t=scout>Scout</button><button data-t=config>Config</button><button data-t=logs>Logs</button></nav>
<main>
<div id=dash>
 <div class=card><h3>Checks</h3><div class=grid id=checks></div></div>
 <div class=card><h3>Monitored services</h3><div id=health></div></div>
 <div class=card><h3>Warmer</h3><div id=warm></div></div>
 <div class=card id=wr-card style=display:none><h3>Westrepair</h3><div id=wr></div></div>
</div>
<div id=scout style=display:none>
 <div class=sk-wrap>
  <div class=sk-head>
   <div class=sk-title>Scout</div>
   <div class=sk-note id=sk-backend>checking the stack...</div>
  </div>
  <div class=sk-searchbar>
   <input id=sk-q class=sk-input placeholder="what do you want to watch?" autocomplete=off>
   <div class=sk-seg id=sk-kind>
    <button class="sk-segb on" data-k=both>Both</button>
    <button class=sk-segb data-k=movie>Movie</button>
    <button class=sk-segb data-k=show>Show</button>
   </div>
   <button id=sk-go class=sk-btn>Search</button>
  </div>
  <div id=sk-results class=sk-results></div>
  <div class=sk-head style="margin-top:18px"><div class="sk-title" style="font-size:24px">Acquiring</div></div>
  <div id=sk-feed class=sk-feed></div>
 </div>
</div>
<div id=config style=display:none></div>
<div id=logs style=display:none></div>
</main><div id=toast></div>
<script>
var tok=new URLSearchParams(location.search).get('token')||'';
function q(p){return p+(p.indexOf('?')>-1?'&':'?')+(tok?'token='+encodeURIComponent(tok):'')}
function E(i){return document.getElementById(i)}
function esc(s){return (s==null?'':''+s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
function toast(m){var e=E('toast');e.textContent=m;e.style.opacity=1;setTimeout(function(){e.style.opacity=0},2600)}
function ago(s){if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';return Math.floor(s/3600)+'h ago'}
var timer;
function show(t){var b=document.querySelectorAll('nav button');for(var i=0;i<b.length;i++)b[i].classList.toggle('active',b[i].dataset.t===t);
 E('dash').style.display=t==='dash'?'':'none';E('scout').style.display=t==='scout'?'':'none';E('config').style.display=t==='config'?'':'none';E('logs').style.display=t==='logs'?'':'none';
 clearInterval(timer);
 if(t==='dash'){loadDash();timer=setInterval(loadDash,5000)}
 if(t==='scout'){loadScoutMeta();loadScoutStatus();timer=setInterval(loadScoutStatus,4000)}
 if(t==='config')loadConfig();
 if(t==='logs'){loadLogs();timer=setInterval(loadLogs,4000)}}
var nb=document.querySelectorAll('nav button');for(var i=0;i<nb.length;i++)nb[i].onclick=(function(t){return function(){show(t)}})(nb[i].dataset.t);
function loadDash(){
 fetch(q('/api/status')).then(function(r){return r.json()}).then(function(s){
  E('sub').textContent='v'+s.version+' / mode '+s.mode+' / load '+s.load+(s.dry_run?' / DRY-RUN':'');
  var h='';for(var i=0;i<s.checks.length;i++){var c=s.checks[i];h+='<div class=chip><span>'+esc(c.name)+'</span><span class="badge '+(c.on?'b-on':'b-off')+'">'+(c.on?'on':'off')+'</span></div>'}
  E('checks').innerHTML=h});
 fetch(q('/api/health')).then(function(r){return r.json()}).then(function(a){
  var h='';for(var i=0;i<a.length;i++){var s=a[i];h+='<div class=row><span>'+esc(s.name)+' <span class=mut>'+esc(s.kind)+'</span></span><span><span class=mut style="margin-right:8px">'+esc(s.detail)+'</span><span class="badge '+(s.up?'b-on':'b-bad')+'">'+(s.up?'up':'down')+'</span></span></div>'}
  E('health').innerHTML=h||'<span class=mut>none</span>'});
 fetch(q('/api/warmer')).then(function(r){return r.json()}).then(function(w){
  var h='<div class=row><span class=mut>total warmed since start</span><span class=big>'+w.total+'</span></div>';
  h+='<div class=row><span class=mut>detail-page (warm what you open)</span><span class="badge '+(w.detail_page?'b-on':'b-off')+'">'+(w.detail_page?'on':'off')+'</span></div>';
  h+='<table style="margin-top:8px">';
  if(!w.recent.length)h+='<tr><td class=mut>nothing warmed yet</td></tr>';
  for(var i=0;i<w.recent.length;i++){var r=w.recent[i];h+='<tr><td>'+esc(r.title)+'</td><td class=why>'+esc(r.why)+'</td><td class=ago>'+ago(r.ago)+'</td></tr>'}
  h+='</table>';E('warm').innerHTML=h});
 fetch(q('/api/westrepair')).then(function(r){return r.json()}).then(function(w){
  var card=E('wr-card');if(!w.enabled){card.style.display='none';return}card.style.display='';
  var st=w.running?'<span class="badge b-on">running</span>':'<span class="badge b-bad">stopped</span>';
  var h='<div class=row><span class=mut>status</span>'+st+'</div>';
  h+='<div class=row><span class=mut>processed / broken / fixed</span><span><b>'+w.items_processed+'</b> / <b>'+w.items_broken+'</b> / <b>'+w.items_fixed+'</b></span></div>';
  if(w.current_item)h+='<div class=row><span class=mut>current item</span><span style="max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(w.current_item)+'</span></div>';
  if(w.next_run_in)h+='<div class=row><span class=mut>next run in</span><span>'+esc(w.next_run_in)+'</span></div>';
  if(w.last_action)h+='<div class=row><span class=mut>last action</span><span class=mut style="font-size:11px;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(w.last_action)+'</span></div>';
  var logOpen=E('wr-log')&&E('wr-log').open;
  if(w.recent_log&&w.recent_log.length){h+='<details id=wr-log style="margin-top:8px"'+(logOpen?' open':'')+'><summary style="cursor:pointer;color:var(--mut);font-size:12px">recent log ('+w.recent_log.length+' lines)</summary>';
   h+='<pre id=wr-logpre style="margin-top:6px;max-height:340px;font-size:11px">'+esc(w.recent_log.join('\n'))+'</pre></details>'}
  h+='<div style="margin-top:10px"><button class=act onclick=plexRescan()>Plex Rescan</button></div>';
  E('wr').innerHTML=h;
  var lp=E('wr-logpre');if(lp)lp.scrollTop=lp.scrollHeight;});
}
function plexRescan(){fetch(q('/api/westrepair/rescan'),{method:'POST'}).then(function(r){return r.json()}).then(function(r){toast(r.msg||'triggered')})}
function ctl(r){
 if(r.secret)return '<input value="set in unit (hidden)" disabled>';
 var k=esc(r.key),v=r.val==null?'':''+r.val;
 if(r.type==='multi'){var set={};v.split(',').forEach(function(x){x=x.trim();if(x)set[x]=1});
  var h='<div class=multi id="cf_'+k+'" data-ct=multi>';
  for(var i=0;i<r.options.length;i++){var o=r.options[i],on=!!set[o];
   h+='<label class="'+(on?'on':'')+'"><input type=checkbox value="'+esc(o)+'"'+(on?' checked':'')+" onchange=\"this.parentNode.classList.toggle('on',this.checked)\"> "+esc(o)+'</label>'}
  return h+'</div>'}
 if(r.type==='select'||r.type==='bool'){var has=false;
  for(var i=0;i<r.options.length;i++)if(''+r.options[i]===v)has=true;
  var h='<select id="cf_'+k+'" data-ct=select><option value=""'+(v===''?' selected':'')+'>(default)</option>';
  for(var i=0;i<r.options.length;i++){var o=esc(r.options[i]);h+='<option'+(''+r.options[i]===v?' selected':'')+'>'+o+'</option>'}
  if(v!==''&&!has)h+='<option selected>'+esc(v)+'</option>';
  return h+'</select>'}
 return '<input id="cf_'+k+'" data-ct=text value="'+esc(v)+'" placeholder="'+esc(r.ph)+'">';
}
function loadConfig(){fetch(q('/api/config')).then(function(r){return r.json()}).then(function(c){
  var h='';for(var g=0;g<c.groups.length;g++){var grp=c.groups[g];h+='<div class=card><h3>'+esc(grp.group)+'</h3><div class=cfg>';
   for(var i=0;i<grp.rows.length;i++){var r=grp.rows[i];h+='<div><label>'+esc(r.key)+'</label>'+ctl(r)+'</div>'}
   h+='</div></div>'}
  h+='<div class=card><button class=act onclick=saveCfg()>Save</button><button class="act warn" onclick=restart()>Save and Restart</button> <span class=mut>changes apply after a restart</span></div>';
  E('config').innerHTML=h})}
function gather(){var o={},els=document.querySelectorAll('[id^=cf_]');
 for(var i=0;i<els.length;i++){var el=els[i],k=el.id.slice(3),ct=el.getAttribute('data-ct');
  if(ct==='multi'){var cbs=el.querySelectorAll('input[type=checkbox]'),vals=[];
   for(var j=0;j<cbs.length;j++)if(cbs[j].checked)vals.push(cbs[j].value);o[k]=vals.join(',')}
  else o[k]=el.value}
 return o}
function saveCfg(){fetch(q('/api/config'),{method:'POST',body:JSON.stringify(gather())}).then(function(r){return r.json()}).then(function(r){toast(r.msg||'saved')})}
function restart(){fetch(q('/api/config'),{method:'POST',body:JSON.stringify(gather())}).then(function(){return fetch(q('/api/restart'),{method:'POST'})}).then(function(){toast('restarting')}).then(function(){setTimeout(function(){show('dash')},4500)})}
function loadLogs(){fetch(q('/api/logs?n=400')).then(function(r){return r.text()}).then(function(t){
  var d=E('logs');if(!d.dataset.i){d.innerHTML='<pre id=lp></pre>';d.dataset.i=1}
  var lp=E('lp'),bot=lp.scrollTop+lp.clientHeight>=lp.scrollHeight-40;lp.textContent=t;if(bot)lp.scrollTop=lp.scrollHeight})}
var skResults=[];var skKind='both';var skMeta={};var skActiveByUid={};
function loadScoutMeta(){fetch(q('/api/scout/meta')).then(function(r){return r.json()}).then(function(m){skMeta=m;
  var el=E('sk-backend');
  if(!m.enabled){el.textContent='Scout is turned off in config.';E('sk-go').disabled=true;return}
  if(!m.available){el.textContent='No acquisition backend found. Enable Sonarr / Radarr or Riven.';E('sk-go').disabled=true;return}
  E('sk-go').disabled=false;
  var line='via '+esc(m.backend);if(m.dry_run)line+=' (DRY-RUN: nothing will download)';if(!m.plex)line+=' (no Plex link)';
  el.textContent=line;
  var ms=E('sk-kind');ms.style.display=(m.mode==='riven')?'none':''})}
function scoutSearch(){var v=E('sk-q').value;if(!v.trim())return;
  E('sk-results').innerHTML='<div class=sk-note>sketching results...</div>';
  fetch(q('/api/scout/search?kind='+encodeURIComponent(skKind)+'&q='+encodeURIComponent(v))).then(function(r){return r.json()}).then(function(d){
   skResults=d.results||[];renderResults()}).catch(function(){E('sk-results').innerHTML='<div class="sk-note sk-bad">search failed</div>'})}
function renderResults(){var h='';
  if(!skResults.length){E('sk-results').innerHTML='<div class=sk-note>nothing found. try another title.</div>';return}
  for(var i=0;i<skResults.length;i++){var r=skResults[i];r._i=i;
   var pos=r.poster?'<img class=sk-poster src="'+esc(r.poster)+'" alt="" onerror="this.style.display=\'none\'">':'<div class="sk-poster sk-noimg">no art</div>';
   var yr=r.year?(' ('+esc(r.year)+')'):'';
   var have=r.hasFile?'<span class=sk-have>in library</span>':'';
   h+='<div class=sk-card>'+pos+'<div class=sk-kindtag>'+esc(r.kind)+'</div>'+
      '<div class=sk-cardtitle>'+esc(r.title)+yr+'</div>'+have+
      '<div class=sk-ov>'+esc(r.overview||'')+'</div>'+
      '<div class=sk-act id="skact_'+i+'">'+skAction(r)+'</div></div>'}
  E('sk-results').innerHTML=h}
function skAction(r){var req=skActiveByUid[r.uid];
  if(req)return skStatusPill(req);
  if(r.hasFile)return '<button class="sk-btn sk-get" disabled>have it</button>';
  return '<button class="sk-btn sk-get" onclick="scoutGet('+r._i+')">Get</button>'}
function skStatusPill(req){var stage=req.stage;
  if(stage==='available'){
   if(req.play)return '<a class="sk-btn sk-play" href="'+esc(req.play)+'" target=_blank rel=noopener>Play in Plex</a>';
   return '<div class="sk-status sk-st-go"><div class=sk-statlab>kaboom, linking Plex...</div></div>'}
  if(stage==='no source')return '<div class="sk-status sk-st-bad"><div class=sk-statlab>no source yet, retrying</div></div>';
  if(stage==='error')return '<div class="sk-status sk-st-bad"><div class=sk-statlab>error</div></div>';
  if(stage==='dry-run')return '<div class=sk-status><div class=sk-statlab>dry-run, nothing sent</div></div>';
  var idx=skStepIndex(stage);if(idx<0)idx=0;
  var lab=SK_STEPS[idx]||stage;
  if(stage==='downloading'&&req.pct!=null)lab='downloading '+req.pct+'%';
  var pct=(stage==='downloading'&&req.pct!=null)?req.pct:Math.round(((idx+1)/SK_STEPS.length)*100);
  var spark=req.prioritized?'<span class=sk-spark>&#9733; priority</span>':'';
  return '<div class="sk-status sk-st-live"><div class=sk-statlab>'+esc(lab)+spark+'</div><div class=sk-bar><span style="width:'+pct+'%"></span></div></div>'}
function scoutGet(i){var r=skResults[i];if(!r)return;
  fetch(q('/api/scout/get'),{method:'POST',body:JSON.stringify(r)}).then(function(x){return x.json()}).then(function(d){
   if(d.ok){toast('on it: '+r.title);
    skActiveByUid[r.uid]={uid:r.uid,title:r.title,year:r.year,kind:r.kind,stage:d.stage||'searching',pct:null,play:'',prioritized:false};
    var el=E('skact_'+i);if(el)el.innerHTML=skAction(r);
    loadScoutStatus()}else{toast(d.error||'could not start')}})}
function refreshCards(){for(var i=0;i<skResults.length;i++){var el=E('skact_'+i);if(el)el.innerHTML=skAction(skResults[i])}}
function loadScoutStatus(){fetch(q('/api/scout/status')).then(function(r){return r.json()}).then(function(d){
   skActiveByUid={};var rs=d.requests||[];
   for(var i=0;i<rs.length;i++){var rq=rs[i];if(rq.uid&&!skActiveByUid[rq.uid])skActiveByUid[rq.uid]=rq}
   refreshCards();renderFeed(d)})}
var SK_STEPS=['searching','grabbed','downloading','importing','verifying','available'];
function skStepIndex(stage){if(stage==='queued'||stage==='searching')return 0;
  for(var i=0;i<SK_STEPS.length;i++)if(SK_STEPS[i]===stage)return i;return -1}
function renderFeed(d){var rs=d.requests||[];
  if(!rs.length){E('sk-feed').innerHTML='<div class=sk-note>nothing in flight. search above and hit Get.</div>';return}
  var h='';for(var i=0;i<rs.length;i++)h+=renderReq(rs[i]);E('sk-feed').innerHTML=h}
function renderReq(r){var yr=r.year?(' ('+esc(r.year)+')'):'';
  var pri=r.prioritized?' <span class="sk-kindtag sk-prio">&#9733; priority</span>':'';
  var head='<div class=sk-reqhead><div class=sk-reqtitle>'+esc(r.title)+yr+' <span class=sk-kindtag>'+esc(r.kind)+'</span>'+pri+'</div>'+
           '<button class=sk-x title=dismiss onclick="scoutClear(\''+esc(r.id)+'\')">x</button></div>';
  var term=(r.stage==='no source'||r.stage==='error'||r.stage==='dry-run');
  var body='';
  if(term){
   var cls=r.stage==='dry-run'?'sk-detail':'sk-bad';
   var msg=r.stage==='no source'?'no source found yet, still trying':(r.stage==='dry-run'?'dry-run: nothing was submitted':'error');
   if(r.detail)msg+=': '+r.detail;
   body='<div class="sk-note '+cls+'">'+esc(msg)+'</div>'}
  else{
   var cur=skStepIndex(r.stage);body='<div class=sk-steps>';
   for(var s=0;s<SK_STEPS.length;s++){
    var st=(s<cur)?'done':(s===cur?'cur':'');
    var lab=SK_STEPS[s];
    if(SK_STEPS[s]==='downloading'&&s===cur&&r.pct!=null)lab='downloading '+r.pct+'%';
    if(SK_STEPS[s]==='available')lab='kaboom';
    body+='<div class="sk-step '+st+'"><span class=sk-dot></span><span class=sk-steplab>'+esc(lab)+'</span></div>';
    if(s<SK_STEPS.length-1)body+='<span class="sk-line '+(s<cur?'done':'')+'"></span>'}
   body+='</div>';
   if(r.detail&&r.stage!=='available')body+='<div class="sk-note sk-detail">'+esc(r.detail)+'</div>'}
  var foot='';
  if(r.stage==='available'){
   if(r.play)foot='<a class="sk-btn sk-play" href="'+esc(r.play)+'" target=_blank rel=noopener>Play in Plex</a>';
   else foot='<div class="sk-note sk-detail">ready, finding the Plex link...</div>'}
  return '<div class=sk-req>'+head+body+foot+'</div>'}
function scoutClear(id){fetch(q('/api/scout/clear'),{method:'POST',body:JSON.stringify({id:id})}).then(function(){loadScoutStatus()})}
(function(){var seg=E('sk-kind').querySelectorAll('.sk-segb');
  for(var i=0;i<seg.length;i++)seg[i].onclick=(function(b){return function(){skKind=b.dataset.k;
   for(var j=0;j<seg.length;j++)seg[j].classList.toggle('on',seg[j]===b);if(skResults.length)scoutSearch()}})(seg[i]);
  E('sk-go').onclick=scoutSearch;
  E('sk-q').addEventListener('keydown',function(e){if(e.key==='Enter'||e.keyCode===13)scoutSearch()})})();
show('dash');
</script></body></html>"""

def _build_server(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    class H(BaseHTTPRequestHandler):
        def _send(self, code, ctype, body):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            try: self.wfile.write(body)
            except Exception: pass
        def _authed(self):
            if not UI_TOKEN:
                return True
            q = parse_qs(urlparse(self.path).query)
            return self.headers.get("X-Doctor-Token") == UI_TOKEN or q.get("token", [""])[0] == UI_TOKEN
        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/health", "/healthz"):
                return self._send(200, "text/plain", "ok")
            if not EN_UI:
                return self._send(404, "text/plain", "nf")
            if not self._authed():
                return self._send(401, "text/plain", "unauthorized")
            if path in ("/", "/ui", "/index.html"):
                return self._send(200, "text/html; charset=utf-8", UI_HTML)
            if path == "/api/status":  return self._send(200, "application/json", json.dumps(_ui_status()))
            if path == "/api/health":  return self._send(200, "application/json", json.dumps(_ui_health()))
            if path == "/api/warmer":      return self._send(200, "application/json", json.dumps(_ui_warmer()))
            if path == "/api/westrepair":  return self._send(200, "application/json", json.dumps(_ui_westrepair()))
            if path == "/api/config":      return self._send(200, "application/json", json.dumps(_ui_config()))
            if path == "/api/scout/meta":   return self._send(200, "application/json", json.dumps(_scout_meta()))
            if path == "/api/scout/status": return self._send(200, "application/json", json.dumps(_scout_status()))
            if path == "/api/scout/search":
                qd = parse_qs(urlparse(self.path).query)
                return self._send(200, "application/json", json.dumps(
                    _scout_search(qd.get("q", [""])[0], qd.get("kind", ["both"])[0])))
            if path == "/api/logs":
                try: n = min(int(parse_qs(urlparse(self.path).query).get("n", ["300"])[0]), 3000)
                except Exception: n = 300
                return self._send(200, "text/plain; charset=utf-8", _ui_logs(n))
            return self._send(404, "text/plain", "nf")
        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            if path in ("/api/config", "/api/restart", "/api/westrepair/rescan", "/api/scout/get", "/api/scout/clear"):
                if not EN_UI or not self._authed():
                    return self._send(401, "text/plain", "unauthorized")
                if path == "/api/scout/get":
                    ok, info = _scout_get(body)
                    return self._send(200 if ok else 400, "application/json", json.dumps(dict(info, ok=ok)))
                if path == "/api/scout/clear":
                    return self._send(200, "application/json", json.dumps({"ok": _scout_clear(body)}))
                if path == "/api/config":
                    ok, msg = _ui_save(body)
                    return self._send(200 if ok else 400, "application/json", json.dumps({"ok": ok, "msg": msg}))
                if path == "/api/westrepair/rescan":
                    threading.Thread(target=lambda: _wr_plex_rescan(), daemon=True).start()
                    return self._send(200, "application/json", json.dumps({"ok": True, "msg": "Plex rescan triggered"}))
                self._send(200, "application/json", json.dumps({"ok": True, "msg": "restarting"}))
                log.info("[ui] restart requested"); threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
                return
            if MODE == "event":                                  # arr webhook
                try: p = json.loads(body or b"{}")
                except Exception: p = {}
                ev = p.get("eventType") or p.get("EventType") or "?"; inst = p.get("instanceName") or p.get("InstanceName")
                self._send(200, "text/plain", "ok")
                if ev == "Test":
                    log.info("webhook Test from %s", inst or "?"); return
                if TRIGGER_EVENTS and ev not in TRIGGER_EVENTS:
                    return
                log.info("event '%s' from %s -> sweep", ev, inst or "all")
                threading.Thread(target=sweep, kwargs={"only": inst}, daemon=True).start(); return
            self._send(404, "text/plain", "nf")
        def log_message(self, *a):
            pass
    return ThreadingHTTPServer(("0.0.0.0", port), H)

def main():
    global INSTANCES
    INSTANCES = load_instances()
    enabled = [c for c, e, _ in CHECKS if e]
    warmer_on = EN_WARMER and bool(PLEX_URL)
    if EN_WARMER and not PLEX_URL:
        log.warning("ENABLE_WARMER set but PLEX_URL is empty -> warmer disabled")
    if EN_QUEUE and not INSTANCES:
        log.error("queue check enabled but no instances. Set INSTANCE_1_URL / _APIKEY / _TYPE.")
        sys.exit(2)
    if not enabled and not warmer_on and not EN_UI:
        log.error("nothing enabled. Set ENABLE_QUEUE / ENABLE_DECYPHARR / ENABLE_PLEX / ENABLE_RESOURCES / ENABLE_JANITOR / ENABLE_WARMER / ENABLE_UI.")
        sys.exit(2)
    extra = [r.name + "(riven)" for r in RIVENS] + [m.name + "(mediastorm)" for m in MEDIASTORMS]
    log.info("stack-doctor v%s | mode=%s | checks=[%s]%s%s | instances=%s | dry_run=%s",
             VERSION, MODE, ",".join(enabled), " +warmer" if warmer_on else "", " +ui" if EN_UI else "",
             ", ".join([a.name for a in INSTANCES] + extra) or "-", DRY_RUN)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if warmer_on:
        threading.Thread(target=warmer_loop, args=(stop,), daemon=True).start()
        if WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE:
            threading.Thread(target=plexlog_loop, args=(stop,), daemon=True).start()

    if EN_WESTREPAIR:
        threading.Thread(target=westrepair_loop, args=(stop,), daemon=True).start()

    # http server(s): arr webhooks (event mode) and/or the web dashboard (ENABLE_UI)
    servers, wanted = [], {}
    if MODE == "event":
        wanted[PORT] = "webhooks"
    if EN_UI:
        wanted[UI_PORT] = (wanted.get(UI_PORT, "") + "+dashboard").lstrip("+")
    for pnum, what in wanted.items():
        try:
            s = _build_server(pnum)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            servers.append(s); log.info("http on :%d (%s)", pnum, what)
        except Exception as e:
            log.error("http bind :%d failed: %s", pnum, e)

    sweep()
    interval = max(INTERVAL, 1800) if MODE == "event" else INTERVAL
    while not stop.wait(interval):
        sweep()
    for s in servers:
        try: s.shutdown()
        except Exception: pass
    log.info("stack-doctor stopped")

if __name__ == "__main__":
    main()
