"""Configuration, logging, and small generic helpers (env-driven)."""
import os
import sys
import json
import re
import time
import signal
import subprocess
import threading
import logging
import logging.handlers
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

VERSION = "0.3"
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
INTERVAL    = _i("DOCTOR_INTERVAL", 900)                              # default/fallback interval; kept for compatibility
PORT        = _i("DOCTOR_PORT", 8088)                                 # webhook port (event mode)
UI_PORT     = _i("DOCTOR_UI_PORT", 12345)                            # web dashboard port
EN_UI       = _b("ENABLE_UI", False)
UI_TOKEN    = os.environ.get("DOCTOR_UI_TOKEN", "")                   # optional ?token= / X-Doctor-Token gate
LOG_LEVEL   = os.environ.get("DOCTOR_LOG_LEVEL", "INFO").upper()
LOG_FILE    = os.environ.get("DOCTOR_LOG_FILE", "")
TIMEOUT     = _i("DOCTOR_HTTP_TIMEOUT", 60)
DRY_RUN     = _b("DOCTOR_DRY_RUN", False)
FAST_INTERVAL        = _dur(os.environ.get("DOCTOR_FAST_INTERVAL", "180s"), 180)     # 3 min
SLOW_INTERVAL        = _dur(os.environ.get("DOCTOR_SLOW_INTERVAL", "1800s"), 1800)   # 30 min
SCHEDULER_TICK       = _dur(os.environ.get("DOCTOR_SCHEDULER_TICK", "30s"), 30)      # how often scheduler wakes
SCHEDULER_CONCURRENCY = _i("DOCTOR_SCHEDULER_CONCURRENCY", 3)                          # max parallel scheduled checks
def _check_interval(cid, speed):
    per = os.environ.get("%s_INTERVAL" % cid.upper())
    if per:
        return _dur(per, INTERVAL)
    return FAST_INTERVAL if speed == "fast" else SLOW_INTERVAL
EN_QUEUE      = _b("ENABLE_QUEUE", True)
EN_DECYPHARR  = _b("ENABLE_DECYPHARR", False)
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
MS_MAX_ACTIONS        = _i("MISSING_SEASONS_MAX_ACTIONS", 5)      # SeasonSearches per sweep
MS_RECHECK            = _dur(os.environ.get("MISSING_SEASONS_RECHECK", "24h"), 86400)  # cooldown between re-searching same season
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
TRIGGER_EVENTS = set(e.strip() for e in os.environ.get(
    "DOCTOR_TRIGGER_EVENTS", "Download,ManualInteractionRequired,DownloadFailed,Grab").split(",") if e.strip())
handlers = [logging.StreamHandler(sys.stdout)]
if LOG_FILE:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3))
    except Exception:
        pass
class _ColorFormatter(logging.Formatter):
    _GREY    = "\033[90m"
    _GREEN   = "\033[32m"
    _YELLOW  = "\033[33m"
    _RED     = "\033[31m"
    _BRED    = "\033[1;31m"
    _CYAN    = "\033[36m"
    _RESET   = "\033[0m"
    _LEVEL   = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    def format(self, record):
        # Let the base class assemble the full message, including exc_info/exc_text/stack_info
        full  = super().format(record)
        ts    = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        lvl   = record.levelname
        lc    = self._LEVEL.get(lvl, "")
        # The base formatter produces "ts | LEVEL | name | msg[\ntraceback]"
        # We replace only the first line's header; any trailing traceback lines are kept as-is
        first_line, *rest = full.splitlines()
        header = (f"{self._GREY}{ts}{self._RESET} "
                  f"{lc}| {lvl:<7} |{self._RESET} "
                  f"{self._CYAN}{record.name}{self._RESET} | "
                  f"{record.getMessage()}")
        lines = [header] + rest
        return "\n".join(lines)
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(_ColorFormatter())
handlers_colored = [_console]
if len(handlers) > 1:                         # file handler was added
    handlers_colored.append(handlers[-1])     # keep rotating file handler (no colour)
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    handlers=handlers_colored)
log = logging.getLogger("doctor")
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

__all__ = [n for n in dir() if not n.startswith("__") and not isinstance(globals()[n], type(os))]
