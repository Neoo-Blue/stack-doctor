"""Check: warmer."""
import os
import re
import time
import subprocess
import threading
from ..config import (
    host_load, log,
    PLEX_TOKEN, PLEX_URL,
    WARM_CONCURRENCY, WARM_COOLDOWN, WARM_HEAD_MB, WARM_INTERVAL,
    WARM_LOAD_MAX, WARM_LOW_CACHE, WARM_MAX_CYCLE, WARM_NEXT_EPS,
    WARM_NEXT_NEAR_END, WARM_ONDECK, WARM_ONDECK_EVERY, WARM_OPEN_CONC,
    WARM_PARTS, WARM_PATH_MAP, WARM_PLEXLOG_CMD, WARM_PLEXLOG_FILE,
    WARM_READ_TIMEOUT, WARM_RECENT_COUNT, WARM_SOURCES, WARM_TAIL_MB,
)
from ..clients import Plex

_warm_state = {}            # host_path -> last_warm_ts
_warm_lock = threading.Lock()
_warm_sem = threading.Semaphore(max(1, WARM_CONCURRENCY))        # background warming lane
_warm_sem_open = threading.Semaphore(max(1, WARM_OPEN_CONC))     # detail-page (you opened it) lane - separate so opens never wait
_warm_last_ondeck = [0.0]
_warm_count = [0]           # total warms since start (for the UI)
_warm_recent = []           # recent warms for the UI: [{"ts","title","why"}]

# Per-cycle metadata (single-element lists so tests can reset them by index)
_last_cycle_ts        = [0.0]    # unix ts when the last warm_cycle() started
_last_cycle_duration  = [0.0]    # seconds taken by the last cycle
_last_cycle_warmed    = [0]      # files warmed in the last cycle
_last_cycle_candidates = [0]     # candidate paths considered in the last cycle
_last_cycle_skipped_load = [False]  # True if the last cycle was skipped due to host load
def _warm_record(title, why):
    _warm_count[0] += 1
    _warm_recent.append({"ts": time.time(), "title": title, "why": why})
    if len(_warm_recent) > 80:
        del _warm_recent[:len(_warm_recent) - 80]
def _limit_parts(files):
    return files if WARM_PARTS <= 0 else files[:WARM_PARTS]
def _host_path(f):
    if WARM_PATH_MAP and ":" in WARM_PATH_MAP:
        a, b = WARM_PATH_MAP.split(":", 1)
        if f.startswith(a):
            return b + f[len(a):]
    return f
def _warm_file(path, reason="cycle"):
    p = _host_path(path)
    # a title you actively opened tolerates more load (2x) than speculative background warming, but
    # both still yield before meltdown; concurrency stays capped either way so a burst can't flood.
    guard = (WARM_LOAD_MAX * 2) if reason == "detail-page" else WARM_LOAD_MAX
    if guard > 0 and host_load() > guard:
        return False
    with _warm_lock:                                    # atomic claim: one warm per file per cooldown
        if time.time() - _warm_state.get(p, 0) < WARM_COOLDOWN:
            return False
        _warm_state[p] = time.time()
    try:
        sz = os.path.getsize(p)
    except Exception as e:
        _warm_state.pop(p, None)                         # release so it can be retried
        log.debug("[warmer] stat fail %s: %s", p, str(e)[:60]); return False
    head = min(WARM_HEAD_MB << 20, sz)
    tail = WARM_TAIL_MB > 0 and sz > head + (WARM_TAIL_MB << 20)
    res = {"got": 0, "err": None}
    def _do():
        try:
            with open(p, "rb", buffering=0) as fh:
                while res["got"] < head:
                    b = fh.read(min(4 << 20, head - res["got"]))
                    if not b: break
                    res["got"] += len(b)
                if tail:
                    fh.seek(sz - (WARM_TAIL_MB << 20))
                    while fh.read(4 << 20):
                        pass
        except Exception as e:
            res["err"] = str(e)[:60]
    t0 = time.time()
    sem = _warm_sem_open if reason == "detail-page" else _warm_sem   # opens get their own lane (instant)
    with sem:                                           # cap concurrent usenet pulls so warming never floods decypharr
        th = threading.Thread(target=_do, daemon=True); th.start(); th.join(WARM_READ_TIMEOUT)
    if th.is_alive():
        _warm_state.pop(p, None)
        log.warning("[warmer] read timed out (%ds, mount slow/hung?): %s", WARM_READ_TIMEOUT, os.path.basename(p))
        return False
    if res["err"]:
        _warm_state.pop(p, None)
        log.warning("[warmer] read fail %s: %s", os.path.basename(p), res["err"]); return False
    _warm_record(os.path.basename(p), reason)
    log.info("[warmer] warmed %dMB head%s in %.1fs: %s",
             res["got"] >> 20, "+%dMB tail" % WARM_TAIL_MB if tail else "",
             time.time() - t0, os.path.basename(p))
    return True
def _warm_targets(plex):
    """Ordered, de-duped list of (reason, plex_file_path) to warm this cycle."""
    targets, seen = [], set()
    def add(reason, path):
        if path and path not in seen:
            seen.add(path); targets.append((reason, path))
    sessions = plex.sessions()
    if "next" in WARM_SOURCES:                              # next episode(s) of anything playing
        for v in sessions:
            if v.get("type") != "episode" or not v.get("grandparentRatingKey"):
                continue
            if WARM_NEXT_NEAR_END > 0:                       # only warm the next ep once the current one nears the end
                try:
                    remain_min = (int(v.get("duration", 0)) - int(v.get("viewOffset", 0))) / 60000.0
                except Exception:
                    remain_min = 0
                if remain_min > WARM_NEXT_NEAR_END:
                    continue
            eps = plex.leaves(v.get("grandparentRatingKey"))
            idx = next((i for i, e in enumerate(eps) if e.get("ratingKey") == v.get("ratingKey")), -1)
            if idx >= 0:
                for e in eps[idx + 1: idx + 1 + WARM_NEXT_EPS]:
                    for f in _limit_parts(plex.parts(e.get("ratingKey"))):
                        add("next-ep", f)
    # Plex-first: speculative On Deck / recent warming pauses while ANYONE is watching (never competes
    # with a live stream), and is skipped entirely in low-cache mode (keep almost nothing pre-warmed).
    if not WARM_LOW_CACHE and not sessions and time.time() - _warm_last_ondeck[0] >= WARM_ONDECK_EVERY:
        _warm_last_ondeck[0] = time.time()
        if WARM_ONDECK and "ondeck" in WARM_SOURCES:        # Continue Watching / Up Next (WARMER_ONDECK is the on/off)
            for v in plex.ondeck():
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("ondeck", f)
        if "recent" in WARM_SOURCES and WARM_RECENT_COUNT > 0:
            for v in plex.recent(WARM_RECENT_COUNT):
                for f in _limit_parts(plex.parts(v.get("ratingKey"))):
                    add("recent", f)
    return targets
def warm_cycle():
    _t0 = time.time()
    _last_cycle_ts[0] = _t0
    if WARM_LOAD_MAX > 0 and host_load() > WARM_LOAD_MAX:
        log.info("[warmer] host load > %.0f -> skip cycle", WARM_LOAD_MAX)
        _last_cycle_skipped_load[0] = True
        _last_cycle_duration[0] = round(time.time() - _t0, 3)
        return
    _last_cycle_skipped_load[0] = False
    targets = _warm_targets(Plex(PLEX_URL, PLEX_TOKEN))
    _last_cycle_candidates[0] = len(targets)
    done = 0
    for reason, path in targets:
        if done >= WARM_MAX_CYCLE:
            break
        if _warm_file(path, reason):
            done += 1
    _last_cycle_warmed[0] = done
    _last_cycle_duration[0] = round(time.time() - _t0, 3)
    if done:
        log.info("[warmer] cycle warmed %d (of %d candidate paths)", done, len(targets))
def warmer_loop(stop):
    mode = (" | LOW-CACHE: no On Deck, next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_LOW_CACHE \
        else ((" | next ep @<=%dmin left" % WARM_NEXT_NEAR_END) if WARM_NEXT_NEAR_END else "")
    log.info("[warmer] started: head=%dMB tail=%dMB sources=%s poll=%ds ondeck-every=%ds%s",
             WARM_HEAD_MB, WARM_TAIL_MB, ",".join(WARM_SOURCES) or "-", WARM_INTERVAL, WARM_ONDECK_EVERY, mode)
    while not stop.is_set():
        try:
            warm_cycle()
        except Exception as e:
            log.error("[warmer] cycle error: %s", e)
        if stop.wait(WARM_INTERVAL):
            break
_PLEXLOG_RE = re.compile(r"/library/metadata/(\d+)(?:/extras|\?[^\s]*includeExtras=1)")
_playing = {"ts": 0.0, "rks": set()}
def _playing_rks(plex):
    """ratingKeys with an active Plex session, cached ~10s (Plex sends the same metadata query while
    you browse a title AND while you play it, so this tells the two apart)."""
    if time.time() - _playing["ts"] > 10:
        try: _playing["rks"] = set(v.get("ratingKey") for v in plex.sessions())
        except Exception: pass
        _playing["ts"] = time.time()
    return _playing["rks"]
def _warm_opened(plex, rk):
    if rk in _playing_rks(plex):                                # already playing (so already cached) -> not a new open
        return
    for f in _limit_parts(plex.parts(rk)):                      # warm just the top version(s) you'd actually play
        if _warm_file(f, "detail-page"):
            log.info("[warmer] you opened rk=%s -> warmed: %s", rk, os.path.basename(_host_path(f)))
def plexlog_loop(stop):
    """Tail Plex's server log; warm the exact title a viewer opens (true pre-play intent)."""
    cmd = WARM_PLEXLOG_CMD or ("tail -n0 -F %r" % WARM_PLEXLOG_FILE if WARM_PLEXLOG_FILE else "")
    if not cmd:
        return
    plex = Plex(PLEX_URL, PLEX_TOKEN)
    seen = {}                                                   # ratingKey -> last-handled ts
    log.info("[warmer] detail-page warming enabled (tailing Plex log)")
    while not stop.is_set():
        proc = None
        try:
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            for line in proc.stdout:
                if stop.is_set():
                    break
                m = _PLEXLOG_RE.search(line)
                if not m:
                    continue
                rk = m.group(1); now = time.time()
                if now - seen.get(rk, 0) < 300:                 # a detail page is polled repeatedly while open -> react once per item / 5 min
                    continue
                seen[rk] = now                                  # warm off-thread so the tailer stays responsive
                threading.Thread(target=_warm_opened, args=(plex, rk), daemon=True).start()
        except Exception as e:
            log.warning("[warmer] plexlog tail error: %s", str(e)[:80])
        finally:
            if proc:
                try: proc.terminate()
                except Exception: pass
        if stop.wait(10):                                       # tail died/rotated -> reconnect
            break
