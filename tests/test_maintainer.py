"""Unit tests for the maintainer check - eligibility logic."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from doctor.checks.maintainer import (_series_is_eligible, _pulsarr_tagged_show,
                                       _pulsarr_tags, _tag_users)


class PulsarrTagsTest(unittest.TestCase):
    def test_extracts_all_matching_tags(self):
        series = {"tags": [1, 2, 3]}
        tag_map = {1: "pulsarr-alice", 2: "pulsarr-bob", 3: "other"}
        result = _pulsarr_tags(series, tag_map, "pulsarr-")
        self.assertEqual(result, {"pulsarr-alice", "pulsarr-bob"})

    def test_returns_empty_when_no_match(self):
        series = {"tags": [1]}
        tag_map = {1: "something-else"}
        self.assertEqual(_pulsarr_tags(series, tag_map, "pulsarr-"), set())

    def test_returns_empty_when_no_tags(self):
        self.assertEqual(_pulsarr_tags({"tags": []}, {1: "pulsarr-x"}, "pulsarr-"), set())

    def test_empty_tag_map(self):
        self.assertEqual(_pulsarr_tags({"tags": [1]}, {}, "pulsarr-"), set())


class TagUsersTest(unittest.TestCase):
    def test_extracts_username_from_tag(self):
        self.assertEqual(_tag_users({"pulsarr-alice"}, "pulsarr-"), {"alice"})

    def test_multiple_users(self):
        self.assertEqual(_tag_users({"pulsarr-alice", "pulsarr-bob"}, "pulsarr-"),
                         {"alice", "bob"})

    def test_empty_suffix_handled(self):
        self.assertEqual(_tag_users({"pulsarr-"}, "pulsarr-"), set())

    def test_empty_labels(self):
        self.assertEqual(_tag_users(set(), "pulsarr-"), set())


class SeriesEligibilityTest(unittest.TestCase):
    def _now(self):
        return datetime.now(timezone.utc)

    def _make_series(self, **kw):
        defaults = {
            "id": 1, "title": "Test Show", "status": "ended",
            "monitored": True, "year": 2020, "tags": [1],
            "added": "2020-01-15T00:00:00Z",
        }
        defaults.update(kw)
        return defaults

    def test_eligible_ended_old_unwatched_pulsarr_tagged(self):
        series = self._make_series()
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertTrue(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_watched_recently(self):
        series = self._make_series()
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertFalse(_series_is_eligible(series, {"Test Show"}, tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_continuing(self):
        series = self._make_series(status="continuing")
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertFalse(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_year_too_recent(self):
        series = self._make_series(year=2025)
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertFalse(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_no_pulsarr_tag(self):
        series = self._make_series(tags=[2])
        tag_map = {1: "other", 2: "something-else"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertFalse(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_no_tags(self):
        series = self._make_series(tags=[])
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertFalse(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_eligible_year_exactly_at_threshold(self):
        series = self._make_series(year=2023)
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0):
            self.assertTrue(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_not_eligible_added_recently(self):
        series = self._make_series(added=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat())
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 30):
            self.assertFalse(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_eligible_added_long_ago(self):
        series = self._make_series(added="2020-01-01T00:00:00Z")
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 30):
            self.assertTrue(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_unparseable_added_date_let_through(self):
        series = self._make_series(added="garbage-date")
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 30):
            self.assertTrue(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))

    def test_missing_added_date_let_through(self):
        series = self._make_series()
        del series["added"]
        tag_map = {1: "pulsarr-test"}
        with patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 30):
            self.assertTrue(_series_is_eligible(series, set(), tag_map, "pulsarr-", self._now(), "tagged"))


class CheckMaintainerIntegrationTest(unittest.TestCase):
    """Integration test for check_maintainer with mocked INSTANCES and API clients."""

    @staticmethod
    def _make_sonarr_instance(name="sonarr-shows", series_list=None):
        arr = MagicMock()
        arr.name = name
        arr.kind = "sonarr"
        arr.tag_map.return_value = {1: "pulsarr-plexuser"}
        arr.series.return_value = series_list or []
        return arr

    @classmethod
    def _make_series(cls, sid=1, title="Old Show", status="ended", year=2020,
                     monitored=True, tags=None):
        return {
            "id": sid, "title": title, "status": status,
            "monitored": monitored, "year": year,
            "tags": tags if tags is not None else [1],
            "tvdbId": sid * 100,
            "added": "2020-01-15T00:00:00Z",
        }

    def test_dry_run_logs_but_does_not_delete(self):
        series = [self._make_series()]
        arr = self._make_sonarr_instance(series_list=series)

        with patch("doctor.checks.maintainer.EN_MAINTAINER", True), \
             patch("doctor.checks.maintainer.TAUTULLI_URL", "http://taut:8181"), \
             patch("doctor.checks.maintainer.TAUTULLI_APIKEY", "k"), \
             patch("doctor.checks.maintainer.DRY_RUN", True), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MAX_ACTIONS", 5), \
             patch("doctor.checks.maintainer.MAINTAINER_LIBRARY_TITLE", "shows"), \
             patch("doctor.checks.maintainer.MAINTAINER_UNWATCHED_DAYS", 30), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0), \
             patch("doctor.checks.maintainer.MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"), \
             patch("doctor.checks.maintainer.MAINTAINER_RECHECK", 86400), \
             patch("doctor.checks.maintainer.MAINTAINER_MODE", "tagged"), \
             patch("doctor.checks.maintainer.MAINTAINER_PLEX_SECTION_KEY", 0), \
             patch("doctor.checks.maintainer.PULSARR_DB_PATH", ""), \
             patch("doctor.checks.maintainer.INSTANCES", [arr]), \
             patch("doctor.clients.tautulli.Tautulli.recently_watched_shows",
                   return_value=set()) as _mock_taut, \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            with patch("builtins.open"):  # state file mock
                check_maintainer()

            # Should NOT call DELETE
            arr._req.assert_not_called()
            # Should log WOULD delete
            log_calls = [c[0][0] for c in mock_log.info.call_args_list if c[0]]
            dry_call = [m for m in log_calls if isinstance(m, str) and "WOULD delete" in m]
            self.assertTrue(len(dry_call) > 0)

    def test_non_matching_library_skipped(self):
        series = [self._make_series()]
        arr = self._make_sonarr_instance(name="sonarr-anime", series_list=series)

        with patch("doctor.checks.maintainer.EN_MAINTAINER", True), \
             patch("doctor.checks.maintainer.TAUTULLI_URL", "http://taut:8181"), \
             patch("doctor.checks.maintainer.TAUTULLI_APIKEY", "k"), \
             patch("doctor.checks.maintainer.DRY_RUN", True), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MAX_ACTIONS", 5), \
             patch("doctor.checks.maintainer.MAINTAINER_LIBRARY_TITLE", "shows"), \
             patch("doctor.checks.maintainer.MAINTAINER_UNWATCHED_DAYS", 30), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0), \
             patch("doctor.checks.maintainer.MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"), \
             patch("doctor.checks.maintainer.MAINTAINER_RECHECK", 86400), \
             patch("doctor.checks.maintainer.MAINTAINER_MODE", "tagged"), \
             patch("doctor.checks.maintainer.MAINTAINER_PLEX_SECTION_KEY", 0), \
             patch("doctor.checks.maintainer.PULSARR_DB_PATH", ""), \
             patch("doctor.checks.maintainer.INSTANCES", [arr]), \
             patch("doctor.clients.tautulli.Tautulli.recently_watched_shows",
                   return_value=set()), \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            with patch("builtins.open"):
                check_maintainer()

            arr.tag_map.assert_not_called()
            debug_calls = [c[0][0] for c in mock_log.debug.call_args_list if c[0]]
            skip_call = [m for m in debug_calls if isinstance(m, str) and "not matching library title" in m]
            self.assertTrue(len(skip_call) > 0)

    def test_watched_show_skipped(self):
        series = [self._make_series(title="Watched Show")]
        arr = self._make_sonarr_instance(series_list=series)

        with patch("doctor.checks.maintainer.EN_MAINTAINER", True), \
             patch("doctor.checks.maintainer.TAUTULLI_URL", "http://taut:8181"), \
             patch("doctor.checks.maintainer.TAUTULLI_APIKEY", "k"), \
             patch("doctor.checks.maintainer.DRY_RUN", True), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MAX_ACTIONS", 5), \
             patch("doctor.checks.maintainer.MAINTAINER_LIBRARY_TITLE", "shows"), \
             patch("doctor.checks.maintainer.MAINTAINER_UNWATCHED_DAYS", 30), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0), \
             patch("doctor.checks.maintainer.MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"), \
             patch("doctor.checks.maintainer.MAINTAINER_RECHECK", 86400), \
             patch("doctor.checks.maintainer.MAINTAINER_MODE", "tagged"), \
             patch("doctor.checks.maintainer.MAINTAINER_PLEX_SECTION_KEY", 0), \
             patch("doctor.checks.maintainer.PULSARR_DB_PATH", ""), \
             patch("doctor.checks.maintainer.INSTANCES", [arr]), \
             patch("doctor.clients.tautulli.Tautulli.recently_watched_shows",
                   return_value={"Watched Show"}) as _mock_taut, \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            with patch("builtins.open"):
                check_maintainer()

            arr._req.assert_not_called()
            log_calls = [c[0][0] for c in mock_log.info.call_args_list if c[0]]
            delete_calls = [m for m in log_calls if isinstance(m, str) and "WOULD delete" in m]
            self.assertEqual(len(delete_calls), 0)

    def test_enabled_false_returns_immediately(self):
        with patch("doctor.checks.maintainer.EN_MAINTAINER", False), \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            check_maintainer()
            mock_log.debug.assert_not_called()

    def test_arrow_show_scenario(self):
        """Simulate 'Arrow' — ended 2012 show, pulsarr-tagged, no Tautulli watches."""
        series = [self._make_series(
            sid=42, title="Arrow", status="ended", year=2012, tags=[1, 2],
        )]
        arr = self._make_sonarr_instance(name="sonarr-shows", series_list=series)
        arr.tag_map.return_value = {1: "pulsarr-alice", 2: "pulsarr-bob"}

        with patch("doctor.checks.maintainer.EN_MAINTAINER", True), \
             patch("doctor.checks.maintainer.TAUTULLI_URL", "http://taut:8181"), \
             patch("doctor.checks.maintainer.TAUTULLI_APIKEY", "k"), \
             patch("doctor.checks.maintainer.DRY_RUN", True), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MAX_ACTIONS", 5), \
             patch("doctor.checks.maintainer.MAINTAINER_LIBRARY_TITLE", "shows"), \
             patch("doctor.checks.maintainer.MAINTAINER_UNWATCHED_DAYS", 30), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0), \
             patch("doctor.checks.maintainer.MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"), \
             patch("doctor.checks.maintainer.MAINTAINER_RECHECK", 86400), \
             patch("doctor.checks.maintainer.MAINTAINER_MODE", "tagged"), \
             patch("doctor.checks.maintainer.MAINTAINER_PLEX_SECTION_KEY", 0), \
             patch("doctor.checks.maintainer.PULSARR_DB_PATH", ""), \
             patch("doctor.checks.maintainer.INSTANCES", [arr]), \
             patch("doctor.clients.tautulli.Tautulli.recently_watched_shows",
                   return_value=set()) as _mock_taut, \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            with patch("builtins.open"):
                check_maintainer()

            arr._req.assert_not_called()
            log_calls = [(c[0], c[1]) for c in mock_log.info.call_args_list]
            delete_calls = [(args, kwargs) for (args, kwargs) in log_calls
                            if args and "WOULD delete" in str(args[0])]
            self.assertEqual(len(delete_calls), 1)
            args = delete_calls[0][0]
            msg = " ".join(str(a) for a in args)
            self.assertIn("Arrow", msg)
            self.assertIn("ended", msg)
            self.assertIn("2012", msg)
            self.assertIn("alice", msg)
            self.assertIn("bob", msg)
            self.assertIn("30d", msg)

    def test_arrow_show_watched_skipped(self):
        """Arrow is watched — should NOT be deleted."""
        series = [self._make_series(
            sid=42, title="Arrow", status="ended", year=2012, tags=[1],
        )]
        arr = self._make_sonarr_instance(name="sonarr-shows", series_list=series)
        arr.tag_map.return_value = {1: "pulsarr-alice"}

        with patch("doctor.checks.maintainer.EN_MAINTAINER", True), \
             patch("doctor.checks.maintainer.TAUTULLI_URL", "http://taut:8181"), \
             patch("doctor.checks.maintainer.TAUTULLI_APIKEY", "k"), \
             patch("doctor.checks.maintainer.DRY_RUN", True), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_YEAR", 2024), \
             patch("doctor.checks.maintainer.MAINTAINER_MAX_ACTIONS", 5), \
             patch("doctor.checks.maintainer.MAINTAINER_LIBRARY_TITLE", "shows"), \
             patch("doctor.checks.maintainer.MAINTAINER_UNWATCHED_DAYS", 30), \
             patch("doctor.checks.maintainer.MAINTAINER_MIN_AGE_DAYS", 0), \
             patch("doctor.checks.maintainer.MAINTAINER_PULSARR_TAG_PREFIX", "pulsarr-"), \
             patch("doctor.checks.maintainer.MAINTAINER_RECHECK", 86400), \
             patch("doctor.checks.maintainer.MAINTAINER_MODE", "tagged"), \
             patch("doctor.checks.maintainer.MAINTAINER_PLEX_SECTION_KEY", 0), \
             patch("doctor.checks.maintainer.PULSARR_DB_PATH", ""), \
             patch("doctor.checks.maintainer.INSTANCES", [arr]), \
             patch("doctor.clients.tautulli.Tautulli.recently_watched_shows",
                   return_value={"Arrow"}) as _mock_taut, \
             patch("doctor.checks.maintainer.log") as mock_log:
            from doctor.checks.maintainer import check_maintainer
            with patch("builtins.open"):
                check_maintainer()

            arr._req.assert_not_called()
            log_calls = [c[0][0] for c in mock_log.info.call_args_list if c[0]]
            delete_msgs = [m for m in log_calls if isinstance(m, str) and "WOULD delete" in m]
            self.assertEqual(len(delete_msgs), 0)


if __name__ == "__main__":
    unittest.main()
