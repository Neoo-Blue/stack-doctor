"""Tests for the altmount check and its safe-rmtree helper."""
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

import doctor


class SafeRmtreeTest(unittest.TestCase):
    def test_refuses_root(self):
        ok, why = doctor._safe_rmtree("/")
        self.assertFalse(ok)
        self.assertIn("root", why.lower())

    def test_refuses_relative_path(self):
        ok, why = doctor._safe_rmtree("tmp/altmount")
        self.assertFalse(ok)
        self.assertIn("absolute", why.lower())

    def test_refuses_parent_traversal(self):
        ok, why = doctor._safe_rmtree("/tmp/altmount/../../etc")
        self.assertFalse(ok)
        self.assertIn("parent", why.lower())

    def test_refuses_non_directory(self):
        with tempfile.NamedTemporaryFile() as f:
            ok, why = doctor._safe_rmtree(f.name)
        self.assertFalse(ok)
        self.assertIn("directory", why.lower())

    def test_removes_safe_temp_directory(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "altmount-uploads")
            os.makedirs(sub)
            ok, why = doctor._safe_rmtree(sub)
            self.assertTrue(ok)
            self.assertFalse(os.path.exists(sub))


class AltMountStagingCleanupTest(unittest.TestCase):
    def _run_check(self, dir_uid, dry_run=False):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(doctor, "ALT_URL", ""), \
             patch.object(doctor, "ALT_FIX_TMP", True), \
             patch.object(doctor, "ALT_TMP_DIRS", [os.path.join(td, "altmount-uploads")]), \
             patch.object(doctor, "ALT_TMP_UID", 1000), \
             patch.object(doctor, "ALT_MOUNT_TEST", ""), \
             patch.object(doctor, "ALT_PROP_CHECKS", []), \
             patch.object(doctor, "ALT_PROP_FIX_CMD", ""), \
             patch.object(doctor, "ALT_RESTART_CMD", ""), \
             patch.object(doctor, "DRY_RUN", dry_run), \
             patch("doctor.os.stat", return_value=MagicMock(st_uid=dir_uid)), \
             patch.object(doctor, "_safe_rmtree", return_value=(True, "")) as mock_rm:
            os.makedirs(os.path.join(td, "altmount-uploads"), exist_ok=True)
            doctor.check_altmount()
            return mock_rm.call_count

    def test_correct_owner_skips_removal(self):
        self.assertEqual(self._run_check(dir_uid=1000), 0)

    def test_wrong_owner_triggers_removal(self):
        self.assertEqual(self._run_check(dir_uid=0), 1)

    def test_dry_run_skips_removal(self):
        self.assertEqual(self._run_check(dir_uid=0, dry_run=True), 0)


if __name__ == "__main__":
    unittest.main()
