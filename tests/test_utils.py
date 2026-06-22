"""Unit tests for the small utility helpers in doctor.utils.

These helpers were extracted from doctor.config.py in Phase 2.
doctor.config still re-exports them for backward compatibility, but the tests target the new home.

All external side effects (urllib, subprocess, /proc/loadavg) are mocked so
the tests run without network access or a real /proc filesystem.
"""
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from doctor.utils import http_code, run_cmd, run_output, host_load


class HttpCodeTest(unittest.TestCase):
    """http_code(url, headers=None, t=10) returns the HTTP status or 0."""

    @patch("doctor.utils.urllib.request.urlopen")
    def test_returns_status_on_success(self, mock_open):
        resp = MagicMock()
        resp.status = 200
        mock_open.return_value = resp
        self.assertEqual(http_code("http://example.com"), 200)
        mock_open.assert_called_once()
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, "http://example.com")
        self.assertEqual(req.headers, {})
        self.assertEqual(mock_open.call_args.kwargs.get("timeout"), 10)

    @patch("doctor.utils.urllib.request.urlopen")
    def test_returns_error_code_on_http_error(self, mock_open):
        from urllib.error import HTTPError
        mock_open.side_effect = HTTPError(
            url="http://example.com", code=503, msg="busy", hdrs=None, fp=None
        )
        self.assertEqual(http_code("http://example.com"), 503)

    @patch("doctor.utils.urllib.request.urlopen")
    def test_returns_zero_on_generic_exception(self, mock_open):
        mock_open.side_effect = OSError("no route")
        self.assertEqual(http_code("http://example.com"), 0)

    @patch("doctor.utils.urllib.request.urlopen")
    def test_passes_headers_and_timeout(self, mock_open):
        resp = MagicMock()
        resp.status = 204
        mock_open.return_value = resp
        self.assertEqual(http_code("http://example.com", headers={"X": "Y"}, t=5), 204)
        req = mock_open.call_args[0][0]
        self.assertEqual(req.headers, {"X": "Y"})
        self.assertEqual(mock_open.call_args.kwargs.get("timeout"), 5)


class RunCmdTest(unittest.TestCase):
    """run_cmd(cmd) returns (rc, combined_output[:300]) or None."""

    @patch("doctor.utils.subprocess.run")
    def test_returns_none_for_empty_cmd(self, mock_run):
        self.assertIsNone(run_cmd(""))
        mock_run.assert_not_called()

    @patch("doctor.utils.subprocess.run")
    def test_returns_rc_and_output(self, mock_run):
        p = MagicMock()
        p.returncode = 0
        p.stdout = "out\n"
        p.stderr = "err\n"
        mock_run.return_value = p
        self.assertEqual(run_cmd("echo hi"), (0, "out\nerr"))
        mock_run.assert_called_once_with(
            "echo hi", shell=True, capture_output=True, text=True, timeout=180
        )

    @patch("doctor.utils.subprocess.run")
    def test_trims_output_to_300_chars(self, mock_run):
        p = MagicMock()
        p.returncode = 0
        p.stdout = "x" * 400
        p.stderr = ""
        mock_run.return_value = p
        self.assertEqual(run_cmd("x"), (0, "x" * 300))

    @patch("doctor.utils.subprocess.run")
    def test_returns_error_tuple_on_exception(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("echo hi", 180)
        rc, out = run_cmd("echo hi")
        self.assertEqual(rc, 1)
        self.assertTrue(out.startswith("cmd error:"))
        self.assertLessEqual(len(out), 120 + len("cmd error: "))


class RunOutputTest(unittest.TestCase):
    """run_output(cmd, t=120) returns stdout or empty string on failure."""

    @patch("doctor.utils.subprocess.run")
    def test_returns_stdout(self, mock_run):
        p = MagicMock()
        p.stdout = "log line\n"
        mock_run.return_value = p
        self.assertEqual(run_output("cat log"), "log line\n")
        mock_run.assert_called_once_with(
            "cat log", shell=True, capture_output=True, text=True, timeout=120
        )

    @patch("doctor.utils.subprocess.run")
    def test_returns_empty_string_on_exception(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("cat log", 120)
        self.assertEqual(run_output("cat log"), "")

    @patch("doctor.utils.subprocess.run")
    def test_passes_custom_timeout(self, mock_run):
        p = MagicMock()
        p.stdout = ""
        mock_run.return_value = p
        run_output("cat log", t=30)
        mock_run.assert_called_once_with(
            "cat log", shell=True, capture_output=True, text=True, timeout=30
        )


class HostLoadTest(unittest.TestCase):
    """host_load() returns the 1-min load from /proc/loadavg or 0.0."""

    @patch("builtins.open")
    def test_returns_first_load_value(self, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = "2.34 1.23 0.45 4/512 12345"
        self.assertEqual(host_load(), 2.34)
        mock_open.assert_called_once_with("/proc/loadavg")

    @patch("builtins.open")
    def test_returns_zero_when_read_fails(self, mock_open):
        mock_open.side_effect = OSError("no /proc")
        self.assertEqual(host_load(), 0.0)

    @patch("builtins.open")
    def test_returns_zero_when_parse_fails(self, mock_open):
        mock_open.return_value.__enter__.return_value.read.return_value = "garbage"
        self.assertEqual(host_load(), 0.0)


if __name__ == "__main__":
    unittest.main()
