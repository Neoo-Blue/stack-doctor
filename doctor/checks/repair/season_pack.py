"""Detect seasons spread across multiple dirs and upgrade them to season packs."""
import os

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
