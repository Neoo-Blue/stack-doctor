"""Check: plexscan."""
import time
from ..config import (
    DECY_MOUNT_TEST, DECY_READ_TIMEOUT, DRY_RUN,
    PLEX_RESTART_CMD, PLEX_SCAN_CANCEL, PLEX_SCAN_STUCK,
    PLEX_TOKEN, PLEX_URL, run_cmd, log,
)
from ..clients import Plex
from .decypharr import _decy_restart, _probe_mount, _FuseStatus

class _State:
    """Tiny reset-able mutable cell used for module-level check state."""
    def __init__(self, default):
        self._default = default
        self.value = default
    def reset(self):
        self.value = self._default

_scan_seen = _State({})      # activity uuid -> {first, prog, prog_ts, title, acted_ts}
_plex_last_restart = _State(0.0)
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
    log.debug("[plexscan] fetched %d Plex activit%s", len(acts), "y" if len(acts)==1 else "ies")
    now = time.time(); cur = set(); stuck = []
    for a in acts:
        if not _is_scan_activity(a):
            log.debug("[plexscan] non-scan activity: type=%s title=%s",
                      a.get("type"), (a.get("title") or "")[:50])
            continue
        uuid = a.get("uuid") or ""
        if not uuid:
            continue
        cur.add(uuid)
        try: prog = int(float(a.get("progress") or 0))
        except Exception: prog = 0
        title = (a.get("title") or a.get("subtitle") or "library scan")[:80]
        s = _scan_seen.value.setdefault(uuid, {"first": now, "prog": -1, "prog_ts": now, "title": title, "acted_ts": 0})
        if prog > s["prog"]:
            s["prog"] = prog; s["prog_ts"] = now           # progress advanced -> not stuck, reset the clock
        s["title"] = title
        if now - s["prog_ts"] >= PLEX_SCAN_STUCK:
            stuck.append((uuid, a, s))
    for u in list(_scan_seen.value):                              # forget scans that finished / disappeared
        if u not in cur:
            _scan_seen.value.pop(u, None)
    if not stuck:
        if cur:
            log.info("[plexscan] %d scan(s) running, progressing", len(cur))
        else:
            log.debug("[plexscan] no active scans")
        return
    for uuid, a, s in stuck:
        if now - s.get("acted_ts", 0) < PLEX_SCAN_STUCK:   # one recovery attempt per stuck-window; don't hammer
            continue
        s["acted_ts"] = now
        mins = int((now - s["prog_ts"]) / 60)
        log.error("[plexscan] STUCK scan '%s' (no progress for %dm, stalled at %d%%)", s["title"], mins, max(s["prog"], 0))
        if DRY_RUN:
            log.info("[plexscan] DRY-RUN: would fix mount + cancel scan"); continue
        # 1) root cause: a hung/dead decypharr mount blocks the scanner on I/O
        if DECY_MOUNT_TEST:
            _ps_status, _ps_detail = _probe_mount(DECY_MOUNT_TEST, DECY_READ_TIMEOUT)
            if _ps_status in (_FuseStatus.DEAD, _FuseStatus.HUNG, _FuseStatus.UNMOUNTED):
                log.error("[plexscan] decypharr mount %s (%s) -> restarting it (usual cause of wedged scan)",
                          _ps_status, _ps_detail)
                _decy_restart("plex scan wedged on %s mount" % _ps_status)
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
            now - _plex_last_restart.value > 1800 and not cancelled):
            log.error("[plexscan] scan still wedged -> restarting Plex: %s", PLEX_RESTART_CMD)
            rc = run_cmd(PLEX_RESTART_CMD); _plex_last_restart.value = time.time()
            log.error("[plexscan] Plex restart rc=%s %s", rc[0] if rc else "?", rc[1] if rc else "")
