"""Scheduler: per-check intervals, bounded concurrency, and the full sweep."""
import time
import threading
from collections import namedtuple
from typing import Optional, Callable, Any
from .config import (
    EN_BAZARR, EN_DECYPHARR, EN_JANITOR, EN_MISSING_SEASONS,
    EN_NO_UPGRADE_PROFILE, EN_PLEX, EN_PLEX_SCAN, EN_PROVIDERS,
    EN_QUEUE, EN_REPAIR, EN_RESOURCES, EN_SEERR,
    FAST_INTERVAL, MULTIPACK_ENABLED, SCHEDULER_CONCURRENCY,
    SCHEDULER_TICK, SLOW_INTERVAL,
    _check_interval, _human, log,
)
from .checks import (  # check_* functions referenced by CHECKS
    check_bazarr,
    check_decypharr,
    check_janitor,
    check_missing_seasons,
    check_multipack,
    check_no_upgrade_profile,
    check_plex,
    check_plex_scan,
    check_providers,
    check_queue,
    check_repair,
    check_resources,
    check_seerr,
)

# Descriptor for a scheduled check.
# Fields:
#   cid         – unique string id; used for logging and env-var lookup (<CID>_INTERVAL)
#   enabled     – bool from config (EN_* constant); False means the check never runs
#   fn          – the check_* function to call each cycle
#   speed       – "fast" or "slow"; selects FAST_INTERVAL / SLOW_INTERVAL when no override
#   default_iv  – optional int (seconds) that overrides speed without touching os.environ;
#                 still overrideable by a <CID>_INTERVAL env var
#
# Using a namedtuple makes field access self-documenting and turns wrong-width table
# edits into a TypeError at import time rather than a ValueError mid-sweep.
CheckEntry = namedtuple("CheckEntry", ["cid", "enabled", "fn", "speed", "default_iv", "needs_instances"])

CHECKS = [CheckEntry("queue",              EN_QUEUE,              check_queue,              "fast", None, True),
          CheckEntry("providers",          EN_PROVIDERS,          check_providers,          "fast", None, True),
          CheckEntry("decypharr",          EN_DECYPHARR,          check_decypharr,          "fast", None, False),
          CheckEntry("plex",               EN_PLEX,               check_plex,               "fast", None, False),
          CheckEntry("plexscan",           EN_PLEX_SCAN,          check_plex_scan,          "fast", None, False),
          CheckEntry("resources",          EN_RESOURCES,          check_resources,          "fast", None, False),
          CheckEntry("janitor",            EN_JANITOR,            check_janitor,            "slow", None, False),
          CheckEntry("repair",             EN_REPAIR,             check_repair,             "slow", None, True),
          CheckEntry("bazarr",             EN_BAZARR,             check_bazarr,             "fast", None, False),
          CheckEntry("seerr",              EN_SEERR,              check_seerr,              "fast", None, False),
          CheckEntry("missing_seasons",    EN_MISSING_SEASONS,    check_missing_seasons,    "slow", 900,   True),   # 15 min default
          CheckEntry("no_upgrade_profile", EN_NO_UPGRADE_PROFILE, check_no_upgrade_profile, "slow", None, True),
          CheckEntry("multipack",          MULTIPACK_ENABLED,     check_multipack,          "slow", None, True)]
_check_locks = {cid: threading.Lock() for cid, _, _, _, _, _ in CHECKS}
_scheduler_sem = threading.Semaphore(max(1, SCHEDULER_CONCURRENCY))
_lock = threading.Lock()

# Per-check run metadata.  Keyed by cid; populated by _run_scheduled_check and sweep().
# Shape: {last_start, last_end, last_duration, last_outcome, last_error, run_count, error_count}
# last_outcome values: "ok" | "error" | "skipped" | "deferred"
_check_runs: dict = {}

def _record_run(cid: str, start: float, end: float, outcome: str, error: str = "") -> None:
    """Update _check_runs for cid in-place (thread-safe: GIL-atomic dict update)."""
    r = _check_runs.get(cid)
    if r is None:
        r = {"run_count": 0, "error_count": 0}
        _check_runs[cid] = r
    r["last_start"]    = start
    r["last_end"]      = end
    r["last_duration"] = round(end - start, 3)
    r["last_outcome"]  = outcome
    r["last_error"]    = error
    if outcome in ("ok", "error"):
        r["run_count"] += 1
    if outcome == "error":
        r["error_count"] += 1

__all__ = ["CHECKS", "CheckEntry", "_check_runs", "scheduler_loop", "sweep", "_run_scheduled_check"]

def sweep(only: Optional[Any] = None) -> None:
    if not _lock.acquire(blocking=False):
        log.debug("sweep already running"); return
    log.info("[sweep] starting initial sweep of %d enabled check(s)", sum(1 for _, e, _, _, _, _ in CHECKS if e))
    try:
        for cid, en, fn, _, _, _ in CHECKS:
            if not en:
                continue
            log.info("[sweep] running %s", cid)
            _t0 = time.time()
            try:
                fn(only) if cid == "queue" else fn()
                _record_run(cid, _t0, time.time(), "ok")
            except Exception as e:
                _record_run(cid, _t0, time.time(), "error", str(e)[:200])
                log.error("[%s] check error: %s", cid, e)
            log.info("[sweep] finished %s", cid)
    finally:
        _lock.release()
        log.info("[sweep] initial sweep complete")
def _run_scheduled_check(cid: str, fn: Callable[[], None]) -> None:
    """Run a single scheduled check with per-check locking and bounded concurrency."""
    lock = _check_locks.get(cid)
    if lock and not lock.acquire(blocking=False):
        log.debug("[%s] already running, skipping scheduled run", cid)
        _record_run(cid, time.time(), time.time(), "skipped")
        return
    acquired = False
    t0 = time.time()
    try:
        if not _scheduler_sem.acquire(blocking=False):
            log.info("[%s] scheduler concurrency full, deferring", cid)
            _record_run(cid, t0, time.time(), "deferred")
            return
        acquired = True
        log.info("[%s] running scheduled check", cid)
        fn()
        _record_run(cid, t0, time.time(), "ok")
    except Exception as e:
        _record_run(cid, t0, time.time(), "error", str(e)[:200])
        log.error("[%s] scheduled check error: %s", cid, e)
    finally:
        if acquired:
            log.info("[%s] scheduled check finished", cid)
            _scheduler_sem.release()
        if lock:
            lock.release()
def scheduler_loop(stop: threading.Event) -> None:
    """Background loop that runs each enabled check on its own interval.
    An initial full sweep runs on startup, then checks are dispatched independently
    so fast checks (queue, providers, plex, ...) run every few minutes while slow
    checks (repair, janitor, missing_seasons, no_upgrade_profile) run every 30 min."""
    log.info("[scheduler] fast=%s, slow=%s, tick=%s, concurrency=%d",
             _human(FAST_INTERVAL), _human(SLOW_INTERVAL), _human(SCHEDULER_TICK), SCHEDULER_CONCURRENCY)
    sweep()
    now = time.time()
    last_run = {cid: now for cid, en, _, _, _, _ in CHECKS if en}
    while not stop.wait(SCHEDULER_TICK):
        now = time.time()
        for cid, en, fn, speed, default_iv, _ in CHECKS:
            if not en:
                continue
            interval = _check_interval(cid, speed, default_iv)
            if now - last_run.get(cid, 0) >= interval:
                elapsed = now - last_run.get(cid, 0)
                last_run[cid] = now
                log.info("[scheduler] dispatching %s (interval=%s, last=%.0fs ago)", cid, _human(interval), elapsed)
                threading.Thread(target=_run_scheduled_check, args=(cid, fn), daemon=True).start()
