"""Entry point: python -m doctor."""
import sys
import signal
import threading
from .config import (
    DRY_RUN,
    EN_UI,
    EN_WARMER,
    MODE,
    PLEX_URL,
    PORT,
    UI_PORT,
    VERSION,
    WARM_PLEXLOG_CMD,
    WARM_PLEXLOG_FILE,
    log,
)
from .clients import INSTANCES, load_instances
from .checks import backfill_missing_seasons, plexlog_loop, warmer_loop
from .scheduler import CHECKS, scheduler_loop
from .webui import _build_server

def main():
    import doctor.clients as _clients
    _clients.INSTANCES[:] = load_instances()
    if "--backfill-missing-seasons" in sys.argv:
        sys.argv.remove("--backfill-missing-seasons")
        backfill_missing_seasons()
    enabled = [c for c, e, _, _, _, _ in CHECKS if e]
    warmer_on = EN_WARMER and bool(PLEX_URL)
    if EN_WARMER and not PLEX_URL:
        log.warning("ENABLE_WARMER set but PLEX_URL is empty -> warmer disabled")
    _needs_instances = [cid for cid, en, _, _, _, needs in CHECKS if en and needs]
    if _needs_instances and not INSTANCES:
        log.error("checks %s require at least one instance. Set INSTANCE_1_URL / _APIKEY / _TYPE.",
                  _needs_instances)
        sys.exit(2)
    if not enabled and not warmer_on and not EN_UI:
        log.error("nothing enabled. Set ENABLE_QUEUE / ENABLE_DECYPHARR / ENABLE_PLEX / ENABLE_PLEX_SCAN / "
                  "ENABLE_RESOURCES / ENABLE_JANITOR / ENABLE_REPAIR / ENABLE_WARMER / ENABLE_UI.")
        sys.exit(2)
    log.info("stack-doctor v%s | mode=%s | checks=[%s]%s%s | instances=%s | dry_run=%s", VERSION,
             MODE, ",".join(enabled), " +warmer" if warmer_on else "", " +ui" if EN_UI else "",
             ", ".join(a.name for a in INSTANCES) or "-", DRY_RUN)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    signal.signal(signal.SIGINT, lambda *a: stop.set())

    if warmer_on:
        threading.Thread(target=warmer_loop, args=(stop,), daemon=True).start()
        if WARM_PLEXLOG_CMD or WARM_PLEXLOG_FILE:
            threading.Thread(target=plexlog_loop, args=(stop,), daemon=True).start()

    # http server(s): arr webhooks (event mode) and/or the web dashboard (ENABLE_UI)
    servers, wanted = [], {}
    if MODE == "event":
        wanted[PORT] = "webhooks"
    if EN_UI:
        wanted[UI_PORT] = (wanted.get(UI_PORT, "") + "+dashboard").lstrip("+")
    for pnum, what in wanted.items():
        try:
            s = _build_server(pnum)
            threading.Thread(target=s.serve_forever, daemon=True).start()
            servers.append(s); log.info("http on :%d (%s)", pnum, what)
        except Exception as e:
            log.error("http bind :%d failed: %s", pnum, e)

    scheduler_loop(stop)
    for s in servers:
        try: s.shutdown()
        except Exception: pass
    log.info("stack-doctor stopped")

if __name__ == "__main__":
    main()
