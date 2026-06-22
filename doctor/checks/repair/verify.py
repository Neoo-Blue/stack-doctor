"""Post-repair search verification."""
import re
import time
from datetime import datetime, timezone
from ...config import REPAIR_VERIFY_DEADLINE, log
from ...clients import INSTANCES

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
        log.debug("[repair:verify] no pending searches to verify")
        return
    log.debug("[repair:verify] checking %d pending search(es)", len(pv))
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
    pv = state.setdefault("__repair_verify__", {})
    # key is stable across sweeps; title slug + arr name
    key = "%s:%s" % (arr.name, re.sub(r"[^a-z0-9]+", "_", title.lower())[:40])
    pv[key] = {
        "arr_name":  arr.name,
        "title":     title,
        "cmd_id":    cmd_id if isinstance(cmd_id, int) else None,
        "media_id":  media_id,
        "entity_ids": entity_ids or [],
        "search_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "deadline":  time.time() + REPAIR_VERIFY_DEADLINE,
    }
