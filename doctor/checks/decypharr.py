"""Check: decypharr + FUSE mount health.

Three-layer health probe for a FUSE (rclone/zurg) mount:

  1. Kernel mount table  - /proc/mounts confirms the mountpoint is still
                           registered with the kernel (walks ancestors so
                           DECYPHARR_MOUNT_TEST can be a sub-path like
                           /mnt/zurg/__all__ when the mount is at /mnt/zurg).
  2. statvfs liveness   - os.statvfs() on the mountpoint returns instantly
                           with ENOTCONN / EIO when FUSE is dead; it does NOT
                           hang like open() can.
  3. File read test     - actually reads a few bytes from a real media file so
                           we know data flows end-to-end.  Runs in a thread
                           with a configurable timeout so a hung mount doesn't
                           block the check.

A configurable strike counter (DECYPHARR_FUSE_STRIKES, default 2) requires
consecutive failures before the restart hook is called, avoiding restarts
on single transient errors.
"""
import os
import threading
import time
from ..config import (
    DECY_FUSE_STRIKES, DECY_MOUNT_TEST, DECY_READ_TIMEOUT,
    DECY_RESTART_CMD, DECY_URL, DRY_RUN,
    http_code, run_cmd, log,
)

# ---------------------------------------------------------------------------
# errno values that signal a dead/stuck FUSE mount
# ---------------------------------------------------------------------------
_FUSE_ERRNOS = frozenset({
    5,   # EIO  - Input/output error
    6,   # ENXIO - No such device or address
    107, # ENOTCONN - Transport endpoint is not connected
})

class _State:
    """Tiny reset-able mutable cell used for module-level check state."""
    def __init__(self, default):
        self._default = default
        self.value = default
    def reset(self):
        self.value = self._default

def _is_fuse_errno(exc):
    """Return True if *exc* looks like a dead FUSE transport."""
    if not isinstance(exc, OSError):
        return False
    if exc.errno in _FUSE_ERRNOS:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "socket not connected",
        "transport endpoint is not connected",
        "input/output error",
        "no such device",
    ))

# ---------------------------------------------------------------------------
# Layer 1 - kernel mount table
# ---------------------------------------------------------------------------
def _mount_registered(path):
    """Return True if *path* or any of its parent directories appears in
    /proc/mounts as a FUSE mountpoint.

    DECYPHARR_MOUNT_TEST is typically a subdirectory of the actual mountpoint
    (e.g. /mnt/zurg/__all__ when the FUSE is mounted at /mnt/zurg), so we
    walk up the path looking for a registered FUSE mount entry rather than
    requiring an exact match."""
    real = os.path.realpath(path)
    # Collect all FUSE mountpoints from /proc/mounts.
    fuse_mounts = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and "fuse" in parts[2]:
                    fuse_mounts.add(parts[1])
    except Exception:
        pass
    if not fuse_mounts:
        return False
    # Check if real path or any ancestor is a registered FUSE mount.
    check = real
    while True:
        if check in fuse_mounts:
            return True
        parent = os.path.dirname(check)
        if parent == check:  # reached filesystem root
            break
        check = parent
    return False

# ---------------------------------------------------------------------------
# Layer 2 - statvfs liveness (fast, non-blocking on dead FUSE)
# ---------------------------------------------------------------------------
class _FuseStatus:
    """Result of a FUSE health probe."""
    OK        = "ok"
    DEAD      = "dead"      # FUSE transport gone (ENOTCONN/EIO)
    UNMOUNTED = "unmounted" # not in /proc/mounts
    HUNG      = "hung"      # statvfs timed out
    EMPTY     = "empty"     # mounted but no test file found
    UNKNOWN   = "unknown"   # unexpected error

def _probe_statvfs(path, timeout=5):
    """Call os.statvfs(path) in a thread.  Returns (_FuseStatus, detail_str)."""
    result = {"status": _FuseStatus.UNKNOWN, "detail": ""}
    def _do():
        try:
            os.statvfs(path)
            result["status"] = _FuseStatus.OK
        except OSError as e:
            if _is_fuse_errno(e):
                result["status"] = _FuseStatus.DEAD
                result["detail"] = "statvfs errno=%d (%s)" % (e.errno or 0, e.strerror or str(e))
            else:
                result["status"] = _FuseStatus.UNKNOWN
                result["detail"] = str(e)
        except Exception as e:
            result["status"] = _FuseStatus.UNKNOWN
            result["detail"] = str(e)
    th = threading.Thread(target=_do, daemon=True)
    th.start(); th.join(timeout)
    if th.is_alive():
        result["status"] = _FuseStatus.HUNG
        result["detail"] = "statvfs blocked for >%ds" % timeout
    return result["status"], result["detail"]

# ---------------------------------------------------------------------------
# Layer 3 - file read test
# ---------------------------------------------------------------------------
def _find_media_file(path):
    """Return the first media file found under *path*, or None."""
    exts = (".mkv", ".mp4", ".avi", ".m4v", ".ts")
    try:
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if fn.lower().endswith(exts):
                    return os.path.join(root, fn)
    except OSError as e:
        if _is_fuse_errno(e):
            raise  # let the caller handle FUSE dead errors from os.walk
    return None

def _read_file(fpath, timeout):
    """Read 64 KiB from *fpath* in a thread within *timeout* seconds.
    Returns (_FuseStatus, detail_str)."""
    result = {"status": _FuseStatus.UNKNOWN, "detail": ""}
    def _do():
        try:
            with open(fpath, "rb") as fh:
                fh.read(65536)
            result["status"] = _FuseStatus.OK
        except OSError as e:
            if _is_fuse_errno(e):
                result["status"] = _FuseStatus.DEAD
                result["detail"] = "read errno=%d (%s)" % (e.errno or 0, e.strerror or str(e))
            else:
                result["status"] = _FuseStatus.UNKNOWN
                result["detail"] = str(e)
        except Exception as e:
            result["status"] = _FuseStatus.UNKNOWN
            result["detail"] = str(e)
    th = threading.Thread(target=_do, daemon=True)
    th.start(); th.join(timeout)
    if th.is_alive():
        result["status"] = _FuseStatus.HUNG
        result["detail"] = "read blocked for >%ds" % timeout
    return result["status"], result["detail"]

def _probe_mount(path, read_timeout):
    """Run all three layers.  Returns (_FuseStatus, detail_str)."""
    # Layer 1 - kernel mount table
    if not _mount_registered(path):
        return _FuseStatus.UNMOUNTED, "not in /proc/mounts"
    log.debug("[decypharr] mount probe layer 1 OK: %s registered in /proc/mounts", path)

    # Layer 2 - statvfs (fast dead-FUSE detector, does not hang)
    status, detail = _probe_statvfs(path, timeout=5)
    if status != _FuseStatus.OK:
        log.debug("[decypharr] mount probe layer 2 FAIL: statvfs %s -> %s (%s)", path, status, detail)
        return status, detail
    log.debug("[decypharr] mount probe layer 2 OK: statvfs %s responsive", path)

    # Layer 3 - real file read
    try:
        fpath = _find_media_file(path)
    except OSError as e:
        return _FuseStatus.DEAD, "os.walk errno=%d (%s)" % (e.errno or 0, e.strerror or str(e))

    if fpath is None:
        return _FuseStatus.EMPTY, "no media file found under %s" % path

    log.debug("[decypharr] mount probe layer 3: reading %s (timeout=%ds)", fpath, read_timeout)
    return _read_file(fpath, read_timeout)

# ---------------------------------------------------------------------------
# Strike counter - require N consecutive failures before acting
# ---------------------------------------------------------------------------
_fuse_strikes = _State(0)   # mutable cell updated by check_decypharr

def _record_fuse_result(status):
    """Increment/reset strike counter.  Returns (strikes, needs_action)."""
    if status in (_FuseStatus.OK, _FuseStatus.EMPTY):
        _fuse_strikes.reset()
        return 0, False
    _fuse_strikes.value += 1
    return _fuse_strikes.value, _fuse_strikes.value >= DECY_FUSE_STRIKES

# ---------------------------------------------------------------------------
# Restart hook
# ---------------------------------------------------------------------------
_decy_last_restart = _State(0.0)

def _decy_restart(reason=""):
    """Run the decypharr restart hook, rate-limited to once per 5 minutes."""
    tag = (" (%s)" % reason) if reason else ""
    if DRY_RUN or not DECY_RESTART_CMD:
        log.error("[decypharr] FUSE unhealthy but no restart cmd (or dry-run) -> alert only%s", tag)
        return False
    if time.time() - _decy_last_restart.value < 300:
        log.warning("[decypharr] restart attempted <5m ago, holding off%s", tag)
        return False
    log.error("[decypharr] running restart hook%s: %s", tag, DECY_RESTART_CMD)
    rc = run_cmd(DECY_RESTART_CMD)
    _decy_last_restart.value = time.time()
    log.error("[decypharr] restart hook rc=%s %s",
              rc[0] if rc else "?", rc[1].strip() if (rc and rc[1]) else "")
    return True

# ---------------------------------------------------------------------------
# Main check entry point
# ---------------------------------------------------------------------------
_STATUS_LABELS = {
    _FuseStatus.DEAD:      "DEAD (transport/socket not connected)",
    _FuseStatus.UNMOUNTED: "UNMOUNTED",
    _FuseStatus.HUNG:      "HUNG (read/statvfs blocked)",
    _FuseStatus.UNKNOWN:   "ERROR",
}

def check_decypharr():
    # --- API health ---
    if DECY_URL:
        c = http_code(DECY_URL, t=10)
        log.info("[decypharr] api %s -> %s", DECY_URL, c if c else "DOWN")

    if not DECY_MOUNT_TEST:
        return

    # --- FUSE mount health (3-layer probe) ---
    status, detail = _probe_mount(DECY_MOUNT_TEST, DECY_READ_TIMEOUT)

    if status == _FuseStatus.OK:
        _fuse_strikes.reset()
        log.info("[decypharr] mount %s OK (statvfs + read)", DECY_MOUNT_TEST)
        return

    if status == _FuseStatus.EMPTY:
        _fuse_strikes.reset()
        log.warning("[decypharr] mount %s: %s", DECY_MOUNT_TEST, detail)
        return

    strikes, act = _record_fuse_result(status)
    label = _STATUS_LABELS.get(status, str(status))

    if act:
        log.error("[decypharr] mount %s %s -- %s (strike %d/%d) -> restarting",
                  DECY_MOUNT_TEST, label, detail, strikes, DECY_FUSE_STRIKES)
        _decy_restart(status)
    else:
        log.warning("[decypharr] mount %s %s -- %s (strike %d/%d, need %d to act)",
                    DECY_MOUNT_TEST, label, detail, strikes, DECY_FUSE_STRIKES, DECY_FUSE_STRIKES)
