"""Pulsarr API client (watchlist exclusions, health check)."""
import json
import urllib.request
import urllib.error
from typing import Optional
from ..config import log


class Pulsarr:
    def __init__(self, url: str, apikey: str):
        self.base = url.rstrip("/") + "/v1"
        self.apikey = apikey

    def _req(self, method: str, path: str, data: Optional[bytes] = None, t: int = 10):
        headers = {"x-api-key": self.apikey, "Content-Type": "application/json"}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        return urllib.request.urlopen(req, timeout=t)

    def _jget(self, path: str, t: int = 10) -> Optional[list]:
        try:
            with self._req("GET", path, t=t) as r:
                return json.loads(r.read())
        except Exception as e:
            log.warning("[pulsarr] GET %s failed: %s", path, str(e)[:70])
            return None

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
                                    users: list = None, all_users: bool = True) -> bool:
        """Create a watchlist exclusion to prevent re-addition.

        Pulsarr will ignore this item on future watchlist syncs.
        When *users* is a list of Pulsarr user IDs, the exclusion applies
        only to those users. Otherwise all_users=True scopes it globally.
        """
        body = {"key": str(tmdb_id), "type": media_type}
        if not all_users and users:
            body["userIds"] = users
        else:
            body["allUsers"] = True
        resp = self._jpost("/watchlist-exclusions", body)
        return resp is not None

    def list_users(self) -> list:
        """Return list of Pulsarr user dicts with at least 'id' and 'plexUsername'."""
        data = self._jget("/users")
        if not data:
            return []
        return data if isinstance(data, list) else data.get("data", [])

    def user_id_for_plex_username(self, plex_username: str) -> str:
        """Return the Pulsarr user ID for a given Plex username, or '' if not found."""
        for u in self.list_users():
            if (u.get("plexUsername") or "").strip().lower() == plex_username.strip().lower():
                return str(u.get("id", ""))
        return ""

    def remove_watchlist(self, tmdb_id: int, pulsarr_user_id: str = "",
                          media_type: str = "tv") -> bool:
        """Best-effort removal of an item from a user's Plex watchlist.

        Pulsarr holds the per-user Plex tokens needed to issue the watchlist
        removal call through Plex's metadata provider API.  When
        *pulsarr_user_id* is empty the removal targets all users.

        Returns False on any error (including 404 if the endpoint isn't
        available in this Pulsarr version).
        """
        body = {"key": str(tmdb_id), "type": media_type}
        if pulsarr_user_id:
            body["userId"] = pulsarr_user_id
        try:
            return self._jpost("/plex/remove-watchlist", body) is not None
        except Exception:
            return False

    def health(self) -> bool:
        """Ping Pulsarr to verify it's reachable."""
        try:
            with self._req("GET", "/system/health", t=5) as r:
                return r.getcode() < 500
        except Exception:
            return False
