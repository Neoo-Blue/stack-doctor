"""HTTP API clients: Arr (Sonarr/Radarr/Prowlarr), Plex, Seerr + instance loader."""
import os
import json
from datetime import datetime
import time
import urllib.request
import urllib.error
import socket
import xml.etree.ElementTree as ET
from typing import Optional
from .config import BLOCKLIST, REMOVE_CLIENT, TIMEOUT, log

class Arr:
    def __init__(self, name: str, kind: str, url: str, apikey: str):
        self.name, self.kind = name, kind                       # sonarr | radarr | prowlarr
        self.base = url.rstrip("/") + ("/api/v1" if kind == "prowlarr" else "/api/v3")
        self.apikey = apikey
        self.unknown = "includeUnknownSeriesItems=true" if kind == "sonarr" else "includeUnknownMovieItems=true"

    def _req(self, method: str, path: str, data: Optional[bytes] = None,
             t: Optional[float] = None, retries: int = 3):
        """Make an HTTP request with retry/backoff for transient failures.

        Retries on: 5xx, 429, timeout, connection reset.
        Does not retry on: 4xx (except 429), 2xx/3xx responses.
        """
        t = t or TIMEOUT
        last_exc = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(self.base + path, data=data, method=method,
                                             headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
                return urllib.request.urlopen(req, timeout=t)
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < retries:
                    wait = 2 ** attempt + (0.5 if e.code == 429 else 0)
                    log.debug("[%s] %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
                              self.name, method, path, e.code, wait, attempt + 1, retries)
                    time.sleep(wait)
                    last_exc = e
                    continue
                raise
            except (socket.timeout, urllib.error.URLError, ConnectionResetError, BrokenPipeError) as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    log.debug("[%s] %s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                              self.name, method, path, str(e)[:50], wait, attempt + 1, retries)
                    time.sleep(wait)
                    last_exc = e
                    continue
                raise
        raise last_exc

    def queue(self):
        if self.kind == "prowlarr":
            return []                                            # prowlarr has no download queue
        try:
            return json.load(self._req("GET", "/queue?page=1&pageSize=1000&" + self.unknown)).get("records", [])
        except Exception as e:
            log.warning("[%s] queue fetch failed: %s", self.name, e); return None

    def health(self):
        try:
            return json.load(self._req("GET", "/health"))
        except Exception:
            return []

    def remove(self, item_id):
        q = "removeFromClient=%s&blocklist=%s" % (str(REMOVE_CLIENT).lower(), str(BLOCKLIST).lower())
        self._req("DELETE", "/queue/%d?%s" % (item_id, q))

    def post(self, path, t=150):
        """POST with empty body (used for /indexer/testall, /downloadclient/testall). Returns parsed JSON or []."""
        try:
            body = self._req("POST", path, data=b"", t=t).read()
            return json.loads(body) if body else []
        except urllib.error.HTTPError as e:
            try: return json.loads(e.read())
            except Exception: return []
        except Exception as ex:
            log.debug("[%s] POST %s err %s", self.name, path, str(ex)[:50]); return []

    def set_monitored(self, ids, monitored):
        """Bulk toggle monitoring for episodes (sonarr) / movies (radarr). Used by the churn brake."""
        if self.kind == "sonarr":
            path, body = "/episode/monitor", {"episodeIds": list(ids), "monitored": monitored}
        elif self.kind == "radarr":
            path, body = "/movie/editor", {"movieIds": list(ids), "monitored": monitored}
        else:
            return False
        try:
            self._req("PUT", path, data=json.dumps(body).encode()); return True
        except Exception as e:
            log.warning("[churn:%s] monitor %s failed: %s", self.name, "on" if monitored else "off", str(e)[:70])
            return False

    def queue_target_id(self, rec):
        """Stable id of what a queue record is FOR (episode for sonarr, movie for radarr)."""
        return rec.get("episodeId") if self.kind == "sonarr" else rec.get("movieId") if self.kind == "radarr" else None

    # ---- repair helpers (map a dead library file -> *arr item, then remove + re-search) ----
    def _jget(self, path, t=30):
        try:
            return json.load(self._req("GET", path, t=t))
        except Exception as e:
            log.warning("[%s] GET %s failed: %s", self.name, path, str(e)[:70]); return None

    def movies(self):
        return self._jget("/movie") or []                       # radarr: each has movieFile.path

    def series(self):
        return self._jget("/series") or []                      # sonarr

    def quality_profiles(self):
        return self._jget("/qualityprofile") or []              # sonarr/radarr

    def update_series(self, series_dict):
        """PUT the full series dict back (used to change qualityProfileId etc.)."""
        return self._req("PUT", "/series/%d" % series_dict["id"],
                         data=json.dumps(series_dict).encode())

    def episode_files(self, sid):
        return self._jget("/episodefile?seriesId=%d" % sid) or []

    def episodes(self, sid):
        return self._jget("/episode?seriesId=%d" % sid) or []

    def delete_file(self, file_id):
        """Delete a movieFile/episodeFile record (removes the dead library symlink so it can be re-grabbed)."""
        ep = "/moviefile/%d" % file_id if self.kind == "radarr" else "/episodefile/%d" % file_id
        try:
            self._req("DELETE", ep); return True
        except Exception as e:
            log.warning("[%s] delete file %s failed: %s", self.name, file_id, str(e)[:70]); return False

    def command(self, name, **kw):
        """POST /command and return the command ID (int) on success, or None on failure."""
        body = {"name": name}; body.update(kw)
        try:
            resp = json.load(self._req("POST", "/command", data=json.dumps(body).encode()))
            return resp.get("id") or True          # return id if present, else True for compat
        except Exception as e:
            log.warning("[%s] command %s failed: %s", self.name, name, str(e)[:70]); return None

    def command_status(self, command_id):
        """Poll GET /command/{id}. Returns the status string, or None on error."""
        try:
            resp = json.load(self._req("GET", "/command/%d" % command_id))
            return resp.get("status")
        except Exception:
            return None

    def release_search(self, series_id, season_number=1, timeout=45):
        """GET /release?seriesId=&seasonNumber= — returns list of release dicts (same as Sonarr UI).
        Returns [] on failure."""
        try:
            resp = self._req("GET", "/release?seriesId=%d&seasonNumber=%d" % (series_id, season_number),
                             t=timeout)
            return json.load(resp) or []
        except Exception as e:
            log.debug("[%s] release_search(%d, %d) failed: %s", self.name, series_id, season_number, str(e)[:60])
            return []

    def release_push(self, release):
        """POST /release/push — bypasses Sonarr's rejection logic and pushes directly to download client.
        Returns True on success."""
        try:
            self._req("POST", "/release/push", data=json.dumps(release).encode())
            return True
        except urllib.error.HTTPError as e:
            log.warning("[%s] release_push failed HTTP %d: %s", self.name, e.code, e.read()[:80])
            return False
        except Exception as e:
            log.warning("[%s] release_push failed: %s", self.name, str(e)[:60])
            return False

    def history_grabbed(self, media_id, since_ts, entity_ids=None):
        """Return the most recent 'grabbed' history record for media_id posted after since_ts.
        For sonarr, optionally filter to specific episode IDs. Returns None if nothing found."""
        records = self.history(media_id, page_size=50)
        if isinstance(records, dict):
            records = records.get("records") or []
        # Parse both timestamps to floats for a reliable "strictly after" comparison.
        # Lexicographic string comparison breaks when *arr dates include milliseconds
        # (e.g. "T04:55:30.5Z") or +00:00 offsets — different suffixes sort differently
        # than the "Z" suffix stored in search_ts.
        try:
            since_epoch = datetime.fromisoformat(since_ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            since_epoch = None
        for rec in records:
            if rec.get("eventType") != "grabbed":
                continue
            rec_date = rec.get("date") or ""
            if since_epoch is not None:
                try:
                    rec_epoch = datetime.fromisoformat(rec_date.replace("Z", "+00:00")).timestamp()
                    if rec_epoch <= since_epoch:
                        continue
                except Exception:
                    continue                          # skip records with unparseable dates
            elif not rec_date or rec_date <= since_ts:
                continue                              # fallback: string compare (since_epoch parse failed)
            if entity_ids and self.kind == "sonarr":
                if rec.get("episodeId") not in entity_ids:
                    continue
            return rec
        return None

    def history(self, media_id, page_size=100):
        """Fetch download history for a specific series (sonarr) or movie (radarr).
        Returns a list of history records, each with eventType, sourceTitle, data dict, etc."""
        if self.kind == "sonarr":
            path = "/history/series?seriesId=%d&pageSize=%d&includeSeries=false&includeEpisode=true" % (media_id, page_size)
        elif self.kind == "radarr":
            path = "/history/movie?movieId=%d&pageSize=%d" % (media_id, page_size)
        else:
            return []
        return self._jget(path) or []
def load_instances():
    out = []
    for n in range(1, 51):
        url = os.environ.get("INSTANCE_%d_URL" % n)
        if not url:
            continue
        key = os.environ.get("INSTANCE_%d_APIKEY" % n, "")
        kind = os.environ.get("INSTANCE_%d_TYPE" % n, "").strip().lower()
        if kind not in ("sonarr", "radarr", "prowlarr"):
            kind = ("radarr" if "radarr" in url.lower() else
                    "prowlarr" if "prowlarr" in url.lower() else "sonarr")
        name = os.environ.get("INSTANCE_%d_NAME" % n, "%s-%d" % (kind, n))
        if not key:
            log.warning("INSTANCE_%d has no APIKEY, skipping", n); continue
        out.append(Arr(name, kind, url, key))
    return out
INSTANCES = []
class Plex:
    def __init__(self, url, token):
        self.url = url.rstrip("/"); self.token = token

    def _get(self, path):
        sep = "&" if "?" in path else "?"
        with urllib.request.urlopen(self.url + path + sep + "X-Plex-Token=" + self.token, timeout=15) as r:
            return ET.fromstring(r.read())

    def sessions(self):
        try: return list(self._get("/status/sessions").iter("Video"))
        except Exception: return []

    def ondeck(self):
        try: return list(self._get("/library/onDeck").iter("Video"))
        except Exception: return []

    def leaves(self, show_rk):
        try: return list(self._get("/library/metadata/%s/allLeaves" % show_rk).iter("Video"))
        except Exception: return []

    def parts(self, rk):
        """File paths for this item, highest-resolution version first (so we can warm just the top one)."""
        out = []
        try:
            for m in self._get("/library/metadata/%s" % rk).iter("Media"):
                try: res = int(m.get("height") or 0) * 1000000 + int(m.get("bitrate") or 0)
                except Exception: res = 0
                for p in m.iter("Part"):
                    if p.get("file"):
                        out.append((res, p.get("file")))
            out.sort(key=lambda x: x[0], reverse=True)
        except Exception:
            return []
        return [f for _, f in out]

    def recent(self, n):
        out = []
        try:
            for d in self._get("/library/sections").iter("Directory"):
                if d.get("type") in ("movie", "show"):
                    ra = self._get("/library/sections/%s/recentlyAdded?X-Plex-Container-Start=0&X-Plex-Container-Size=%d" % (d.get("key"), n))
                    out += list(ra.iter("Video"))[:n]
        except Exception: pass
        return out

    def activities(self):
        """Running background activities (library scans, analysis...). Used by the plexscan check."""
        try: return list(self._get("/activities").iter("Activity"))
        except Exception: return []

    def cancel_activity(self, uuid):
        try:
            req = urllib.request.Request(self.url + "/activities/" + uuid + "?X-Plex-Token=" + self.token, method="DELETE")
            urllib.request.urlopen(req, timeout=10); return True
        except Exception:
            return False
class Seerr:
    def __init__(self, url, apikey):
        self.base = url.rstrip("/") + "/api/v1"
        self.apikey = apikey

    def _req(self, method: str, path: str, data: Optional[bytes] = None,
             t: Optional[float] = None, retries: int = 3):
        """Make an HTTP request with retry/backoff for transient failures.

        Retries on: 5xx, 429, timeout, connection reset.
        Does not retry on: 4xx (except 429), 2xx/3xx responses.
        """
        t = t or TIMEOUT
        last_exc = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(self.base + path, data=data, method=method,
                                             headers={"X-Api-Key": self.apikey, "Content-Type": "application/json"})
                return urllib.request.urlopen(req, timeout=t)
            except urllib.error.HTTPError as e:
                if e.code in (429, 502, 503, 504) and attempt < retries:
                    wait = 2 ** attempt + (0.5 if e.code == 429 else 0)
                    log.debug("[%s] %s %s -> %d, retrying in %.1fs (attempt %d/%d)",
                              self.name, method, path, e.code, wait, attempt + 1, retries)
                    time.sleep(wait)
                    last_exc = e
                    continue
                raise
            except (socket.timeout, urllib.error.URLError, ConnectionResetError, BrokenPipeError) as e:
                if attempt < retries:
                    wait = 2 ** attempt
                    log.debug("[%s] %s %s -> %s, retrying in %.1fs (attempt %d/%d)",
                              self.name, method, path, str(e)[:50], wait, attempt + 1, retries)
                    time.sleep(wait)
                    last_exc = e
                    continue
                raise
        raise last_exc

    def failed(self):
        """Requests currently in the FAILED state (seerr could not hand them to the arr)."""
        try:
            d = json.load(self._req("GET", "/request?take=100&skip=0&filter=failed&sort=added", t=15))
            return d.get("results", [])
        except Exception as e:
            log.warning("[seerr] failed-list fetch error: %s", str(e)[:80]); return None

    def retry(self, rid):
        self._req("POST", "/request/%d/retry" % int(rid), data=b"", t=30)

__all__ = [n for n in dir() if not n.startswith("__") and not isinstance(globals()[n], type(os))]
