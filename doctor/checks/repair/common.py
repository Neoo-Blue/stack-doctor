"""Helpers for the repair check."""
import os
from ...config import REPAIR_DEBRID_MOUNT, log

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
            log.debug("[repair] debrid mount %s OK (%d entries)", p, len(children))
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
