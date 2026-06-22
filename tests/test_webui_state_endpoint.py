"""Tests for the /api/state endpoint (B3).

GET /api/state returns the full contents of state.json as JSON,
authenticated via the existing UI_TOKEN mechanism.
"""
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch


def _get(port, path, token=None):
    conn = HTTPConnection("localhost", port, timeout=3)
    headers = {}
    if token:
        headers["X-Doctor-Token"] = token
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


class ApiStateEndpointTest(unittest.TestCase):
    """Integration tests for GET /api/state."""

    def _start(self, port, state_data, ui_token="", en_ui=True):
        """Start the server with patches held for the test's lifetime."""
        self._patches = [
            patch("doctor.webui.EN_UI", en_ui),
            patch("doctor.webui.UI_TOKEN", ui_token),
            patch("doctor.state._load_state_unlocked", return_value=state_data),
        ]
        for p in self._patches:
            p.start()
        from doctor.webui import _build_server
        srv = _build_server(port)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.05)      # let the server bind
        return srv

    def tearDown(self):
        for p in getattr(self, "_patches", []):
            try: p.stop()
            except Exception: pass

    def test_returns_state_json_when_no_token_required(self):
        """Without UI_TOKEN, /api/state is accessible and returns state data."""
        state = {"__seerr__": {"1": 2}, "__repair_verify__": {}}
        srv = self._start(19100, state)
        try:
            code, body = _get(19100, "/api/state")
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertEqual(data.get("__seerr__"), {"1": 2})
        finally:
            srv.shutdown()

    def test_returns_401_without_token_when_token_required(self):
        """With UI_TOKEN set, unauthenticated request returns 401."""
        srv = self._start(19101, {}, ui_token="secret")
        try:
            code, _ = _get(19101, "/api/state")
            self.assertEqual(code, 401)
        finally:
            srv.shutdown()

    def test_returns_state_with_valid_token(self):
        """With correct X-Doctor-Token header, /api/state returns state data."""
        state = {"__repair_mfd__": {"arr:1": 1234}}
        srv = self._start(19102, state, ui_token="secret")
        try:
            code, body = _get(19102, "/api/state", token="secret")
            self.assertEqual(code, 200)
            data = json.loads(body)
            self.assertIn("__repair_mfd__", data)
        finally:
            srv.shutdown()

    def test_returns_404_when_ui_disabled(self):
        """When EN_UI=False, /api/state returns 404 (same as all other UI endpoints)."""
        srv = self._start(19103, {}, en_ui=False)
        try:
            code, _ = _get(19103, "/api/state")
            self.assertEqual(code, 404)
        finally:
            srv.shutdown()

    def test_empty_state_returns_empty_object(self):
        """An empty state file returns an empty JSON object."""
        srv = self._start(19104, {})
        try:
            code, body = _get(19104, "/api/state")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), {})
        finally:
            srv.shutdown()
