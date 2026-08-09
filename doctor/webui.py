"""Optional web dashboard: status, health, warmer stats, config editor, logs."""
import os
import json
import time
import threading
from .config import (
    BAZARR_APIKEY, BAZARR_URL, CONFIG_FILE, DECY_URL, DRY_RUN, EN_UI,
    LOG_FILE, MODE, PLEX_TOKEN, PLEX_URL, PULSARR_APIKEY, PULSARR_URL,
    RESCAN_LIBRARY_PATHS, SEERR_APIKEY, SEERR_URL, TAUTULLI_APIKEY, TAUTULLI_URL,
    TRIGGER_EVENTS, UI_TOKEN, VERSION, WARM_PLEXLOG_CMD, WARM_PLEXLOG_FILE,
    _b, host_load, http_code, log,
)
from .clients import INSTANCES
from .actions.plex import plex_rescan, plex_empty_trash
from .checks.rescan import rescan_backlog
from .checks import warmer as _warmer

from .scheduler import CHECKS, _check_runs, sweep, _run_scheduled_check
from .state import _load_state


UI_HTML = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html"), encoding="utf-8").read()

_SECRET_HINT = ("APIKEY", "API_KEY", "TOKEN", "PASSWORD", "PASS", "SECRET")
UI_SCHEMA = [
    ("Mode", [("DOCTOR_MODE", "cron|event"), ("DOCTOR_INTERVAL", "900"),
              ("DOCTOR_FAST_INTERVAL", "180s"), ("DOCTOR_SLOW_INTERVAL", "1800s"),
              ("DOCTOR_SCHEDULER_TICK", "30s"), ("DOCTOR_SCHEDULER_CONCURRENCY", "3"),
              ("DOCTOR_DRY_RUN", "false"), ("DOCTOR_LOG_LEVEL", "INFO")]),
    ("Checks (on/off)", [("ENABLE_QUEUE", ""), ("ENABLE_PROVIDERS", ""), ("ENABLE_DECYPHARR", ""),
              ("ENABLE_DECYPHARR_PROVIDERS", ""),
              ("ENABLE_PLEX", ""), ("ENABLE_PLEX_SCAN", ""), ("ENABLE_RESOURCES", ""),
              ("ENABLE_JANITOR", ""), ("ENABLE_REPAIR", ""), ("ENABLE_BAZARR", ""),
              ("ENABLE_SEERR", ""), ("ENABLE_WARMER", ""),
              ("ENABLE_MISSING_SEASONS", ""), ("ENABLE_NO_UPGRADE_PROFILE", ""),
              ("ENABLE_MAINTAINER", ""), ("ENABLE_FORCE_IMPORT", "")]),
    ("Decypharr provider health", [("DECYPHARR_PROVIDERS_THRESHOLD", "5"), ("DECYPHARR_PROVIDERS_WINDOW", "10m"),
              ("DECYPHARR_PROVIDERS_COOLDOWN", "1h"), ("DECYPHARR_PROVIDERS_AUTO_DISABLE", "true|false"),
              ("DECYPHARR_PROVIDERS_REENABLE", "true|false"), ("DECYPHARR_PROVIDERS_RESTART_CMD", "docker restart decypharr")]),
    ("Plex scan recovery", [("PLEX_SCAN_STUCK_AFTER", "30m"), ("PLEX_SCAN_CANCEL", "true|false")]),
    ("Plex rescan (missing files)", [("ENABLE_RESCAN", ""), ("RESCAN_LIBRARY_PATHS", "/mnt/library/movies,/mnt/library/tv"),
              ("RESCAN_MAX_ACTIONS", "20"), ("RESCAN_SCAN_DELAY", "5")]),
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
    ("("Library Maintainer", [("TAUTULLI_URL", "http://tautulli:8181"), ("TAUTULLI_APIKEY", ""),
              ("PULSARR_URL", "http://pulsarr:3003"), ("PULSARR_APIKEY", ""),
              ("PULSARR_DB_PATH", "/var/lib/docker/volumes/pulsarr-config/_data/db/pulsarr.db"),
              ("MAINTAINER_MAX_ACTIONS", "5"), ("MAINTAINER_UNWATCHED_DAYS", "30"),
              ("MAINTAINER_MIN_YEAR", "2024"), ("MAINTAINER_MIN_AGE_DAYS", "30"),
              ("MAINTAINER_MODE", "tagged|all"), ("MAINTAINER_PLEX_SECTION_KEY", "0"),
              ("MAINTAINER_LIBRARY_TITLE", "shows"),
              ("MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"),
              ("MAINTAINER_RECHECK", "24h")]),

    ("Seerr (failed-request retry)", [("SEERR_URL", "http://overseerr:5055"), ("SEERR_APIKEY", ""),
              ("SEERR_RETRY_MAX", "10"), ("SEERR_MAX_ATTEMPTS", "5")]),
    ("Library Maintainer", [("TAUTULLI_URL", "http://tautulli:8181"), ("TAUTULLI_APIKEY", ""),
              ("PULSARR_URL", "http://pulsarr:3003"), ("PULSARR_APIKEY", ""),
              ("PULSARR_DB_PATH", "/var/lib/docker/volumes/pulsarr-config/_data/db/pulsarr.db"),
              ("MAINTAINER_MAX_ACTIONS", "5"), ("MAINTAINER_UNWATCHED_DAYS", "30"),
              ("MAINTAINER_MIN_YEAR", "2024"), ("MAINTAINER_MIN_AGE_DAYS", "30"),
              ("MAINTAINER_MODE", "tagged|all"), ("MAINTAINER_PLEX_SECTION_KEY", "0"),
              ("MAINTAINER_LIBRARY_TITLE", "shows"),
              ("MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"),
              ("MAINTAINER_RECHECK", "24h")]),

    ("Queue / churn brake", [("DOCTOR_MIN_STRIKES", "2"), ("DOCTOR_MAX_ACTIONS", "20"), ("DOCTOR_BLOCKLIST", "true"),
              ("DOCTOR_CHURN_LIMIT", "0"), ("DOCTOR_CHURN_ACTION", "report|park|backoff"), ("DOCTOR_CHURN_BACKOFF", "10m,1h,24h")]),
    ("Warmer", [("WARMER_PRECACHE_MB", "64"), ("WARMER_TAIL_MB", "8"), ("WARMER_SOURCES", "ondeck,next"),
              ("WARMER_ONDECK", "true|false"), ("WARMER_MAX_PER_CYCLE", "40"), ("WARMER_NEXT_EPISODES", "1"),
              ("WARMER_COOLDOWN", "3600"), ("WARMER_LOAD_MAX", "0")]),
    ("Resources", [("RES_LOAD_WARN", "40"), ("RES_SWAP_WARN_MB", "7000"), ("RES_MEM_MIN_MB", "800")]),
]
UI_KEYS = set(k for _, items in UI_SCHEMA for k, _ in items)

__all__ = ["_build_server"]

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
    if TAUTULLI_URL:
        jobs.append(("tautulli", "tautulli", lambda: (http_code(
            TAUTULLI_URL.rstrip("/") + "/api/v2?apikey=" + TAUTULLI_APIKEY + "&cmd=get_activity",
            t=5) == 200, "")))
    if PULSARR_URL:
        jobs.append(("pulsarr", "pulsarr", lambda: (http_code(
            PULSARR_URL.rstrip("/") + "/v1/system/health",
            headers={"x-api-key": PULSARR_APIKEY}, t=5) == 200, "")))
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
_RUN_NULL = {"last_start": None, "last_end": None, "last_duration": None,
             "last_outcome": None, "last_error": None, "run_count": 0, "error_count": 0}

def _ui_status():
    def _run_info(cid):
        """Return run metadata for cid, or nulled defaults if the check has never run."""
        r = _check_runs.get(cid) or {}
        return {
            "last_start":    r.get("last_start"),
            "last_end":      r.get("last_end"),
            "last_duration": r.get("last_duration"),
            "last_outcome":  r.get("last_outcome"),
            "last_error":    r.get("last_error", ""),
            "run_count":     r.get("run_count", 0),
            "error_count":   r.get("error_count", 0),
        }
    checks = [{"name": n, "on": bool(e), **_run_info(n)} for n, e, _, _, _, _ in CHECKS]
    # Synthetic entries: warmer and detail-page warm are not in CHECKS but appear in the UI.
    # They have no run metadata in _check_runs so we always emit nulled defaults.
    checks.append({"name": "warmer", "on": _b("ENABLE_WARMER", False) and bool(PLEX_URL), **_RUN_NULL})
    checks.append({"name": "detail-page warm", "on": bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE), **_RUN_NULL})
    return {"version": VERSION, "mode": MODE, "dry_run": DRY_RUN, "load": round(host_load(), 2), "checks": checks, "rescan_backlog": rescan_backlog()}
def _ui_warmer():
    stats = _warmer.get_stats()
    stats["enabled"] = _b("ENABLE_WARMER", False) and bool(PLEX_URL)
    stats["detail_page"] = bool(WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE)
    stats["recent"] = stats["recent"][:40]
    return stats
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
def _ui_state():
    """Return the full state.json as a dict for operator inspection."""
    return _load_state()


def _is_under_roots(target, roots):
    target = os.path.realpath(target)
    for r in roots:
        if target == os.path.realpath(r) or target.startswith(os.path.realpath(r) + os.sep):
            return True
    return False


def _rescan_folders(parent=None):
    roots = [r for r in (RESCAN_LIBRARY_PATHS or []) if os.path.isdir(r)]
    if not roots:
        return []
    if parent:
        parent = os.path.realpath(parent)
        if not _is_under_roots(parent, roots):
            return []
        base = parent
    else:
        base = None
    if base is None:
        out = []
        for r in roots:
            out.append({"name": os.path.basename(r) or r, "path": r, "is_root": True})
        return sorted(out, key=lambda x: x["name"].lower())
    try:
        entries = []
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if os.path.isdir(p):
                entries.append({"name": name, "path": p, "is_root": False})
        return sorted(entries, key=lambda x: x["name"].lower())
    except OSError:
        return []


def _trigger_rescan_path(folder_path):
    from .clients import Plex
    if not (PLEX_URL and PLEX_TOKEN):
        return False, "PLEX_URL/PLEX_TOKEN not set"
    roots = [r for r in (RESCAN_LIBRARY_PATHS or []) if os.path.isdir(r)]
    folder_path = os.path.realpath(folder_path)
    if not _is_under_roots(folder_path, roots):
        return False, "path not in RESCAN_LIBRARY_PATHS"
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    sections = []
    raw = plex.sections()
    for sec in raw or []:
        sec["locations"] = plex.section_locations(sec["key"]) or []
        sections.append(sec)
    best = None
    best_len = 0
    for sec in sections:
        for loc in sec.get("locations", []):
            loc = os.path.realpath(loc)
            if folder_path == loc or folder_path.startswith(loc + os.sep):
                if len(loc) > best_len:
                    best = sec
                    best_len = len(loc)
    if not best:
        return False, "no Plex section for path"
    log.info("[ui] manual partial scan for %s in section %s", folder_path, best["title"])
    if plex.scan_path(best["key"], folder_path):
        return True, "scan triggered"
    return False, "Plex scan_path failed"


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
            if path == "/api/state":             return self._send(200, "application/json", json.dumps(_ui_state()))
            if path == "/api/rescan/folders":
                parent = parse_qs(urlparse(self.path).query).get("parent", [""])[0] or None
                return self._send(200, "application/json", json.dumps({"roots": [r for r in (RESCAN_LIBRARY_PATHS or []) if os.path.isdir(r)], "folders": _rescan_folders(parent)}))
            if path == "/api/logs":
                try: n = min(int(parse_qs(urlparse(self.path).query).get("n", ["300"])[0]), 3000)
                except Exception: n = 300
                return self._send(200, "text/plain; charset=utf-8", _ui_logs(n))
            return self._send(404, "text/plain", "nf")
        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else b""
            if path in ("/api/config", "/api/restart", "/api/rescan/scan",
                        "/api/plex/rescan", "/api/plex/emptytrash", "/api/sweep") or path.startswith("/api/check/"):
                if not EN_UI or not self._authed():
                    return self._send(401, "text/plain", "unauthorized")
                if path == "/api/config":
                    ok, msg = _ui_save(body)
                    return self._send(200 if ok else 400, "application/json", json.dumps({"ok": ok, "msg": msg}))
                if path == "/api/plex/rescan":
                    threading.Thread(target=plex_rescan, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex rescan started"}))
                if path == "/api/plex/emptytrash":
                    threading.Thread(target=plex_empty_trash, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "Plex empty trash started"}))
                if path == "/api/sweep":
                    threading.Thread(target=sweep, daemon=True).start()
                    return self._send(202, "application/json", json.dumps({"ok": True, "msg": "sweep started"}))
                if path == "/api/rescan/scan":
                    try: p = json.loads(body or b"{"); folder = p.get("path", "")
                    except Exception: folder = ""
                    if not folder:
                        return self._send(400, "application/json", json.dumps({"ok": False, "msg": "missing path"}))
                    ok, msg = _trigger_rescan_path(folder)
                    return self._send(202 if ok else 500, "application/json", json.dumps({"ok": ok, "msg": msg}))
                if path.startswith("/api/check/"):
                    cid = path.split("/api/check/", 1)[1]
                    for name, en, fn, _, _, _ in CHECKS:
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
