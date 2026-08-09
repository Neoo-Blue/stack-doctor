"""Pulsarr API client (watchlist exclusions, health check)."""
import json
import os
import sqlite3
import urllib.request
import urllib.error
from typing import Optional
from ..config import log


class Pulsarr:
    def __init__(self, url: str, apikey: str, db_path: str = ""):
        self.base = url.rstrip("/")
        self.apikey = apikey
        self.db_path = db_path

    def _req(self, method: str, path: str, data: Optional[bytes] = None, t: int = 10):
        headers = {"x-api-key": self.apikey, "Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=t)

    def _jpost(self, path: str, body: dict = None, t: int = 10) -> Optional[dict]:
        data = json.dumps(body or {}).encode()
        try:
            with self._req("POST", path, data=data, t=t) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            log.debug("[pulsarr] POST %s -> HTTP %d: %s", path, e.code, str(e.read())[:80])
            return None
        except Exception as e:
            log.warning("[pulsarr] POST %s failed: %s", path, str(e)[:70])
            return None

    def create_watchlist_exclusion(self, tmdb_id: int, media_type: str = "tv",
                                    users: list = None, title: str = "") -> bool:
        """Create per-user watchlist exclusions so Pulsarr won't re-add."""
        body = {
            "key": str(tmdb_id),
            "type": media_type,
            "userIds": users or [],
            "title": title,
            "guids": [],
        }
        resp = self._jpost("/v1/watchlist-exclusions", body)
        return resp is not None

    def _db_users(self) -> list:
        """Fallback: query the Pulsarr SQLite DB directly for user list."""
        if not self.db_path or not os.path.exists(self.db_path):
            return []
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute("SELECT id, name FROM users").fetchall()
            return [{"id": r[0], "plexUsername": r[1]} for r in rows]
        except Exception as e:
            log.debug("[pulsarr] DB users query failed: %s", str(e)[:60])
            return []

    def user_id_for_plex_username(self, plex_username: str) -> str:
        """Return the Pulsarr user ID for a given Plex username, or '' if not found."""
        target = plex_username.strip().lower()
        if not target:
            return ""
        for u in self._db_users():
            if (u.get("plexUsername") or "").strip().lower() == target:
                return str(u.get("id", ""))
        return ""

    def health(self) -> bool:
        """Ping Pulsarr to verify it's reachable."""
        try:
            with self._req("GET", "/health", t=5) as r:
                return r.getcode() < 500
        except Exception:
            return False
