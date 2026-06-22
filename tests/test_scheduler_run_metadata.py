"""Tests for per-check run metadata in scheduler._check_runs (B1).

These tests verify that _run_scheduled_check and sweep() correctly populate
the _check_runs dict with timing, outcome, and counter information.
"""
import time
import unittest
from unittest.mock import MagicMock

import doctor.scheduler as sched_mod
from doctor.scheduler import _run_scheduled_check, _check_runs


def _reset_runs():
    """Clear _check_runs between tests."""
    _check_runs.clear()


class RunMetadataBasicTest(unittest.TestCase):

    def setUp(self):
        _reset_runs()

    def test_successful_run_records_ok_outcome(self):
        fn = MagicMock()
        _run_scheduled_check("queue", fn)
        r = _check_runs.get("queue")
        self.assertIsNotNone(r, "_check_runs should have 'queue' entry")
        self.assertEqual(r["last_outcome"], "ok")
        self.assertEqual(r["last_error"], "")

    def test_successful_run_increments_run_count(self):
        fn = MagicMock()
        _run_scheduled_check("queue", fn)
        _run_scheduled_check("queue", fn)
        self.assertEqual(_check_runs["queue"]["run_count"], 2)

    def test_error_run_records_error_outcome(self):
        fn = MagicMock(side_effect=RuntimeError("disk full"))
        _run_scheduled_check("queue", fn)
        r = _check_runs["queue"]
        self.assertEqual(r["last_outcome"], "error")
        self.assertIn("disk full", r["last_error"])

    def test_error_run_increments_error_count(self):
        fn = MagicMock(side_effect=RuntimeError("oops"))
        _run_scheduled_check("queue", fn)
        _run_scheduled_check("queue", fn)
        r = _check_runs["queue"]
        self.assertEqual(r["error_count"], 2)
        self.assertEqual(r["run_count"], 2)

    def test_successful_run_does_not_increment_error_count(self):
        fn = MagicMock()
        _run_scheduled_check("queue", fn)
        self.assertEqual(_check_runs["queue"]["error_count"], 0)

    def test_records_last_start_and_end_timestamps(self):
        before = time.time()
        fn = MagicMock()
        _run_scheduled_check("queue", fn)
        after = time.time()
        r = _check_runs["queue"]
        self.assertGreaterEqual(r["last_start"], before)
        self.assertLessEqual(r["last_end"], after)
        self.assertGreater(r["last_end"], r["last_start"] - 0.001)  # end >= start

    def test_records_duration(self):
        fn = MagicMock()
        _run_scheduled_check("queue", fn)
        r = _check_runs["queue"]
        self.assertGreaterEqual(r["last_duration"], 0.0)
        self.assertIsInstance(r["last_duration"], float)

    def test_different_checks_tracked_separately(self):
        fn_q = MagicMock()
        fn_r = MagicMock(side_effect=RuntimeError("boom"))
        _run_scheduled_check("queue", fn_q)
        _run_scheduled_check("repair", fn_r)
        self.assertEqual(_check_runs["queue"]["last_outcome"], "ok")
        self.assertEqual(_check_runs["repair"]["last_outcome"], "error")

    def test_run_count_accumulates_across_ok_and_error(self):
        fn_ok = MagicMock()
        fn_err = MagicMock(side_effect=RuntimeError("x"))
        _run_scheduled_check("queue", fn_ok)
        _run_scheduled_check("queue", fn_err)
        _run_scheduled_check("queue", fn_ok)
        r = _check_runs["queue"]
        self.assertEqual(r["run_count"], 3)
        self.assertEqual(r["error_count"], 1)


class RunMetadataConcurrencyTest(unittest.TestCase):

    def setUp(self):
        _reset_runs()

    def test_deferred_when_semaphore_full(self):
        """When _scheduler_sem cannot be acquired, outcome is 'deferred'."""
        fn = MagicMock()
        # Drain the semaphore completely
        acquired = []
        for _ in range(sched_mod._scheduler_sem._value if hasattr(sched_mod._scheduler_sem, '_value') else 3):
            if sched_mod._scheduler_sem.acquire(blocking=False):
                acquired.append(True)
        try:
            _run_scheduled_check("queue", fn)
            r = _check_runs.get("queue")
            self.assertIsNotNone(r)
            self.assertEqual(r["last_outcome"], "deferred")
            fn.assert_not_called()
        finally:
            for _ in acquired:
                sched_mod._scheduler_sem.release()

    def test_skipped_when_check_already_running(self):
        """When the per-check lock is already held, outcome is 'skipped'."""
        lock = sched_mod._check_locks.get("queue")
        if lock is None:
            self.skipTest("no lock for 'queue'")
        lock.acquire()
        try:
            fn = MagicMock()
            _run_scheduled_check("queue", fn)
            r = _check_runs.get("queue")
            self.assertIsNotNone(r)
            self.assertEqual(r["last_outcome"], "skipped")
            fn.assert_not_called()
        finally:
            lock.release()
