"""Unit tests for the churn-brake logic in doctor.state.

These tests exercise _churn_record and _churn_remonitor in isolation using
only an in-memory state dict and a mocked Arr so no network or filesystem
access is required.

The churn-brake constants (CHURN_LIMIT, CHURN_ACTION, CHURN_BACKOFF) are
module-level names in doctor.state (imported via star from doctor.config at
module load time).  We patch them on the state module directly so the tests
are independent of env-var parsing order and don't affect each other.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

from doctor.state import _churn_record, _churn_remonitor, _offenders


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_arr(kind="sonarr", name="sonarr", target_id=42):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    arr.set_monitored.return_value = True  # success by default
    # queue_target_id is a real method on Arr that inspects rec; since we use MagicMock
    # we must return the target_id explicitly so _churn_record writes the right state key.
    arr.queue_target_id.return_value = target_id
    return arr


def _make_rec(episode_id=42, movie_id=None):
    """A minimal queue record (content not inspected by tests; queue_target_id is mocked)."""
    rec = {"id": 1}
    if episode_id is not None:
        rec["episodeId"] = episode_id
    if movie_id is not None:
        rec["movieId"] = movie_id
    return rec


def _patched(**kwargs):
    """Return a unittest.mock._patch context manager stack for churn constants."""
    defaults = {
        "doctor.state.CHURN_LIMIT": 3,
        "doctor.state.CHURN_ACTION": "report",
        "doctor.state.CHURN_BACKOFF": [600, 3600, 86400],
    }
    defaults.update({"doctor.state." + k: v for k, v in kwargs.items()})
    # Build a single patcher using patch.multiple
    return patch.multiple("doctor.state", **{k.replace("doctor.state.", ""): v for k, v in defaults.items()})


# ---------------------------------------------------------------------------
# _churn_record — CHURN_LIMIT=0 (disabled)
# ---------------------------------------------------------------------------

class ChurnDisabledTest(unittest.TestCase):
    def test_disabled_when_limit_zero(self):
        """CHURN_LIMIT=0 means the brake is off; _churn_record must return False immediately."""
        state = {}
        arr = _make_arr()
        rec = _make_rec()
        with patch("doctor.state.CHURN_LIMIT", 0):
            result = _churn_record(state, arr, rec, "Show S01E01")
        self.assertFalse(result)
        # No state should have been written
        self.assertEqual(state, {})

    def test_disabled_when_no_target_id(self):
        """If queue_target_id returns None, _churn_record must return False."""
        arr = _make_arr(kind="prowlarr")  # queue_target_id returns None for prowlarr
        rec = _make_rec(episode_id=None, movie_id=None)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", 3):
            result = _churn_record(state, arr, rec, "Some Title")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# _churn_record — accumulation below limit
# ---------------------------------------------------------------------------

class ChurnAccumulationTest(unittest.TestCase):
    def _run(self, n_fails, limit=3, action="report"):
        arr = _make_arr(target_id=99)
        rec = _make_rec(episode_id=99)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", limit), \
             patch("doctor.state.CHURN_ACTION", action), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            results = []
            for _ in range(n_fails):
                results.append(_churn_record(state, arr, rec, "Show S01E01"))
        return state, results

    def test_fails_accumulate_below_limit(self):
        state, results = self._run(n_fails=2, limit=3)
        self.assertTrue(all(r is False for r in results))
        offs = _offenders(state).get("sonarr", {}).get("99", {})
        self.assertEqual(offs["fails"], 2)

    def test_no_action_at_limit_minus_one(self):
        _, results = self._run(n_fails=2, limit=3, action="park")
        # At fail #2 (< limit 3), still no action
        self.assertFalse(any(results))

    def test_counters_accumulate_across_calls(self):
        arr = _make_arr(target_id=7)
        rec = _make_rec(episode_id=7)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", 5), \
             patch("doctor.state.CHURN_ACTION", "report"), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            for i in range(4):
                _churn_record(state, arr, rec, "Movie")
        offs = _offenders(state)["sonarr"]["7"]
        self.assertEqual(offs["fails"], 4)


# ---------------------------------------------------------------------------
# _churn_record — action=report
# ---------------------------------------------------------------------------

class ChurnReportActionTest(unittest.TestCase):
    def _hit_limit(self, limit=3):
        arr = _make_arr(target_id=10)
        rec = _make_rec(episode_id=10)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", limit), \
             patch("doctor.state.CHURN_ACTION", "report"), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            results = [_churn_record(state, arr, rec, "Show") for _ in range(limit)]
        return arr, state, results

    def test_report_returns_false(self):
        """report action must not un-monitor, so the caller's re-search still fires."""
        _, _, results = self._hit_limit()
        self.assertFalse(results[-1])  # last call hits limit

    def test_report_does_not_call_set_monitored(self):
        arr, _, _ = self._hit_limit()
        arr.set_monitored.assert_not_called()

    def test_report_sets_until_to_sentinel(self):
        """until=-1 signals 'reported, no backoff scheduled'."""
        _, state, _ = self._hit_limit()
        offs = _offenders(state)["sonarr"]["10"]
        self.assertEqual(offs["until"], -1)

    def test_report_only_fires_once(self):
        """Subsequent calls after the first report must short-circuit (until != 0)."""
        arr = _make_arr()
        rec = _make_rec(episode_id=10)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", 2), \
             patch("doctor.state.CHURN_ACTION", "report"), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            for _ in range(5):
                _churn_record(state, arr, rec, "Show")
        # set_monitored must never have been called
        arr.set_monitored.assert_not_called()


# ---------------------------------------------------------------------------
# _churn_record — action=park
# ---------------------------------------------------------------------------

class ChurnParkActionTest(unittest.TestCase):
    def _park(self, limit=3):
        arr = _make_arr(target_id=20)
        rec = _make_rec(episode_id=20)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", limit), \
             patch("doctor.state.CHURN_ACTION", "park"), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            results = [_churn_record(state, arr, rec, "Movie") for _ in range(limit)]
        return arr, state, results

    def test_park_returns_true_at_limit(self):
        _, _, results = self._park()
        self.assertTrue(results[-1])

    def test_park_calls_set_monitored_false(self):
        arr, _, _ = self._park()
        arr.set_monitored.assert_called_once_with([20], False)

    def test_park_sets_until_to_sentinel(self):
        _, state, _ = self._park()
        offs = _offenders(state)["sonarr"]["20"]
        self.assertEqual(offs["until"], -1)

    def test_park_resets_fails_counter(self):
        _, state, _ = self._park()
        offs = _offenders(state)["sonarr"]["20"]
        self.assertEqual(offs["fails"], 0)

    def test_park_no_action_if_set_monitored_fails(self):
        arr = _make_arr(target_id=20)
        arr.set_monitored.return_value = False  # API failure
        rec = _make_rec(episode_id=20)
        state = {}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "park"), \
             patch("doctor.state.CHURN_BACKOFF", [600]):
            results = [_churn_record(state, arr, rec, "X") for _ in range(3)]
        self.assertFalse(results[-1])


# ---------------------------------------------------------------------------
# _churn_record — action=backoff
# ---------------------------------------------------------------------------

class ChurnBackoffActionTest(unittest.TestCase):
    def _backoff(self, limit=3, backoff_levels=None):
        arr = _make_arr(target_id=30)
        rec = _make_rec(episode_id=30)
        state = {}
        levels = backoff_levels or [600, 3600, 86400]
        with patch("doctor.state.CHURN_LIMIT", limit), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.CHURN_BACKOFF", levels):
            results = [_churn_record(state, arr, rec, "Series") for _ in range(limit)]
        return arr, state, results

    def test_backoff_returns_true_at_limit(self):
        _, _, results = self._backoff()
        self.assertTrue(results[-1])

    def test_backoff_schedules_remonitor_timestamp(self):
        t_before = time.time()
        _, state, _ = self._backoff(backoff_levels=[600])
        t_after = time.time()
        offs = _offenders(state)["sonarr"]["30"]
        self.assertGreater(offs["until"], t_before + 590)
        self.assertLess(offs["until"], t_after + 610)

    def test_backoff_level_increments(self):
        _, state, _ = self._backoff()
        offs = _offenders(state)["sonarr"]["30"]
        self.assertEqual(offs["level"], 1)

    def test_backoff_level_escalates_on_repeated_breach(self):
        """Each successive park cycle uses the next backoff tier."""
        arr = _make_arr(target_id=30)
        rec = _make_rec(episode_id=30)
        state = {}
        levels = [600, 3600, 86400]

        def _run_cycle(n):
            with patch("doctor.state.CHURN_LIMIT", 2), \
                 patch("doctor.state.CHURN_ACTION", "backoff"), \
                 patch("doctor.state.CHURN_BACKOFF", levels):
                for _ in range(n):
                    _churn_record(state, arr, rec, "S")
                # Simulate re-monitor elapsed: reset until so next cycle can park again
                offs = _offenders(state)["sonarr"]["30"]
                offs["until"] = 0

        _run_cycle(2)  # first park: level 0 -> 1
        _run_cycle(2)  # second park: level 1 -> 2
        offs = _offenders(state)["sonarr"]["30"]
        self.assertEqual(offs["level"], 2)

    def test_backoff_clamps_to_last_tier(self):
        """Level beyond the backoff list clamps to the last entry."""
        arr = _make_arr(target_id=31)
        rec = _make_rec(episode_id=31)
        state = {}
        levels = [600]  # only one tier

        def _cycle():
            with patch("doctor.state.CHURN_LIMIT", 2), \
                 patch("doctor.state.CHURN_ACTION", "backoff"), \
                 patch("doctor.state.CHURN_BACKOFF", levels):
                for _ in range(2):
                    _churn_record(state, arr, rec, "T")
                _offenders(state)["sonarr"]["31"]["until"] = 0

        for _ in range(3):  # three park cycles
            _cycle()

        # After 3 park cycles with a single-tier [600] backoff, level should be 3
        # (level keeps incrementing even when clamped to last tier).
        offs = _offenders(state)["sonarr"]["31"]
        self.assertEqual(offs["level"], 3)
        # until was reset to 0 by the last _cycle iteration so we only check level here.


# ---------------------------------------------------------------------------
# _churn_remonitor
# ---------------------------------------------------------------------------

class ChurnRemonitorTest(unittest.TestCase):
    def test_noop_when_limit_zero(self):
        """If CHURN_LIMIT=0, remonitor must do nothing."""
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"5": {"until": 1, "fails": 0, "level": 0, "title": "X"}}}}
        with patch("doctor.state.CHURN_LIMIT", 0), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        arr.set_monitored.assert_not_called()

    def test_noop_when_action_is_park(self):
        """remonitor only runs for action=backoff; park is manual."""
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"5": {"until": 1, "fails": 0, "level": 0, "title": "X"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "park"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        arr.set_monitored.assert_not_called()

    def test_noop_when_until_not_elapsed(self):
        far_future = time.time() + 9999
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"5": {"until": far_future, "fails": 0, "level": 1, "title": "X"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        arr.set_monitored.assert_not_called()

    def test_remonitor_fires_when_elapsed(self):
        past = time.time() - 1  # already elapsed
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"42": {"until": past, "fails": 0, "level": 1, "title": "Show"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        arr.set_monitored.assert_called_once_with([42], True)

    def test_remonitor_resets_fails_and_until(self):
        past = time.time() - 1
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"42": {"until": past, "fails": 3, "level": 1, "title": "Show"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        offs = state["__offenders__"]["sonarr"]["42"]
        self.assertEqual(offs["fails"], 0)
        self.assertEqual(offs["until"], 0)

    def test_remonitor_preserves_level(self):
        """Level must survive remonitor so the next park escalates."""
        past = time.time() - 1
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"42": {"until": past, "fails": 0, "level": 2, "title": "X"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        self.assertEqual(state["__offenders__"]["sonarr"]["42"]["level"], 2)

    def test_remonitor_skips_sentinel_until(self):
        """until=-1 (permanent park) must not be re-monitored automatically."""
        arr = _make_arr()
        state = {"__offenders__": {"sonarr": {"7": {"until": -1, "fails": 0, "level": 0, "title": "X"}}}}
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr]):
            _churn_remonitor(state)
        arr.set_monitored.assert_not_called()

    def test_remonitor_multiple_instances(self):
        """remonitor iterates over all INSTANCES, not just the first."""
        past = time.time() - 1
        arr1 = _make_arr(name="sonarr")
        arr2 = _make_arr(name="radarr", kind="radarr")
        state = {
            "__offenders__": {
                "sonarr": {"1": {"until": past, "fails": 0, "level": 0, "title": "A"}},
                "radarr": {"2": {"until": past, "fails": 0, "level": 0, "title": "B"}},
            }
        }
        with patch("doctor.state.CHURN_LIMIT", 3), \
             patch("doctor.state.CHURN_ACTION", "backoff"), \
             patch("doctor.state.INSTANCES", [arr1, arr2]):
            _churn_remonitor(state)
        arr1.set_monitored.assert_called_once_with([1], True)
        arr2.set_monitored.assert_called_once_with([2], True)


if __name__ == "__main__":
    unittest.main()
