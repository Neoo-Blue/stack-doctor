"""Check: seerr."""
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

def check_seerr():
    if not SEERR_URL or not SEERR_APIKEY:
        return
    s = Seerr(SEERR_URL, SEERR_APIKEY)
    reqs = s.failed()
    if reqs is None:
        log.error("[seerr] %s unreachable", SEERR_URL); return
    if not reqs:
        log.info("[seerr] no failed requests"); return
    with state_transaction() as state:
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
            if SEERR_MAX_TRIES and n >= SEERR_MAX_TRIES:
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
        live = set(str(r.get("id")) for r in reqs)
        for k in [k for k in tries if k not in live]:
            tries.pop(k, None)
        if acted:
            log.info("[seerr] re-drove %d failed request(s)", acted)
