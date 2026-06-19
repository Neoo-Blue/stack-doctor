"""Check: queue."""
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
from ..config import *
from ..clients import *
from ..state import *

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
def check_queue(only=None):
    if LOAD_MAX > 0 and host_load() > LOAD_MAX:
        log.info("[queue] host load > %.0f -> skipping", LOAD_MAX); return
    with state_transaction() as state:
        actions = 0
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
                stuck += 1; iid = str(r.get("id")); cnt = strikes.get(iid, 0) + 1; new[iid] = cnt
                if cnt >= MIN_STRIKES and actions < MAX_ACTIONS:
                    title = (r.get("title") or "")[:70]
                    if DRY_RUN:
                        log.info("[queue:%s] WOULD remove (%s strike %d): %s", arr.name, reason, cnt, title)
                    else:
                        parked = _churn_record(state, arr, r, title)   # un-monitor first so the remove can't re-search
                        try:
                            arr.remove(r["id"]); actions += 1; new.pop(iid, None)
                            log.info("[queue:%s] removed (%s, blocklist=%s)%s: %s", arr.name, reason, BLOCKLIST,
                                     " [parked, no re-search]" if parked else " -> re-search", title)
                        except Exception as e:
                            log.warning("[queue:%s] remove failed: %s", arr.name, e)
            state[arr.name] = new
            if stuck:
                log.info("[queue:%s] %d stuck tracked, %d acted", arr.name, stuck, actions)
            for h in arr.health():
                if h.get("type") in ("error", "warning"):
                    log.debug("[queue:%s] health %s: %s", arr.name, h.get("type"), (h.get("message") or "")[:90])
