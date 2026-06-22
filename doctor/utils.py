"""Small, stateless utility helpers used across the doctor package.

These functions were extracted from doctor.config.py in Phase 2 so that
doctor.config.py can focus on environment parsing and logging setup while
still re-exporting them for backward compatibility.
"""
import logging
import subprocess
import urllib.error
import urllib.request

log = logging.getLogger("doctor")

__all__ = ["http_code", "run_cmd", "run_output", "host_load"]


def http_code(url, headers=None, t=10):
    """Return the HTTP status code for *url*, or 0 on any failure."""
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=t)
        return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def run_cmd(cmd):
    """Run *cmd* in a shell and return (returncode, combined_output[:300]).

    Returns None if *cmd* is empty.
    """
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        return (p.returncode, (p.stdout + p.stderr).strip()[:300])
    except Exception as e:
        return (1, "cmd error: " + str(e)[:120])


def run_output(cmd, t=120):
    """Run *cmd* in a shell and return stdout; return "" on failure."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
        return p.stdout
    except Exception as e:
        log.warning("log cmd failed: %s", str(e)[:80])
        return ""


def host_load():
    """Return the 1-minute host load from /proc/loadavg, or 0.0 on failure."""
    try:
        with open("/proc/loadavg") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0
