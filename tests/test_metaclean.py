"""Tests for the metaclean check helpers and mount-guard prefix matching."""
import unittest
from unittest.mock import patch

import doctor


class MetaExtractKeysTest(unittest.TestCase):
    def test_strips_leading_digits_dash_and_extensions(self):
        text = "123-Release.Name.2024.nzb\n456-Another.Show.S01E01.par2.vol000+01"
        keys = doctor._meta_extract_keys(text)
        self.assertIn("release.name.2024", keys)
        self.assertIn("another.show.s01e01", keys)

    def test_strips_bracket_suffix(self):
        text = "Some.Release[12345].nzb"
        keys = doctor._meta_extract_keys(text)
        self.assertIn("some.release", keys)


class MetaStormKeysTest(unittest.TestCase):
    def test_extracts_file_name_from_crc_mismatch_line(self):
        text = "ERROR yEnc CRC mismatch for file_name=/movies/Iceman.2024.1080p.WEB-DL"
        keys = doctor._meta_storm_keys(text)
        self.assertIn("iceman.2024.1080p.web", keys)

    def test_ignores_non_mismatch_lines(self):
        text = "INFO decoded file_name=/movies/Iceman.2024.1080p.WEB-DL"
        keys = doctor._meta_storm_keys(text)
        self.assertEqual(len(keys), 0)


class MetaFirstKeyTest(unittest.TestCase):
    def test_returns_first_long_token(self):
        self.assertEqual(doctor._meta_first_key("Iceman.2024.1080p.WEB-DL"), "iceman.2024.1080p.web")

    def test_returns_none_for_short_name(self):
        self.assertIsNone(doctor._meta_first_key("abc"))


class MountGuardPrefixTest(unittest.TestCase):
    def test_longest_prefix_wins(self):
        guards = {"/mnt/zurg": "/mnt/zurg/__all__", "/mnt/zurg2": "/mnt/zurg2/__all__"}
        with patch.object(doctor, "MOUNT_GUARDS", guards), \
             patch.object(doctor, "_realpath_with_timeout",
                          lambda p, t=None, return_timeout=False: (p, False) if return_timeout else p), \
             patch.object(doctor, "_probe_mount", return_value=True) as probe:
            self.assertTrue(doctor._mount_ok_for("/mnt/zurg2/file.mkv"))
        probe.assert_called_once_with("/mnt/zurg2", "/mnt/zurg2/__all__")

    def test_unrelated_path_returns_none(self):
        guards = {"/mnt/zurg": "/mnt/zurg/__all__"}
        with patch.object(doctor, "MOUNT_GUARDS", guards), \
             patch.object(doctor, "_realpath_with_timeout",
                          lambda p, t=None, return_timeout=False: (p, False) if return_timeout else p), \
             patch.object(doctor, "_probe_mount") as probe:
            self.assertIsNone(doctor._mount_ok_for("/mnt/local/file.mkv"))
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
