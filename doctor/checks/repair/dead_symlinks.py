"""Dead symlink detection and repair actions."""
from ...config import DRY_RUN, REPAIR_LIBS, REPAIR_UNMONITORED, REPAIR_VERIFY, log
from .common import _dead_symlink
from .verify import _repair_record_verify

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

