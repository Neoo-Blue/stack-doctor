"""Check: janitor.

1. Reads the decypharr log tail and quarantines library symlinks that point to dead releases
   (ARTICLE_NOT_FOUND, still missing, marked as bad, empty_link, etc.).
2. Scans the same log tail for operational/infra error patterns (panic, fatal, rate-limit,
   cloudflare, auth, network timeouts) and logs a summary, throttled so it doesn't spam.
3. Optionally probes the decypharr HTTP API (if DECY_URL is set) and logs when it returns
   errors or becomes unreachable.
"""
import os
import json
import re
import time
from ..config import (
    DECY_URL, DRY_RUN, JAN_ALERT_COOLDOWN, JAN_ERROR_PATTERNS,
    JAN_LIBS, JAN_LOG, JAN_LOG_CMD, JAN_PATTERNS, JAN_QUAR,
    http_code, run_output, log,
)

# Operational-error categories we scan for in the decypharr log.
# Each regex is case-insensitive and matches a whole word / short phrase.
_JAN_OP_PATTERNS = [
    ("panic/fatal", re.compile(r"\b(panic|fatal|runtime error)\b", re.I)),
    ("rate-limit", re.compile(r"\b(rate limit|rate limited|too many requests|429)\b", re.I)),
    ("cloudflare/blocked", re.compile(r"\b(cloudflare|cf-ray|blocked|403)\b", re.I)),
    ("auth", re.compile(r"\b(unauthorized|token expired|401)\b", re.I)),
    ("network/timeout", re.compile(r"\b(context deadline exceeded|connection refused|i/o timeout|timeout)\b", re.I)),
]
# User-configurable extra patterns added to the scan.
# Wrap each pattern with word boundaries so short numeric codes (401, 403, 429)
# and other tokens do not match inside hex hashes, alldebrid IDs, etc.
_JAN_USER_PATTERNS = [(p, re.compile(r"\b" + re.escape(p) + r"\b", re.I)) for p in JAN_ERROR_PATTERNS]

# Throttle repeated operational/API alerts so we don't log the same thing every 3 minutes.
_jan_alert_last = {}

def _jan_alert(name, msg, *args):
    now = time.time()
    if now - _jan_alert_last.get(name, 0) < JAN_ALERT_COOLDOWN:
        return
    _jan_alert_last[name] = now
    log.warning(msg, *args)

def _scan_operational_errors(data):
    """Return {category: count} for operational error lines in the log tail."""
    counts = {}
    for line in data.splitlines():
        for label, pat in _JAN_OP_PATTERNS + _JAN_USER_PATTERNS:
            if pat.search(line):
                counts[label] = counts.get(label, 0) + 1
                break  # count a line only once, under the first matching category
    return counts

def _probe_decy_api():
    """Probe the decypharr API root and /api/status. Log only on problems."""
    if not DECY_URL:
        return
    base = DECY_URL.rstrip("/")
    for path in ("", "/api/status"):
        url = base + (path or "/")
        try:
            code = http_code(url, t=5)
        except Exception as e:
            _jan_alert("decy_api:%s" % path, "[janitor] decypharr API %s unreachable: %s", url, str(e)[:60])
            continue
        if code >= 500:
            _jan_alert("decy_api:%s" % path, "[janitor] decypharr API %s returned HTTP %d", url, code)
        elif code in (401, 403):
            _jan_alert("decy_api:%s" % path, "[janitor] decypharr API %s returned HTTP %d (auth/blocked)", url, code)
        elif code == 0:
            _jan_alert("decy_api:%s" % path, "[janitor] decypharr API %s unreachable (no response)", url)
        elif 200 <= code < 300:
            log.debug("[janitor] decypharr API %s -> HTTP %d OK", url, code)
        else:
            log.debug("[janitor] decypharr API %s -> HTTP %d (unexpected but non-critical)", url, code)

def _read_log_tail():
    """Return the last ~2MB of the decypharr log as a string."""
    if JAN_LOG_CMD:
        return run_output(JAN_LOG_CMD)
    if JAN_LOG and os.path.exists(JAN_LOG):
        with open(JAN_LOG, errors="ignore") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2_000_000))
            return f.read()
    return None

def check_janitor():
    data = _read_log_tail()
    if data is None:
        log.debug("[janitor] need JANITOR_LOG_CMD or a readable JANITOR_DECYPHARR_LOG")
        return
    log.debug("[janitor] scanning %d bytes of log tail", len(data))

    bad = set()

    # Pattern 1: [webdav] Error streaming file: <path> error="<msg>"
    # Catches: ARTICLE_NOT_FOUND, still missing, marked as bad, etc.
    pat_stream = re.compile(r"Error streaming file: (.+?) error=\"([^\"]*)\"")
    for m in pat_stream.finditer(data):
        path, err = m.group(1), m.group(2)
        if any(p.strip() and p.strip() in err for p in JAN_PATTERNS):
            bad.add(path.strip().split("/")[0])

    # Pattern 2: [link] Giving up on entry ... filename=<name> reason=empty_link
    pat_filename = re.compile(r"(?:Giving up on entry|empty_link).*?\bfilename=(\S+)")
    for m in pat_filename.finditer(data):
        bad.add(m.group(1).split("/")[0])

    # Operational errors that don't necessarily map to a single dead release.
    op_counts = _scan_operational_errors(data)
    if op_counts:
        summary = ", ".join("%dx %s" % (n, k) for k, n in sorted(op_counts.items(), key=lambda x: -x[1]))
        _jan_alert("janitor:ops", "[janitor] operational errors in log tail: %s", summary)

    # Probe the decypharr API for correlated health issues.
    _probe_decy_api()

    if not bad:
        log.debug("[janitor] no dead releases in log tail")
        return
    log.debug("[janitor] found %d dead release(s): %s", len(bad), ", ".join(sorted(bad)[:10]))

    if not JAN_LIBS:
        _jan_alert("janitor:dead", "[janitor] %d dead release(s) in log but no JANITOR_LIBRARY_PATHS to quarantine", len(bad))
        return

    moved = 0
    qroot = os.path.join(JAN_QUAR, time.strftime("%Y%m%d-%H%M%S"))
    manifest = []
    for libp in JAN_LIBS:
        libp = os.path.abspath(libp)
        for root, _, files in os.walk(libp):
            for fn in files:
                fp = os.path.abspath(os.path.join(root, fn))
                if not os.path.islink(fp):
                    continue
                try:
                    tgt = os.readlink(fp)
                except Exception:
                    continue
                mm = re.search(r"/(?:__all__|complete)/([^/]+)(?:/|$)", tgt)
                if not mm or mm.group(1) not in bad:
                    continue
                if DRY_RUN:
                    log.info("[janitor] WOULD quarantine: %s", fp)
                    continue
                try:
                    dst = os.path.join(qroot, fp.lstrip("/"))
                    if os.path.exists(dst) or os.path.islink(dst):
                        continue
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.symlink(tgt, dst)
                    os.unlink(fp)
                    manifest.append({"orig": fp, "target": tgt})
                    moved += 1
                except Exception as e:
                    log.warning("[janitor] move failed %s: %s", fp, e)

    if manifest:
        try:
            os.makedirs(qroot, exist_ok=True)
            with open(os.path.join(qroot, "manifest.json"), "w") as f:
                json.dump(manifest, f, indent=1)
        except Exception:
            pass

    if moved:
        log.info("[janitor] quarantined %d dead-file symlink(s) across %d release(s) -> %s", moved, len(bad), qroot)
