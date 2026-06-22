"""Check: plex."""
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from ..config import http_code, PLEX_SCAN, PLEX_TOKEN, PLEX_URL, log

def check_plex():
    if not PLEX_URL:
        return
    sep = "&" if "?" in PLEX_URL else "?"
    url = PLEX_URL.rstrip("/") + "/identity"
    c = http_code(url + (sep + "X-Plex-Token=" + PLEX_TOKEN if PLEX_TOKEN else ""), t=10)
    if c == 200:
        log.info("[plex] %s -> 200 OK", PLEX_URL)
    else:
        log.error("[plex] %s -> %s (unresponsive)", PLEX_URL, c if c else "DOWN")
    if PLEX_SCAN and PLEX_TOKEN and c == 200:
        try:
            urllib.request.urlopen(PLEX_URL.rstrip("/") + "/library/sections/all/refresh?X-Plex-Token=" + PLEX_TOKEN, timeout=10)
            log.info("[plex] triggered library refresh")
        except Exception as e:
            log.debug("[plex] refresh failed: %s", e)
def _plex_sections():
    """Return list of (key, title) for all Plex library sections. Raises on error."""
    plex_url   = PLEX_URL
    plex_token = PLEX_TOKEN
    if not plex_url or not plex_token:
        raise ValueError("PLEX_URL or PLEX_TOKEN not set")
    with urllib.request.urlopen(
            urllib.request.Request("%s/library/sections?X-Plex-Token=%s" % (plex_url, plex_token)),
            timeout=10) as r:
        root = ET.fromstring(r.read())
    sections = [(d.get("key"), d.get("title", d.get("key")))
                for d in root.findall("Directory") if d.get("key")]
    if not sections:
        raise ValueError("no library sections found")
    return plex_url, plex_token, sections
def _plex_rescan():
    """Trigger a Plex library scan (refresh) for all sections. Returns (ok, message)."""
    try:
        plex_url, plex_token, sections = _plex_sections()
    except Exception as e:
        return False, str(e)
    ok, failed = [], []
    for key, title in sections:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "%s/library/sections/%s/refresh?X-Plex-Token=%s" % (plex_url, key, plex_token),
                    method="GET"),
                timeout=10)
            ok.append(title)
        except Exception as e:
            log.warning("[plex] rescan section %s (%s) failed: %s", key, title, e)
            failed.append(title)
    msg = "rescanned %d section(s): %s" % (len(ok), ", ".join(ok))
    if failed:
        msg += " | failed: %s" % ", ".join(failed)
    log.info("[plex] %s", msg)
    return len(failed) == 0, msg
def _plex_empty_trash():
    """Empty trash in all Plex library sections. Returns (ok, message)."""
    try:
        plex_url, plex_token, sections = _plex_sections()
    except Exception as e:
        return False, str(e)
    ok, failed = [], []
    for key, title in sections:
        try:
            urllib.request.urlopen(
                urllib.request.Request(
                    "%s/library/sections/%s/emptyTrash?X-Plex-Token=%s" % (plex_url, key, plex_token),
                    method="PUT"),
                timeout=10)
            ok.append(title)
        except Exception as e:
            log.warning("[plex] empty trash section %s (%s) failed: %s", key, title, e)
            failed.append(title)
    msg = "emptied trash for %d section(s): %s" % (len(ok), ", ".join(ok))
    if failed:
        msg += " | failed: %s" % ", ".join(failed)
    log.info("[plex] %s", msg)
    return len(failed) == 0, msg
