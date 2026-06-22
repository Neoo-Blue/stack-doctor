"""Main repair check orchestrator."""
import time
from ...config import (
    DRY_RUN, host_load, REPAIR_ITEM_INTERVAL, REPAIR_LOAD_MAX, REPAIR_MAX_ACTIONS,
    REPAIR_MAX_SYMLINKS, REPAIR_MISSING_FROM_DISK, REPAIR_ORPHAN_SCAN,
    REPAIR_SEASON_PACKS, REPAIR_VERIFY, log,
)
from ...clients import INSTANCES
from ...state import state_transaction
from .common import _debrid_mount_ok
from .dead_symlinks import _radarr_dead_files, _sonarr_dead_files, _repair_radarr_movie, _repair_sonarr_season
from .season_pack import _sonarr_season_pack_check
from .missing_from_disk import _missing_from_disk_check
from .verify import _repair_verify_pending
from .orphan import _orphan_dead_symlink_scan

def check_repair():
    if not INSTANCES:
        log.warning("[repair] no Sonarr/Radarr instances configured"); return
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
                    log.debug("[repair:%s] scanning %d series for dead symlinks", arr.name, len(series))
                    for sid, title, sn, efids in _sonarr_dead_files(arr, series):
                        if acted >= REPAIR_MAX_ACTIONS:
                            cap_hit = "REPAIR_MAX_ACTIONS"; break
                        if symlinks >= REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        count = len(efids)
                        if symlinks + count > REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        log.debug("[repair:%s] dead symlink(s) found: %s S%02d (%d file(s))",
                                  arr.name, title, sn, count)
                        if _repair_sonarr_season(arr, sid, title, sn, efids, state):
                            acted += 1
                            symlinks += count
                            if REPAIR_ITEM_INTERVAL > 0:
                                time.sleep(REPAIR_ITEM_INTERVAL)
                else:
                    movies = arr.movies()
                    log.debug("[repair:%s] scanning %d movies for dead symlinks", arr.name, len(movies))
                    for mid, title, mfid in _radarr_dead_files(movies):
                        if acted >= REPAIR_MAX_ACTIONS:
                            cap_hit = "REPAIR_MAX_ACTIONS"; break
                        if symlinks >= REPAIR_MAX_SYMLINKS:
                            cap_hit = "REPAIR_MAX_SYMLINKS"; break
                        log.debug("[repair:%s] dead symlink found: %s", arr.name, title)
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
        else:
            log.debug("[repair] symlink sweep: no dead symlinks found")
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
                    log.debug("[repair:season_pack:%s] multi-dir season detected: %s S%02d", arr.name, title, sn)
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
            acted = _missing_from_disk_check(state, acted, REPAIR_MAX_ACTIONS - acted)
    # Orphan scan runs outside the state_transaction: it is a read-only filesystem walk that
    # neither reads nor writes the state dict, and can take seconds on large libraries.  Holding
    # STATE_LOCK for the entire walk would block every other concurrent check unnecessarily.
    if REPAIR_ORPHAN_SCAN:
        _orphan_dead_symlink_scan()
