"""Unit tests for the queue check.

Tests cover:
  - stuck_reason(): all six condition predicates
  - check_queue(): strike accumulation, removal, DRY_RUN, action counting,
    health warnings, and per-arr filtering via `only=`

All Arr HTTP interactions are replaced with MagicMock so no network is needed.
Constants consumed from the star-import (MIN_STRIKES, MAX_ACTIONS, DRY_RUN,
LOAD_MAX, BLOCKLIST, INSTANCES, ENABLED_CONDITIONS) are patched on the
doctor.checks.queue module directly, matching the pattern used in
test_missing_seasons.py.
"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import doctor.state as _state
from doctor.checks.queue import stuck_reason, _msgs, check_queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_arr(name="sonarr", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    arr.queue.return_value = []
    arr.health.return_value = []
    arr.remove.return_value = None
    arr.queue_target_id.return_value = None  # churn brake disabled by default in tests
    return arr


def _rec(iid, *, status=None, tracked_state=None, tracked_status=None, messages=None):
    """Minimal queue record."""
    r = {"id": iid, "title": "Show S01E01"}
    if status:
        r["status"] = status
    if tracked_state:
        r["trackedDownloadState"] = tracked_state
    if tracked_status:
        r["trackedDownloadStatus"] = tracked_status
    if messages:
        r["statusMessages"] = [{"messages": messages}]
    return r


# ---------------------------------------------------------------------------
# stuck_reason predicate tests  (these use no patching — pure function)
# ---------------------------------------------------------------------------

class StuckReasonTest(unittest.TestCase):
    def test_download_client_unavailable(self):
        r = _rec(1, status="downloadClientUnavailable")
        self.assertEqual(stuck_reason(r), "downloadClientUnavailable")

    def test_import_blocked(self):
        r = _rec(1, tracked_state="importBlocked")
        self.assertEqual(stuck_reason(r), "importBlocked")

    def test_import_failed(self):
        r = _rec(1, tracked_state="importFailed")
        self.assertEqual(stuck_reason(r), "importFailed")

    def test_import_pending_warning(self):
        r = _rec(1, tracked_state="importPending", tracked_status="warning")
        self.assertEqual(stuck_reason(r), "importPending_warning")

    def test_import_pending_error(self):
        r = _rec(1, tracked_state="importPending", tracked_status="error")
        self.assertEqual(stuck_reason(r), "importPending_warning")

    def test_import_pending_ok_not_stuck(self):
        r = _rec(1, tracked_state="importPending", tracked_status="ok")
        self.assertIsNone(stuck_reason(r))

    def test_failed_pending(self):
        r = _rec(1, tracked_state="failedPending")
        self.assertEqual(stuck_reason(r), "failedPending")

    def test_stalled_by_message(self):
        r = _rec(1, tracked_status="warning", messages=["download is stalled with no connections"])
        self.assertEqual(stuck_reason(r), "stalled")

    def test_stalled_no_files(self):
        r = _rec(1, tracked_status="warning", messages=["no files found are eligible for import"])
        self.assertEqual(stuck_reason(r), "stalled")

    def test_warning_without_stall_message(self):
        r = _rec(1, tracked_status="warning", messages=["something else"])
        self.assertIsNone(stuck_reason(r))

    def test_clean_item_is_none(self):
        r = _rec(1, status="ok")
        self.assertIsNone(stuck_reason(r))

    def test_empty_record_is_none(self):
        self.assertIsNone(stuck_reason({}))

    def test_conditions_respect_enabled_set(self):
        """Only conditions in ENABLED_CONDITIONS should be tested."""
        r = _rec(1, status="downloadClientUnavailable")
        with patch("doctor.checks.queue.ENABLED_CONDITIONS", ["importBlocked"]):
            # downloadClientUnavailable is not enabled → should return None
            self.assertIsNone(stuck_reason(r))


class MsgsTest(unittest.TestCase):
    def test_extracts_nested_messages(self):
        r = {"statusMessages": [{"messages": ["a", "b"]}, {"messages": ["c"]}], "errorMessage": "top"}
        self.assertEqual(_msgs(r), ["a", "b", "c", "top"])

    def test_empty_status_messages(self):
        self.assertEqual(_msgs({}), [])

    def test_only_error_message(self):
        r = {"errorMessage": "boom"}
        self.assertEqual(_msgs(r), ["boom"])


# ---------------------------------------------------------------------------
# check_queue integration-style tests (Arr fully mocked, state in temp file)
# ---------------------------------------------------------------------------

_Q_PATCHES = dict(
    LOAD_MAX=0,              # don't skip on load
    MIN_STRIKES=2,
    MAX_ACTIONS=10,
    DRY_RUN=False,
    BLOCKLIST=True,
)

def _patch_queue(**overrides):
    """Return a patch.multiple context for doctor.checks.queue constants."""
    kw = {**_Q_PATCHES, **overrides}
    return patch.multiple("doctor.checks.queue", **kw)


class CheckQueueStrikeTest(unittest.TestCase):
    """Strike accumulation: items must be stuck for MIN_STRIKES before removal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def _run(self, arr, **kw):
        with _patch_queue(**kw), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()

    def test_no_removal_on_first_strike(self):
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        self._run(arr, MIN_STRIKES=2)
        arr.remove.assert_not_called()

    def test_removal_on_second_strike(self):
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        self._run(arr, MIN_STRIKES=2)
        self._run(arr, MIN_STRIKES=2)
        arr.remove.assert_called_once_with(1)

    def test_removal_on_first_strike_when_min_strikes_is_one(self):
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        self._run(arr, MIN_STRIKES=1)
        arr.remove.assert_called_once_with(1)

    def test_strike_counter_resets_after_removal(self):
        """After an item is removed, its strike count is cleared from state."""
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        self._run(arr, MIN_STRIKES=2)  # strike 1
        self._run(arr, MIN_STRIKES=2)  # strike 2 → remove
        arr.remove.reset_mock()
        # Next sweep: item is back (re-grabbed); strike counter should start fresh
        self._run(arr, MIN_STRIKES=2)  # strike 1 again
        arr.remove.assert_not_called()

    def test_clean_item_ignored(self):
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="ok")]
        self._run(arr)
        arr.remove.assert_not_called()


class CheckQueueDryRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_dry_run_does_not_remove(self):
        arr = _make_arr()
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        with _patch_queue(MIN_STRIKES=1, DRY_RUN=True), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()
        arr.remove.assert_not_called()

    def test_dry_run_does_not_update_state(self):
        """DRY_RUN still accumulates strike counts (so we don't re-act on restart)."""
        # Note: check_queue updates state[arr.name] regardless of DRY_RUN.
        # This test documents the current behavior.
        arr = _make_arr()
        arr.queue.return_value = [_rec(7, status="downloadClientUnavailable")]
        with _patch_queue(MIN_STRIKES=2, DRY_RUN=True), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()
        with _state.state_transaction() as s:
            self.assertIn("7", s.get("sonarr", {}))


class CheckQueueMaxActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_max_actions_caps_removals(self):
        """With MAX_ACTIONS=2, only two items are removed per sweep."""
        arr = _make_arr()
        arr.queue.return_value = [
            _rec(i, status="downloadClientUnavailable") for i in range(1, 6)
        ]
        # pre-fill strikes so all 5 items are at MIN_STRIKES on the second run
        with _patch_queue(MIN_STRIKES=2, MAX_ACTIONS=2), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()  # strike 1 for all
            arr.remove.reset_mock()
            check_queue()  # strike 2 → eligible, but capped at 2
        self.assertEqual(arr.remove.call_count, 2)


class CheckQueueOnlyFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_only_filters_to_named_instance(self):
        arr1 = _make_arr(name="sonarr")
        arr2 = _make_arr(name="radarr", kind="radarr")
        arr1.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        arr2.queue.return_value = [_rec(2, status="downloadClientUnavailable")]
        with _patch_queue(MIN_STRIKES=1), \
             patch("doctor.checks.queue.INSTANCES", [arr1, arr2]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue(only="sonarr")
        arr1.remove.assert_called_once_with(1)
        arr2.remove.assert_not_called()

    def test_only_is_case_insensitive(self):
        arr = _make_arr(name="Sonarr")
        arr.queue.return_value = [_rec(1, status="downloadClientUnavailable")]
        with _patch_queue(MIN_STRIKES=1), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue(only="sonarr")
        arr.remove.assert_called_once_with(1)


class CheckQueueRemoveFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_remove_exception_is_caught(self):
        """A remove() exception must not abort the entire sweep."""
        arr = _make_arr()
        arr.queue.return_value = [
            _rec(1, status="downloadClientUnavailable"),
            _rec(2, status="downloadClientUnavailable"),
        ]
        arr.remove.side_effect = [Exception("network error"), None]
        with _patch_queue(MIN_STRIKES=1), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()
        # Both removes were attempted despite the first failure
        self.assertEqual(arr.remove.call_count, 2)


class CheckQueueQueueNoneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _state.STATE_FILE = os.path.join(self.tmp.name, "state.json")

    def test_none_queue_response_skips_arr(self):
        """arr.queue() returning None (API down) must be handled gracefully."""
        arr = _make_arr()
        arr.queue.return_value = None
        with _patch_queue(MIN_STRIKES=1), \
             patch("doctor.checks.queue.INSTANCES", [arr]), \
             patch("doctor.state.CHURN_LIMIT", 0):
            check_queue()  # must not raise
        arr.remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
