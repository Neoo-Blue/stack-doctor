"""Tautulli API client (watch history queries)."""
import json
import time
import urllib.request
import urllib.parse
from typing import Optional
from ..config import log


class Tautulli:
    def __init__(self, url: str, apikey: str):
        self.base = url.rstrip("/") + "/api/v2"
        self.apikey = apikey

    def _get(self, cmd: str, **params) -> Optional[dict]:
        params["apikey"] = self.apikey
        params["cmd"] = cmd
        qs = urllib.parse.urlencode(params)
        url = self.base + "?" + qs
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                body = r.read()
                if not body:
                    return None
                data = json.loads(body)
                if data.get("response", {}).get("result") != "success":
                    return None
                return data.get("response", {}).get("data")
        except Exception as e:
            log.warning("[tautulli] %s failed: %s", cmd, str(e)[:70])
            return None

    def recently_watched_shows(self, since_days: int = 30) -> set:
        """Return a set of show titles with watch activity in the last N days.

        Queries Tautulli's get_history with media_type=episode.  Because
        Tautulli's after parameter is unreliable with large length values,
        we fetch all recent history and filter client-side.
        """
        cutoff = time.time() - since_days * 86400
        data = self._get("get_history", media_type="episode", length=20000)
        if not data:
            return set()
        records = data if isinstance(data, list) else data.get("data", [])
        titles = set()
        for rec in records:
            if rec.get("date", 0) >= cutoff:
                title = (rec.get("grandparent_title") or "").strip()
                if title:
                    titles.add(title)
        return titles
