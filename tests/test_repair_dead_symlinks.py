"""Characterization tests for doctor.checks.repair.dead_symlinks.

Lock in the current behavior of:
  - _radarr_dead_files(): filtering / yielding dead movie files
  - _sonarr_dead_files(): filtering / yielding dead episode files per season
  - _repair_radarr_movie(): delete + toggle + search orchestration
  - _repair_sonarr_season(): delete + toggle + SeasonSearch orchestration

All filesystem access (_dead_symlink) is patched so tests run without real
symlinks.  Arr instances are MagicMock.  Config globals are patched on the
dead_symlinks module (they arrive via star-import).
"""
import unittest
from unittest.mock import MagicMock, patch

from doctor.checks.repair.dead_symlinks import (
    _radarr_dead_files,
    _sonarr_dead_files,
    _repair_radarr_movie,
    _repair_sonarr_season,
)

# Module path prefix for patching star-imported names
_MOD = "doctor.checks.repair.dead_symlinks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _movie(mid, title, path, monitored=True, mfid=None):
    """Minimal Radarr movie dict."""
    mf = {"path": path}
    if mfid is not None:
        mf["id"] = mfid
    return {"id": mid, "title": title, "monitored": monitored, "movieFile": mf}


def _series(sid, title, monitored=True):
    return {"id": sid, "title": title, "monitored": monitored}


def _efile(efid, path, season_number=None):
    """Minimal Sonarr episode file dict."""
    ef = {"id": efid, "path": path}
    if season_number is not None:
        ef["seasonNumber"] = season_number
    return ef


def _episode(epid, efid, season_number):
    """Minimal Sonarr episode dict."""
    return {"id": epid, "episodeFileId": efid, "seasonNumber": season_number}


def _make_arr(name="sonarr-1", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    return arr


# ---------------------------------------------------------------------------
# _radarr_dead_files
# ---------------------------------------------------------------------------

class RadarrDeadFilesTest(unittest.TestCase):

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_yields_dead_movie(self, _ds):
        movies = [_movie(1, "Dead Movie", "/lib/dead.mkv", mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(result, [(1, "Dead Movie", 10)])

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_skips_live_symlink(self, _ds):
        movies = [_movie(1, "Live Movie", "/lib/live.mkv", mfid=10)]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", False)
    def test_skips_unmonitored_when_flag_off(self, _ds):
        movies = [_movie(1, "Unmon Movie", "/lib/x.mkv", monitored=False, mfid=10)]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", True)
    def test_includes_unmonitored_when_flag_on(self, _ds):
        movies = [_movie(1, "Unmon Movie", "/lib/x.mkv", monitored=False, mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", ["/allowed/"])
    def test_repair_libs_filters_path(self, _ds):
        movies = [
            _movie(1, "Allowed", "/allowed/a.mkv", mfid=10),
            _movie(2, "Blocked", "/other/b.mkv", mfid=20),
        ]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", [])
    def test_empty_repair_libs_allows_all(self, _ds):
        """Empty REPAIR_LIBS means no path filter -> all paths pass."""
        movies = [_movie(1, "Any", "/any/path.mkv", mfid=10)]
        self.assertEqual(len(list(_radarr_dead_files(movies))), 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_without_id(self, _ds):
        movies = [{"title": "No ID", "movieFile": {"path": "/x.mkv"}}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_without_file_path(self, _ds):
        movies = [{"id": 1, "title": "No Path", "movieFile": {}}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_with_no_movie_file(self, _ds):
        movies = [{"id": 1, "title": "No File"}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_title_truncated_to_70_chars(self, _ds):
        long_title = "A" * 100
        movies = [_movie(1, long_title, "/lib/x.mkv", mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result[0][1]), 70)


# ---------------------------------------------------------------------------
# _sonarr_dead_files
# ---------------------------------------------------------------------------

class SonarrDeadFilesTest(unittest.TestCase):

    def _setup_arr(self, efiles, eps):
        arr = _make_arr()
        arr.episode_files.return_value = efiles
        arr.episodes.return_value = eps
        return arr

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_yields_dead_episode_files_grouped_by_season(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1),
                  _efile(101, "/lib/S01E02.mkv", season_number=1)]
        eps = [_episode(10, 100, 1), _episode(11, 101, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 1)
        sid, title, sn, efids = result[0]
        self.assertEqual(sid, 5)
        self.assertEqual(sn, 1)
        self.assertCountEqual(efids, [100, 101])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_multiple_seasons_yield_separately(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1),
                  _efile(200, "/lib/S02E01.mkv", season_number=2)]
        eps = [_episode(10, 100, 1), _episode(20, 200, 2)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 2)
        seasons = {r[2] for r in result}
        self.assertEqual(seasons, {1, 2})

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_skips_live_files(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", False)
    def test_skips_unmonitored_series(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "UnmonShow", monitored=False)]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", True)
    def test_includes_unmonitored_series_when_flag_on(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "UnmonShow", monitored=False)]
        self.assertEqual(len(list(_sonarr_dead_files(arr, series))), 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", ["/allowed/"])
    def test_repair_libs_filters_episode_paths(self, _ds):
        efiles = [_efile(100, "/allowed/S01E01.mkv", season_number=1),
                  _efile(101, "/other/S01E02.mkv", season_number=1)]
        eps = [_episode(10, 100, 1), _episode(11, 101, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        # Only the /allowed/ file should be included
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], [100])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_season_from_episode_cross_reference(self, _ds):
        """Episode file without seasonNumber falls back to episode cross-reference."""
        efiles = [_efile(100, "/lib/S01E01.mkv")]  # no seasonNumber on the file
        eps = [_episode(10, 100, 1)]  # episode knows season 1
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], 1)  # season number from cross-reference

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_file_without_id(self, _ds):
        efiles = [{"path": "/lib/S01E01.mkv", "seasonNumber": 1}]  # no "id"
        eps = []
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_file_without_season(self, _ds):
        """File with no seasonNumber and no cross-reference is skipped."""
        efiles = [_efile(100, "/lib/orphan.mkv")]  # no seasonNumber
        eps = []  # no episode cross-reference either
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_continues_on_episode_files_exception(self, _ds):
        """If arr.episode_files() raises, the series is skipped silently."""
        arr = _make_arr()
        arr.episode_files.side_effect = Exception("API error")
        arr.episodes.return_value = []
        series = [_series(5, "Show"), _series(6, "Show2")]

        # The generator should not raise; it just skips the broken series
        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(result, [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_series_without_id(self, _ds):
        series = [{"title": "No ID", "monitored": True}]
        arr = _make_arr()
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])


# ---------------------------------------------------------------------------
# _repair_radarr_movie
# ---------------------------------------------------------------------------

class RepairRadarrMovieTest(unittest.TestCase):

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_deletes_file_toggles_monitor_and_searches(self):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.delete_file.assert_called_once_with(10)
        arr.set_monitored.assert_any_call([1], False)
        arr.set_monitored.assert_any_call([1], True)
        arr.command.assert_called_once_with("MoviesSearch", movieIds=[1])

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_skips_delete_when_no_mfid(self):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=None)
        arr.delete_file.assert_not_called()
        # toggle and search still happen
        arr.command.assert_called_once()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", True)
    def test_dry_run_does_not_call_arr(self):
        arr = _make_arr("radarr-1", "radarr")

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.delete_file.assert_not_called()
        arr.set_monitored.assert_not_called()
        arr.command.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_monitor_toggle_failure_does_not_abort(self):
        """If set_monitored raises, repair still issues the search command."""
        arr = _make_arr("radarr-1", "radarr")
        arr.set_monitored.side_effect = Exception("API down")
        arr.command.return_value = 42

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.command.assert_called_once_with("MoviesSearch", movieIds=[1])

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_records_when_enabled(self, mock_verify):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42
        state = {}

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10, state=state)
        mock_verify.assert_called_once_with(state, arr, "Movie", 42, 1, [1])

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_skipped_when_state_is_none(self, mock_verify):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10, state=None)
        mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# _repair_sonarr_season
# ---------------------------------------------------------------------------

class RepairSonarrSeasonTest(unittest.TestCase):

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_deletes_all_efids_and_searches_season(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [
            {"id": 10, "seasonNumber": 1},
            {"id": 11, "seasonNumber": 1},
            {"id": 20, "seasonNumber": 2},
        ]
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100, 101])
        self.assertTrue(result)
        # Both episode files deleted
        self.assertEqual(arr.delete_file.call_count, 2)
        arr.delete_file.assert_any_call(100)
        arr.delete_file.assert_any_call(101)
        # Monitor toggle on episodes for season 1 only
        arr.set_monitored.assert_any_call([10, 11], False)
        arr.set_monitored.assert_any_call([10, 11], True)
        # Season search
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", True)
    def test_dry_run_does_not_call_arr(self):
        arr = _make_arr("sonarr-1", "sonarr")

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100, 101])
        self.assertTrue(result)
        arr.delete_file.assert_not_called()
        arr.set_monitored.assert_not_called()
        arr.command.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_monitor_toggle_failure_does_not_abort(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.set_monitored.side_effect = Exception("API down")
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100])
        self.assertTrue(result)
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_records_when_enabled(self, mock_verify):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        state = {}

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[100], state=state)
        mock_verify.assert_called_once_with(state, arr, "Show", 99, 5, [10])

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_skipped_when_state_is_none(self, mock_verify):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = []
        arr.command.return_value = 99

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[100], state=None)
        mock_verify.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_empty_epids_still_searches(self):
        """If no episodes match the season (edge case), toggle is skipped but search runs."""
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = []  # no episodes for this season
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100])
        self.assertTrue(result)
        arr.set_monitored.assert_not_called()
        arr.command.assert_called_once()


if __name__ == "__main__":
    unittest.main()
