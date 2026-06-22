"""Filesystem-only orphan dead-symlink scanner."""
import os
from ...config import REPAIR_LIBS, log
from ...clients import INSTANCES
from .common import _dead_symlink

def _collect_known_paths():
    """Return a set of all file paths currently tracked by Sonarr/Radarr."""
    known = set()
    for arr in INSTANCES:
        if arr.kind not in ("sonarr", "radarr"):
            continue
        try:
            if arr.kind == "sonarr":
                for ser in arr.series():
                    sid = ser.get("id")
                    if not sid:
                        continue
                    for ef in arr.episode_files(sid):
                        fp = ef.get("path")
                        if fp:
                            known.add(fp)
            else:
                for m in arr.movies():
                    mf = m.get("movieFile") or {}
                    fp = mf.get("path")
                    if fp:
                        known.add(fp)
        except Exception as e:
            log.warning("[repair:orphan] failed to collect paths from %s: %s", arr.name, str(e)[:70])
    return known


def _orphan_dead_symlink_scan():
    """Walk REPAIR_LIBRARY_PATHS and report dead symlinks that are not tracked by *arr."""
    if not REPAIR_LIBS:
        return
    known = _collect_known_paths()
    log.debug("[repair:orphan] collected %d known path(s) from *arr, scanning %d library root(s)",
              len(known), len(REPAIR_LIBS))
    orphans = []
    for root in REPAIR_LIBS:
        if not os.path.isdir(root):
            log.warning("[repair:orphan] library path not a directory: %s", root)
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                fp = os.path.join(dirpath, name)
                if fp in known:
                    continue
                if not _dead_symlink(fp):
                    continue
                orphans.append(fp)
    if orphans:
        log.warning("[repair:orphan] found %d dead symlink(s) not tracked by *arr; manual cleanup may be needed", len(orphans))
        for fp in orphans[:20]:
            log.warning("[repair:orphan] %s", fp)
        if len(orphans) > 20:
            log.warning("[repair:orphan] ... and %d more", len(orphans) - 20)
    else:
        log.debug("[repair:orphan] no orphan dead symlinks found in %d library path(s)", len(REPAIR_LIBS))


