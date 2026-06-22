"""Check: multipack — find cached multi-season torrent packs and push them to the download client.

Sonarr's automatic SeasonSearch only grabs single-season releases; it hard-rejects any torrent
whose title spans multiple seasons (e.g. "Show S01-S05").  When you search manually in the
Sonarr UI and pick such a pack, Sonarr bypasses its own rejection logic via /api/v3/release/push.
This check automates exactly that:

  1. Only consider series that missing_seasons has already SeasonSearch-ed at least once —
     meaning Sonarr tried per-season searches but the show is still incomplete.
  2. Call Sonarr's release-search API for Season 1 (same data the UI shows).
  3. Filter to multi-season packs: fullSeason=True AND title matches S\\d+-S\\d+.
  4. Discard packs whose season range has zero overlap with the show's incomplete seasons
     (e.g. S01-S04 pack when only S05-S07 are missing is useless).
  5. Sort remaining packs by coverage of incomplete seasons (most covered first), then by
     total pack width as a tie-breaker — prefer S01-S07 over S01-S06.
  6. For each candidate (best first), verify it is debrid-cached by checking whether its
     folder name exists in a normalised snapshot of the zurg __all__ mount taken once per
     sweep (O(1) lookup rather than O(n) directory scan per pack).
  7. Push the first cached pack via Sonarr's /api/v3/release/push, which bypasses
     quality/format rejection and delivers it straight to decypharr.

By scoping to already-searched series, the check avoids hammering Prowlarr with
release searches for shows that missing_seasons hasn't tried yet.
"""
import os
import re
import time
from ..config import (
    DECY_MOUNT_TEST, DRY_RUN, MULTIPACK_ENABLED, MULTIPACK_ITEM_INTERVAL,
    MULTIPACK_MAX_ACTIONS, MULTIPACK_RECHECK, log,
)
from ..clients import INSTANCES
from ..state import state_transaction
from .missing_seasons import searched_series as _ms_searched_series

# Detects multi-season pack titles: S01-S05, S1-S3, S01-S02, etc.
_MULTI_SEASON_RE = re.compile(r'\bS(\d+)[.\-]S(\d+)\b', re.IGNORECASE)


def _pack_season_range(title):
    """Parse the season range from a multi-season pack title.

    Returns (first_season, last_season) as ints, or None if not parseable.
    E.g. "Show S01-S05 BluRay" -> (1, 5)
    """
    m = _MULTI_SEASON_RE.search(title)
    if not m:
        return None
    s1, s2 = int(m.group(1)), int(m.group(2))
    if s1 > s2:
        s1, s2 = s2, s1
    return s1, s2


def _incomplete_seasons_covered(pack_range, incomplete_season_numbers):
    """Return the count of incomplete seasons that fall within the pack's range.

    pack_range: (first, last) ints from _pack_season_range()
    incomplete_season_numbers: set of int season numbers that are missing/partial
    """
    first, last = pack_range
    return sum(1 for sn in incomplete_season_numbers if first <= sn <= last)


def _normalize(s):
    """Strip all non-alphanumeric chars for fuzzy folder-name matching."""
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _zurg_cache_set(mount_path):
    """Return a frozenset of normalised folder names from the zurg __all__ mount.

    Built once per sweep so individual cache lookups are O(1) set membership
    tests rather than O(n) directory scans.  Returns None if the mount is not
    accessible.
    """
    if not mount_path or not os.path.isdir(mount_path):
        return None
    try:
        return frozenset(_normalize(e) for e in os.listdir(mount_path))
    except OSError:
        return None


def _is_cached(pack_title, cache_set):
    """Return True if pack_title matches any entry in cache_set (O(1))."""
    return _normalize(pack_title) in cache_set


def _incomplete_series(arr):
    """Return dict of series_id -> (title, incomplete_season_numbers) for monitored series
    that have at least one incomplete/missing season.

    incomplete_season_numbers is a frozenset of int season numbers with
    episodeFileCount < totalEpisodeCount.
    """
    try:
        all_series = arr.series()
    except Exception as e:
        log.warning("[multipack:%s] failed to fetch series: %s", arr.name, str(e)[:60])
        return {}
    result = {}
    for ser in all_series:
        if not ser.get("monitored"):
            continue
        incomplete = set()
        for season in (ser.get("seasons") or []):
            sn = season.get("seasonNumber", 0)
            if sn == 0 or not season.get("monitored"):
                continue
            st = season.get("statistics") or {}
            tc = st.get("totalEpisodeCount", 0)
            fc = st.get("episodeFileCount", 0)
            if tc > 0 and fc < tc:
                incomplete.add(sn)
        if incomplete:
            result[ser["id"]] = ((ser.get("title") or "")[:60], frozenset(incomplete))
    return result


def _rank_packs(packs, incomplete_seasons):
    """Sort packs best-first and return (pack, range, covered) triples.

    Each triple contains the raw release dict, its parsed (first, last) season
    range, and the count of incomplete seasons it covers.  Storing these avoids
    re-parsing the title in the caller's push loop.

    Sort order:
      Primary:   most incomplete seasons covered (descending)
      Secondary: widest pack range (descending) — S01-S07 beats S01-S06
      Tertiary:  highest qualityWeight (descending)

    Packs that cover zero incomplete seasons or have an unparseable title are
    excluded entirely.
    """
    ranked = []
    for pack in packs:
        pr = _pack_season_range(pack.get("title", ""))
        if pr is None:
            continue
        covered = _incomplete_seasons_covered(pr, incomplete_seasons)
        if covered == 0:
            continue
        width = pr[1] - pr[0]
        qw = pack.get("qualityWeight", 0)
        ranked.append((-covered, -width, -qw, pack, pr, covered))
    ranked.sort(key=lambda x: (x[0], x[1], x[2]))
    return [(x[3], x[4], x[5]) for x in ranked]


def check_multipack():
    """For incomplete Sonarr series that missing_seasons has already tried, search for and push
    cached multi-season packs that Sonarr would normally reject."""
    if not MULTIPACK_ENABLED:
        return
    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        return

    # Build zurg cache set once for the entire sweep (O(1) lookups per pack)
    cache_set = _zurg_cache_set(DECY_MOUNT_TEST)
    if cache_set is None:
        log.warning("[multipack] DECY_MOUNT_TEST not accessible — cannot verify debrid cache")
        return

    with state_transaction() as state:
        mp = state.setdefault("__multipack__", {})
        now = time.time()
        acted = 0

        for arr in sonarr_instances:
            if acted >= MULTIPACK_MAX_ACTIONS:
                break

            # Only target series missing_seasons has already tried
            already_searched = _ms_searched_series(state, arr.name)
            if not already_searched:
                log.debug("[multipack:%s] no series searched by missing_seasons yet — skipping", arr.name)
                continue

            incomplete_map = _incomplete_series(arr)
            # Intersection: incomplete AND already tried by missing_seasons
            candidates = {sid: info for sid, info in incomplete_map.items()
                          if sid in already_searched}
            log.debug("[multipack:%s] %d series targeted (incomplete + already SeasonSearched)",
                      arr.name, len(candidates))

            for sid, (title, incomplete_seasons) in candidates.items():
                if acted >= MULTIPACK_MAX_ACTIONS:
                    break

                state_key = "%s:%d" % (arr.name, sid)
                if now - mp.get(state_key, 0) < MULTIPACK_RECHECK:
                    log.debug("[multipack:%s] cooldown: %s", arr.name, title)
                    continue

                log.debug("[multipack:%s] searching releases for: %s (missing S%s)",
                          arr.name, title,
                          "+S".join(str(s) for s in sorted(incomplete_seasons)))
                releases = arr.release_search(sid, season_number=1)
                if not releases:
                    mp[state_key] = now
                    continue

                # Filter to multi-season packs, rank by coverage of incomplete seasons
                raw_packs = [r for r in releases
                             if r.get("fullSeason") and _MULTI_SEASON_RE.search(r.get("title", ""))]
                ranked = _rank_packs(raw_packs, incomplete_seasons)

                log.debug("[multipack:%s] %s — %d release(s), %d multi-season pack(s), "
                          "%d with overlap to missing seasons",
                          arr.name, title, len(releases), len(raw_packs), len(ranked))

                pushed = False
                for pack, pr, covered in ranked:
                    pack_title = pack.get("title", "")
                    if not _is_cached(pack_title, cache_set):
                        log.debug("[multipack:%s] not cached (covers %d missing season(s)): %s",
                                  arr.name, covered, pack_title[:70])
                        continue
                    if DRY_RUN:
                        log.info("[multipack:%s] DRY-RUN would push (covers %d/%d missing season(s)): "
                                 "%s -> %s",
                                 arr.name, covered, len(incomplete_seasons),
                                 title, pack_title[:70])
                        mp[state_key] = now
                        acted += 1
                        pushed = True
                        break
                    if arr.release_push(pack):
                        log.warning("[multipack:%s] pushed cached pack (covers %d/%d missing "
                                    "season(s)): %s -> %s",
                                    arr.name, covered, len(incomplete_seasons),
                                    title, pack_title[:70])
                        mp[state_key] = now
                        acted += 1
                        pushed = True
                        if MULTIPACK_ITEM_INTERVAL > 0:
                            time.sleep(MULTIPACK_ITEM_INTERVAL)
                        break

                if not pushed:
                    log.debug("[multipack:%s] no cached pack with overlap found: %s", arr.name, title)
                    mp[state_key] = now

        if acted:
            log.info("[multipack] pushed %d cached multi-season pack(s) this sweep", acted)
        else:
            log.debug("[multipack] no cached multi-season packs found this sweep")
