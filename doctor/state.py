"""Persistent JSON state with an atomic transaction lock + churn-brake bookkeeping."""
import contextlib
import json
import os
import time
import threading
from .config import CHURN_ACTION, CHURN_BACKOFF, CHURN_LIMIT, STATE_FILE, _human, log
from .clients import INSTANCES


# Single process-wide lock guarding read-modify-write cycles on the shared state file.
# The scheduler runs checks concurrently; without this, two checks could each load the
# state, modify their own slice, and the second save would clobber the first.
STATE_LOCK = threading.RLock()

@contextlib.contextmanager
def state_transaction():
    """Load the persistent state, yield it for modification, then atomically save it.

    Any code that reads the state, makes decisions based on it, and writes it back
    should use this context manager. A per-call lock around _load_state/_save_state
    is not enough because the lock is released between load and save, allowing a
    concurrent check to overwrite the changes.
    """
    with STATE_LOCK:
        state = _load_state_unlocked()
        try:
            yield state
        except Exception:
            # Don't persist a partially modified state if the check failed.
            raise
        else:
            _save_state_unlocked(state)

def _load_state():
    with STATE_LOCK:
        return _load_state_unlocked()

def _save_state(s):
    with STATE_LOCK:
        _save_state_unlocked(s)

def _load_state_unlocked():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_state_unlocked(s):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(s, f)
    except Exception as e:
        log.warning("state save failed: %s", e)
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

__all__ = [n for n in dir() if not n.startswith("__") and not isinstance(globals()[n], type(os))]
