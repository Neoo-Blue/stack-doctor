#!/usr/bin/env python3
"""Probe before you grab, for a handful of arr items (episodes or movies). Shared by stack-doctor's backlog, repair and
queue checks so nothing in the stack grabs blind: each approved candidate's NZB is fetched, 20 spread articles are
STATed, and the first release whose articles all exist (on AltMount's first provider, or across providers since
AltMount fdfa7ee8 / upstream #975 falls back to slower providers) is grabbed, then the import is verified before
moving on. A failed import tries the next candidate.
usage: probe_grab.py sonarr|sonarr4k|radarr|radarr4k ID [ID ...] [--dry-run]
  IDs are episodeIds (sonarr*) or movieIds (radarr*). Exit 0 if anything was grabbed, 1 if nothing, 2 on error."""
import json, os, re, shlex, subprocess, sys, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usenet

PORTS = {"sonarr": 8989, "sonarr4k": 8990, "radarr": 7878, "radarr4k": 7879}
ARR_DIR = os.environ.get("ARR_CONFIG_DIR", "/opt/arr")   # <dir>/<instance>/config.xml holds each ApiKey
ARR_HOST = os.environ.get("ARR_HOST", "127.0.0.1")
PROWLARR_HOSTPORT = os.environ.get("PROWLARR_HOSTPORT", ARR_HOST + ":9696")
FETCH_VIA_HOSTS = {h for h in os.environ.get("PROBE_FETCH_VIA_HOSTS", "").split(",") if h}   # hosts only reachable through...
FETCH_VIA_CMD = shlex.split(os.environ.get("PROBE_FETCH_VIA_CMD", ""))                 # ...this prefix, e.g. "docker exec indexer"
LOG = os.environ.get("PROBE_GRAB_LOG", "/var/log/probe_grab.log")
PAUSE = os.environ.get("PROBE_GRAB_PAUSE_FILE", "/run/probe_grab.pause")   # touch it so no import is cut off mid-flight
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "8"))
VIDEO_EXT = ("mkv", "mp4", "avi", "m4v", "ts", "mov", "webm", "mpg", "mpeg", "wmv", "flv")


def log(m):
    line = time.strftime("%F %T ") + m
    print(line, flush=True)
    open(LOG, "a").write(line + "\n")


def _open(req, m):
    """urlopen().read() with retries for GETs: Sonarr answers 500 now and then under search load."""
    for i in range(4 if m == "GET" else 1):
        try:
            return urllib.request.urlopen(req, timeout=900).read()
        except urllib.error.HTTPError as e:
            if m != "GET" or i == 3 or e.code < 500:
                raise
            time.sleep(5 * (i + 1))


class Arr:
    def __init__(self, name):
        self.name, self.tv = name, name.startswith("sonarr")
        self.port = PORTS[name]
        self.key = re.search(r"<ApiKey>(.*?)</ApiKey>",
                             open(f"{ARR_DIR}/{name}/config.xml").read())[1]

    def api(self, m, p, b=None):
        req = urllib.request.Request(f"http://{ARR_HOST}:{self.port}/api/v3/{p}", method=m,
                                     data=None if b is None else json.dumps(b).encode(),
                                     headers={"X-Api-Key": self.key, "Content-Type": "application/json"})
        raw = _open(req, m)
        return json.loads(raw) if raw else None


def importable(subjects):
    """AltMount streams, never extracts: a post needs a video file or a RAR set. Split binaries (name.avi.001) and
    par2-only posts fail with "no files with allowed extensions found"."""
    for s in subjects:
        names = re.findall(rb"&quot;([^&]+)&quot;|\"([^\"]+)\"", s)
        name = next((a or b for a, b in reversed(names)), s).decode(errors="replace").strip()
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if ext in VIDEO_EXT or ext == "rar" or re.fullmatch(r"r\d\d", ext):
            return True
    return False


def ids_from(raw, n=20):
    files = re.findall(rb'<file[^>]*subject="([^"]*)"[^>]*>(.*?)</file>', raw, re.S)
    if not importable([subj for subj, _ in files]):
        return None
    segs = [m.decode() for subj, body in files if not re.search(rb"\.par2|vol\d+\+\d+", subj, re.I)
            for m in re.findall(rb"<segment[^>]*>([^<]+)</segment>", body)]
    return segs if len(segs) <= n else [segs[int(i * (len(segs) - 1) / (n - 1))] for i in range(n)]


def nzb_ids(url, ua):
    u = urllib.parse.urlsplit(url)
    if u.hostname in ("prowlarr", "localhost", "127.0.0.1"):
        url = urllib.parse.urlunsplit((u.scheme, PROWLARR_HOSTPORT, u.path, u.query, u.fragment))
    elif u.hostname in FETCH_VIA_HOSTS:
        out = subprocess.run(FETCH_VIA_CMD + ["curl", "-fsS", "-m", "120", url], capture_output=True, timeout=180)
        if out.returncode != 0:
            raise RuntimeError("indexer fetch failed")
        return ids_from(out.stdout)
    return ids_from(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=120).read())


def rank(r, orig):
    """The arrs return releases in indexer order. Sort as the arr would, original language first."""
    langs = {l.get("name") for l in (r.get("languages") or [])}
    lang = 0 if orig and langs == {orig} else (1 if orig and orig in langs else 2)
    return (lang, -(r.get("qualityWeight") or 0), -(r.get("customFormatScore") or 0), -(r.get("size") or 0))


def calm(arr):
    while os.path.exists(PAUSE):
        time.sleep(30)
    for _ in range(90):
        busy = [r for r in arr.api("GET", "queue?pageSize=500").get("records", [])
                if r.get("trackedDownloadState") == "importing"]
        if not busy and subprocess.run(["pgrep", "-c", "-x", "ffprobe"], capture_output=True, text=True).stdout.strip() in ("", "0"):
            return
        time.sleep(20)


def imported(arr, iid, since):
    """Wait out the import; a complete post can still fail there (extension-less obfuscated file, short RAR set)."""
    item, hist = ("episode", "episodeId") if arr.tv else ("movie", "movieId")
    for _ in range(36):
        time.sleep(5)
        if arr.api("GET", f"{item}/{iid}").get("hasFile"):
            return True
        recs = arr.api("GET", f"history?{hist}={iid}&pageSize=5&sortKey=date&sortDirection=descending")["records"]
        if any(h["eventType"] == "downloadFailed" and h["date"] > since for h in recs):
            return False
    return False


def complete(conns, ids):
    """(first_provider_count, union_count): an article counts for the union if ANY provider has it."""
    first = union = 0
    for m in ids:
        if conns[0].has(m):
            first += 1; union += 1
        elif any(c.has(m) for c in conns[1:]):
            union += 1
    return first, union


def grab_one(arr, conns, iid, dry):
    if arr.tv:
        e = arr.api("GET", f"episode/{iid}")
        s = arr.api("GET", f"series/{e['seriesId']}")
        name = "%s S%02dE%02d" % (s["title"], e["seasonNumber"], e["episodeNumber"])
        if e.get("hasFile") or not e.get("monitored"):
            return None
        orig = (s.get("originalLanguage") or {}).get("name") or "English"
        cands = [r for r in arr.api("GET", f"release?episodeId={iid}") if r.get("protocol") == "usenet"
                 and r.get("approved") and len(r.get("episodeNumbers") or []) == 1]
    else:
        mv = arr.api("GET", f"movie/{iid}")
        name = f"{mv['title']} ({mv.get('year')})"
        if mv.get("hasFile") or not mv.get("monitored"):
            return None
        orig = (mv.get("originalLanguage") or {}).get("name") or "English"
        cands = [r for r in arr.api("GET", f"release?movieId={iid}") if r.get("protocol") == "usenet" and r.get("approved")]
    if not cands:
        log(f"   [{arr.name}] {name}: no approved release")
        return False
    for r in sorted(cands, key=lambda r: rank(r, orig))[:MAX_CANDIDATES]:
        try:
            ids = nzb_ids(r["downloadUrl"], "Sonarr" if arr.tv else "Radarr")
        except Exception as ex:
            log(f"   [{arr.name}] {name} {r['title'][:55]}: nzb fetch failed ({str(ex)[:40]})"); continue
        if ids is None:
            log(f"   [{arr.name}] {name} skip {r['title'][:55]}: archive-only nzb, AltMount cannot import it"); continue
        if not ids:
            continue
        first, union = complete(conns, ids)
        if union < len(ids):
            log(f"   [{arr.name}] {name} skip {r['title'][:55]}: {union}/{len(ids)} articles on any provider"); continue
        where = "first provider" if first == len(ids) else "across providers"
        if dry:
            log(f"   [{arr.name}] {name} would grab {r['title'][:60]} ({r['size']/1e9:.1f} GB, complete {where})")
            return True
        calm(arr)
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5))
        arr.api("POST", "release", {"guid": r["guid"], "indexerId": r["indexerId"]})
        if imported(arr, iid, since):
            log(f"   [{arr.name}] {name} GRAB {r['title'][:66]} ({r['size']/1e9:.1f} GB, complete {where})")
            return True
        log(f"   [{arr.name}] {name} grabbed but did not import: {r['title'][:55]}; next candidate")
    log(f"   [{arr.name}] {name}: nothing importable in top {MAX_CANDIDATES}")
    return False


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if len(args) < 2 or args[0] not in PORTS:
        print(__doc__); return 2
    arr, iids = Arr(args[0]), [int(x) for x in args[1:]]
    conns = [usenet.NNTP(p) for p in usenet.providers()]
    got = 0
    try:
        for iid in iids:
            try:
                got += bool(grab_one(arr, conns, iid, dry))
            except Exception as ex:   # one bad item must not sink the batch
                log(f"   [{arr.name}] item {iid}: error {str(ex)[:80]}")
    finally:
        for c in conns:
            c.close()
    log(f"=== probe_grab {arr.name}: {got}/{len(iids)} grabbed{' (dry run)' if dry else ''}")
    return 0 if got else 1


if __name__ == "__main__":
    sys.exit(main())
