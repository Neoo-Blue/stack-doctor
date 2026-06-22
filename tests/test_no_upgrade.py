"""Unit tests for the no_upgrade_profile check.

Covers profile lookup, series filtering, and the PUT update call via mocked Arr.
The check's three _req calls are now all behind public Arr methods (quality_profiles,
series, update_series) so we can mock them cleanly without touching _req.
"""
import unittest
from unittest.mock import MagicMock, patch

from doctor.checks.no_upgrade import check_no_upgrade_profile


def _make_arr(name="sonarr", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    arr.quality_profiles.return_value = []
    arr.series.return_value = []
    arr.update_series.return_value = None
    return arr


def _series(sid, title, status="ended", pct=100, ep_count=10, profile_id=1):
    return {
        "id": sid,
        "title": title,
        "status": status,
        "qualityProfileId": profile_id,
        "statistics": {"episodeCount": ep_count, "percentOfEpisodes": pct},
    }


_BASE = dict(
    INSTANCES=[],           # overridden per test
    EN_NO_UPGRADE_PROFILE=True,
    NO_UPGRADE_PROFILE_NAME="No Upgrade",
    NO_UPGRADE_PROFILE_ID=0,
)

def _patch(**overrides):
    kw = {**_BASE, **overrides}
    return patch.multiple("doctor.checks.no_upgrade", **kw)


class ProfileLookupTest(unittest.TestCase):
    def test_skips_when_profile_not_found(self):
        arr = _make_arr()
        arr.quality_profiles.return_value = [{"id": 5, "name": "Other"}]
        arr.series.return_value = []
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=0):
            check_no_upgrade_profile()
        arr.update_series.assert_not_called()

    def test_resolves_profile_by_name(self):
        arr = _make_arr()
        arr.quality_profiles.return_value = [{"id": 7, "name": "No Upgrade"}]
        arr.series.return_value = [_series(1, "Show A", profile_id=2)]
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=0):
            check_no_upgrade_profile()
        updated = arr.update_series.call_args[0][0]
        self.assertEqual(updated["qualityProfileId"], 7)

    def test_uses_explicit_profile_id_without_lookup(self):
        arr = _make_arr()
        arr.series.return_value = [_series(1, "Show A", profile_id=2)]
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=9):
            check_no_upgrade_profile()
        arr.quality_profiles.assert_not_called()
        updated = arr.update_series.call_args[0][0]
        self.assertEqual(updated["qualityProfileId"], 9)


class SeriesFilterTest(unittest.TestCase):
    def _run(self, series_list, profile_id=5):
        arr = _make_arr()
        arr.quality_profiles.return_value = [{"id": profile_id, "name": "No Upgrade"}]
        arr.series.return_value = series_list
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=0):
            check_no_upgrade_profile()
        return arr

    def test_skips_continuing_series(self):
        arr = self._run([_series(1, "Ongoing", status="continuing")])
        arr.update_series.assert_not_called()

    def test_skips_already_on_target_profile(self):
        arr = self._run([_series(1, "Done", profile_id=5)], profile_id=5)
        arr.update_series.assert_not_called()

    def test_skips_incomplete_ended_series(self):
        arr = self._run([_series(1, "Partial", pct=80, ep_count=10)])
        arr.update_series.assert_not_called()

    def test_skips_ended_with_zero_episodes(self):
        arr = self._run([_series(1, "Empty", pct=100, ep_count=0)])
        arr.update_series.assert_not_called()

    def test_moves_complete_ended_series(self):
        arr = self._run([_series(1, "Complete", status="ended", pct=100, ep_count=10)])
        arr.update_series.assert_called_once()

    def test_moves_multiple_eligible_series(self):
        series = [
            _series(1, "Show A", status="ended", pct=100, ep_count=5),
            _series(2, "Show B", status="ended", pct=100, ep_count=12),
        ]
        arr = self._run(series)
        self.assertEqual(arr.update_series.call_count, 2)


class UpdateSeriesTest(unittest.TestCase):
    def test_update_sets_quality_profile_id(self):
        arr = _make_arr()
        arr.quality_profiles.return_value = [{"id": 3, "name": "No Upgrade"}]
        s = _series(42, "My Show", profile_id=1)
        arr.series.return_value = [s]
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=0):
            check_no_upgrade_profile()
        updated = arr.update_series.call_args[0][0]
        self.assertEqual(updated["id"], 42)
        self.assertEqual(updated["qualityProfileId"], 3)

    def test_update_failure_does_not_abort(self):
        """update_series() raising must not stop the rest of the series from being processed."""
        arr = _make_arr()
        arr.quality_profiles.return_value = [{"id": 3, "name": "No Upgrade"}]
        arr.series.return_value = [
            _series(1, "Fail Show", profile_id=1),
            _series(2, "Good Show", profile_id=1),
        ]
        arr.update_series.side_effect = [Exception("timeout"), None]
        with _patch(INSTANCES=[arr], NO_UPGRADE_PROFILE_ID=0):
            check_no_upgrade_profile()
        self.assertEqual(arr.update_series.call_count, 2)

    def test_skips_non_sonarr_instances(self):
        arr = _make_arr(kind="radarr")
        with _patch(INSTANCES=[arr]):
            check_no_upgrade_profile()
        arr.series.assert_not_called()


if __name__ == "__main__":
    unittest.main()
