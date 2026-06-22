"""Unit tests for doctor.checks.repair.orphan.

Tests cover:
  _collect_known_paths():
    - returns empty set when INSTANCES is empty
    - collects Sonarr episode file paths
    - collects Radarr movie file paths
    - skips instances of other kinds
    - skips Sonarr series/episode_files that have no path field
    - handles arr.series() / arr.movies() exceptions gracefully

  _orphan_dead_symlink_scan():
    - returns immediately when REPAIR_LIBS is empty
    - logs a warning for library roots that are not directories
    - does NOT report a path that is tracked by *arr (known path)
    - does NOT report a path that is a live file (_dead_symlink=False)
    - reports a dead symlink that is not tracked by *arr (orphan)
    - caps the per-run log output at 20 individual paths

All filesystem access (os.walk, os.path.isdir, _dead_symlink) is patched.
"""
import unittest
from unittest.mock import MagicMock, patch

_MOD = "doctor.checks.repair.orphan"


def _make_arr(name="sonarr-1", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    return arr


class CollectKnownPathsTest(unittest.TestCase):

    def _run(self, instances):
        from doctor.checks.repair.orphan import _collect_known_paths
        with patch(_MOD + ".INSTANCES", instances):
            return _collect_known_paths()

    def test_empty_when_no_instances(self):
        result = self._run([])
        self.assertEqual(result, set())

    def test_collects_sonarr_episode_paths(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{"id": 1}]
        arr.episode_files.return_value = [{"path": "/lib/Show/ep.mkv"}]
        result = self._run([arr])
        self.assertIn("/lib/Show/ep.mkv", result)

    def test_collects_radarr_movie_paths(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [{"movieFile": {"path": "/lib/Movie/movie.mkv"}}]
        result = self._run([arr])
        self.assertIn("/lib/Movie/movie.mkv", result)

    def test_skips_other_instance_kinds(self):
        arr = _make_arr(kind="prowlarr")
        result = self._run([arr])
        self.assertEqual(result, set())

    def test_skips_sonarr_episode_without_path(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{"id": 1}]
        arr.episode_files.return_value = [{"path": None}, {"no_path_key": True}]
        result = self._run([arr])
        self.assertEqual(result, set())

    def test_skips_sonarr_series_without_id(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{"title": "No ID here"}]
        arr.episode_files.return_value = [{"path": "/lib/ep.mkv"}]
        result = self._run([arr])
        # episode_files should not be called without an id
        arr.episode_files.assert_not_called()
        self.assertEqual(result, set())

    def test_handles_series_exception(self):
        arr = _make_arr(kind="sonarr")
        arr.series.side_effect = RuntimeError("API gone")
        result = self._run([arr])
        self.assertEqual(result, set())

    def test_handles_movies_exception(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.side_effect = RuntimeError("timeout")
        result = self._run([arr])
        self.assertEqual(result, set())


class OrphanScanTest(unittest.TestCase):

    def _run(self, repair_libs, known_paths=None, walk_files=None, dead_symlinks=None):
        """
        repair_libs:   list of library root paths
        known_paths:   set of paths known to *arr
        walk_files:    dict {root: [filename, ...]} for os.walk simulation
        dead_symlinks: set of paths that _dead_symlink() returns True for
        """
        if known_paths is None:
            known_paths = set()
        if walk_files is None:
            walk_files = {}
        if dead_symlinks is None:
            dead_symlinks = set()

        def fake_isdir(p):
            return p in repair_libs

        def fake_walk(root):
            files = walk_files.get(root, [])
            if files:
                yield root, [], files
            # No recursion needed for unit tests

        def fake_dead_symlink(fp):
            return fp in dead_symlinks

        from doctor.checks.repair.orphan import _orphan_dead_symlink_scan

        with patch(_MOD + ".REPAIR_LIBS", repair_libs), \
             patch(_MOD + "._collect_known_paths", return_value=known_paths), \
             patch(_MOD + "._dead_symlink", side_effect=fake_dead_symlink), \
             patch("os.path.isdir", side_effect=fake_isdir), \
             patch("os.walk", side_effect=fake_walk):
            _orphan_dead_symlink_scan()

    def test_returns_immediately_when_no_repair_libs(self):
        # Should not raise, should not call os.walk
        with patch("os.walk") as mock_walk:
            self._run([])
            mock_walk.assert_not_called()

    def test_skips_root_that_is_not_a_directory(self):
        # /bad/path doesn't exist → log a warning, don't walk
        with patch("os.walk") as mock_walk, \
             patch(_MOD + ".REPAIR_LIBS", ["/bad/path"]), \
             patch(_MOD + "._collect_known_paths", return_value=set()), \
             patch("os.path.isdir", return_value=False):
            from doctor.checks.repair.orphan import _orphan_dead_symlink_scan
            _orphan_dead_symlink_scan()
            mock_walk.assert_not_called()

    def test_does_not_report_known_path(self):
        known = {"/lib/Show/ep.mkv"}
        walk = {"/lib": ["ep.mkv"]}
        dead = {"/lib/ep.mkv"}
        # The path is known → not an orphan even if dead
        with patch(_MOD + ".log") as mock_log:
            self._run(["/lib"], known_paths=known, walk_files=walk, dead_symlinks=dead)
            # Warning about found orphans should NOT be called
            calls_str = str(mock_log.warning.call_args_list)
            self.assertNotIn("orphan", calls_str.lower().replace("repair:orphan", ""))

    def test_does_not_report_live_file(self):
        walk = {"/lib": ["live.mkv"]}
        dead = set()  # live file
        with patch(_MOD + ".log") as mock_log:
            self._run(["/lib"], walk_files=walk, dead_symlinks=dead)
            calls_str = str(mock_log.warning.call_args_list)
            self.assertNotIn("found 1 dead", calls_str)

    def test_reports_orphan_dead_symlink(self):
        walk = {"/lib": ["orphan.mkv"]}
        dead = {"/lib/orphan.mkv"}
        with patch(_MOD + ".log") as mock_log:
            self._run(["/lib"], walk_files=walk, dead_symlinks=dead)
            # Check that warning was called with count=1
            counts = [a[0][1] for a in mock_log.warning.call_args_list
                      if "dead symlink(s) not tracked" in (a[0][0] or "")]
            self.assertTrue(any(c == 1 for c in counts))

    def test_caps_individual_path_log_at_20(self):
        """More than 20 orphans → logs first 20 + summary line."""
        files = [f"f{i}.mkv" for i in range(25)]
        walk = {"/lib": files}
        dead = {"/lib/" + f for f in files}
        with patch(_MOD + ".log") as mock_log:
            self._run(["/lib"], walk_files=walk, dead_symlinks=dead)
            # Check summary count=25
            counts = [a[0][1] for a in mock_log.warning.call_args_list
                      if "dead symlink(s) not tracked" in (a[0][0] or "")]
            self.assertTrue(any(c == 25 for c in counts))
            # Check overflow "and N more"
            overflow = [a[0][1] for a in mock_log.warning.call_args_list
                        if "and %d more" in (a[0][0] or "")]
            self.assertTrue(any(c == 5 for c in overflow))
