"""Check: missing_seasons."""
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
from ..config import *
from ..clients import *
from ..state import *

def _season_still_airing(episodes, season_number):
    """Return True if *season_number* has at least one episode whose air date is in the future.
    This prevents triggering a SeasonSearch for a season that is still actively airing
    (only some episodes have been released so far)."""
    now = datetime.now(timezone.utc)
    for ep in episodes:
        if ep.get("seasonNumber") != season_number:
            continue
        air = ep.get("airDateUtc") or ""
        if not air:
            continue
        try:
            dt = datetime.fromisoformat(air.replace("Z", "+00:00"))
            if dt > now:
                return True
        except (ValueError, TypeError):
            pass
    return False
def check_missing_seasons():
    """Walk every monitored Sonarr series. For each season that is fully monitored, has been
    around long enough (MS_MIN_AGE_HOURS), has zero episode files, and is not still airing
    (no future air dates), trigger a SeasonSearch.
    State tracks the last time each (instance, series_id, season) was searched so we don't
    hammer the same season every sweep."""
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        log.debug("[missing_seasons] no sonarr instances configured"); return
    with state_transaction() as state:
        ms = state.setdefault("__missing_seasons__", {})
        now = time.time(); acted = 0; skipped = 0; airing = 0
        min_age_secs = MS_MIN_AGE_HOURS * 3600
        for arr in sonarr_instances:
            try:
                all_series = arr.series()
            except Exception as e:
                log.warning("[missing_seasons:%s] failed to fetch series: %s", arr.name, str(e)[:60]); continue
            for ser in all_series:
                if not ser.get("monitored"):
                    continue
                sid   = ser.get("id")
                title = (ser.get("title") or "")[:60]
                # use the series added date as a proxy for how long it's been monitored
                added_str = ser.get("added") or ""
                try:
                    import email.utils
                    added_ts = email.utils.parsedate_to_datetime(added_str).timestamp() if added_str else 0
                except Exception:
                    added_ts = 0
                if added_ts and (now - added_ts) < min_age_secs:
                    continue                                  # too new, give Sonarr time to grab it first
                ep_cache = None                               # lazy-fetched per series
                for season in (ser.get("seasons") or []):
                    sn = season.get("seasonNumber", 0)
                    if sn == 0:
                        continue                              # skip specials
                    if not season.get("monitored"):
                        continue
                    stats = season.get("statistics") or {}
                    if stats.get("episodeFileCount", 0) > 0:
                        continue                              # has files, all good
                    if stats.get("totalEpisodeCount", 0) == 0:
                        continue                              # no episodes exist yet in Sonarr
                    key = "%s:%d:%d" % (arr.name, sid, sn)
                    if now - ms.get(key, 0) < MS_RECHECK:
                        skipped += 1; continue               # searched recently, wait for cooldown
                    if acted >= MS_MAX_ACTIONS:
                        break
                    # lazy-fetch episodes once per series to check air dates
                    if ep_cache is None:
                        try:
                            ep_cache = arr.episodes(sid)
                        except Exception:
                            ep_cache = []
                    if _season_still_airing(ep_cache, sn):
                        airing += 1
                        log.debug("[missing_seasons:%s] skipping still-airing season: %s S%02d", arr.name, title, sn)
                        continue
                    if DRY_RUN:
                        log.info("[missing_seasons:%s] DRY-RUN would search: %s S%02d", arr.name, title, sn)
                        ms[key] = now; acted += 1; continue
                    if arr.command("SeasonSearch", seriesId=sid, seasonNumber=sn):
                        log.warning("[missing_seasons:%s] 0 files in monitored season -> SeasonSearch: %s S%02d",
                                    arr.name, title, sn)
                        ms[key] = now; acted += 1
                if acted >= MS_MAX_ACTIONS:
                    break
        log.info("[missing_seasons] searched %d season(s), skipped %d (cooldown), %d (still airing)", acted, skipped, airing)
