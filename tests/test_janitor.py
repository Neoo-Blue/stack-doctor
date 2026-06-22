"""Characterization tests for doctor.checks.janitor.

Lock in the current behavior of:
  - _scan_operational_errors(): regex matching on log text
  - _read_log_tail(): file read and command fallback
  - _jan_alert(): throttled alerting with cooldown
  - _probe_decy_api(): HTTP health probing with throttled alerts
  - _JAN_OP_PATTERNS: built-in pattern matching (panic, rate-limit, etc.)
  - _JAN_USER_PATTERNS: user-configurable extra patterns with word-boundary wrapping

All I/O is mocked or uses temp files.  Config globals are patched on the
janitor module directly (they arrive via star-import).
"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from doctor.checks.janitor import (
    _scan_operational_errors,
    _read_log_tail,
    _jan_alert,
    _jan_alert_last,
    _probe_decy_api,
)

_MOD = "doctor.checks.janitor"


# ---------------------------------------------------------------------------
# _scan_operational_errors
# ---------------------------------------------------------------------------

class ScanOperationalErrorsTest(unittest.TestCase):
    """Characterize _scan_operational_errors: regex matching and counting."""

    def test_empty_log_returns_empty(self):
        self.assertEqual(_scan_operational_errors(""), {})

    def test_panic_detected(self):
        data = "2024-01-01 some panic happened here\n"
        counts = _scan_operational_errors(data)
        self.assertIn("panic/fatal", counts)
        self.assertEqual(counts["panic/fatal"], 1)

    def test_fatal_detected(self):
        data = "goroutine 1: fatal error\n"
        counts = _scan_operational_errors(data)
        self.assertIn("panic/fatal", counts)

    def test_runtime_error_detected(self):
        data = "runtime error: index out of range\n"
        counts = _scan_operational_errors(data)
        self.assertIn("panic/fatal", counts)

    def test_rate_limit_detected(self):
        data = "API returned rate limit exceeded\n"
        counts = _scan_operational_errors(data)
        self.assertIn("rate-limit", counts)

    def test_rate_limited_detected(self):
        data = "provider rate limited us\n"
        counts = _scan_operational_errors(data)
        self.assertIn("rate-limit", counts)

    def test_too_many_requests_detected(self):
        data = "HTTP 429 too many requests\n"
        counts = _scan_operational_errors(data)
        # "429" matches rate-limit; "too many requests" also matches rate-limit
        # The line matches the first pattern that hits
        self.assertIn("rate-limit", counts)

    def test_429_as_word_detected(self):
        data = "server returned 429\n"
        counts = _scan_operational_errors(data)
        self.assertIn("rate-limit", counts)

    def test_cloudflare_detected(self):
        data = "blocked by cloudflare challenge\n"
        counts = _scan_operational_errors(data)
        self.assertIn("cloudflare/blocked", counts)

    def test_403_as_word_detected(self):
        data = "server returned 403 forbidden\n"
        counts = _scan_operational_errors(data)
        self.assertIn("cloudflare/blocked", counts)

    def test_unauthorized_detected(self):
        data = "request returned unauthorized\n"
        counts = _scan_operational_errors(data)
        self.assertIn("auth", counts)

    def test_401_as_word_detected(self):
        data = "HTTP 401 from server\n"
        counts = _scan_operational_errors(data)
        self.assertIn("auth", counts)

    def test_token_expired_detected(self):
        data = "token expired, renewing\n"
        counts = _scan_operational_errors(data)
        self.assertIn("auth", counts)

    def test_timeout_detected(self):
        data = "context deadline exceeded while fetching\n"
        counts = _scan_operational_errors(data)
        self.assertIn("network/timeout", counts)

    def test_connection_refused_detected(self):
        data = "dial tcp: connection refused\n"
        counts = _scan_operational_errors(data)
        self.assertIn("network/timeout", counts)

    def test_io_timeout_detected(self):
        data = "i/o timeout reading body\n"
        counts = _scan_operational_errors(data)
        self.assertIn("network/timeout", counts)

    def test_line_counted_only_once(self):
        """A line matching multiple categories should only be counted under the first match."""
        # "panic" matches panic/fatal; if it also contained "timeout", only panic/fatal should count
        data = "panic: context deadline exceeded\n"
        counts = _scan_operational_errors(data)
        # Should only have panic/fatal
        self.assertEqual(counts.get("panic/fatal", 0), 1)
        total = sum(counts.values())
        self.assertEqual(total, 1, "Line should be counted exactly once")

    def test_multiple_lines_accumulate(self):
        data = "panic error\npanic again\nrate limit hit\n"
        counts = _scan_operational_errors(data)
        self.assertEqual(counts.get("panic/fatal", 0), 2)
        self.assertEqual(counts.get("rate-limit", 0), 1)

    def test_case_insensitive(self):
        data = "PANIC in goroutine\n"
        counts = _scan_operational_errors(data)
        self.assertIn("panic/fatal", counts)

    def test_clean_log_no_matches(self):
        data = "INFO: everything is fine\nDEBUG: all good\n"
        self.assertEqual(_scan_operational_errors(data), {})

    def test_hash_ids_not_false_positive(self):
        """Hex hashes and alldebrid IDs containing '401' or '403' should NOT match
        because patterns use word boundaries."""
        data = "downloading hash=a401b9f3c2 from provider\n"
        counts = _scan_operational_errors(data)
        # "401" is embedded in a hex string -> word boundary should prevent match
        self.assertEqual(counts.get("auth", 0), 0)

    def test_false_positive_suppression_429_in_hash(self):
        """'429' embedded in a hash should not match rate-limit."""
        data = "item id=abc429def status=ok\n"
        counts = _scan_operational_errors(data)
        self.assertEqual(counts.get("rate-limit", 0), 0)


# ---------------------------------------------------------------------------
# _read_log_tail
# ---------------------------------------------------------------------------

class ReadLogTailTest(unittest.TestCase):

    @patch(_MOD + ".JAN_LOG_CMD", "")
    @patch(_MOD + ".JAN_LOG", "")
    def test_returns_none_when_no_source_configured(self):
        self.assertIsNone(_read_log_tail())

    @patch(_MOD + ".JAN_LOG_CMD", "")
    def test_reads_from_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("line 1\nline 2\nline 3\n")
            f.flush()
            fname = f.name
        try:
            with patch(_MOD + ".JAN_LOG", fname):
                data = _read_log_tail()
            self.assertIn("line 1", data)
            self.assertIn("line 3", data)
        finally:
            os.unlink(fname)

    @patch(_MOD + ".JAN_LOG_CMD", "")
    def test_reads_tail_of_large_file(self):
        """For files > 2MB, only the last ~2MB should be returned."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            # Write 3MB of data
            chunk = "x" * 1000 + "\n"
            for _ in range(3000):
                f.write(chunk)
            f.write("TAIL_MARKER\n")
            f.flush()
            fname = f.name
        try:
            with patch(_MOD + ".JAN_LOG", fname):
                data = _read_log_tail()
            self.assertIn("TAIL_MARKER", data)
            # Should be approximately 2MB, not 3MB
            self.assertLess(len(data), 2_100_000)
        finally:
            os.unlink(fname)

    @patch(_MOD + ".run_output", return_value="cmd output here")
    @patch(_MOD + ".JAN_LOG_CMD", "some command")
    def test_reads_from_command_when_configured(self, mock_run):
        data = _read_log_tail()
        self.assertEqual(data, "cmd output here")
        mock_run.assert_called_once_with("some command")

    @patch(_MOD + ".run_output", return_value="cmd output")
    @patch(_MOD + ".JAN_LOG_CMD", "some command")
    @patch(_MOD + ".JAN_LOG", "/some/file.log")
    def test_command_takes_priority_over_file(self, mock_run):
        """When both JAN_LOG_CMD and JAN_LOG are set, command takes priority."""
        data = _read_log_tail()
        self.assertEqual(data, "cmd output")

    @patch(_MOD + ".JAN_LOG_CMD", "")
    def test_returns_none_for_nonexistent_file(self):
        with patch(_MOD + ".JAN_LOG", "/nonexistent/file.log"):
            self.assertIsNone(_read_log_tail())


# ---------------------------------------------------------------------------
# _jan_alert (throttled alerting)
# ---------------------------------------------------------------------------

class JanAlertTest(unittest.TestCase):

    def setUp(self):
        # Save and clear the throttle state
        self._saved = dict(_jan_alert_last)
        _jan_alert_last.clear()

    def tearDown(self):
        _jan_alert_last.clear()
        _jan_alert_last.update(self._saved)

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 300)
    @patch(_MOD + ".log")
    def test_first_alert_fires(self, mock_log):
        _jan_alert("test_key", "message %s", "arg1")
        mock_log.warning.assert_called_once_with("message %s", "arg1")

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 300)
    @patch(_MOD + ".log")
    def test_second_alert_within_cooldown_suppressed(self, mock_log):
        _jan_alert("test_key", "first")
        mock_log.warning.reset_mock()
        _jan_alert("test_key", "second")
        mock_log.warning.assert_not_called()

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 0)
    @patch(_MOD + ".log")
    def test_zero_cooldown_always_fires(self, mock_log):
        _jan_alert("test_key", "first")
        _jan_alert("test_key", "second")
        self.assertEqual(mock_log.warning.call_count, 2)

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 300)
    @patch(_MOD + ".log")
    def test_different_keys_not_throttled(self, mock_log):
        _jan_alert("key_a", "msg a")
        _jan_alert("key_b", "msg b")
        self.assertEqual(mock_log.warning.call_count, 2)

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 1)
    @patch(_MOD + ".log")
    def test_alert_fires_after_cooldown_expires(self, mock_log):
        _jan_alert("test_key", "first")
        # Fake expiry by backdating the timestamp
        _jan_alert_last["test_key"] = time.time() - 2
        _jan_alert("test_key", "second")
        self.assertEqual(mock_log.warning.call_count, 2)


# ---------------------------------------------------------------------------
# _probe_decy_api
# ---------------------------------------------------------------------------

class ProbeDecyApiTest(unittest.TestCase):

    def setUp(self):
        self._saved = dict(_jan_alert_last)
        _jan_alert_last.clear()

    def tearDown(self):
        _jan_alert_last.clear()
        _jan_alert_last.update(self._saved)

    @patch(_MOD + ".DECY_URL", "")
    @patch(_MOD + ".http_code")
    def test_noop_when_no_url(self, mock_http):
        _probe_decy_api()
        mock_http.assert_not_called()

    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", return_value=200)
    @patch(_MOD + ".log")
    def test_ok_logs_debug_only(self, mock_log, mock_http):
        _probe_decy_api()
        # Should call http_code for both "" and "/api/status" paths
        self.assertEqual(mock_http.call_count, 2)
        # No warning for 200
        mock_log.warning.assert_not_called()

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 0)
    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", return_value=500)
    @patch(_MOD + ".log")
    def test_500_triggers_alert(self, mock_log, mock_http):
        _probe_decy_api()
        # Should log warnings for 500
        self.assertTrue(mock_log.warning.called)

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 0)
    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", return_value=401)
    @patch(_MOD + ".log")
    def test_401_triggers_auth_alert(self, mock_log, mock_http):
        _probe_decy_api()
        self.assertTrue(mock_log.warning.called)

    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 0)
    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", return_value=0)
    @patch(_MOD + ".log")
    def test_zero_code_triggers_unreachable_alert(self, mock_log, mock_http):
        _probe_decy_api()
        self.assertTrue(mock_log.warning.called)

    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", side_effect=Exception("DNS failure"))
    @patch(_MOD + ".JAN_ALERT_COOLDOWN", 0)
    @patch(_MOD + ".log")
    def test_exception_triggers_unreachable_alert(self, mock_log, mock_http):
        _probe_decy_api()
        self.assertTrue(mock_log.warning.called)

    @patch(_MOD + ".DECY_URL", "http://decy:8282")
    @patch(_MOD + ".http_code", return_value=302)
    @patch(_MOD + ".log")
    def test_unexpected_non_critical_code_logs_debug(self, mock_log, mock_http):
        _probe_decy_api()
        # 302 is not 2xx, not 5xx, not auth => debug only, no warning
        mock_log.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
