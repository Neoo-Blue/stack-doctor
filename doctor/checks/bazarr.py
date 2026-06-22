"""Check: bazarr."""
from ..config import BAZARR_APIKEY, BAZARR_URL, http_code, log

def check_bazarr():
    if not BAZARR_URL:
        return
    c = http_code(BAZARR_URL.rstrip("/") + "/api/system/status",
                  headers={"X-API-KEY": BAZARR_APIKEY} if BAZARR_APIKEY else None, t=10)
    (log.info if c == 200 else log.error)("[bazarr] %s -> %s", BAZARR_URL, c if c else "DOWN")
