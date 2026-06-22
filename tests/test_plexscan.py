"""Characterization tests for doctor.checks.plexscan.

Lock in the current behavior of:
  - _is_scan_activity(): activity classification predicate
  - check_plex_scan(): stuck-scan detection, progress tracking, and
    multi-step recovery (mount probe -> cancel -> restart)

Plex is fully mocked.  Time is patched so tests are deterministic.
Module-level mutable state (_scan_seen, _plex_last_restart) is reset
between tests.
"""
import time
import unittest
from unittest.mock import patch, MagicMock

from doctor.checks.plexscan import (
    _is_scan_activity,
    check_plex_scan,
    _scan_seen,
    _plex_last_restart,
)

_MOD = "doctor.checks.plexscan"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _activity(uuid, title="Library scan", atype="library.update.section",
              progress=0, subtitle="", cancellable="1"):
    """Minimal Plex activity dict."""
    a = {
        "uuid": uuid,
        "type": atype,
        "title": title,
        "subtitle": subtitle,
        "progress": str(progress),
        "cancellable": cancellable,
    }
    return a


def _make_plex(activities=None):
    plex = MagicMock()
    plex.activities.return_value = activities or []
    plex.cancel_activity.return_value = True
    return plex


# ---------------------------------------------------------------------------
# _is_scan_activity
# ---------------------------------------------------------------------------

class IsScanActivityTest(unittest.TestCase):

    def test_library_update_type(self):
        a = {"type": "library.update.section", "title": "", "subtitle": ""}
        self.assertTrue(_is_scan_activity(a))

    def test_library_refresh_type(self):
        a = {"type": "library.refresh", "title": "", "subtitle": ""}
        self.assertTrue(_is_scan_activity(a))

    def test_scan_in_title(self):
        a = {"type": "something", "title": "Scanning Movies", "subtitle": ""}
        self.assertTrue(_is_scan_activity(a))

    def test_scan_in_subtitle(self):
        a = {"type": "something", "title": "", "subtitle": "Library scan in progress"}
        self.assertTrue(_is_scan_activity(a))

    def test_non_scan_activity(self):
        a = {"type": "media.play", "title": "Playing Movie", "subtitle": ""}
        self.assertFalse(_is_scan_activity(a))

    def test_empty_activity(self):
        a = {"type": "", "title": "", "subtitle": ""}
        self.assertFalse(_is_scan_activity(a))

    def test_missing_fields_default_to_empty(self):
        a = {}
        self.assertFalse(_is_scan_activity(a))

    def test_case_insensitive(self):
        a = {"type": "Library.Update.Section", "title": "", "subtitle": ""}
        self.assertTrue(_is_scan_activity(a))


# ---------------------------------------------------------------------------
# check_plex_scan - basic flow
# ---------------------------------------------------------------------------

class CheckPlexScanBasicTest(unittest.TestCase):
    """Tests for the basic flow: no scans, progressing scans, early return."""

    def setUp(self):
        _scan_seen.value.clear()
        _plex_last_restart.reset()

    def tearDown(self):
        _scan_seen.value.clear()
        _plex_last_restart.reset()

    @patch(_MOD + ".PLEX_URL", "")
    @patch(_MOD + ".PLEX_TOKEN", "")
    def test_noop_when_no_plex_configured(self):
        """Early return when PLEX_URL or PLEX_TOKEN are empty."""
        check_plex_scan()  # should not raise

    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_no_activities_clears_seen(self, MockPlex):
        plex = _make_plex([])
        MockPlex.return_value = plex

        check_plex_scan()
        self.assertEqual(len(_scan_seen.value), 0)

    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_progressing_scan_tracked_not_stuck(self, MockPlex):
        plex = _make_plex([_activity("uuid-1", progress=10)])
        MockPlex.return_value = plex

        check_plex_scan()
        self.assertIn("uuid-1", _scan_seen.value)
        # Not stuck yet (just started)
        self.assertEqual(_scan_seen.value["uuid-1"]["prog"], 10)

    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_finished_scan_removed_from_seen(self, MockPlex):
        """When a scan disappears from activities, it's removed from _scan_seen."""
        plex = _make_plex([_activity("uuid-1")])
        MockPlex.return_value = plex

        check_plex_scan()
        self.assertIn("uuid-1", _scan_seen.value)

        # Second call: scan is gone
        plex.activities.return_value = []
        check_plex_scan()
        self.assertNotIn("uuid-1", _scan_seen.value)

    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_non_scan_activity_ignored(self, MockPlex):
        a = {"uuid": "uuid-play", "type": "media.play",
             "title": "Movie", "subtitle": "", "progress": "0"}
        plex = _make_plex([a])
        MockPlex.return_value = plex

        check_plex_scan()
        self.assertNotIn("uuid-play", _scan_seen.value)

    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_activity_without_uuid_skipped(self, MockPlex):
        a = {"type": "library.update.section", "title": "Scan", "subtitle": "",
             "progress": "0"}  # no uuid
        plex = _make_plex([a])
        MockPlex.return_value = plex

        check_plex_scan()
        self.assertEqual(len(_scan_seen.value), 0)


# ---------------------------------------------------------------------------
# check_plex_scan - stuck detection & recovery
# ---------------------------------------------------------------------------

class CheckPlexScanStuckTest(unittest.TestCase):
    """Tests for stuck-scan detection and the 3-step recovery."""

    def setUp(self):
        _scan_seen.value.clear()
        _plex_last_restart.reset()

    def tearDown(self):
        _scan_seen.value.clear()
        _plex_last_restart.reset()

    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_stuck_scan_detected_and_cancelled(self, MockPlex):
        """A scan with no progress for >= PLEX_SCAN_STUCK is detected and cancelled."""
        now = time.time()
        plex = _make_plex([_activity("uuid-stuck", progress=50)])
        MockPlex.return_value = plex

        # Seed the scan as already tracked and stale
        _scan_seen.value["uuid-stuck"] = {
            "first": now - 3600,
            "prog": 50,
            "prog_ts": now - 2000,  # stale for 2000s > 1800s threshold
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        plex.cancel_activity.assert_called_once_with("uuid-stuck")

    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_progress_advance_resets_stuck_timer(self, MockPlex):
        """When progress advances, prog_ts resets and the scan is not stuck."""
        now = time.time()
        plex = _make_plex([_activity("uuid-prog", progress=60)])
        MockPlex.return_value = plex

        # Previously at progress 50, stale timing
        _scan_seen.value["uuid-prog"] = {
            "first": now - 3600,
            "prog": 50,         # current progress 60 > 50 -> advances
            "prog_ts": now - 2000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        # Progress advanced -> not stuck -> no cancel
        plex.cancel_activity.assert_not_called()
        # prog_ts should be refreshed to approximately now
        self.assertGreater(_scan_seen.value["uuid-prog"]["prog_ts"], now - 5)

    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_acted_ts_prevents_repeated_action(self, MockPlex):
        """After acting on a stuck scan, the acted_ts throttle prevents re-acting within the window."""
        now = time.time()
        plex = _make_plex([_activity("uuid-acted", progress=50)])
        MockPlex.return_value = plex

        _scan_seen.value["uuid-acted"] = {
            "first": now - 3600,
            "prog": 50,
            "prog_ts": now - 2000,
            "title": "Library scan",
            "acted_ts": now - 100,  # acted 100s ago, within the PLEX_SCAN_STUCK window
        }

        check_plex_scan()
        plex.cancel_activity.assert_not_called()

    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", True)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_dry_run_does_not_cancel(self, MockPlex):
        now = time.time()
        plex = _make_plex([_activity("uuid-dry", progress=50)])
        MockPlex.return_value = plex

        _scan_seen.value["uuid-dry"] = {
            "first": now - 3600,
            "prog": 50,
            "prog_ts": now - 2000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        plex.cancel_activity.assert_not_called()

    @patch(_MOD + "._decy_restart")
    @patch(_MOD + "._probe_mount")
    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "/mnt/zurg")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_stuck_scan_probes_mount_on_dead(self, MockPlex, mock_probe, mock_restart):
        """When mount is DEAD, _decy_restart is called before cancelling."""
        from doctor.checks.decypharr import _FuseStatus
        mock_probe.return_value = (_FuseStatus.DEAD, "transport gone")
        now = time.time()
        plex = _make_plex([_activity("uuid-mount", progress=50)])
        MockPlex.return_value = plex

        _scan_seen.value["uuid-mount"] = {
            "first": now - 3600,
            "prog": 50,
            "prog_ts": now - 2000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        mock_restart.assert_called_once()
        plex.cancel_activity.assert_called_once_with("uuid-mount")

    @patch(_MOD + ".run_cmd", return_value=(0, "ok"))
    @patch(_MOD + ".PLEX_RESTART_CMD", "systemctl restart plex")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_restart_fires_when_scan_wedged_long_and_cancel_fails(self, MockPlex, mock_cmd):
        """Plex restart fires when: wedged >= 2*threshold, cancel fails, and no recent restart."""
        now = time.time()
        plex = _make_plex([_activity("uuid-restart", progress=50)])
        plex.cancel_activity.return_value = False  # cancel fails
        MockPlex.return_value = plex

        _scan_seen.value["uuid-restart"] = {
            "first": now - 7200,  # started 2h ago, well past 2*1800=3600
            "prog": 50,
            "prog_ts": now - 4000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        mock_cmd.assert_called_once_with("systemctl restart plex")

    @patch(_MOD + ".run_cmd", return_value=(0, "ok"))
    @patch(_MOD + ".PLEX_RESTART_CMD", "systemctl restart plex")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_restart_suppressed_when_cancelled_successfully(self, MockPlex, mock_cmd):
        """Successful cancel suppresses the restart, even if timing qualifies."""
        now = time.time()
        plex = _make_plex([_activity("uuid-norest", progress=50)])
        plex.cancel_activity.return_value = True  # cancel succeeds
        MockPlex.return_value = plex

        _scan_seen.value["uuid-norest"] = {
            "first": now - 7200,
            "prog": 50,
            "prog_ts": now - 4000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        mock_cmd.assert_not_called()

    @patch(_MOD + ".run_cmd", return_value=(0, "ok"))
    @patch(_MOD + ".PLEX_RESTART_CMD", "systemctl restart plex")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", True)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_restart_rate_limited_to_30min(self, MockPlex, mock_cmd):
        """Restart should not fire if _plex_last_restart was < 1800s ago."""
        now = time.time()
        _plex_last_restart.value = now - 600  # restarted 10 min ago
        plex = _make_plex([_activity("uuid-rl", progress=50)])
        plex.cancel_activity.return_value = False
        MockPlex.return_value = plex

        _scan_seen.value["uuid-rl"] = {
            "first": now - 7200,
            "prog": 50,
            "prog_ts": now - 4000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        mock_cmd.assert_not_called()

    @patch(_MOD + ".PLEX_RESTART_CMD", "")
    @patch(_MOD + ".PLEX_SCAN_CANCEL", False)
    @patch(_MOD + ".DECY_MOUNT_TEST", "")
    @patch(_MOD + ".DRY_RUN", False)
    @patch(_MOD + ".PLEX_SCAN_STUCK", 1800)
    @patch(_MOD + ".Plex")
    @patch(_MOD + ".PLEX_URL", "http://plex:32400")
    @patch(_MOD + ".PLEX_TOKEN", "token123")
    def test_cancel_skipped_when_disabled(self, MockPlex):
        now = time.time()
        plex = _make_plex([_activity("uuid-nocancel", progress=50)])
        MockPlex.return_value = plex

        _scan_seen.value["uuid-nocancel"] = {
            "first": now - 3600,
            "prog": 50,
            "prog_ts": now - 2000,
            "title": "Library scan",
            "acted_ts": 0,
        }

        check_plex_scan()
        plex.cancel_activity.assert_not_called()


if __name__ == "__main__":
    unittest.main()
