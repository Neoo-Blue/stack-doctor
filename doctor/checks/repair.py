"""Check: repair."""
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

def _debrid_mount_ok():
    """Return True if the debrid mount looks live (path exists and has at least one child entry).
    An empty or missing mount means the debrid service is down or the FUSE mount dropped — we must
    not run repair in that state or we'd mass-delete + mass-regrab every file in the library."""
    p = REPAIR_DEBRID_MOUNT
    if not p:
        return True                                              # not configured -> no check, proceed
    try:
        children = os.listdir(p)
        if children:
            return True
        log.warning("[repair] debrid mount %s exists but is empty -> service down? skipping sweep", p)
        return False
    except Exception as e:
        log.warning("[repair] debrid mount %s not accessible (%s) -> skipping sweep", p, str(e)[:60])
        return False
def _dead_symlink(fp):
    """True if fp is a symlink whose target no longer exists. If REPAIR_DEBRID_MOUNT is set, only
    symlinks whose target lives under that root are considered (avoids acting on local files)."""
    try:
        if not os.path.islink(fp):
            return False
        target = os.readlink(fp)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(fp), target)
        if REPAIR_DEBRID_MOUNT and not target.startswith(REPAIR_DEBRID_MOUNT):
            return False
        return not os.path.exists(target)
    except Exception:
        return False
def _radarr_dead_files(movies):
    """Yield (movie_id, title, movie_file_id) for monitored movies whose on-disk symlink is dead.
    Skips unmonitored movies unless REPAIR_UNMONITORED."""
    for m in movies:
        if not m.get("monitored", True) and not REPAIR_UNMONITORED:
            continue
        mid = m.get("id")
        mf = m.get("movieFile") or {}
        fp = mf.get("path")
        if not mid or not fp:
            continue
        if REPAIR_LIBS and not any(fp.startswith(p) for p in REPAIR_LIBS):
            continue
        if _dead_symlink(fp):
            yield mid, (m.get("title") or "")[:70], mf.get("id")
def _sonarr_dead_files(arr, series):
    """Yield (series_id, title, season_number, [episode_file_ids]) per season that has dead symlinks.
    Skips unmonitored series unless REPAIR_UNMONITORED."""
    for ser in series:
        if not ser.get("monitored", True) and not REPAIR_UNMONITORED:
            continue
        sid = ser.get("id")
        if not sid:
            continue
        title = (ser.get("title") or "")[:70]
        try:
            efiles = arr.episode_files(sid)
            eps = arr.episodes(sid)
        except Exception:
            continue
        # episodeFile objects may not include seasonNumber, so cross-reference with episodes
        efid_to_season = {}
        for ep in eps:
            if ep.get("episodeFileId"):
                efid_to_season[ep["episodeFileId"]] = ep.get("seasonNumber")
        dead_by_season = {}
        for ef in efiles:
            fp = ef.get("path")
            if not fp:
                continue
            if REPAIR_LIBS and not any(fp.startswith(p) for p in REPAIR_LIBS):
                continue
            if not _dead_symlink(fp):
                continue
            efid = ef.get("id")
            if not efid:
                continue
            sn = ef.get("seasonNumber") if ef.get("seasonNumber") is not None else efid_to_season.get(efid)
            if sn is None:
                continue
            dead_by_season.setdefault(sn, []).append(efid)
        for sn, efids in dead_by_season.items():
            yield sid, title, sn, efids
def _sonarr_season_pack_check(arr, series):
    """Yield (series_title, season_number, arr) for any fully-available sonarr season whose episode
    files are spread across more than one parent directory — a sign that individual episode grabs
    replaced what should be a season pack. Only emits seasons where every episode is monitored."""
    for ser in series:
        if not ser.get("monitored", True):
            continue
        sid = ser.get("id")
        title = (ser.get("title") or "")[:60]
        try:
            seasons = {s["seasonNumber"]: s for s in (ser.get("seasons") or []) if s.get("seasonNumber", 0) > 0}
            efiles = arr.episode_files(sid)
            eps    = arr.episodes(sid)
        except Exception:
            continue
        # group episode files by season
        ef_by_season = {}
        for ef in efiles:
            sn = ef.get("seasonNumber")
            if sn:
                ef_by_season.setdefault(sn, []).append(ef)
        ep_by_season = {}
        for ep in eps:
            sn = ep.get("seasonNumber")
            if sn:
                ep_by_season.setdefault(sn, []).append(ep)
        for sn, efs in ef_by_season.items():
            season_meta = seasons.get(sn, {})
            stats = season_meta.get("statistics") or {}
            # only act when the season is fully downloaded
            if stats.get("episodeFileCount", 0) < stats.get("totalEpisodeCount", 1):
                continue
            parent_dirs = set(os.path.dirname(ef.get("path", "")) for ef in efs if ef.get("path"))
            if len(parent_dirs) > 1:
                yield title, sn, sid, arr
def _missing_from_disk_check(state, acted, budget):
    """Query *arr download history for items with reason=MissingFromDisk and re-trigger searches.
    This catches files that Sonarr/Radarr knows are gone but which have no on-disk symlink to probe
    (e.g. usenet direct downloads, or files cleaned up by an external tool). Shares the REPAIR_MAX_ACTIONS
    budget with the filesystem sweep so the two modes together never exceed the cap in one sweep."""
    mfd = state.setdefault("__repair_mfd__", {})
    now = time.time()
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr") or budget <= 0:
            break
        try:
            all_media = arr.series() if arr.kind == "sonarr" else arr.movies()
        except Exception as e:
            log.warning("[repair:mfd:%s] failed to fetch media list: %s", arr.name, str(e)[:60]); continue
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
def _repair_verify_pending(state):
    """Check any in-flight repair searches from previous sweeps.
    State entry per pending item (keyed by '<arr_name>:<title_slug>'):
      {cmd_id, media_id, entity_ids, kind, title, search_ts, arr_name}
    Flow per item each sweep:
      1. If command_id present, poll /command/{id} — log when done/failed.
      2. Poll /history for a new 'grabbed' event after search_ts.
      3. On confirmed grab: log indexer + sourceTitle, remove from pending.
      4. On deadline exceeded without grab: log warning, remove from pending.
    """
    pv = state.setdefault("__repair_verify__", {})
    if not pv:
        return
    now = time.time()
    arr_map = {a.name: a for a in INSTANCES}
    expired = []
    for key, v in list(pv.items()):
        arr = arr_map.get(v.get("arr_name"))
        if not arr:
            expired.append(key); continue
        title    = v.get("title", key)
        search_ts = v.get("search_ts", "")
        deadline  = v.get("deadline", 0)
        cmd_id    = v.get("cmd_id")
        media_id  = v.get("media_id")
        entity_ids = v.get("entity_ids") or []

        # step 1: poll command status if we haven't confirmed it finished yet
        if cmd_id and not v.get("cmd_done"):
            status = arr.command_status(cmd_id)
            if status in ("completed", "failed", "aborted"):
                log.info("[repair:verify:%s] search command %s: %s", arr.name, cmd_id, status)
                v["cmd_done"] = True
            elif status is None:
                v["cmd_done"] = True          # endpoint gone, assume finished

        # step 2: check history for a new grab
        if media_id:
            rec = arr.history_grabbed(media_id, search_ts, entity_ids if arr.kind == "sonarr" else None)
            if rec:
                src     = rec.get("sourceTitle") or "?"
                indexer = (rec.get("data") or {}).get("indexer") or "?"
                log.warning("[repair:verify:%s] GRABBED '%s' via %s: %s", arr.name, title, indexer, src)
                expired.append(key); continue

        # step 3: deadline check
        if now > deadline:
            log.warning("[repair:verify:%s] no grab confirmed for '%s' within deadline — search may have stalled",
                        arr.name, title)
            expired.append(key)

    for key in expired:
        pv.pop(key, None)
def _repair_record_verify(state, arr, title, cmd_id, media_id, entity_ids):
    """Store a pending verification entry so the next sweep can check if the grab landed."""
    import datetime
    pv = state.setdefault("__repair_verify__", {})
    # key is stable across sweeps; title slug + arr name
    key = "%s:%s" % (arr.name, re.sub(r"[^a-z0-9]+", "_", title.lower())[:40])
    pv[key] = {
        "arr_name":  arr.name,
        "title":     title,
        "cmd_id":    cmd_id if isinstance(cmd_id, int) else None,
        "media_id":  media_id,
        "entity_ids": entity_ids or [],
        "search_ts": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "deadline":  time.time() + REPAIR_VERIFY_DEADLINE,
    }
def _repair_radarr_movie(arr, mid, title, mfid, state=None):
    """Delete a dead movie file record, toggle the movie monitor off+on, and re-search."""
    if DRY_RUN:
        log.info("[repair:%s] DRY-RUN would delete dead file + re-search movie: %s", arr.name, title)
        return True
    if mfid:
        arr.delete_file(mfid)
    # toggle monitor off+on to force the arr to refresh the title's availability state
    try:
        arr.set_monitored([mid], False)
        arr.set_monitored([mid], True)
    except Exception as e:
        log.warning("[repair:%s] monitor toggle failed for movie %s: %s", arr.name, title, str(e)[:70])
    cmd_id = arr.command("MoviesSearch", movieIds=[mid])
    log.warning("[repair:%s] dead symlink -> deleted file + re-searching movie: %s", arr.name, title)
    if REPAIR_VERIFY and state is not None:
        _repair_record_verify(state, arr, title, cmd_id, mid, [mid])
    return True
def _repair_sonarr_season(arr, sid, title, season_number, efids, state=None):
    """Delete all dead episode file records for a season, toggle the season's episodes off+on, and
    trigger a SeasonSearch so the whole season is treated as a unit."""
    if DRY_RUN:
        log.info("[repair:%s] DRY-RUN would delete %d dead file(s) + re-search season: %s S%02d",
                 arr.name, len(efids), title, season_number)
        return True
    for efid in efids:
        arr.delete_file(efid)
    # toggle every episode in this season off then on to force a fresh availability state
    epids = []
    try:
        eps = arr.episodes(sid)
        epids = [e.get("id") for e in eps if e.get("seasonNumber") == season_number and e.get("id")]
        if epids:
            arr.set_monitored(epids, False)
            arr.set_monitored(epids, True)
    except Exception as e:
        log.warning("[repair:%s] monitor toggle failed for %s S%02d: %s", arr.name, title, season_number, str(e)[:70])
    cmd_id = arr.command("SeasonSearch", seriesId=sid, seasonNumber=season_number)
    log.warning("[repair:%s] dead symlinks -> deleted %d file(s) + re-searching season: %s S%02d",
                arr.name, len(efids), title, season_number)
    if REPAIR_VERIFY and state is not None:
        _repair_record_verify(state, arr, title, cmd_id, sid, epids)
    return True
def check_repair():
    if not INSTANCES:
        log.debug("[repair] need at least one sonarr/radarr instance"); return
    if REPAIR_LOAD_MAX > 0 and host_load() > REPAIR_LOAD_MAX:
        log.info("[repair] host load > %.0f -> skip sweep", REPAIR_LOAD_MAX); return
    if not _debrid_mount_ok():
        return
    with state_transaction() as state:
        # verify pending searches from previous sweeps before starting a new one
        if REPAIR_VERIFY:
            _repair_verify_pending(state)
        acted = 0          # search commands issued (groups)
        symlinks = 0       # total dead symlinks deleted
        cap_hit = None
        for arr in INSTANCES:
            if arr.kind not in ("sonarr", "radarr"):
                continue
            if acted >= REPAIR_MAX_ACTIONS or symlinks >= REPAIR_MAX_SYMLINKS:
                break
            try:
                if arr.kind == "sonarr":
                    series = arr.series()
                    for sid, title, sn, efids in _sonarr_dead_files(arr, series):
                        if acted >= REPAIR_MAX_ACTIONS:
                            cap_hit = "REPAIR_MAX_ACTIONS"; break
                        if symlinks >= REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        count = len(efids)
                        if symlinks + count > REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        if _repair_sonarr_season(arr, sid, title, sn, efids, state):
                            acted += 1
                            symlinks += count
                            if REPAIR_ITEM_INTERVAL > 0:
                                time.sleep(REPAIR_ITEM_INTERVAL)
                else:
                    movies = arr.movies()
                    for mid, title, mfid in _radarr_dead_files(movies):
                        if acted >= REPAIR_MAX_ACTIONS:
                            cap_hit = "REPAIR_MAX_ACTIONS"; break
                        if symlinks >= REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        if _repair_radarr_movie(arr, mid, title, mfid, state):
                            acted += 1
                            symlinks += 1
                            if REPAIR_ITEM_INTERVAL > 0:
                                time.sleep(REPAIR_ITEM_INTERVAL)
            except Exception as e:
                log.warning("[repair:%s] sweep error: %s", arr.name, str(e)[:70])
        if acted or symlinks:
            log.info("[repair] symlink sweep: %d season(s)/movie(s), %d dead symlink(s) re-grabbed%s",
                     acted, symlinks, " (capped by %s)" % cap_hit if cap_hit else "")
        # season-pack check: find sonarr seasons that are fully downloaded but spread across multiple
        # parent dirs (individual episode grabs, not a season pack). Trigger a season search to upgrade.
        if REPAIR_SEASON_PACKS and acted < REPAIR_MAX_ACTIONS:
            sp_budget = REPAIR_MAX_ACTIONS - acted
            for arr in INSTANCES:
                if arr.kind != "sonarr" or sp_budget <= 0:
                    break
                try:
                    series = arr.series()
                except Exception:
                    continue
                for title, sn, sid, a in _sonarr_season_pack_check(arr, series):
                    if sp_budget <= 0:
                        break
                    if DRY_RUN:
                        log.info("[repair:season_pack] DRY-RUN would search season pack: %s S%02d", title, sn); sp_budget -= 1; continue
                    if a.command("SeasonSearch", seriesId=sid, seasonNumber=sn):
                        log.warning("[repair:season_pack] non-season-pack detected -> searching season pack: %s S%02d", title, sn)
                        sp_budget -= 1
                        if REPAIR_ITEM_INTERVAL > 0:
                            time.sleep(REPAIR_ITEM_INTERVAL)
        # MissingFromDisk check: query *arr history for items Sonarr/Radarr knows are gone from disk.
        # Runs after the symlink sweep so both modes share the REPAIR_MAX_ACTIONS budget.
        if REPAIR_MISSING_FROM_DISK and acted < REPAIR_MAX_ACTIONS:
            _missing_from_disk_check(state, acted, REPAIR_MAX_ACTIONS - acted)
