"""Re-trigger searches for items *arr reports as MissingFromDisk."""
import time
from ...config import DRY_RUN, REPAIR_ITEM_INTERVAL, REPAIR_MFD_RECHECK, REPAIR_UNMONITORED, log
from ...clients import INSTANCES

def _missing_from_disk_check(state, acted, budget):
    """Query *arr download history for items with reason=MissingFromDisk and re-trigger searches.
    This catches files that Sonarr/Radarr knows are gone but which have no on-disk symlink to probe
    (e.g. usenet direct downloads, or files cleaned up by an external tool). Shares the REPAIR_MAX_ACTIONS
    budget with the filesystem sweep so the two modes together never exceed the cap in one sweep."""
    mfd = state.setdefault("__repair_mfd__", {})
    now = time.time()
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr"):
            continue                                         # skip non-media instances (prowlarr, etc.)
        if budget <= 0:
            break                                            # budget exhausted: stop processing all instances
        try:
            all_media = arr.series() if arr.kind == "sonarr" else arr.movies()
        except Exception as e:
            log.warning("[repair:mfd:%s] failed to fetch media list: %s", arr.name, str(e)[:60]); continue
        log.debug("[repair:mfd:%s] scanning %d item(s) for MissingFromDisk history", arr.name, len(all_media))
        for item in all_media:
            if budget <= 0:
                break
            if not item.get("monitored") and not REPAIR_UNMONITORED:
                continue
            mid = item.get("id")
            title = (item.get("title") or "")[:60]
            try:
                records = arr.history(mid)
            except Exception as e:
                log.warning("[repair:mfd:%s] history fetch failed for %s: %s", arr.name, title, str(e)[:60]); continue
            # sonarr returns a list directly; radarr wraps in {"records": [...]}
            if isinstance(records, dict):
                records = records.get("records") or []
            # find the most recent grabbed record that is now MissingFromDisk
            # group by season (sonarr) or movie so we only search once per parent
            searched = set()
            for rec in records:
                if rec.get("eventType") != "grabbed":
                    continue
                data = rec.get("data") or {}
                if data.get("reason") != "MissingFromDisk":
                    continue
                log.debug("[repair:mfd:%s] MissingFromDisk history entry for: %s", arr.name, title)
                if arr.kind == "sonarr":
                    ep = rec.get("episode") or {}
                    season_number = ep.get("seasonNumber")
                    series_id = ep.get("seriesId") or mid
                    key = "%s:%d:s%s" % (arr.name, series_id, season_number)
                else:
                    key = "%s:%d" % (arr.name, mid)
                if key in searched:
                    continue
                if now - mfd.get(key, 0) < REPAIR_MFD_RECHECK:
                    continue                                  # searched recently, wait for cooldown
                if budget <= 0:
                    break
                if DRY_RUN:
                    log.info("[repair:mfd:%s] DRY-RUN would re-search MissingFromDisk: %s", arr.name, title)
                    mfd[key] = now; searched.add(key); acted += 1; budget -= 1; continue
                if arr.kind == "sonarr" and season_number is not None:
                    arr.command("SeasonSearch", seriesId=series_id, seasonNumber=season_number)
                    log.warning("[repair:mfd:%s] MissingFromDisk -> SeasonSearch: %s S%02d", arr.name, title, season_number)
                elif arr.kind == "radarr":
                    arr.command("MoviesSearch", movieIds=[mid])
                    log.warning("[repair:mfd:%s] MissingFromDisk -> MoviesSearch: %s", arr.name, title)
                else:
                    continue
                mfd[key] = now; searched.add(key); acted += 1; budget -= 1
                if REPAIR_ITEM_INTERVAL > 0:
                    time.sleep(REPAIR_ITEM_INTERVAL)
    return acted
