"""Unit tests for the missing_seasons check - candidate gathering logic."""
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from doctor.checks.missing_seasons import _gather_candidates


def _make_season(sn, monitored=True, file_count=0, total=10):
    return {
        "seasonNumber": sn,
        "monitored": monitored,
        "statistics": {
            "episodeFileCount": file_count,
            "totalEpisodeCount": total,
            "episodeCount": total,
        },
    }

def _make_series(sid, title, status="ended", monitored=True, seasons=None):
    return {
        "id": sid,
        "title": title,
        "status": status,
        "monitored": monitored,
        "added": "Mon, 01 Jan 2020 00:00:00 +0000",
        "seasons": seasons or [],
    }

def _make_arr(series_list, episodes_by_sid=None):
    arr = MagicMock()
    arr.name = "sonarr"
    arr.kind = "sonarr"
    arr.series.return_value = series_list
    episodes_by_sid = episodes_by_sid or {}
    arr.episodes.side_effect = lambda sid: episodes_by_sid.get(sid, [])
    arr.command.return_value = True
    return arr

def _run(series_list, episodes_by_sid=None, ms=None, recheck=0, partial=True):
    """Helper: run _gather_candidates with patched INSTANCES and MS_PARTIAL."""
    if ms is None:
        ms = {}
    arr = _make_arr(series_list, episodes_by_sid)
    now = time.time()
    with patch("doctor.checks.missing_seasons.INSTANCES", [arr]), \
         patch("doctor.checks.missing_seasons.MS_PARTIAL", partial):
        cands, skipped, airing = _gather_candidates(ms, now, 0, recheck, backfill=False)
    return cands, skipped, airing, now


class ZeroFileSeasonTest(unittest.TestCase):
    """Original behaviour: zero-file seasons are always candidates."""

    def test_zero_file_ended_season_is_candidate(self):
        s = _make_series(1, "Show A", status="ended", seasons=[_make_season(1, file_count=0)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 1)
        self.assertFalse(cands[0]["is_partial"])
        self.assertEqual(cands[0]["file_count"], 0)

    def test_zero_file_continuing_not_airing_is_candidate(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        eps = {1: [{"seasonNumber": 1, "airDateUtc": past}]}
        s = _make_series(1, "Show B", status="continuing", seasons=[_make_season(1, file_count=0)])
        cands, _, _, _ = _run([s], eps)
        self.assertEqual(len(cands), 1)

    def test_complete_season_not_a_candidate(self):
        s = _make_series(1, "Show C", seasons=[_make_season(1, file_count=10, total=10)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 0)

    def test_unmonitored_season_skipped(self):
        s = _make_series(1, "Show D", seasons=[_make_season(1, monitored=False, file_count=0)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 0)

    def test_unmonitored_series_skipped(self):
        s = _make_series(1, "Show E", monitored=False, seasons=[_make_season(1, file_count=0)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 0)

    def test_season_zero_skipped(self):
        s = _make_series(1, "Show F", seasons=[_make_season(0, file_count=0)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 0)

    def test_still_airing_skipped(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        eps = {1: [{"seasonNumber": 1, "airDateUtc": future}]}
        s = _make_series(1, "Show G", status="continuing", seasons=[_make_season(1, file_count=0)])
        cands, _, airing, _ = _run([s], eps)
        self.assertEqual(len(cands), 0)
        self.assertEqual(airing, 1)

    def test_cooldown_skips(self):
        ms = {}
        now = time.time()
        ms["sonarr:1:1"] = now - 100  # searched 100s ago, recheck=3600
        s = _make_series(1, "Show H", seasons=[_make_season(1, file_count=0)])
        cands, skipped, _, _ = _run([s], ms=ms, recheck=3600)
        self.assertEqual(len(cands), 0)
        self.assertEqual(skipped, 1)

    def test_cooldown_expired_is_candidate(self):
        recheck = 3600
        ms = {"sonarr:1:1": time.time() - recheck - 1}
        s = _make_series(1, "Show I", seasons=[_make_season(1, file_count=0)])
        cands, _, _, _ = _run([s], ms=ms, recheck=recheck)
        self.assertEqual(len(cands), 1)

    def test_total_episodes_zero_skipped(self):
        s = _make_series(1, "Show J", seasons=[_make_season(1, file_count=0, total=0)])
        cands, _, _, _ = _run([s])
        self.assertEqual(len(cands), 0)


class PartialSeasonTest(unittest.TestCase):
    """New behaviour: partial seasons (some files, not complete) on fully-aired seasons."""

    def test_partial_ended_season_is_candidate_when_enabled(self):
        s = _make_series(1, "Partial Show", status="ended",
                         seasons=[_make_season(1, file_count=5, total=10)])
        cands, _, _, _ = _run([s], partial=True)
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["is_partial"])
        self.assertEqual(cands[0]["file_count"], 5)
        self.assertEqual(cands[0]["total_episodes"], 10)

    def test_partial_ended_season_skipped_when_disabled(self):
        s = _make_series(1, "Partial Show", status="ended",
                         seasons=[_make_season(1, file_count=5, total=10)])
        cands, _, _, _ = _run([s], partial=False)
        self.assertEqual(len(cands), 0)

    def test_partial_continuing_not_airing_is_candidate(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        eps = {1: [{"seasonNumber": 2, "airDateUtc": past}]}
        s = _make_series(1, "Cont Show", status="continuing",
                         seasons=[_make_season(2, file_count=3, total=8)])
        cands, _, _, _ = _run([s], eps, partial=True)
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["is_partial"])

    def test_partial_continuing_still_airing_skipped(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        eps = {1: [{"seasonNumber": 2, "airDateUtc": future}]}
        s = _make_series(1, "Airing Show", status="continuing",
                         seasons=[_make_season(2, file_count=3, total=8)])
        cands, _, airing, _ = _run([s], eps, partial=True)
        self.assertEqual(len(cands), 0)
        self.assertEqual(airing, 1)

    def test_complete_season_never_a_candidate_even_with_partial_on(self):
        s = _make_series(1, "Complete Show", seasons=[_make_season(1, file_count=10, total=10)])
        cands, _, _, _ = _run([s], partial=True)
        self.assertEqual(len(cands), 0)

    def test_partial_respects_cooldown(self):
        recheck = 3600
        ms = {"sonarr:1:1": time.time() - 60}  # searched 60s ago
        s = _make_series(1, "Recent Show", seasons=[_make_season(1, file_count=3, total=10)])
        cands, skipped, _, _ = _run([s], ms=ms, recheck=recheck, partial=True)
        self.assertEqual(len(cands), 0)
        self.assertEqual(skipped, 1)

    def test_mixed_zero_and_partial_returned_together(self):
        seasons = [
            _make_season(1, file_count=0, total=10),   # zero -> candidate
            _make_season(2, file_count=5, total=10),   # partial -> candidate
            _make_season(3, file_count=10, total=10),  # complete -> skip
        ]
        s = _make_series(1, "Mixed Show", status="ended", seasons=seasons)
        cands, _, _, _ = _run([s], partial=True)
        self.assertEqual(len(cands), 2)
        by_sn = {c["sn"]: c for c in cands}
        self.assertFalse(by_sn[1]["is_partial"])
        self.assertTrue(by_sn[2]["is_partial"])

    def test_one_file_out_of_many_is_partial(self):
        s = _make_series(1, "Sparse Show", status="ended",
                         seasons=[_make_season(1, file_count=1, total=24)])
        cands, _, _, _ = _run([s], partial=True)
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["is_partial"])




if __name__ == "__main__":
    unittest.main()
