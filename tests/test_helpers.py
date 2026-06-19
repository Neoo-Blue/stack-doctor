"""Unit tests for pure helpers that have no external dependencies."""
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from doctor.config import _dur, _human
from doctor.checks.queue import stuck_reason, _msgs
from doctor.checks.missing_seasons import _season_still_airing
from doctor.checks.repair import _dead_symlink


class DurationParsingTest(unittest.TestCase):
    def test_dur_seconds(self):
        self.assertEqual(_dur("30s"), 30)

    def test_dur_minutes(self):
        self.assertEqual(_dur("5m"), 300)

    def test_dur_hours(self):
        self.assertEqual(_dur("2h"), 7200)

    def test_dur_days(self):
        self.assertEqual(_dur("1d"), 86400)

    def test_dur_bare_number(self):
        self.assertEqual(_dur("900"), 900)

    def test_dur_empty_uses_default(self):
        self.assertEqual(_dur(""), 0)
        self.assertEqual(_dur("garbage", 42), 42)


class HumanReadableTest(unittest.TestCase):
    def test_human_seconds(self):
        self.assertEqual(_human(45), "45s")

    def test_human_minutes(self):
        self.assertEqual(_human(180), "3m")

    def test_human_hours(self):
        self.assertEqual(_human(7200), "2h")

    def test_human_days(self):
        self.assertEqual(_human(86400), "1d")


class QueuePredicateTest(unittest.TestCase):
    def test_stuck_reason_download_client_unavailable(self):
        self.assertEqual(stuck_reason({"status": "downloadClientUnavailable"}),
                         "downloadClientUnavailable")

    def test_stuck_reason_import_blocked(self):
        rec = {"trackedDownloadState": "importBlocked"}
        self.assertEqual(stuck_reason(rec), "importBlocked")

    def test_stuck_reason_import_failed(self):
        rec = {"trackedDownloadState": "importFailed"}
        self.assertEqual(stuck_reason(rec), "importFailed")

    def test_stuck_reason_import_pending_warning(self):
        rec = {"trackedDownloadState": "importPending",
               "trackedDownloadStatus": "warning"}
        self.assertEqual(stuck_reason(rec), "importPending_warning")

    def test_stuck_reason_stalled(self):
        rec = {"trackedDownloadStatus": "warning",
               "statusMessages": [{"messages": ["download is stalled"]}]}
        self.assertEqual(stuck_reason(rec), "stalled")

    def test_stuck_reason_no_match(self):
        self.assertIsNone(stuck_reason({"status": "ok"}))

    def test_msgs_extracts_messages(self):
        rec = {
            "statusMessages": [{"messages": ["m1", "m2"]}, {"messages": ["m3"]}],
            "errorMessage": "top"
        }
        self.assertEqual(_msgs(rec), ["m1", "m2", "m3", "top"])


class SeasonAiringTest(unittest.TestCase):
    def test_still_airing_future_episode(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        eps = [{"seasonNumber": 1, "airDateUtc": future}]
        self.assertTrue(_season_still_airing(eps, 1))

    def test_not_airing_all_past(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        eps = [{"seasonNumber": 2, "airDateUtc": past}]
        self.assertFalse(_season_still_airing(eps, 2))

    def test_wrong_season_ignored(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        eps = [{"seasonNumber": 1, "airDateUtc": future}]
        self.assertFalse(_season_still_airing(eps, 2))


class DeadSymlinkTest(unittest.TestCase):
    def test_dead_symlink_detected(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "missing")
            link = os.path.join(d, "link")
            os.symlink(target, link)
            self.assertTrue(_dead_symlink(link))

    def test_live_symlink_not_dead(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "real")
            open(target, "w").close()
            link = os.path.join(d, "link")
            os.symlink(target, link)
            self.assertFalse(_dead_symlink(link))

    def test_regular_file_not_dead(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "file")
            open(f, "w").close()
            self.assertFalse(_dead_symlink(f))


if __name__ == "__main__":
    unittest.main()
