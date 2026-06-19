"""Optional web dashboard: status, health, warmer stats, config editor, logs."""
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
from .clients import *
from .checks import *
from .checks.plex import _plex_rescan, _plex_empty_trash
from .checks import warmer as _warmer
from .scheduler import CHECKS, sweep, _run_scheduled_check


UI_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html"), encoding="utf-8").read()

_SECRET_HINT = ("APIKEY", "API_KEY", "TOKEN", "PASSWORD", "PASS", "SECRET")
UI_SCHEMA = [
    ("Mode", [("DOCTOR_MODE", "cron|event"), ("DOCTOR_INTERVAL", "900"),
              ("DOCTOR_FAST_INTERVAL", "180s"), ("DOCTOR_SLOW_INTERVAL", "1800s"),
              ("DOCTOR_SCHEDULER_TICK", "30s"), ("DOCTOR_SCHEDULER_CONCURRENCY", "3"),
              ("DOCTOR_DRY_RUN", "false"), ("DOCTOR_LOG_LEVEL", "INFO")]),
    ("Checks (on/off)", [("ENABLE_QUEUE", ""), ("ENABLE_PROVIDERS", ""), ("ENABLE_DECYPHARR", ""),
              ("ENABLE_PLEX", ""), ("ENABLE_PLEX_SCAN", ""), ("ENABLE_RESOURCES", ""),
              ("ENABLE_JANITOR", ""), ("ENABLE_REPAIR", ""), ("ENABLE_BAZARR", ""),
              ("ENABLE_SEERR", ""), ("ENABLE_WARMER", ""),
              ("ENABLE_MISSING_SEASONS", ""), ("ENABLE_NO_UPGRADE_PROFILE", "")]),
    ("Plex scan recovery", [("PLEX_SCAN_STUCK_AFTER", "30m"), ("PLEX_SCAN_CANCEL", "true|false")]),
    ("Repair (dead-file re-grab)", [("REPAIR_LIBRARY_PATHS", "/mnt/library/movies,/mnt/library/tv"),
              ("REPAIR_MAX_ACTIONS", "20"), ("REPAIR_MAX_SYMLINKS", "100"), ("REPAIR_LOAD_MAX", "0"),
              ("REPAIR_DEBRID_MOUNT", ""),
              ("REPAIR_ITEM_INTERVAL", "0"), ("REPAIR_SEASON_PACKS", "false"),
              ("REPAIR_UNMONITORED", "false"),
              ("REPAIR_MISSING_FROM_DISK", "false"), ("REPAIR_MFD_RECHECK", "24h"),
              ("REPAIR_VERIFY", "false"), ("REPAIR_VERIFY_DEADLINE", "4h")]),
    ("Missing Seasons", [("MISSING_SEASONS_MIN_AGE_HOURS", "1"), ("MISSING_SEASONS_MAX_ACTIONS", "5"),
              ("MISSING_SEASONS_RECHECK", "24h")]),
    ("No-Upgrade Profile", [("NO_UPGRADE_PROFILE_NAME", "WEB-1080p (No Upgrade)"),
              ("NO_UPGRADE_PROFILE_ID", "0")]),
    ("Seerr (failed-request retry)", [("SEERR_URL", "http://overseerr:5055"), ("SEERR_APIKEY", ""),
              ("SEERR_RETRY_MAX", "10"), ("SEERR_MAX_ATTEMPTS", "5")]),

    ("Queue / churn brake", [("DOCTOR_MIN_STRIKES", "2"), ("DOCTOR_MAX_ACTIONS", "20"), ("DOCTOR_BLOCKLIST", "true"),
              ("DOCTOR_CHURN_LIMIT", "0"), ("DOCTOR_CHURN_ACTION", "report|park|backoff"), ("DOCTOR_CHURN_BACKOFF", "10m,1h,24h")]),
    ("Warmer", [("WARMER_PRECACHE_MB", "64"), ("WARMER_TAIL_MB", "8"), ("WARMER_SOURCES", "ondeck,next"),
              ("WARMER_ONDECK", "true|false"), ("WARMER_MAX_PER_CYCLE", "40"), ("WARMER_NEXT_EPISODES", "1"),
              ("WARMER_COOLDOWN", "3600"), ("WARMER_LOAD_MAX", "0")]),
    ("Resources", [("RES_LOAD_WARN", "40"), ("RES_SWAP_WARN_MB", "7000"), ("RES_MEM_MIN_MB", "800")]),
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
    checks = [{"name": n, "on": bool(e)} for n, e, _, _ in CHECKS]
    checks.append({"name": "warmer", "on": _b("ENABLE_WARMER", False) and bool(PLEX_URL)})
    checks.append({"name": "detail-page warm", "on": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE)})
    return {"version": VERSION, "mode": MODE, "dry_run": DRY_RUN, "load": round(host_load(), 2), "checks": checks}
def _ui_warmer():
    rec = [{"title": r["title"], "why": r["why"], "ago": int(time.time() - r["ts"])} for r in reversed(_warmer._warm_recent)]
    return {"enabled": _b("ENABLE_WARMER", False) and bool(PLEX_URL),
            "detail_page": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE),
            "total": _warmer._warm_count[0], "recent": rec[:40]}
def _ui_config():
    groups = []
    for g, items in UI_SCHEMA:
        rows = [{"key": k, "val": ("" if _is_secret(k) else os.environ.get(k, "")), "ph": ph, "secret": _is_secret(k)}
                for k, ph in items]
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

            if path == "/api/config":           return self._send(200, "application/json", json.dumps(_ui_config()))
            if path == "/api/logs":
                try: n = min(int(parse_qs(urlparse(self.path).query).get("n", ["300"])[0]), 3000)
                except Exception: n = 300
                return self._send(200, "text/plain; charset=utf-8", _ui_logs(n))
            return self._send(404, "text/plain", "nf")
        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            if path in ("/api/config", "/api/restart",
                        "/api/plex/rescan", "/api/plex/emptytrash", "/api/sweep") or path.startswith("/api/check/"):
                if not EN_UI or not self._authed():
                    return self._send(401, "text/plain", "unauthorized")
                if path == "/api/config":
                    ok, msg = _ui_save(body)
                    return self._send(200 if ok else 400, "application/json", json.dumps({"ok": ok, "msg": msg}))
                if path == "/api/plex/rescan":
                    threading.Thread(target=_plex_rescan, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex rescan started"}))
                if path == "/api/plex/emptytrash":
                    threading.Thread(target=_plex_empty_trash, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex empty trash started"}))
                if path == "/api/sweep":
                    threading.Thread(target=sweep, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "sweep started"}))
                if path.startswith("/api/check/"):
                    cid = path.split("/api/check/", 1)[1]
                    for name, en, fn, _ in CHECKS:
                        if name == cid and en:
                            threading.Thread(target=_run_scheduled_check, args=(cid, fn), daemon=True).start()
                            return self._send(202, "application/json", json.dumps({"ok": True, "msg": "check %s started" % cid}))
                    return self._send(400, "application/json", json.dumps({"ok": False, "msg": "unknown or disabled check"}))
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
