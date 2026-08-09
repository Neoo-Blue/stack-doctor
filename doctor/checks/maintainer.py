"""Check: maintainer.

Deletes pulsarr-requested TV shows that have ended, were released before a configurable
year threshold, and haven't been watched in N days (per Tautulli).

For each deleted series:
  1. Creates per-user Pulsarr watchlist exclusions so the show won't be
     re-added on future Plex watchlist syncs.
  2. Severs from the Sonarr library (delete files + series record).

Users can remove the exclusion through Pulsarr to request the show again.
"""
import os
import time
from datetime import datetime, timezone
from ..config import (
    DRY_RUN, EN_MAINTAINER, MAINTAINER_LIBRARY_TITLE,
    MAINTAINER_MAX_ACTIONS, MAINTAINER_MIN_AGE_DAYS, MAINTAINER_MIN_YEAR,
    MAINTAINER_MODE, MAINTAINER_PLEX_SECTION_KEY,
    MAINTAINER_PULSARR_TAG_PREFIX, MAINTAINER_RECHECK,
    MAINTAINER_ROOT_FOLDER_PATHS, MAINTAINER_UNWATCHED_DAYS,
    PLEX_TOKEN, PLEX_URL, PULSARR_APIKEY, PULSARR_URL,
    TAUTULLI_APIKEY, TAUTULLI_URL, log,
)
from ..clients import INSTANCES
from ..clients.pulsarr import Pulsarr
from ..clients.tautulli import Tautulli
from ..state import state_transaction


def _pulsarr_tags(series, tag_map, prefix):
    out = set()
    for tid in series.get("tags", []) or []:
        label = tag_map.get(tid, "")
        if label and label.lower().startswith(prefix.lower()):
            out.add(label)
    return out


def _pulsarr_tagged_show(series, tag_map, prefix):
    return bool(_pulsarr_tags(series, tag_map, prefix))


def _tag_users(tag_labels, prefix):
    users = set()
    for label in tag_labels:
        rest = label[len(prefix):].strip()
        if rest.startswith("-user-"):
            username = rest[6:].strip()
        elif rest.startswith("user-"):
            username = rest[5:].strip()
        else:
            continue
        if username:
            users.add(username)
    return users


def _series_is_eligible(series, recently_watched, tag_map, prefix, now, mode):
    if series.get("year", 9999) >= MAINTAINER_MIN_YEAR:
        return False
    if series.get("status") != "ended":
        return False
    added = series.get("added", "")
    if added:
        try:
            added_dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
            age_days = (now - added_dt).total_seconds() / 86400
            if age_days < MAINTAINER_MIN_AGE_DAYS:
                return False
        except (ValueError, TypeError):
            pass
    if mode == "tagged" and not _pulsarr_tagged_show(series, tag_map, prefix):
        return False
    if series.get("title", "").strip() in recently_watched:
        return False
    return True


def _exclude_from_pulsarr(series, users, pulsarr):
    """Create per-user Pulsarr exclusions so the show won't be re-added."""
    tmdb_id = series.get("tmdbId")
    title = series.get("title", "?")
    if not tmdb_id:
        log.debug("[maintainer] no tmdbId for %s, cannot create exclusions", title)
        return 0

    user_ids = []
    for username in users:
        uid = pulsarr.user_id_for_plex_username(username)
        if uid:
            try:
                uid = int(uid)
            except (ValueError, TypeError):
                continue
            user_ids.append(uid)

    if user_ids:
        pulsarr.create_watchlist_exclusion(tmdb_id, media_type="tv",
                                            users=user_ids, title=title)
    return len(user_ids)


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

    pulsarr = None
    if PULSARR_URL and PULSARR_APIKEY:
        pulsarr = Pulsarr(PULSARR_URL, PULSARR_APIKEY,
                           db_path=os.environ.get("PULSARR_DB_PATH", ""))

    with state_transaction() as state:
        maintainer_state = state.setdefault("__maintainer__", {})
        deleted = 0
        excluded = 0
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

            allowed_roots = [p.strip() for p in MAINTAINER_ROOT_FOLDER_PATHS.split(",") if p.strip()]

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

                if allowed_roots:
                    rfp = (series.get("rootFolderPath") or "").rstrip("/")
                    if not any(rfp == p.rstrip("/") for p in allowed_roots):
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
                    excl_msg = ""
                    if users and pulsarr:
                        excl_msg = ", excluded=%d" % len(users)
                    log.info("[maintainer:%s] WOULD delete: %s (ended, year=%s, unwatched %s%s%s)",
                             arr.name, title, series.get("year", "?"),
                             ">=%dd" % MAINTAINER_UNWATCHED_DAYS,
                             excl_msg,
                             (", users=" + ",".join(sorted(users))) if users else "")
                    maintainer_state[state_key] = now
                    deleted += 1
                    continue

                if users and pulsarr:
                    n = _exclude_from_pulsarr(series, users, pulsarr)
                    if n:
                        excluded += n
                        log.info("[maintainer:%s] Pulsarr: %d exclusion(s) for %s",
                                 arr.name, n, title)

                try:
                    arr._req("DELETE", "/series/%d?deleteFiles=true" % sid)
                    log.info("[maintainer:%s] deleted: %s (id=%d, year=%s%s)",
                             arr.name, title, sid, series.get("year", "?"),
                             (", users=" + ",".join(sorted(users))) if users else "")
                    deleted += 1
                except Exception as e:
                    log.warning("[maintainer:%s] delete failed for %s: %s",
                                arr.name, title, str(e)[:70])

                maintainer_state[state_key] = time.time()

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

        report = ("[maintainer] sweep complete: %d deleted, %d excluded, "
                  "%d skipped (cooldown), %d cap" %
                  (deleted, excluded, candidates_skipped, MAINTAINER_MAX_ACTIONS))
        if DRY_RUN:
            report = "[maintainer DRY-RUN] " + report
        if deleted or candidates_skipped:
            log.info(report)
        else:
            log.debug(report)
