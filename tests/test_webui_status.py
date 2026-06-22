"""Tests for _ui_status() run metadata exposure (B2).

These tests verify that the /api/status response includes per-check run
metadata from scheduler._check_runs, without breaking existing fields.
"""
import time
import unittest


def _call_ui_status():
    from doctor.webui import _ui_status
    return _ui_status()


class UiStatusFieldsTest(unittest.TestCase):
    """Existing /api/status contract must remain intact."""

    def test_top_level_keys_present(self):
        r = _call_ui_status()
        for key in ("version", "mode", "dry_run", "load", "checks"):
            self.assertIn(key, r)

    def test_checks_is_a_list(self):
        r = _call_ui_status()
        self.assertIsInstance(r["checks"], list)

    def test_each_check_has_name_and_on(self):
        r = _call_ui_status()
        for c in r["checks"]:
            self.assertIn("name", c)
            self.assertIn("on", c)
            self.assertIsInstance(c["on"], bool)


class UiStatusRunMetadataTest(unittest.TestCase):
    """Per-check run metadata keys must appear in each check entry."""

    RUN_FIELDS = ("last_start", "last_end", "last_duration",
                  "last_outcome", "last_error", "run_count", "error_count")

    def test_run_fields_present_on_unrun_check(self):
        """Checks with no run record yet should still have the metadata keys (None/0)."""
        import doctor.scheduler as sched
        # Ensure the check has no record
        sched._check_runs.pop("queue", None)
        r = _call_ui_status()
        queue_entry = next((c for c in r["checks"] if c["name"] == "queue"), None)
        self.assertIsNotNone(queue_entry)
        for field in self.RUN_FIELDS:
            self.assertIn(field, queue_entry, "missing field: %s" % field)

    def test_run_fields_null_when_never_run(self):
        import doctor.scheduler as sched
        sched._check_runs.pop("queue", None)
        r = _call_ui_status()
        q = next(c for c in r["checks"] if c["name"] == "queue")
        self.assertIsNone(q["last_start"])
        self.assertIsNone(q["last_outcome"])
        self.assertEqual(q["run_count"], 0)
        self.assertEqual(q["error_count"], 0)

    def test_run_fields_populated_after_run(self):
        import doctor.scheduler as sched
        now = time.time()
        sched._check_runs["queue"] = {
            "last_start": now - 1.5,
            "last_end": now,
            "last_duration": 1.5,
            "last_outcome": "ok",
            "last_error": "",
            "run_count": 3,
            "error_count": 0,
        }
        r = _call_ui_status()
        q = next(c for c in r["checks"] if c["name"] == "queue")
        self.assertEqual(q["last_outcome"], "ok")
        self.assertEqual(q["run_count"], 3)
        self.assertAlmostEqual(q["last_duration"], 1.5, places=2)
        # Cleanup
        sched._check_runs.pop("queue", None)

    def test_error_metadata_propagated(self):
        import doctor.scheduler as sched
        now = time.time()
        sched._check_runs["repair"] = {
            "last_start": now - 5,
            "last_end": now,
            "last_duration": 5.0,
            "last_outcome": "error",
            "last_error": "connection refused",
            "run_count": 1,
            "error_count": 1,
        }
        r = _call_ui_status()
        rep = next(c for c in r["checks"] if c["name"] == "repair")
        self.assertEqual(rep["last_outcome"], "error")
        self.assertIn("connection", rep["last_error"])
        self.assertEqual(rep["error_count"], 1)
        sched._check_runs.pop("repair", None)

    def test_warmer_synthetic_entry_still_has_name_and_on(self):
        """The synthetic warmer entry appended at the end must still have name + on."""
        r = _call_ui_status()
        warmer = next((c for c in r["checks"] if c["name"] == "warmer"), None)
        self.assertIsNotNone(warmer)
        self.assertIn("on", warmer)


class UiWarmerMetadataTest(unittest.TestCase):
    """_ui_warmer() must include per-cycle metadata fields."""

    def _call(self):
        from doctor.webui import _ui_warmer
        return _ui_warmer()

    def test_cycle_metadata_fields_present(self):
        r = self._call()
        for field in ("last_cycle_ts", "last_cycle_ago", "last_cycle_duration_s",
                      "last_cycle_warmed", "last_cycle_candidates", "last_cycle_skipped_load"):
            self.assertIn(field, r, "missing field: %s" % field)

    def test_null_when_never_run(self):
        import doctor.checks.warmer as w
        w._last_cycle_ts[0] = 0.0
        r = self._call()
        self.assertIsNone(r["last_cycle_ts"])
        self.assertIsNone(r["last_cycle_ago"])

    def test_populated_when_run(self):
        import time, doctor.checks.warmer as w
        now = time.time()
        w._last_cycle_ts[0] = now - 60
        w._last_cycle_duration[0] = 2.5
        w._last_cycle_warmed[0] = 4
        w._last_cycle_candidates[0] = 10
        w._last_cycle_skipped_load[0] = False
        r = self._call()
        self.assertIsNotNone(r["last_cycle_ts"])
        self.assertAlmostEqual(r["last_cycle_ago"], 60, delta=2)
        self.assertEqual(r["last_cycle_duration_s"], 2.5)
        self.assertEqual(r["last_cycle_warmed"], 4)
        self.assertEqual(r["last_cycle_candidates"], 10)
        self.assertFalse(r["last_cycle_skipped_load"])
        # Reset
        w._last_cycle_ts[0] = 0.0
