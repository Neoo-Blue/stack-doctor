"""Unit tests for doctor.checks.repair.missing_from_disk._missing_from_disk_check().

Tests cover:
  - skips instance if kind is not sonarr/radarr
  - skips when budget <= 0 on entry
  - skips unmonitored items when REPAIR_UNMONITORED=False
  - skips items within MFD recheck cooldown
  - triggers SeasonSearch for a Sonarr MissingFromDisk entry
  - triggers MoviesSearch for a Radarr MissingFromDisk entry
  - does NOT trigger search for non-grabbed history events
  - does NOT trigger search for grabbed events without reason=MissingFromDisk
  - only searches each (arr, season/movie) key once per sweep
  - records the timestamp in state after a search
  - decrements budget after each action
  - DRY_RUN: logs intent but does not call arr.command()
  - handles arr.history() raising an exception (skips, does not crash)
  - handles arr.series()/movies() raising an exception (skips, does not crash)
  - radarr records dict unwrapped from {"records": [...]} correctly

All external I/O is replaced with MagicMock.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

_MOD = "doctor.checks.repair.missing_from_disk"


def _make_arr(name="sonarr-1", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    arr.series.return_value = []
    arr.movies.return_value = []
    arr.history.return_value = []
    arr.command.return_value = None
    return arr


def _series(sid, title="Show", monitored=True):
    return {"id": sid, "title": title, "monitored": monitored}


def _movie(mid, title="Movie", monitored=True):
    return {"id": mid, "title": title, "monitored": monitored}


def _grabbed_mfd_ep(series_id, season_number):
    """Sonarr grabbed + MissingFromDisk history record."""
    return {
        "eventType": "grabbed",
        "episode": {"seriesId": series_id, "seasonNumber": season_number},
        "data": {"reason": "MissingFromDisk"},
    }


def _grabbed_mfd_movie():
    """Radarr grabbed + MissingFromDisk history record."""
    return {
        "eventType": "grabbed",
        "data": {"reason": "MissingFromDisk"},
    }


def _grabbed_ok():
    """Grabbed but NOT MissingFromDisk — should be ignored."""
    return {"eventType": "grabbed", "data": {"reason": "SomethingElse"}}


def _run(instances, state=None, *, dry_run=False, repair_unmonitored=True,
         repair_mfd_recheck=0, repair_item_interval=0, budget=10):
    """Call _missing_from_disk_check with fully patched config globals."""
    if state is None:
        state = {}

    from doctor.checks.repair.missing_from_disk import _missing_from_disk_check

    with patch(_MOD + ".INSTANCES", instances), \
         patch(_MOD + ".DRY_RUN", dry_run), \
         patch(_MOD + ".REPAIR_UNMONITORED", repair_unmonitored), \
         patch(_MOD + ".REPAIR_MFD_RECHECK", repair_mfd_recheck), \
         patch(_MOD + ".REPAIR_ITEM_INTERVAL", repair_item_interval):
        acted = _missing_from_disk_check(state, acted=0, budget=budget)

    return acted, state


class MfdSkipTest(unittest.TestCase):

    def test_skips_non_sonarr_radarr_instance(self):
        arr = _make_arr(kind="prowlarr")
        acted, _ = _run([arr])
        arr.series.assert_not_called()
        arr.movies.assert_not_called()
        self.assertEqual(acted, 0)

    def test_skips_when_budget_zero(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        acted, _ = _run([arr], budget=0)
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)

    def test_skips_unmonitored_when_flag_off(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1, monitored=False)]
        arr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        acted, _ = _run([arr], repair_unmonitored=False)
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)

    def test_includes_unmonitored_when_flag_on(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1, monitored=False)]
        arr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        acted, _ = _run([arr], repair_unmonitored=True)
        arr.command.assert_called_once()
        self.assertEqual(acted, 1)

    def test_skips_item_within_recheck_cooldown(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        state = {"__repair_mfd__": {"sonarr-1:1:s1": time.time()}}  # just searched
        acted, _ = _run([arr], state=state, repair_mfd_recheck=3600)
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)


class MfdSonarrTest(unittest.TestCase):

    def test_triggers_season_search(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(10)]
        arr.history.return_value = [_grabbed_mfd_ep(10, 2)]
        acted, state = _run([arr])
        arr.command.assert_called_once_with("SeasonSearch", seriesId=10, seasonNumber=2)
        self.assertEqual(acted, 1)
        self.assertIn("sonarr-1:10:s2", state["__repair_mfd__"])

    def test_only_searches_each_season_once_per_sweep(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(10)]
        # Two records for the same series+season
        arr.history.return_value = [_grabbed_mfd_ep(10, 2), _grabbed_mfd_ep(10, 2)]
        acted, _ = _run([arr])
        self.assertEqual(arr.command.call_count, 1)
        self.assertEqual(acted, 1)

    def test_does_not_trigger_for_non_grabbed_events(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.return_value = [{"eventType": "downloadFolderImported", "data": {}}]
        acted, _ = _run([arr])
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)

    def test_does_not_trigger_for_grabbed_without_mfd_reason(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.return_value = [_grabbed_ok()]
        acted, _ = _run([arr])
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)

    def test_decrements_budget(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1), _series(2)]
        arr.history.side_effect = [
            [_grabbed_mfd_ep(1, 1)],
            [_grabbed_mfd_ep(2, 1)],
        ]
        acted, _ = _run([arr], budget=1)
        # Budget of 1 → only one search
        self.assertEqual(arr.command.call_count, 1)
        self.assertEqual(acted, 1)


class MfdRadarrTest(unittest.TestCase):

    def test_triggers_movies_search(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [_movie(5)]
        arr.history.return_value = [_grabbed_mfd_movie()]
        acted, state = _run([arr])
        arr.command.assert_called_once_with("MoviesSearch", movieIds=[5])
        self.assertEqual(acted, 1)
        self.assertIn("radarr-1:5", state["__repair_mfd__"])

    def test_unwraps_radarr_records_dict(self):
        """Radarr wraps history in {"records": [...]}."""
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [_movie(5)]
        arr.history.return_value = {"records": [_grabbed_mfd_movie()]}
        acted, _ = _run([arr])
        arr.command.assert_called_once()
        self.assertEqual(acted, 1)

    def test_only_searches_each_movie_once_per_sweep(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [_movie(5)]
        arr.history.return_value = [_grabbed_mfd_movie(), _grabbed_mfd_movie()]
        acted, _ = _run([arr])
        self.assertEqual(arr.command.call_count, 1)
        self.assertEqual(acted, 1)


class MfdDryRunTest(unittest.TestCase):

    def test_dry_run_sonarr_does_not_call_command(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.return_value = [_grabbed_mfd_ep(1, 3)]
        acted, state = _run([arr], dry_run=True)
        arr.command.assert_not_called()
        self.assertEqual(acted, 1)
        self.assertIn("sonarr-1:1:s3", state["__repair_mfd__"])

    def test_dry_run_radarr_does_not_call_command(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [_movie(5)]
        arr.history.return_value = [_grabbed_mfd_movie()]
        acted, state = _run([arr], dry_run=True)
        arr.command.assert_not_called()
        self.assertEqual(acted, 1)


class MfdErrorHandlingTest(unittest.TestCase):

    def test_history_exception_is_swallowed(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [_series(1)]
        arr.history.side_effect = RuntimeError("API error")
        acted, _ = _run([arr])
        arr.command.assert_not_called()
        self.assertEqual(acted, 0)

    def test_series_fetch_exception_is_swallowed(self):
        arr = _make_arr(kind="sonarr")
        arr.series.side_effect = RuntimeError("connection error")
        acted, _ = _run([arr])
        self.assertEqual(acted, 0)

    def test_movies_fetch_exception_is_swallowed(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.side_effect = RuntimeError("timeout")
        acted, _ = _run([arr])
        self.assertEqual(acted, 0)


class MfdBreakContinueBugTest(unittest.TestCase):
    """Regression tests for the break/continue bug on line 14.

    When a non-sonarr/radarr instance (e.g. prowlarr) appears BEFORE a valid
    Sonarr instance in INSTANCES, the old `break` would stop processing all
    remaining instances.  The fix uses `continue` for the kind-guard so only
    that one instance is skipped.
    """

    def test_prowlarr_before_sonarr_does_not_block_sonarr(self):
        """Prowlarr first, then Sonarr with an MFD entry -> Sonarr must still be processed."""
        prowlarr = _make_arr(name="prowlarr-1", kind="prowlarr")
        sonarr = _make_arr(name="sonarr-1", kind="sonarr")
        sonarr.series.return_value = [_series(1)]
        sonarr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        acted, _ = _run([prowlarr, sonarr])
        # Prowlarr should be skipped (not touched), Sonarr should act
        prowlarr.series.assert_not_called()
        prowlarr.movies.assert_not_called()
        sonarr.command.assert_called_once()
        self.assertEqual(acted, 1)

    def test_budget_zero_still_breaks_out(self):
        """budget=0 must still prevent any work even across multiple instances."""
        sonarr = _make_arr(name="sonarr-1", kind="sonarr")
        sonarr.series.return_value = [_series(1)]
        sonarr.history.return_value = [_grabbed_mfd_ep(1, 1)]
        radarr = _make_arr(name="radarr-1", kind="radarr")
        radarr.movies.return_value = [_movie(5)]
        radarr.history.return_value = [_grabbed_mfd_movie()]
        acted, _ = _run([sonarr, radarr], budget=0)
        sonarr.command.assert_not_called()
        radarr.command.assert_not_called()
        self.assertEqual(acted, 0)
