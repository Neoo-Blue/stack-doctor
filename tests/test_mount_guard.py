"""Regression tests for the mount-health gate and per-check safety caps.

Pattern follows tests/test_altmount.py: patch module attributes on `doctor`
and use tempfile for any on-disk state. These tests exercise the exact
incident-class behaviour: a transiently-down mount must never cause a mass
delete/quarantine, and a per-check action cap must bound the blast radius.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import doctor


def _make_janitor_lib(n):
    """Create a temp library dir with `n` symlinks whose targets reference a
    dead release via the `/__all__/<release>/...` shape the janitor matches."""
    lib = tempfile.mkdtemp()
    for i in range(n):
        os.symlink("/__all__/myrelease/file%d.mkv" % i, os.path.join(lib, "link%d" % i))
    return lib


def _write_janitor_log():
    """A decypharr-style log line that resolves to `bad == {"myrelease"}`."""
    f = tempfile.NamedTemporaryFile("w", suffix=".log", delete=False)
    f.write('Error streaming file: myrelease/file.mkv error="ARTICLE_NOT_FOUND"\n')
    f.close()
    return f.name


class JanitorMountGateTest(unittest.TestCase):
    def _run_janitor(self, probe_result, dry_run=False, max_moves=50):
        lib = _make_janitor_lib(1)
        logf = _write_janitor_log()
        quar = tempfile.mkdtemp()
        try:
            with patch.object(doctor, "JAN_LIBS", [lib]), \
                 patch.object(doctor, "JAN_LOG", logf), \
                 patch.object(doctor, "JAN_LOG_CMD", ""), \
                 patch.object(doctor, "JAN_QUAR", quar), \
                 patch.object(doctor, "DRY_RUN", dry_run), \
                 patch.object(doctor, "JAN_MAX_MOVES", max_moves), \
                 patch.object(doctor, "MOUNT_GUARDS", {"/__all__": "/__all__"}), \
                 patch.object(doctor, "_realpath_with_timeout", lambda p, t=None: p), \
                 patch.object(doctor, "_probe_mount", return_value=probe_result):
                doctor.check_janitor()
            return lib, quar
        finally:
            os.unlink(logf)

    def test_skips_when_mount_down(self):
        lib, quar = self._run_janitor(probe_result=False)
        try:
            self.assertTrue(os.path.lexists(os.path.join(lib, "link0")),
                            "symlink must survive a down mount")
            self.assertEqual(os.listdir(quar), [], "no manifest/quarantine when mount down")
        finally:
            doctor._safe_rmtree(lib); doctor._safe_rmtree(quar)

    def test_acts_when_mount_up(self):
        lib, quar = self._run_janitor(probe_result=True, dry_run=False)
        try:
            self.assertFalse(os.path.lexists(os.path.join(lib, "link0")),
                             "symlink must be quarantined on a healthy mount")
            self.assertNotEqual(os.listdir(quar), [], "a quarantine dir + manifest must exist")
        finally:
            doctor._safe_rmtree(lib); doctor._safe_rmtree(quar)


class ScrubberMountGateTest(unittest.TestCase):
    def test_action_skipped_when_mount_down(self):
        manifest = []
        with tempfile.TemporaryDirectory() as qroot, \
             patch.object(doctor, "_mount_ok_for", return_value=False):
            ret = doctor._scrub_act_on_bad("/mnt/zurg/real.mkv", "/mnt/lib/link.mkv",
                                           "tier1 BAD", qroot, manifest)
        self.assertFalse(ret)
        self.assertEqual(len(manifest), 1)
        self.assertTrue(manifest[0].get("skipped_mount_down"))
        self.assertFalse(manifest[0].get("moved"))


class JanitorCapTest(unittest.TestCase):
    def test_per_check_cap_stops_at_limit(self):
        lib = _make_janitor_lib(3)
        logf = _write_janitor_log()
        quar = tempfile.mkdtemp()
        try:
            with patch.object(doctor, "JAN_LIBS", [lib]), \
                 patch.object(doctor, "JAN_LOG", logf), \
                 patch.object(doctor, "JAN_LOG_CMD", ""), \
                 patch.object(doctor, "JAN_QUAR", quar), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "JAN_MAX_MOVES", 1), \
                 patch.object(doctor, "MOUNT_GUARDS", {"/__all__": "/__all__"}), \
                 patch.object(doctor, "_realpath_with_timeout", lambda p, t=None: p), \
                 patch.object(doctor, "_probe_mount", return_value=True):
                doctor.check_janitor()
            remaining = sum(1 for n in os.listdir(lib) if os.path.lexists(os.path.join(lib, n)))
            self.assertEqual(remaining, 2, "exactly one symlink may be quarantined at cap=1")
        finally:
            os.unlink(logf)
            doctor._safe_rmtree(lib); doctor._safe_rmtree(quar)


class MissingFromDiskTest(unittest.TestCase):
    def _run(self, mount_ok, dry_run=False):
        arr = MagicMock()
        arr.name = "radarr"; arr.kind = "radarr"
        arr.get_json.return_value = [{"id": 1, "hasFile": True,
                                      "movieFile": {"id": 10, "path": "/mnt/zurg/movie.mkv"},
                                      "title": "Test Movie"}]
        arr.command.return_value = {}
        with patch.object(doctor, "INSTANCES", [arr]), \
             patch.object(doctor, "DRY_RUN", dry_run), \
             patch.object(doctor, "host_load", return_value=0.0), \
             patch.object(doctor, "_mount_ok_for", return_value=mount_ok), \
             patch.object(doctor, "_realpath_with_timeout", lambda p, t=None: p), \
             patch.object(doctor.os.path, "exists", return_value=False), \
             patch.object(doctor, "_missing_disk_load_state", return_value={}), \
             patch.object(doctor, "_missing_disk_save_state", return_value=None):
            doctor.check_missing_from_disk()
        return arr

    def test_skips_when_mount_down(self):
        arr = self._run(mount_ok=False)
        arr.command.assert_not_called()

    def test_acts_when_mount_up(self):
        arr = self._run(mount_ok=True, dry_run=False)
        arr.command.assert_called_once()


class ConfigDivergenceTest(unittest.TestCase):
    def test_config_divergence_warns(self):
        """A key already set in the environment is NOT overwritten by config.json."""
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        f.write('{"DOCTOR_INTERVAL": "111"}')
        cfg = f.name; f.close()
        try:
            with patch.object(doctor, "CONFIG_FILE", cfg), \
                 patch.dict("os.environ", {"DOCTOR_INTERVAL": "900"}, clear=False), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                doctor._load_overrides()
                out = err.getvalue()
                after = os.environ.get("DOCTOR_INTERVAL")
            self.assertIn("ignored", out)
            self.assertIn("DOCTOR_INTERVAL", out)
            self.assertEqual(after, "900", "environment value must win over config.json")
        finally:
            os.unlink(cfg)

    def test_config_applies_when_env_unset(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        f.write('{"DOCTOR_CHURN_LIMIT": "7"}')
        cfg = f.name; f.close()
        try:
            with patch.object(doctor, "CONFIG_FILE", cfg), \
                 patch("sys.stderr", new_callable=io.StringIO):
                os.environ.pop("DOCTOR_CHURN_LIMIT", None)
                doctor._load_overrides()
                after = os.environ.get("DOCTOR_CHURN_LIMIT")
            self.assertEqual(after, "7", "config.json must apply keys not set in the env")
        finally:
            os.unlink(cfg)
            os.environ.pop("DOCTOR_CHURN_LIMIT", None)

    def test_empty_string_does_not_override(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        f.write('{"DOCTOR_DRY_RUN": "", "DOCTOR_CONDITIONS": ""}')
        cfg = f.name; f.close()
        try:
            with patch.object(doctor, "CONFIG_FILE", cfg), \
                 patch.dict("os.environ", {"DOCTOR_DRY_RUN": "true"}, clear=False), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                doctor._load_overrides()
                out = err.getvalue()
                after = os.environ.get("DOCTOR_DRY_RUN")
            self.assertNotIn("override the environment", out)
            self.assertEqual(after, "true",
                             "empty-string config value must not clobber the env")
        finally:
            os.unlink(cfg)

    def test_empty_string_does_not_override(self):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        f.write('{"DOCTOR_DRY_RUN": "", "DOCTOR_CONDITIONS": ""}')
        cfg = f.name; f.close()
        try:
            with patch.object(doctor, "CONFIG_FILE", cfg), \
                 patch.dict("os.environ", {"DOCTOR_DRY_RUN": "true"}, clear=False), \
                 patch("sys.stderr", new_callable=io.StringIO) as err:
                doctor._load_overrides()
                out = err.getvalue()
                after = os.environ.get("DOCTOR_DRY_RUN")
            self.assertNotIn("override the environment", out)
            self.assertEqual(after, "true",
                             "empty-string config value must not clobber the env")
        finally:
            os.unlink(cfg)


class ScrubberCapTest(unittest.TestCase):
    def test_per_check_cap_stops_at_limit(self):
        st = MagicMock()
        st.st_mtime = 0.0
        st.st_size = 12345
        files = [("/mnt/zurg/f%d.mkv" % i, "/mnt/lib/link%d.mkv" % i) for i in range(3)]
        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "SCRUB_LOAD_MAX", 0), \
             patch.object(doctor, "SCRUB_STRIKES", 1), \
             patch.object(doctor, "SCRUB_MIN_AGE", 0), \
             patch.object(doctor, "SCRUB_MAX_DELETES", 1), \
             patch.object(doctor, "SCRUB_MAX_FILES", 10), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "_scrub_walk", return_value=iter(files)), \
             patch.object(doctor.os, "stat", return_value=st), \
             patch.object(doctor, "_scrub_load_state", return_value={}), \
             patch.object(doctor, "_scrub_save_state", return_value=None), \
             patch.object(doctor, "_scrub_t1_header", return_value=(False, "torn container")), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_scrub_act_on_bad", return_value=True) as act:
            doctor.check_scrubber()
            self.assertEqual(act.call_count, 1, "only one delete allowed at SCRUBBER_MAX_DELETES=1")


class MetacleanCapTest(unittest.TestCase):
    def test_per_check_cap_stops_at_limit(self):
        root = tempfile.mkdtemp()
        cat = os.path.join(root, "radarr")
        os.makedirs(cat)
        names = ["release.one.2024", "release.two.2024", "release.three.2024"]
        for n in names:
            os.makedirs(os.path.join(cat, n))
        links = tempfile.mkdtemp()
        os.symlink("/mnt/library/some-live-show", os.path.join(links, "live"))
        failed = "\n".join(names)
        try:
            with patch.object(doctor, "META_ROOT", root), \
                 patch.object(doctor, "META_LINK_DIRS", [links]), \
                 patch.object(doctor, "META_CATS", ["radarr"]), \
                 patch.object(doctor, "META_FAILED_CMD", "echo failed"), \
                 patch.object(doctor, "META_STORM_CMD", ""), \
                 patch.object(doctor, "META_MIN_AGE", 0), \
                 patch.object(doctor, "META_MAX_REMOVES", 1), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "run_output", return_value=failed):
                doctor.check_metaclean()
            remaining = sum(1 for n in names if os.path.isdir(os.path.join(cat, n)))
            self.assertEqual(remaining, 2, "only one metadata dir removed at METACLEAN_MAX_REMOVES=1")
        finally:
            doctor._safe_rmtree(root); doctor._safe_rmtree(links)


class MissingFromDiskSonarrTest(unittest.TestCase):
    def test_sonarr_uses_episode_ids(self):
        arr = MagicMock()
        arr.name = "sonarr"; arr.kind = "sonarr"
        def _get_json(path, t=None):
            if path == "/episodefile":
                return [{"id": 55, "seriesId": 9, "seasonNumber": 2,
                         "path": "/mnt/zurg/x.mkv", "relativePath": "S02E03.mkv"}]
            if path.startswith("/episode?seriesId="):
                return [{"id": 501, "episodeFileId": 55}, {"id": 502, "episodeFileId": 77}]
            return None
        arr.get_json.side_effect = _get_json
        arr.command.return_value = {}
        with patch.object(doctor, "INSTANCES", [arr]), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "host_load", return_value=0.0), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", lambda p, t=None: p), \
             patch.object(doctor.os.path, "exists", return_value=False), \
             patch.object(doctor, "_missing_disk_load_state", return_value={}), \
             patch.object(doctor, "_missing_disk_save_state", return_value=None):
            doctor.check_missing_from_disk()
        calls = [c.args[0] for c in arr.command.call_args_list]
        self.assertTrue(calls, "expected a search command")
        body = calls[0]
        self.assertEqual(body.get("name"), "EpisodeSearch")
        self.assertEqual(body.get("episodeIds"), [501])
        self.assertNotIn("episodeNumbers", body)
        arr._req.assert_any_call("DELETE", "/episodefile/55")

    def test_two_episodes_same_series_both_acted(self):
        arr = MagicMock()
        arr.name = "sonarr"; arr.kind = "sonarr"
        def _get_json(path, t=None):
            if path == "/episodefile":
                return [
                    {"id": 55, "seriesId": 9, "seasonNumber": 2, "path": "/mnt/zurg/a.mkv", "relativePath": "a"},
                    {"id": 56, "seriesId": 9, "seasonNumber": 2, "path": "/mnt/zurg/b.mkv", "relativePath": "b"},
                ]
            if path.startswith("/episode?seriesId="):
                return [{"id": 501, "episodeFileId": 55}, {"id": 502, "episodeFileId": 56}]
            return None
        arr.get_json.side_effect = _get_json
        arr.command.return_value = {}
        with patch.object(doctor, "INSTANCES", [arr]), \
             patch.object(doctor, "DRY_RUN", False), \
             patch.object(doctor, "host_load", return_value=0.0), \
             patch.object(doctor, "_mount_ok_for", return_value=True), \
             patch.object(doctor, "_realpath_with_timeout", lambda p, t=None: p), \
             patch.object(doctor.os.path, "exists", return_value=False), \
             patch.object(doctor, "_missing_disk_load_state", return_value={}), \
             patch.object(doctor, "_missing_disk_save_state", return_value=None):
            doctor.check_missing_from_disk()
        self.assertEqual(arr.command.call_count, 2, "both episodes of the series must be actioned")


class ScrubberMountGateOrderTest(unittest.TestCase):
    def test_scrubber_skips_stat_on_down_mount(self):
        files = [("/mnt/zurg/f0.mkv", "/mnt/lib/link0.mkv")]
        with tempfile.TemporaryDirectory() as quar, \
             patch.object(doctor, "SCRUB_PATHS", ["/mnt/lib"]), \
             patch.object(doctor, "SCRUB_QUAR", quar), \
             patch.object(doctor, "SCRUB_LOAD_MAX", 0), \
             patch.object(doctor, "SCRUB_MIN_AGE", 0), \
             patch.object(doctor, "_scrub_walk", return_value=iter(files)), \
             patch.object(doctor, "_scrub_load_state", return_value={}), \
             patch.object(doctor, "_scrub_save_state", return_value=None), \
             patch.object(doctor, "_mount_ok_for", return_value=False), \
             patch.object(doctor, "_stat_with_timeout") as mock_stat:
            doctor.check_scrubber()
        mock_stat.assert_not_called()


class MetacleanStormOnlyTest(unittest.TestCase):
    def test_removes_storm_only_orphan(self):
        root = tempfile.mkdtemp()
        cat = os.path.join(root, "radarr"); os.makedirs(cat)
        os.makedirs(os.path.join(cat, "storming.release.2024"))
        links = tempfile.mkdtemp()
        os.symlink("/mnt/library/other", os.path.join(links, "live"))
        try:
            with patch.object(doctor, "META_ROOT", root), \
                 patch.object(doctor, "META_LINK_DIRS", [links]), \
                 patch.object(doctor, "META_CATS", ["radarr"]), \
                 patch.object(doctor, "META_FAILED_CMD", ""), \
                 patch.object(doctor, "META_STORM_CMD", "echo storm"), \
                 patch.object(doctor, "META_MIN_AGE", 999999), \
                 patch.object(doctor, "META_MAX_REMOVES", 50), \
                 patch.object(doctor, "DRY_RUN", False), \
                 patch.object(doctor, "_meta_extract_keys", return_value=set()), \
                 patch.object(doctor, "_meta_storm_keys", return_value={"storming.release.2024"}), \
                 patch.object(doctor, "run_output", return_value="storm"):
                doctor.check_metaclean()
            self.assertFalse(os.path.isdir(os.path.join(cat, "storming.release.2024")),
                             "storm-only orphan must be removed even with no failed-list match")
        finally:
            doctor._safe_rmtree(root); doctor._safe_rmtree(links)


if __name__ == "__main__":
    unittest.main()
