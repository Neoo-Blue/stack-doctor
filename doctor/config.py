"""Configuration and small generic helpers (env-driven)."""
import os
import json

VERSION = "0.3"
def _b(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")
def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
def _dur(tok, default: int = 0) -> int:
    """Parse a duration token: 30s / 10m / 2h / 1d, or a bare number of seconds."""
    t = str(tok).strip().lower()
    if not t:
        return default
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        return int(float(t[:-1]) * mult[t[-1]]) if t[-1] in mult else int(float(t))
    except (ValueError, KeyError):
        return default
def _human(sec: int) -> str:
    sec = int(sec)
    for size, suf in ((86400, "d"), (3600, "h"), (60, "m")):
        if sec >= size and sec % size == 0:
            return "%d%s" % (sec // size, suf)
    return "%ds" % sec
CONFIG_FILE = os.environ.get("DOCTOR_CONFIG_FILE", "/data/config.json")
def _load_overrides():
    """Load config.json into os.environ.  Environment variables already set
    (e.g. from docker-compose) take priority over config.json values so that
    compose overrides always win."""
    try:
        with open(CONFIG_FILE) as f:
            for k, v in json.load(f).items():
                if v is not None and str(k) not in os.environ:
                    os.environ[str(k)] = str(v)
    except Exception:
        pass
_load_overrides()
MODE        = os.environ.get("DOCTOR_MODE", "cron").strip().lower()   # cron | event
INTERVAL    = _i("DOCTOR_INTERVAL", 900)                              # default/fallback interval; kept for compatibility
PORT        = _i("DOCTOR_PORT", 8088)                                 # webhook port (event mode)
UI_PORT     = _i("DOCTOR_UI_PORT", 12345)                            # web dashboard port
EN_UI       = _b("ENABLE_UI", False)
UI_TOKEN    = os.environ.get("DOCTOR_UI_TOKEN", "")                   # optional ?token= / X-Doctor-Token gate
LOG_LEVEL   = os.environ.get("DOCTOR_LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("DOCTOR_LOG_FILE", "")
# Respect NO_COLOR and add explicit opt-out. Default to colored output for humans.
LOG_COLORS  = _b("DOCTOR_LOG_COLORS", True) and not _b("NO_COLOR", False)
TIMEOUT     = _i("DOCTOR_HTTP_TIMEOUT", 60)
DRY_RUN     = _b("DOCTOR_DRY_RUN", False)
FAST_INTERVAL        = _dur(os.environ.get("DOCTOR_FAST_INTERVAL", "180s"), 180)     # 3 min
SLOW_INTERVAL        = _dur(os.environ.get("DOCTOR_SLOW_INTERVAL", "1800s"), 1800)   # 30 min
SCHEDULER_TICK       = _dur(os.environ.get("DOCTOR_SCHEDULER_TICK", "30s"), 30)      # how often scheduler wakes
SCHEDULER_CONCURRENCY = _i("DOCTOR_SCHEDULER_CONCURRENCY", 3)                          # max parallel scheduled checks
def _check_interval(cid, speed, default_iv=None):
    """Return the run interval in seconds for a check.

    Resolution order (first match wins):
      1. <CHECK_ID>_INTERVAL env var (or config.json key)
      2. default_iv argument (per-check override from CHECKS table)
      3. FAST_INTERVAL / SLOW_INTERVAL based on speed tag
    """
    per = os.environ.get("%s_INTERVAL" % cid.upper())
    if per:
        return _dur(per, INTERVAL)
    if default_iv is not None:
        return int(default_iv)
    return FAST_INTERVAL if speed == "fast" else SLOW_INTERVAL
EN_QUEUE              = _b("ENABLE_QUEUE", True)
EN_DECYPHARR          = _b("ENABLE_DECYPHARR", False)
EN_DECYPHARR_PROVIDERS = _b("ENABLE_DECYPHARR_PROVIDERS", False)
EN_PLEX       = _b("ENABLE_PLEX", False)
EN_RESOURCES  = _b("ENABLE_RESOURCES", False)
EN_JANITOR    = _b("ENABLE_JANITOR", False)
EN_PROVIDERS  = _b("ENABLE_PROVIDERS", False)
EN_BAZARR     = _b("ENABLE_BAZARR", False)
EN_SEERR      = _b("ENABLE_SEERR", False)       # Overseerr/Jellyseerr/Seerr: auto-retry FAILED requests
EN_PLEX_SCAN  = _b("ENABLE_PLEX_SCAN", False)   # detect + recover a wedged Plex library scan
EN_REPAIR     = _b("ENABLE_REPAIR", False)      # probe library for dead files -> remove + re-search
EN_MISSING_SEASONS    = _b("ENABLE_MISSING_SEASONS", False)
MS_MIN_AGE_HOURS      = _f("MISSING_SEASONS_MIN_AGE_HOURS", 1)   # ignore seasons added less than this long ago
MS_MAX_ACTIONS        = _i("MISSING_SEASONS_MAX_ACTIONS", 25)     # SeasonSearches per sweep
MS_RECHECK            = _dur(os.environ.get("MISSING_SEASONS_RECHECK", "6h"), 21600)  # cooldown between re-searching same season
MS_SORT_BY            = os.environ.get("MISSING_SEASONS_SORT_BY", "mixed").strip().lower()  # mixed | added | episodes
MS_BACKFILL_BATCH     = _i("MISSING_SEASONS_BACKFILL_BATCH", 50)  # sleep after this many SeasonSearches in backfill mode
MS_BACKFILL_DELAY     = _f("MISSING_SEASONS_BACKFILL_DELAY", 0)   # seconds to pause between backfill batches
MS_PARTIAL            = _b("MISSING_SEASONS_PARTIAL", True)        # also search seasons that are partially complete (some files, not all) when the season has fully aired
# ---- multipack ----
MULTIPACK_ENABLED       = _b("ENABLE_MULTIPACK", True)             # push cached multi-season packs that Sonarr would normally reject
MULTIPACK_MAX_ACTIONS   = _i("MULTIPACK_MAX_ACTIONS", 3)           # max packs pushed per sweep
MULTIPACK_RECHECK       = _f("MULTIPACK_RECHECK", 7 * 86400)       # seconds before re-checking a series for new packs (default 7 days)
MULTIPACK_ITEM_INTERVAL = _f("MULTIPACK_ITEM_INTERVAL", 2)         # seconds between pushes
# ---- force_import (importarr-style: force import matched-by-ID releases) ----
EN_FORCE_IMPORT        = _b("ENABLE_FORCE_IMPORT", False)          # try manual import of obfuscated/misnamed releases
FI_MAX_ACTIONS         = _i("FORCE_IMPORT_MAX_ACTIONS", 10)       # max manual imports per sweep
FI_MIN_STRIKES         = _i("FORCE_IMPORT_MIN_STRIKES", 1)        # consecutive hits before acting (often safe at 1)
FI_FALLBACK            = _b("FORCE_IMPORT_FALLBACK", True)         # remove + re-search if force import fails
FI_IMPORT_MODE         = os.environ.get("FORCE_IMPORT_MODE", "auto").strip().lower()  # auto | copy | move
FI_RECHECK             = _dur(os.environ.get("FORCE_IMPORT_RECHECK", "1h"), 3600)  # cooldown per item
# missing_seasons runs on a tighter default interval than other slow checks;
# the scheduler handles this via its per-check default_interval column.
EN_NO_UPGRADE_PROFILE   = _b("ENABLE_NO_UPGRADE_PROFILE", False)
NO_UPGRADE_PROFILE_ID   = _i("NO_UPGRADE_PROFILE_ID", 0)   # target quality profile id in Sonarr
NO_UPGRADE_PROFILE_NAME = os.environ.get("NO_UPGRADE_PROFILE_NAME", "WEB-1080p (No Upgrade)")
BAZARR_URL    = os.environ.get("BAZARR_URL", "")
BAZARR_APIKEY = os.environ.get("BAZARR_APIKEY", "")
SEERR_URL       = os.environ.get("SEERR_URL", "")
SEERR_APIKEY    = os.environ.get("SEERR_APIKEY", "")
SEERR_MAX       = _i("SEERR_RETRY_MAX", 10)      # max requests retried per sweep
SEERR_MAX_TRIES = _i("SEERR_MAX_ATTEMPTS", 5)    # give up after this many auto-retries (0 = never)
MIN_STRIKES   = _i("DOCTOR_MIN_STRIKES", 2)
MAX_ACTIONS   = _i("DOCTOR_MAX_ACTIONS", 20)
BLOCKLIST     = _b("DOCTOR_BLOCKLIST", True)
REMOVE_CLIENT = _b("DOCTOR_REMOVE_FROM_CLIENT", True)
STATE_FILE    = os.environ.get("DOCTOR_STATE_FILE", "/data/state.json")
CHURN_LIMIT    = _i("DOCTOR_CHURN_LIMIT", 0)              # 0 = brake off
CHURN_ACTION   = os.environ.get("DOCTOR_CHURN_ACTION", "report").strip().lower()
CHURN_BACKOFF  = [_dur(x) for x in os.environ.get("DOCTOR_CHURN_BACKOFF", "").split(",") if x.strip()]
if not CHURN_BACKOFF:
    _legacy = os.environ.get("DOCTOR_CHURN_COOLDOWN")    # back-compat with the old single fixed cooldown
    CHURN_BACKOFF = [_dur(_legacy)] if _legacy else [600, 3600, 86400]
DEFAULT_CONDITIONS = "downloadClientUnavailable,importBlocked,importFailed,importPending_warning,failedPending,stalled"
ENABLED_CONDITIONS = [c.strip() for c in os.environ.get("DOCTOR_CONDITIONS", DEFAULT_CONDITIONS).split(",") if c.strip()]
LOAD_MAX        = _f("DOCTOR_LOAD_MAX", 0)         # queue check pauses above this (0=off)
RES_LOAD_WARN   = _f("RES_LOAD_WARN", 40)
RES_SWAP_WARN   = _i("RES_SWAP_WARN_MB", 7000)
RES_MEM_MIN     = _i("RES_MEM_MIN_MB", 800)
RES_DROP_CACHES = _b("RES_DROP_CACHES", False)       # echo 1 > drop_caches on memory pressure (needs privilege)
DECY_URL          = os.environ.get("DECYPHARR_URL", "")             # e.g. http://192.168.50.202:8282
DECY_MOUNT_TEST   = os.environ.get("DECYPHARR_MOUNT_TEST", "")      # a dir on the FUSE mount to read-test
DECY_READ_TIMEOUT = _i("DECYPHARR_READ_TIMEOUT", 25)
DECY_RESTART_CMD  = os.environ.get("DECYPHARR_RESTART_CMD", "")     # shell cmd to recover a hung mount
DECY_FUSE_STRIKES = _i("DECYPHARR_FUSE_STRIKES", 2)                 # consecutive failures before restart hook fires
# ---- decypharr repair trigger (ask decypharr to run its own repair sweep) ----
DECY_REPAIR_TRIGGER   = _b("DECYPHARR_REPAIR_TRIGGER", True)       # ask decypharr to repair on stack-doctor sweep
DECY_REPAIR_INTERVAL  = _dur(os.environ.get("DECYPHARR_REPAIR_INTERVAL", "2h"), 7200)  # min seconds between triggers
# ---- decypharr link-error cache poisoning detector ----
# decypharr caches ALL provider errors (including transient RD CDN errors like
# read_pxy_timeout) as permanent in-memory validation failures.  Once poisoned
# the only fix is a restart.  stack-doctor detects this by counting these
# error lines in the log tail over a rolling window.
DECY_LINK_ERR_LOG_CMD   = os.environ.get("DECYPHARR_LINK_ERR_LOG_CMD", "")  # cmd to fetch log; falls back to JAN_LOG_CMD / JAN_LOG
DECY_LINK_ERR_THRESHOLD = _i("DECYPHARR_LINK_ERR_THRESHOLD", 20)    # errors in window before acting (default 20)
DECY_LINK_ERR_WINDOW    = _dur(os.environ.get("DECYPHARR_LINK_ERR_WINDOW", "10m"), 600)  # rolling window in seconds (default 10m)
DECY_LINK_ERR_RESTART   = _b("DECYPHARR_LINK_ERR_RESTART", True)    # restart decypharr when threshold hit (uses DECY_RESTART_CMD)
# ---- decypharr provider health / auto-disable ----
# Watches decypharr's log for per-provider seedbox/add failures and, when a provider
# is consistently failing, removes it from decypharr/config.json and restarts decypharr.
# The original provider block is saved in stack-doctor state for later re-enable.
DCP_LOG_CMD             = os.environ.get("DECYPHARR_PROVIDERS_LOG_CMD", "")  # log source; falls back to janitor log cmd
DCP_LOG                 = os.environ.get("DECYPHARR_PROVIDERS_LOG", "")      # or a plain log file path
DCP_CONFIG_PATH         = os.environ.get("DECYPHARR_CONFIG_PATH", "/data/decypharr/config.json")
DCP_THRESHOLD           = _i("DECYPHARR_PROVIDERS_THRESHOLD", 5)    # failed submissions in window before provider is considered broken
DCP_WINDOW              = _dur(os.environ.get("DECYPHARR_PROVIDERS_WINDOW", "10m"), 600)
DCP_AUTO_DISABLE        = _b("DECYPHARR_PROVIDERS_AUTO_DISABLE", True)
DCP_COOLDOWN            = _dur(os.environ.get("DECYPHARR_PROVIDERS_COOLDOWN", "1h"), 3600)  # before attempting re-enable
DCP_REENABLE            = _b("DECYPHARR_PROVIDERS_REENABLE", True)  # test provider API and re-add after cooldown
DCP_PROVIDERS_RESTART_CMD = os.environ.get("DECYPHARR_PROVIDERS_RESTART_CMD", DECY_RESTART_CMD or "docker restart decypharr")
PLEX_URL   = os.environ.get("PLEX_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")
PLEX_SCAN  = _b("PLEX_SCAN_ON_CHECK", False)
PLEX_SCAN_STUCK  = _dur(os.environ.get("PLEX_SCAN_STUCK_AFTER", "30m"), 1800)  # no-progress time before "stuck"
PLEX_SCAN_CANCEL = _b("PLEX_SCAN_CANCEL", True)                                # cancel the wedged scan via the activities API
PLEX_RESTART_CMD = os.environ.get("PLEX_RESTART_CMD", "")                      # last-resort hook if the scan stays wedged
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
WARM_LOW_CACHE    = _b("WARMER_LOW_CACHE", False)
WARM_NEXT_REMAIN  = _i("WARMER_NEXT_REMAINING_MIN", 0)  # warm the next episode only when <= this many minutes remain (0 = as soon as playback is seen)
WARM_NEXT_NEAR_END = WARM_NEXT_REMAIN if WARM_NEXT_REMAIN > 0 else (10 if WARM_LOW_CACHE else 0)
WARM_SOURCES      = [s.strip().lower() for s in os.environ.get("WARMER_SOURCES", "ondeck,next").split(",") if s.strip()]
WARM_ONDECK       = _b("WARMER_ONDECK", True)          # quick on/off for Continue Watching (On Deck) warming
WARM_PATH_MAP     = os.environ.get("WARMER_PATH_MAP", "")   # "plexPrefix:hostPrefix" if Plex's file path != this host's
WARM_PLEXLOG_CMD  = os.environ.get("WARMER_PLEXLOG_CMD", "")
WARM_PLEXLOG_FILE = os.environ.get("WARMER_PLEXLOG_FILE", "")
JAN_LIBS      = [p.strip() for p in os.environ.get("JANITOR_LIBRARY_PATHS", "").split(",") if p.strip()]
JAN_LOG       = os.environ.get("JANITOR_DECYPHARR_LOG", "")         # log file path
JAN_LOG_CMD   = os.environ.get("JANITOR_LOG_CMD", "")               # cmd printing the log, e.g. "journalctl -u decypharr -n 10000 --no-hostname"
JAN_QUAR      = os.environ.get("JANITOR_QUARANTINE_DIR", "/data/quarantine")
JAN_PATTERNS  = os.environ.get("JANITOR_DEAD_PATTERNS", "ARTICLE_NOT_FOUND,still missing,marked as bad").split(",")
JAN_ERROR_PATTERNS = [p.strip() for p in os.environ.get(
    "JANITOR_ERROR_PATTERNS",
    "panic,fatal,runtime error,rate limit,rate limited,too many requests,cloudflare,cf-ray,blocked,unauthorized,token expired,context deadline exceeded,connection refused,timeout,i/o timeout"
).split(",") if p.strip()]
JAN_ALERT_COOLDOWN = _dur(os.environ.get("JANITOR_ALERT_COOLDOWN", "5m"), 300)
REPAIR_LIBS             = [p.strip() for p in os.environ.get("REPAIR_LIBRARY_PATHS",
                           os.environ.get("JANITOR_LIBRARY_PATHS", "")).split(",") if p.strip()]
REPAIR_MAX_ACTIONS      = _i("REPAIR_MAX_ACTIONS", 20)       # re-grab/search commands per sweep
REPAIR_MAX_SYMLINKS     = _i("REPAIR_MAX_SYMLINKS", 100)     # dead symlinks processed per sweep
REPAIR_LOAD_MAX         = _f("REPAIR_LOAD_MAX", 0)           # skip the whole repair sweep above this host 1-min load (0=off)
REPAIR_DEBRID_MOUNT     = os.environ.get("REPAIR_DEBRID_MOUNT", "")  # debrid mount root; non-empty means "check it's live before sweep"
REPAIR_ITEM_INTERVAL    = _dur(os.environ.get("REPAIR_ITEM_INTERVAL", "0"), 0)  # seconds to wait between each re-grab (0=off)
REPAIR_SEASON_PACKS     = _b("REPAIR_SEASON_PACKS", False)   # flag sonarr seasons spread across multiple dirs (non-season-pack)
REPAIR_UNMONITORED      = _b("REPAIR_UNMONITORED", False)    # include unmonitored series/movies in the repair sweep
REPAIR_MISSING_FROM_DISK = _b("REPAIR_MISSING_FROM_DISK", False)  # enable history-based missing-file re-grab
REPAIR_MFD_RECHECK       = _dur(os.environ.get("REPAIR_MFD_RECHECK", "24h"), 86400)  # cooldown per item before re-searching
REPAIR_VERIFY            = _b("REPAIR_VERIFY", False)              # enable post-repair grab verification
REPAIR_VERIFY_DEADLINE   = _dur(os.environ.get("REPAIR_VERIFY_DEADLINE", "4h"), 14400)  # give up after this long
REPAIR_ORPHAN_SCAN      = _b("REPAIR_ORPHAN_SCAN", True)             # report dead symlinks not tracked by *arr
REPAIR_HIERARCHICAL_SEARCH = _b("REPAIR_HIERARCHICAL_SEARCH", False)  # prefer series/season/episode searches based on airing status
REPAIR_HIERARCHICAL_FALLBACK = _b("REPAIR_HIERARCHICAL_FALLBACK", True)  # fall back to narrower search if wider search finds nothing
REPAIR_SEASON_ENDED_THRESHOLD = _dur(os.environ.get("REPAIR_SEASON_ENDED_THRESHOLD", "7d"), 604800)  # how long after last aired date to treat a season as ended
EN_RESCAN               = _b("ENABLE_RESCAN", False)
RESCAN_LIBRARY_PATHS    = [p.strip() for p in os.environ.get("RESCAN_LIBRARY_PATHS",
                           os.environ.get("REPAIR_LIBRARY_PATHS", "")).split(",") if p.strip()]
RESCAN_MAX_ACTIONS      = _i("RESCAN_MAX_ACTIONS", 5)        # partial Plex scans per sweep (keep low to avoid DB hammering)
RESCAN_SCAN_DELAY       = _i("RESCAN_SCAN_DELAY", 60)        # seconds between partial scans
RESCAN_MAX_WAIT         = _dur(os.environ.get("RESCAN_MAX_WAIT", "10m"), 600)  # max time to wait for Plex to finish a scan before giving up
RESCAN_COOLDOWN         = _dur(os.environ.get("RESCAN_COOLDOWN", "1h"), 3600)  # don't rescan same missing folder within this window
RESCAN_INTERVAL         = _dur(os.environ.get("RESCAN_INTERVAL", "15m"), 900)  # seconds between rescan sweeps
RESCAN_LOAD_MAX         = _f("RESCAN_LOAD_MAX", 0)           # skip sweep if 1-min load above this (0=off)
RESCAN_PLEX_RESPONSIVE_TIMEOUT = _f("RESCAN_PLEX_RESPONSIVE_TIMEOUT", 3.0)  # abort if Plex root ping takes longer than this
RESCAN_DECYPHARR_REPAIR_BACKOFF = _b("RESCAN_DECYPHARR_REPAIR_BACKOFF", True)  # skip sweep while decypharr repair is active
RESCAN_INCREMENTAL      = _b("RESCAN_INCREMENTAL", True)      # only scan files/parents changed since last sweep
RESCAN_FULL_INTERVAL    = _dur(os.environ.get("RESCAN_FULL_INTERVAL", "24h"), 86400)  # do a full walk this often
RESCAN_SECTIONS_CACHE_TTL = _dur(os.environ.get("RESCAN_SECTIONS_CACHE_TTL", "5m"), 300)  # cache Plex sections/locations
RESCAN_JANITOR_CANDIDATES = _b("RESCAN_JANITOR_CANDIDATES", True)  # use janitor dead-files as candidate parents
RESCAN_ARR_QUEUE_BACKOFF = _b("RESCAN_ARR_QUEUE_BACKOFF", True)  # skip sweep while *arr has active queue items
RESCAN_ARR_QUEUE_MAX = _i("RESCAN_ARR_QUEUE_MAX", 0)            # skip if any *arr queue exceeds this (0=any)
RESCAN_FULL_REFRESH_THRESHOLD = _i("RESCAN_FULL_REFRESH_THRESHOLD", 0)  # full-section refresh if >N parents missing (0=off)
TRIGGER_EVENTS = set(e.strip() for e in os.environ.get(
    "DOCTOR_TRIGGER_EVENTS", "Download,ManualInteractionRequired,DownloadFailed,Grab").split(",") if e.strip())
# Logging is configured in a separate module so config.py stays focused on env constants.
from .logging_config import log

# Re-export utils helpers for backward compatibility with any code that imports them
# from doctor.config. The canonical home is doctor.utils.
from doctor.utils import http_code, run_cmd, run_output, host_load  # noqa: F401

# Public surface for ``from doctor.config import *``: uppercase constants, the logger,
# and the backward-compatible utils re-exports.
__all__ = [n for n in dir() if n.isupper()]
__all__ += ["log", "http_code", "run_cmd", "run_output", "host_load"]

# ---- debridlink migration ----
EN_DEBRIDLINK_MIGRATION = _b("ENABLE_DEBRIDLINK_MIGRATION", False)
DBR_PROWLARR_URL = os.environ.get("DBR_PROWLARR_URL", "")
DBR_PROWLARR_APIKEY = os.environ.get("DBR_PROWLARR_APIKEY", "")
DBR_QBT_URL = os.environ.get("DBR_QBT_URL", "")
DBR_QBT_CATEGORY = os.environ.get("DBR_QBT_CATEGORY", "sonarr")
DBR_MAX_ACTIONS = _i("DBR_MAX_ACTIONS", 10)
DBR_RECHECK = _dur(os.environ.get("DBR_RECHECK", "12h"), 43200)
DBR_MIN_SEEDS = _i("DBR_MIN_SEEDS", 1)
DBR_SCAN_DELAY = _f("DBR_SCAN_DELAY", 10)
# ---- library maintainer ----
EN_MAINTAINER         = _b("ENABLE_MAINTAINER", False)
MAINTAINER_MAX_ACTIONS   = _i("MAINTAINER_MAX_ACTIONS", 5)
MAINTAINER_UNWATCHED_DAYS = _i("MAINTAINER_UNWATCHED_DAYS", 30)
MAINTAINER_MIN_YEAR       = _i("MAINTAINER_MIN_YEAR", 2024)
MAINTAINER_MIN_AGE_DAYS   = _i("MAINTAINER_MIN_AGE_DAYS", 30)
MAINTAINER_LIBRARY_TITLE  = os.environ.get("MAINTAINER_LIBRARY_TITLE", "shows")
MAINTAINER_PULSARR_TAG_PREFIX = os.environ.get("MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-")
MAINTAINER_MODE             = os.environ.get("MAINTAINER_MODE", "tagged").strip().lower()
MAINTAINER_PLEX_SECTION_KEY = _i("MAINTAINER_PLEX_SECTION_KEY", 0)
MAINTAINER_RECHECK         = _dur(os.environ.get("MAINTAINER_RECHECK", "24h"), 86400)
TAUTULLI_URL    = os.environ.get("TAUTULLI_URL", "")
TAUTULLI_APIKEY = os.environ.get("TAUTULLI_APIKEY", "")
PULSARR_URL     = os.environ.get("PULSARR_URL", "")
PULSARR_APIKEY  = os.environ.get("PULSARR_APIKEY", "")
PULSARR_DB_PATH = os.environ.get("PULSARR_DB_PATH", "")

DBR_MIGRATE_MODE = os.environ.get("# ---- library maintainer ----
EN_MAINTAINER         = _b("ENABLE_MAINTAINER", False)
MAINTAINER_MAX_ACTIONS   = _i("MAINTAINER_MAX_ACTIONS", 5)
MAINTAINER_UNWATCHED_DAYS = _i("MAINTAINER_UNWATCHED_DAYS", 30)
MAINTAINER_MIN_YEAR       = _i("MAINTAINER_MIN_YEAR", 2024)
MAINTAINER_MIN_AGE_DAYS   = _i("MAINTAINER_MIN_AGE_DAYS", 30)
MAINTAINER_LIBRARY_TITLE  = os.environ.get("MAINTAINER_LIBRARY_TITLE", "shows")
MAINTAINER_PULSARR_TAG_PREFIX = os.environ.get("MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-")
MAINTAINER_MODE             = os.environ.get("MAINTAINER_MODE", "tagged").strip().lower()
MAINTAINER_PLEX_SECTION_KEY = _i("MAINTAINER_PLEX_SECTION_KEY", 0)
MAINTAINER_RECHECK         = _dur(os.environ.get("MAINTAINER_RECHECK", "24h"), 86400)
TAUTULLI_URL    = os.environ.get("TAUTULLI_URL", "")
TAUTULLI_APIKEY = os.environ.get("TAUTULLI_APIKEY", "")
PULSARR_URL     = os.environ.get("PULSARR_URL", "")
PULSARR_APIKEY  = os.environ.get("PULSARR_APIKEY", "")
PULSARR_DB_PATH = os.environ.get("PULSARR_DB_PATH", "")

DBR_MIGRATE_MODE", "continuous").strip().lower()
# ---- library maintainer ----
EN_MAINTAINER         = _b("ENABLE_MAINTAINER", False)
MAINTAINER_MAX_ACTIONS   = _i("MAINTAINER_MAX_ACTIONS", 5)       # max shows deleted per sweep
MAINTAINER_UNWATCHED_DAYS = _i("MAINTAINER_UNWATCHED_DAYS", 30)   # must be unwatched for at least this many days
MAINTAINER_MIN_YEAR       = _i("MAINTAINER_MIN_YEAR", 2024)       # shows released before this year are eligible
MAINTAINER_MIN_AGE_DAYS   = _i("MAINTAINER_MIN_AGE_DAYS", 30)     # series must have been added to Sonarr at least this long ago
MAINTAINER_LIBRARY_TITLE  = os.environ.get("MAINTAINER_LIBRARY_TITLE", "shows")  # only delete from this Sonarr instance whose name contains this
MAINTAINER_PULSARR_TAG_PREFIX = os.environ.get("MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-")
MAINTAINER_MODE             = os.environ.get("MAINTAINER_MODE", "tagged").strip().lower()  # tagged | all
MAINTAINER_PLEX_SECTION_KEY = _i("MAINTAINER_PLEX_SECTION_KEY", 0)   # Plex section to empty trash on (all mode)
MAINTAINER_RECHECK         = _dur(os.environ.get("MAINTAINER_RECHECK", "24h"), 86400)  # cooldown before a previously-flagged series is reconsidered
TAUTULLI_URL    = os.environ.get("TAUTULLI_URL", "")
TAUTULLI_APIKEY = os.environ.get("TAUTULLI_APIKEY", "")
PULSARR_URL     = os.environ.get("PULSARR_URL", "")
PULSARR_APIKEY  = os.environ.get("PULSARR_APIKEY", "")
PULSARR_DB_PATH = os.environ.get("PULSARR_DB_PATH", "")  # direct sqlite3 access to pulsarr.db for watchlist record deletion
