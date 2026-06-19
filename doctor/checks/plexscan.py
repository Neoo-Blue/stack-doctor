"""Check: plexscan."""
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
from .decypharr import _decy_restart, _read_test

_scan_seen = {}              # activity uuid -> {first, prog, prog_ts, title, acted_ts}
_plex_last_restart = [0.0]
def _is_scan_activity(a):
    t = (a.get("type") or "").lower()
    txt = ((a.get("title") or "") + " " + (a.get("subtitle") or "")).lower()
    if "scan" in txt:
        return True
    return t.startswith("library.update") or t.startswith("library.refresh")
def check_plex_scan():
    if not (PLEX_URL and PLEX_TOKEN):
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    acts = plex.activities()
    now = time.time(); cur = set(); stuck = []
    for a in acts:
        if not _is_scan_activity(a):
            continue
        uuid = a.get("uuid") or ""
        if not uuid:
            continue
        cur.add(uuid)
        try: prog = int(float(a.get("progress") or 0))
        except Exception: prog = 0
        title = (a.get("title") or a.get("subtitle") or "library scan")[:80]
        s = _scan_seen.setdefault(uuid, {"first": now, "prog": -1, "prog_ts": now, "title": title, "acted_ts": 0})
        if prog > s["prog"]:
            s["prog"] = prog; s["prog_ts"] = now           # progress advanced -> not stuck, reset the clock
        s["title"] = title
        if now - s["prog_ts"] >= PLEX_SCAN_STUCK:
            stuck.append((uuid, a, s))
    for u in list(_scan_seen):                              # forget scans that finished / disappeared
        if u not in cur:
            _scan_seen.pop(u, None)
    if not stuck:
        if cur:
            log.info("[plexscan] %d scan(s) running, progressing", len(cur))
        return
    for uuid, a, s in stuck:
        if now - s.get("acted_ts", 0) < PLEX_SCAN_STUCK:   # one recovery attempt per stuck-window; don't hammer
            continue
        s["acted_ts"] = now
        mins = int((now - s["prog_ts"]) / 60)
        log.error("[plexscan] STUCK scan '%s' (no progress for %dm, stalled at %d%%)", s["title"], mins, max(s["prog"], 0))
        if DRY_RUN:
            log.info("[plexscan] DRY-RUN: would fix mount + cancel scan"); continue
        # 1) root cause: a hung decypharr mount blocks the scanner on I/O
        if DECY_MOUNT_TEST and _read_test(DECY_MOUNT_TEST, DECY_READ_TIMEOUT) is False:
            log.error("[plexscan] decypharr mount is hung -> restarting it (the usual cause of a wedged scan)")
            _decy_restart("plex scan wedged on hung mount")
        # 2) cancel the wedged scan so Plex stops blocking on the bad item
        cancelled = False
        if PLEX_SCAN_CANCEL and (a.get("cancellable") in ("1", "true", None)):
            if plex.cancel_activity(uuid):
                log.warning("[plexscan] cancelled stuck scan '%s'", s["title"])
                cancelled = True
            else:
                log.warning("[plexscan] cancel failed for '%s'", s["title"])
        # 3) last resort: restart Plex if a scan stays wedged well past the threshold AND cancellation didn't succeed
        if (PLEX_RESTART_CMD and now - s["first"] >= PLEX_SCAN_STUCK * 2 and 
            now - _plex_last_restart[0] > 1800 and not cancelled):
            log.error("[plexscan] scan still wedged -> restarting Plex: %s", PLEX_RESTART_CMD)
            rc = run_cmd(PLEX_RESTART_CMD); _plex_last_restart[0] = time.time()
            log.error("[plexscan] Plex restart rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")
