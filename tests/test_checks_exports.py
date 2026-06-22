"""Compatibility tests for the doctor.checks public export boundary.

These tests lock in the names that doctor.checks is expected to expose before
doctor/checks/__init__.py is converted from star-imports to explicit re-exports.

The consumers we must preserve are:
  - doctor/scheduler.py: imports 13 check_* functions explicitly
  - doctor/__main__.py: imports backfill_missing_seasons, warmer_loop, plexlog_loop
  - doctor/webui.py: imports warmer (as _warmer) explicitly
"""
import unittest

import doctor.checks as _checks


class CheckFunctionsExportTest(unittest.TestCase):
    """All check functions referenced by CHECKS must be importable from doctor.checks."""

    def test_check_queue(self):
        self.assertTrue(callable(_checks.check_queue))

    def test_check_providers(self):
        self.assertTrue(callable(_checks.check_providers))

    def test_check_decypharr(self):
        self.assertTrue(callable(_checks.check_decypharr))

    def test_check_plex(self):
        self.assertTrue(callable(_checks.check_plex))

    def test_check_plex_scan(self):
        self.assertTrue(callable(_checks.check_plex_scan))

    def test_check_resources(self):
        self.assertTrue(callable(_checks.check_resources))

    def test_check_janitor(self):
        self.assertTrue(callable(_checks.check_janitor))

    def test_check_repair(self):
        self.assertTrue(callable(_checks.check_repair))

    def test_check_bazarr(self):
        self.assertTrue(callable(_checks.check_bazarr))

    def test_check_seerr(self):
        self.assertTrue(callable(_checks.check_seerr))

    def test_check_missing_seasons(self):
        self.assertTrue(callable(_checks.check_missing_seasons))

    def test_check_no_upgrade_profile(self):
        self.assertTrue(callable(_checks.check_no_upgrade_profile))

    def test_check_multipack(self):
        self.assertTrue(callable(_checks.check_multipack))


class AuxiliaryExportsTest(unittest.TestCase):
    """Names used by __main__.py and webui.py must remain available."""

    def test_backfill_missing_seasons(self):
        self.assertTrue(callable(_checks.backfill_missing_seasons))

    def test_warmer_loop(self):
        self.assertTrue(callable(_checks.warmer_loop))

    def test_plexlog_loop(self):
        self.assertTrue(callable(_checks.plexlog_loop))

    def test_warmer_module(self):
        """webui.py imports warmer from doctor.checks as _warmer."""
        self.assertTrue(hasattr(_checks, "warmer"))
        self.assertTrue(callable(_checks.warmer.warmer_loop))


class ConsumerImportPatternsTest(unittest.TestCase):
    """Simulate the exact import patterns used by the three consumers."""

    def test_scheduler_import_pattern(self):
        from doctor.checks import (
            check_queue,
            check_repair,
        )
        self.assertTrue(callable(check_queue))
        self.assertTrue(callable(check_repair))

    def test_main_import_pattern(self):
        from doctor.checks import backfill_missing_seasons, plexlog_loop, warmer_loop
        self.assertTrue(callable(backfill_missing_seasons))
        self.assertTrue(callable(plexlog_loop))
        self.assertTrue(callable(warmer_loop))

    def test_webui_import_pattern(self):
        from doctor.checks import warmer
        self.assertTrue(hasattr(warmer, "warmer_loop"))


if __name__ == "__main__":
    unittest.main()
