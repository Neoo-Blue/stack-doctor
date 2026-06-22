"""Unit tests for doctor.checks.repair.verify.

Tests cover:
  _repair_verify_pending(state):
    - returns immediately when no pending entries
    - removes entry when the arr instance is unknown
    - polls command_status when cmd_id is present and not yet done
    - marks cmd_done=True when command reaches terminal state
    - marks cmd_done=True when command_status returns None (endpoint gone)
    - detects a successful grab via history_grabbed and removes the entry
    - removes entry when deadline has passed without a grab
    - leaves entry in place when within deadline and no grab yet

  _repair_record_verify(state, arr, title, cmd_id, media_id, entity_ids):
    - creates a new pending entry with correct fields
    - uses a string key derived from arr.name + title slug
    - cmd_id is stored only if it is an int (None otherwise)
    - deadline is approximately time.time() + REPAIR_VERIFY_DEADLINE
    - search_ts is a UTC datetime string
    - entity_ids defaults to [] when None is passed

All time, Arr API calls, and state are provided by the test.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

_MOD = "doctor.checks.repair.verify"


def _make_arr(name="sonarr-1"):
    arr = MagicMock()
    arr.name = name
    arr.kind = "sonarr"
    arr.command_status.return_value = None
    arr.history_grabbed.return_value = None
    return arr


def _pending(arr_name, title="Show", cmd_id=None, media_id=1,
             entity_ids=None, deadline=None, cmd_done=False, search_ts="2026-01-01T00:00:00Z"):
    return {
        "arr_name":   arr_name,
        "title":      title,
        "cmd_id":     cmd_id,
        "cmd_done":   cmd_done,
        "media_id":   media_id,
        "entity_ids": entity_ids or [],
        "search_ts":  search_ts,
        "deadline":   deadline if deadline is not None else time.time() + 3600,
    }


def _state_with(*entries):
    """Build a state dict with given pending verify entries keyed by index."""
    pv = {}
    for i, e in enumerate(entries):
        pv[f"key{i}"] = e
    return {"__repair_verify__": pv}


def _run_pending(state, instances):
    from doctor.checks.repair.verify import _repair_verify_pending
    with patch(_MOD + ".INSTANCES", instances):
        _repair_verify_pending(state)
    return state


class VerifyPendingNoOpTest(unittest.TestCase):

    def test_no_op_when_no_pending_entries(self):
        state = {}
        arr = _make_arr()
        _run_pending(state, [arr])
        # state should have the key created by setdefault, but it should be empty
        self.assertEqual(state.get("__repair_verify__", {}), {})

    def test_removes_entry_for_unknown_arr(self):
        arr = _make_arr(name="known")
        entry = _pending("unknown-arr")  # not in INSTANCES
        state = _state_with(entry)
        _run_pending(state, [arr])
        self.assertEqual(state["__repair_verify__"], {})


class VerifyPendingCommandPollTest(unittest.TestCase):

    def test_marks_cmd_done_on_terminal_status(self):
        arr = _make_arr()
        arr.command_status.return_value = "completed"
        entry = _pending(arr.name, cmd_id=42, cmd_done=False)
        state = _state_with(entry)
        _run_pending(state, [arr])
        arr.command_status.assert_called_once_with(42)
        remaining = state["__repair_verify__"]
        if remaining:
            self.assertTrue(list(remaining.values())[0].get("cmd_done"))

    def test_marks_cmd_done_when_status_is_none(self):
        arr = _make_arr()
        arr.command_status.return_value = None  # endpoint gone
        entry = _pending(arr.name, cmd_id=99, cmd_done=False)
        state = _state_with(entry)
        _run_pending(state, [arr])
        remaining = state["__repair_verify__"]
        if remaining:
            self.assertTrue(list(remaining.values())[0].get("cmd_done"))

    def test_skips_command_poll_when_already_done(self):
        arr = _make_arr()
        entry = _pending(arr.name, cmd_id=7, cmd_done=True)
        state = _state_with(entry)
        _run_pending(state, [arr])
        arr.command_status.assert_not_called()


class VerifyPendingGrabTest(unittest.TestCase):

    def test_removes_entry_on_successful_grab(self):
        arr = _make_arr()
        arr.history_grabbed.return_value = {
            "sourceTitle": "Show.S01E01.mkv",
            "data": {"indexer": "SomeIndexer"},
        }
        entry = _pending(arr.name, media_id=1, cmd_done=True)
        state = _state_with(entry)
        _run_pending(state, [arr])
        self.assertEqual(state["__repair_verify__"], {})

    def test_leaves_entry_when_no_grab_within_deadline(self):
        arr = _make_arr()
        arr.history_grabbed.return_value = None
        entry = _pending(arr.name, media_id=1, deadline=time.time() + 3600, cmd_done=True)
        state = _state_with(entry)
        _run_pending(state, [arr])
        # Entry should still be present
        self.assertEqual(len(state["__repair_verify__"]), 1)

    def test_removes_entry_when_deadline_exceeded(self):
        arr = _make_arr()
        arr.history_grabbed.return_value = None
        past_deadline = time.time() - 1  # already expired
        entry = _pending(arr.name, media_id=1, deadline=past_deadline, cmd_done=True)
        state = _state_with(entry)
        _run_pending(state, [arr])
        self.assertEqual(state["__repair_verify__"], {})

    def test_multiple_entries_handled_independently(self):
        arr = _make_arr()
        # Entry 0: grabbed → removed
        # Entry 1: past deadline → removed
        # Entry 2: within deadline, no grab → kept
        arr.history_grabbed.side_effect = [
            {"sourceTitle": "A", "data": {}},  # entry 0 grabbed
            None,                               # entry 1 not grabbed (expired)
            None,                               # entry 2 not grabbed (alive)
        ]
        e0 = _pending(arr.name, media_id=1, deadline=time.time() + 3600, cmd_done=True)
        e1 = _pending(arr.name, media_id=2, deadline=time.time() - 1, cmd_done=True)
        e2 = _pending(arr.name, media_id=3, deadline=time.time() + 3600, cmd_done=True)
        state = {"__repair_verify__": {"k0": e0, "k1": e1, "k2": e2}}
        _run_pending(state, [arr])
        remaining = state["__repair_verify__"]
        self.assertNotIn("k0", remaining)
        self.assertNotIn("k1", remaining)
        self.assertIn("k2", remaining)


class RecordVerifyTest(unittest.TestCase):

    def _run_record(self, arr, title, cmd_id, media_id, entity_ids,
                    deadline_delta=3600, now=None):
        if now is None:
            now = time.time()
        state = {}
        from doctor.checks.repair.verify import _repair_record_verify
        with patch(_MOD + ".REPAIR_VERIFY_DEADLINE", deadline_delta), \
             patch(_MOD + ".time") as mock_time:
            mock_time.time.return_value = now
            _repair_record_verify(state, arr, title, cmd_id, media_id, entity_ids)
        return state

    def test_creates_pending_entry(self):
        arr = _make_arr()
        state = self._run_record(arr, "My Show", 42, 10, [1, 2])
        pv = state.get("__repair_verify__", {})
        self.assertEqual(len(pv), 1)
        entry = list(pv.values())[0]
        self.assertEqual(entry["arr_name"], arr.name)
        self.assertEqual(entry["title"], "My Show")
        self.assertEqual(entry["cmd_id"], 42)
        self.assertEqual(entry["media_id"], 10)
        self.assertEqual(entry["entity_ids"], [1, 2])

    def test_key_is_stable_slug(self):
        arr = _make_arr(name="sonarr-1")
        state = self._run_record(arr, "My Show!", 1, 1, [])
        pv = state["__repair_verify__"]
        key = list(pv.keys())[0]
        self.assertTrue(key.startswith("sonarr-1:"))
        # Key should only contain safe chars
        slug = key.split(":", 1)[1]
        self.assertRegex(slug, r"^[a-z0-9_]+$")

    def test_deadline_is_now_plus_delta(self):
        arr = _make_arr()
        now = 1_000_000.0
        state = self._run_record(arr, "Show", None, 1, [], deadline_delta=7200, now=now)
        entry = list(state["__repair_verify__"].values())[0]
        self.assertAlmostEqual(entry["deadline"], now + 7200, places=0)

    def test_non_int_cmd_id_stored_as_none(self):
        arr = _make_arr()
        state = self._run_record(arr, "Show", "not-an-int", 1, [])
        entry = list(state["__repair_verify__"].values())[0]
        self.assertIsNone(entry["cmd_id"])

    def test_none_entity_ids_stored_as_empty_list(self):
        arr = _make_arr()
        state = self._run_record(arr, "Show", None, 1, None)
        entry = list(state["__repair_verify__"].values())[0]
        self.assertEqual(entry["entity_ids"], [])

    def test_search_ts_is_utc_string(self):
        arr = _make_arr()
        state = self._run_record(arr, "Show", None, 1, [])
        entry = list(state["__repair_verify__"].values())[0]
        ts = entry["search_ts"]
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
