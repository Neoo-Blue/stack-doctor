"""Check: no_upgrade."""
from ..config import EN_NO_UPGRADE_PROFILE, NO_UPGRADE_PROFILE_ID, NO_UPGRADE_PROFILE_NAME, log
from ..clients import INSTANCES

def check_no_upgrade_profile():
    """Find ended Sonarr series that are 100% complete and move them to the no-upgrade profile."""
    if not EN_NO_UPGRADE_PROFILE:
        return

    sonarr_instances = [a for a in INSTANCES if a.kind == "sonarr"]
    if not sonarr_instances:
        log.warning("[no_upgrade_profile] no Sonarr instances configured")
        return

    for arr in sonarr_instances:
        # Resolve target profile id per-instance — each Sonarr may have different profile IDs
        target_id = NO_UPGRADE_PROFILE_ID
        try:
            if not target_id:
                profiles = arr.quality_profiles()
                match = next((p for p in profiles if p["name"] == NO_UPGRADE_PROFILE_NAME), None)
                if not match:
                    log.warning("[no_upgrade_profile:%s] profile %r not found — skipping", arr.name, NO_UPGRADE_PROFILE_NAME)
                    continue
                target_id = match["id"]
                log.info("[no_upgrade_profile:%s] resolved profile %r -> id %d", arr.name, NO_UPGRADE_PROFILE_NAME, target_id)

            # Fetch all series
            all_series = arr.series()
        except Exception as e:
            log.warning("[no_upgrade_profile:%s] fetch failed: %s", arr.name, e)
            continue
        log.debug("[no_upgrade_profile:%s] scanning %d series (target profile id=%d)",
                  arr.name, len(all_series), target_id)

        to_move = []
        for s in all_series:
            if s.get("status") != "ended":
                continue
            if s.get("qualityProfileId") == target_id:
                log.debug("[no_upgrade_profile:%s] already on target profile: %s", arr.name, s.get("title", "")[:60])
                continue
            stats = s.get("statistics", {})
            ep_count  = stats.get("episodeCount", 0)
            pct       = stats.get("percentOfEpisodes", 0)
            if ep_count > 0 and pct >= 100:
                to_move.append(s)
            else:
                log.debug("[no_upgrade_profile:%s] ended but incomplete (%.0f%%): %s",
                          arr.name, pct, s.get("title", "")[:60])

        if not to_move:
            log.debug("[no_upgrade_profile:%s] no newly completed ended shows found", arr.name)
            continue

        log.info("[no_upgrade_profile:%s] moving %d completed ended show(s) to profile %d (%s)",
                 arr.name, len(to_move), target_id, NO_UPGRADE_PROFILE_NAME)
        moved, failed = 0, 0
        for s in to_move:
            try:
                s["qualityProfileId"] = target_id
                arr.update_series(s)
                log.info("[no_upgrade_profile:%s] -> %s", arr.name, s["title"])
                moved += 1
            except Exception as e:
                log.warning("[no_upgrade_profile:%s] failed to update %s: %s", arr.name, s["title"], e)
                failed += 1

        log.info("[no_upgrade_profile:%s] done — moved:%d failed:%d", arr.name, moved, failed)
