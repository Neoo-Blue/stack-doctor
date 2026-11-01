#!/usr/bin/env python3
"""Catch up on releases an indexer posted while it was down: arr RSS never sees them and nothing searches for them.
Reads the indexer's feed for the window through Prowlarr, matches it by IMDb/TVDB id against monitored missing and
cutoff-unmet items, and hands only those to probe_grab, one at a time, so an outage never turns into a bulk search.
usage: backfill_window.py START END [--dry-run]     (ISO times, UTC). Exit 0 ok, 3 too many items (nothing grabbed)."""
import datetime as D, email.utils, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_grab as pg, usenet

INDEXER = int(os.environ["WATCH_INDEXER_ID"])   # the indexer's Prowlarr id
MAX_ITEMS = 150                                 # ponytail: above this a person decides what a long outage deserves
NS = {"n": "http://www.newznab.com/DTD/2010/feeds/attributes/"}
PK = re.search(r"<ApiKey>(.*?)</ApiKey>", open(f"{pg.ARR_DIR}/prowlarr/config.xml").read())[1]


def feed(cat, start, end):
    out, off = [], 0
    while off < 20000:
        q = urllib.parse.urlencode({"t": "search", "cat": cat, "limit": 100, "offset": off, "extended": 1, "apikey": PK})
        root = ET.fromstring(urllib.request.urlopen(f"http://{pg.PROWLARR_HOSTPORT}/{INDEXER}/api?{q}", timeout=120).read())
        if root.tag == "error":
            raise RuntimeError(root.attrib.get("description", "indexer error"))
        items = root.findall(".//item")
        if not items:
            break
        for i in items:
            d = email.utils.parsedate_to_datetime(i.findtext("pubDate"))
            if start <= d <= end:
                a = {x.get("name"): x.get("value") for x in i.findall("n:attr", NS)}
                m = re.search(r"\bS(\d{1,2})(?:E(\d{1,3}))?\b", i.findtext("title"), re.I)
                out.append({"title": i.findtext("title"), "imdb": (a.get("imdb") or a.get("imdbid") or "").lstrip("t0"),
                            "tvdb": a.get("tvdbid"), "s": int(m[1]) if m else None, "e": int(m[2] or 0) if m else None})
        off += 100
        if d < start:
            break
        time.sleep(1)
    return out


def wanted_movies(arr):
    cut = {r["id"] for r in arr.api("GET", "wanted/cutoff?pageSize=5000&monitored=true")["records"]}
    return {m["imdbId"].lstrip("t0"): m for m in arr.api("GET", "movie") if m.get("imdbId") and m["monitored"]
            and (m["id"] in cut or (m.get("isAvailable") and not m["hasFile"]))}


def wanted_eps(arr):
    tv = {s["id"]: str(s.get("tvdbId")) for s in arr.api("GET", "series")}
    idx = {}
    for kind in ("missing", "cutoff"):
        for r in arr.api("GET", f"wanted/{kind}?pageSize=5000&monitored=true")["records"]:
            idx.setdefault((tv.get(r["seriesId"]), r["seasonNumber"]), {})[r["episodeNumber"]] = r
    return idx


def matches(name, arr, mv, tv):
    hits = {}
    if arr.tv:
        idx = wanted_eps(arr)
        for r in tv:
            for num, e in idx.get((r["tvdb"], r["s"]), {}).items():
                if r["e"] in (0, num):
                    hits[e["id"]] = bool(e.get("hasFile"))
    else:
        idx = wanted_movies(arr)
        for r in mv:
            if r["imdb"] in idx:
                hits[idx[r["imdb"]]["id"]] = idx[r["imdb"]]["hasFile"]
    return hits


def file_id(arr, iid):
    if arr.tv:
        return arr.api("GET", f"episode/{iid}").get("episodeFileId")
    return (arr.api("GET", f"movie/{iid}").get("movieFile") or {}).get("id")


def upgrade_one(arr, conns, iid, dry):
    """probe_grab skips anything with a file, so an upgrade RSS would have taken needs its own probe-then-grab."""
    old, field = file_id(arr, iid), "episodeId" if arr.tv else "movieId"
    cands = [r for r in arr.api("GET", f"release?{field}={iid}") if r.get("protocol") == "usenet" and r.get("approved")]
    for r in sorted(cands, key=lambda r: pg.rank(r, "English"))[:pg.MAX_CANDIDATES]:
        try:
            ids = pg.nzb_ids(r["downloadUrl"], "Sonarr" if arr.tv else "Radarr")
        except Exception as ex:
            pg.log(f"   [{arr.name}] upgrade {iid} nzb fetch failed ({str(ex)[:40]})"); continue
        if not ids or pg.complete(conns, ids)[1] < len(ids):
            continue
        if dry:
            pg.log(f"   [{arr.name}] upgrade {iid} would grab {r['title'][:66]}"); return True
        pg.calm(arr)
        arr.api("POST", "release", {"guid": r["guid"], "indexerId": r["indexerId"]})
        for _ in range(36):
            time.sleep(5)
            if file_id(arr, iid) != old:
                pg.log(f"   [{arr.name}] upgrade {iid} GRAB {r['title'][:66]}"); return True
        pg.log(f"   [{arr.name}] upgrade {iid} did not import: {r['title'][:55]}; next candidate")
    return False


def main():
    dry = "--dry-run" in sys.argv
    start, end = [D.datetime.fromisoformat(a).replace(tzinfo=D.timezone.utc) for a in sys.argv[1:3]]
    mv, tv = feed("2000", start, end), feed("5000", start, end)
    plan = {n: matches(n, pg.Arr(n), mv, tv) for n in pg.PORTS}
    total = sum(map(len, plan.values()))
    pg.log(f"=== backfill {start:%F %H:%M}..{end:%F %H:%M}: feed {len(mv)} movies, {len(tv)} tv, {total} wanted: " +
           ", ".join(f"{n} {sum(not v for v in p.values())} missing/{sum(p.values())} upgrade" for n, p in plan.items()))
    if total > MAX_ITEMS:
        return 3
    conns, got = [usenet.NNTP(p) for p in usenet.providers()], 0
    try:
        for n, items in plan.items():
            arr = pg.Arr(n)
            for iid, has in items.items():
                try:
                    got += bool((upgrade_one if has else pg.grab_one)(arr, conns, iid, dry))
                except Exception as ex:
                    pg.log(f"   [{n}] item {iid}: error {str(ex)[:80]}")
    finally:
        for c in conns:
            c.close()
    pg.log(f"=== backfill done: {got}/{total} grabbed{' (dry run)' if dry else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
