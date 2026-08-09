"""Check: maintainer.

Deletes pulsarr-requested TV shows that have ended, were released before a configurable
year threshold, and haven't been watched in N days (per Tautulli). Only operates on the
configured Sonarr library (not anime_shows or movies).

For each deleted series:
  1. Severs from the Sonarr library (delete files + series record).  The Sonarr
     tags disappear with the series.
  2. Deletes the Pulsarr watchlist_items DB record so it won't try to re-sync.
  3. Pulsarr's own tag-based delete-sync handles per-user Plex watchlist removal
     on its next sweep (or when triggered manually).
"""
import os
import subprocess
import time
from datetime import datetime, timezone
from ..config import (
    DRY_RUN, EN_MAINTAINER, MAINTAINER_LIBRARY_TITLE,
    MAINTAINER_MAX_ACTIONS, MAINTAINER_MIN_AGE_DAYS, MAINTAINER_MIN_YEAR,
    MAINTAINER_MODE, MAINTAINER_PLEX_SECTION_KEY,
    MAINTAINER_PULSARR_TAG_PREFIX, MAINTAINER_RECHECK,
    MAINTAINER_UNWATCHED_DAYS, PLEX_TOKEN, PLEX_URL, PULSARR_DB_PATH,
    TAUTULLI_APIKEY, TAUTULLI_URL, log,
)
from ..clients import INSTANCES
from ..clients.tautulli import Tautulli
from ..state import state_transaction


def _pulsarr_tags(series, tag_map, prefix):
    """Return the set of Pulsarr-related tag labels from a series.

    Extracts all tag labels whose lowercased form starts with *prefix*.
    For tags like ``pulsarr-alice``, ``pulsarr-bob``, the returned set
    contains the full labels so callers can derive the Plex username.
    """
    out = set()
    for tid in series.get("tags", []) or []:
        label = tag_map.get(tid, "")
        if label and label.lower().startswith(prefix.lower()):
            out.add(label)
    return out


def _pulsarr_tagged_show(series, tag_map, prefix):
    """Return True if the series has at least one tag matching the pulsarr prefix."""
    return bool(_pulsarr_tags(series, tag_map, prefix))


def _tag_users(tag_labels, prefix):
    """Extract Plex usernames from pulsarr tag labels.

    Given a tag label like ``pulsarr-alice`` and prefix ``pulsarr-``,
    returns the suffix ``alice`` as the username.
    """
    users = set()
    for label in tag_labels:
        suffix = label[len(prefix):].strip()
        if suffix:
            users.add(suffix)
    return users


def _series_is_eligible(series, recently_watched, tag_map, prefix, now, mode):
    """Determine if a series is eligible for deletion — ordered by cheapest check first."""
    # 1. Year — free field on the series dict
    if series.get("year", 9999) >= MAINTAINER_MIN_YEAR:
        return False
    # 2. Status — free field
    if series.get("status") != "ended":
        return False
    # 3. Added age — must have been in Sonarr long enough
    added = series.get("added", "")
    if added:
        try:
            added_dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
            age_days = (now - added_dt).total_seconds() / 86400
            if age_days < MAINTAINER_MIN_AGE_DAYS:
                return False
        except (ValueError, TypeError):
            pass
    # 4. Pulsarr tag — only checked in 'tagged' mode
    if mode == "tagged" and not _pulsarr_tagged_show(series, tag_map, prefix):
        return False
    # 5. Tautulli watch check — most expensive (requires API call)
    if series.get("title", "").strip() in recently_watched:
        return False
    return True


def _pulsarr_delete_watchlist_records(tvdb_id):
    """Delete Pulsarr watchlist_items records matching a tvdbId."""
    if not PULSARR_DB_PATH or not os.path.exists(PULSARR_DB_PATH):
        return 0
    try:
        sql = (
            "DELETE FROM watchlist_items WHERE type='show' AND guids LIKE "
            "'%%\"tvdb:%d\"%%'; SELECT changes();"
        ) % int(tvdb_id)
        proc = subprocess.run(
            ["sqlite3", PULSARR_DB_PATH, sql],
            capture_output=True, text=True, timeout=5,
        )
        return int(proc.stdout.strip() or 0)
    except Exception as e:
        log.debug("[maintainer] pulsarr delete query failed: %s", str(e)[:60])
        return 0


def check_maintainer():
    if not EN_MAINTAINER:
        return
    if not TAUTULLI_URL or not TAUTULLI_APIKEY:
        log.debug("[maintainer] TAUTULLI_URL/TAUTULLI_APIKEY not set")
        return

    tautulli = Tautulli(TAUTULLI_URL, TAUTULLI_APIKEY)
    recently_watched = tautulli.recently_watched_shows(MAINTAINER_UNWATCHED_DAYS)
    log.debug("[maintainer] Tautulli: %d show(s) watched in %d days",
              len(recently_watched), MAINTAINER_UNWATCHED_DAYS)

    if PULSARR_DB_PATH and not os.path.exists(PULSARR_DB_PATH):
        log.warning("[maintainer] PULSARR_DB_PATH set but file not found: %s", PULSARR_DB_PATH)

    with state_transaction() as state:
        maintainer_state = state.setdefault("__maintainer__", {})
        deleted = 0
        db_cleaned = 0
        candidates_skipped = 0

        for arr in INSTANCES:
            if arr.kind != "sonarr":
                continue
            if MAINTAINER_LIBRARY_TITLE not in arr.name:
                log.debug("[maintainer] skipping %s (not matching library title filter '%s')",
                          arr.name, MAINTAINER_LIBRARY_TITLE)
                continue

            series_list = arr.series()
            if not series_list:
                continue

            tag_map = arr.tag_map()
            if MAINTAINER_MODE == "tagged" and not tag_map:
                log.debug("[maintainer:%s] no tags found", arr.name)
                continue

            log.debug("[maintainer:%s] %d tag(s), %d show(s) watched recently",
                      arr.name, len(tag_map), len(recently_watched))

            for series in series_list:
                if deleted >= MAINTAINER_MAX_ACTIONS:
                    log.info("[maintainer:%s] action cap (%d) reached",
                             arr.name, MAINTAINER_MAX_ACTIONS)
                    break

                sid = series.get("id")
                if not sid:
                    continue

                if MAINTAINER_MODE == "tagged" and not _pulsarr_tagged_show(
                    series, tag_map, MAINTAINER_PULSARR_TAG_PREFIX):
                    continue

                if not _series_is_eligible(series, recently_watched, tag_map,
                                           MAINTAINER_PULSARR_TAG_PREFIX,
                                           datetime.now(timezone.utc),
                                           MAINTAINER_MODE):
                    continue

                title = series.get("title", "?")
                tag_labels = _pulsarr_tags(series, tag_map, MAINTAINER_PULSARR_TAG_PREFIX)
                users = _tag_users(tag_labels, MAINTAINER_PULSARR_TAG_PREFIX)

                state_key = "%s:%s" % (arr.name, sid)
                last_eval = maintainer_state.get(state_key)
                now = time.time()
                if last_eval and now - last_eval < MAINTAINER_RECHECK:
                    candidates_skipped += 1
                    continue

                if DRY_RUN:
                    log.info("[maintainer:%s] WOULD delete: %s (ended, year=%s, unwatched %s%s)",
                             arr.name, title, series.get("year", "?"),
                             ">=%dd" % MAINTAINER_UNWATCHED_DAYS,
                             (", users=" + ",".join(sorted(users))) if users else "")
                    maintainer_state[state_key] = now
                    deleted += 1
                    continue

                delete_success = False
                try:
                    arr._req("DELETE", "/series/%d?deleteFiles=true" % sid)
                    delete_success = True
                    log.info("[maintainer:%s] deleted: %s (id=%d, year=%s%s)",
                             arr.name, title, sid, series.get("year", "?"),
                             (", users=" + ",".join(sorted(users))) if users else "")
                except Exception as e:
                    log.warning("[maintainer:%s] delete failed for %s: %s",
                                arr.name, title, str(e)[:70])
                    maintainer_state[state_key] = now

                if not delete_success:
                    continue

                deleted += 1
                maintainer_state[state_key] = now

                # Pulsarr cleanup only for tagged shows
                if not users:
                    continue
                tvdb_id = series.get("tvdbId")
                if tvdb_id:
                    n = _pulsarr_delete_watchlist_records(tvdb_id)
                    if n:
                        db_cleaned += 1
                        log.info("[maintainer:%s] Pulsarr DB: %d record(s) removed for %s",
                                 arr.name, n, title)

        # In "all" mode, empty Plex trash to clean up dead entries after deletions
        if MAINTAINER_MODE == "all" and deleted and MAINTAINER_PLEX_SECTION_KEY:
            if not DRY_RUN:
                try:
                    import urllib.request
                    url = "%s/library/sections/%d/emptyTrash?X-Plex-Token=%s" % (
                        PLEX_URL.rstrip("/"), MAINTAINER_PLEX_SECTION_KEY, PLEX_TOKEN)
                    urllib.request.urlopen(urllib.request.Request(url, method="PUT"), timeout=120)
                    log.info("[maintainer] Plex: emptyTrash section %d", MAINTAINER_PLEX_SECTION_KEY)
                except Exception as e:
                    log.warning("[maintainer] Plex emptyTrash failed: %s", str(e)[:60])

        report = ("[maintainer] sweep complete: %d deleted, %d db cleaned, "
                  "%d skipped (cooldown), %d cap" %
                  (deleted, db_cleaned, candidates_skipped, MAINTAINER_MAX_ACTIONS))
        if DRY_RUN:
            report = "[maintainer DRY-RUN] " + report
        if deleted or candidates_skipped:
            log.info(report)
        else:
            log.debug(report)
