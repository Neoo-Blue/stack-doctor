"""Scheduler: per-check intervals, bounded concurrency, and the full sweep."""
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
from .config import *
from .checks import *  # check_* functions referenced by CHECKS

CHECKS = [("queue", EN_QUEUE, check_queue, "fast"),
          ("providers", EN_PROVIDERS, check_providers, "fast"),
          ("decypharr", EN_DECYPHARR, check_decypharr, "fast"),
          ("plex", EN_PLEX, check_plex, "fast"),
          ("plexscan", EN_PLEX_SCAN, check_plex_scan, "fast"),
          ("resources", EN_RESOURCES, check_resources, "fast"),
          ("janitor", EN_JANITOR, check_janitor, "slow"),
          ("repair", EN_REPAIR, check_repair, "slow"),
          ("bazarr", EN_BAZARR, check_bazarr, "fast"),
          ("seerr", EN_SEERR, check_seerr, "fast"),
          ("missing_seasons", EN_MISSING_SEASONS, check_missing_seasons, "slow"),
          ("no_upgrade_profile", EN_NO_UPGRADE_PROFILE, check_no_upgrade_profile, "slow")]
_check_locks = {cid: threading.Lock() for cid, _, _, _ in CHECKS}
_scheduler_sem = threading.Semaphore(max(1, SCHEDULER_CONCURRENCY))
_lock = threading.Lock()
def sweep(only=None):
    if not _lock.acquire(blocking=False):
        log.debug("sweep already running"); return
    try:
        for cid, en, fn, _ in CHECKS:
            if not en:
                continue
            try:
                fn(only) if cid == "queue" else fn()
            except Exception as e:
                log.error("[%s] check error: %s", cid, e)
    finally:
        _lock.release()
def _run_scheduled_check(cid, fn):
    """Run a single scheduled check with per-check locking and bounded concurrency."""
    lock = _check_locks.get(cid)
    if lock and not lock.acquire(blocking=False):
        log.debug("[%s] already running, skipping scheduled run", cid)
        return
    acquired = False
    try:
        if not _scheduler_sem.acquire(blocking=False):
            log.debug("[%s] scheduler concurrency full, deferring", cid)
            return
        acquired = True
        log.debug("[%s] running scheduled check", cid)
        fn()
    except Exception as e:
        log.error("[%s] scheduled check error: %s", cid, e)
    finally:
        if acquired:
            _scheduler_sem.release()
        if lock:
            lock.release()
def scheduler_loop(stop):
    """Background loop that runs each enabled check on its own interval.
    An initial full sweep runs on startup, then checks are dispatched independently
    so fast checks (queue, providers, plex, ...) run every few minutes while slow
    checks (repair, janitor, missing_seasons, no_upgrade_profile) run every 30 min."""
    log.info("[scheduler] fast=%s, slow=%s, tick=%s, concurrency=%d",
             _human(FAST_INTERVAL), _human(SLOW_INTERVAL), _human(SCHEDULER_TICK), SCHEDULER_CONCURRENCY)
    sweep()
    now = time.time()
    last_run = {cid: now for cid, en, _, _ in CHECKS if en}
    while not stop.wait(SCHEDULER_TICK):
        now = time.time()
        for cid, en, fn, speed in CHECKS:
            if not en:
                continue
            interval = _check_interval(cid, speed)
            if now - last_run.get(cid, 0) >= interval:
                last_run[cid] = now
                threading.Thread(target=_run_scheduled_check, args=(cid, fn), daemon=True).start()
