"""Unit tests for doctor.checks.seerr.check_seerr().

Tests cover:
  - skips when SEERR_URL or SEERR_APIKEY is unset
  - skips individual requests with no id field
  - logs and returns when Seerr.failed() returns None (unreachable)
  - logs and returns when there are no failed requests
  - retries a failed request and updates the state counter
  - respects SEERR_MAX_TRIES: stops retrying after n attempts
  - respects SEERR_MAX: caps total actions per sweep
  - DRY_RUN: logs intent but does not call s.retry()
  - cleans up state keys for requests no longer in the failed list
  - handles s.retry() raising an exception gracefully

All external I/O is replaced with MagicMock.  Config globals are patched on
doctor.checks.seerr directly.
"""
import unittest
from unittest.mock import MagicMock, patch

_MOD = "doctor.checks.seerr"


def _make_seerr_client(failed=None):
    s = MagicMock()
    s.failed.return_value = [] if failed is None else failed
    s.retry.return_value = None
    return s


def _req(rid, media_type="movie", tmdb_id=123):
    return {"id": rid, "media": {"mediaType": media_type, "tmdbId": tmdb_id}}


def _run(failed_reqs, *, state=None, seerr_url="http://seerr",
         seerr_apikey="key", seerr_max=10, seerr_max_tries=3,
         dry_run=False, client=None):
    if state is None:
        state = {}
    if client is None:
        client = _make_seerr_client(failed_reqs)

    from doctor.checks.seerr import check_seerr

    with patch(_MOD + ".SEERR_URL", seerr_url), \
         patch(_MOD + ".SEERR_APIKEY", seerr_apikey), \
         patch(_MOD + ".SEERR_MAX", seerr_max), \
         patch(_MOD + ".SEERR_MAX_TRIES", seerr_max_tries), \
         patch(_MOD + ".DRY_RUN", dry_run), \
         patch(_MOD + ".Seerr", return_value=client), \
         patch(_MOD + ".state_transaction") as mock_tx:
        mock_tx.return_value.__enter__ = lambda s: state
        mock_tx.return_value.__exit__ = MagicMock(return_value=False)
        check_seerr()

    return state, client


class SeerrSkipTest(unittest.TestCase):

    def test_skips_when_no_url(self):
        _, client = _run([], seerr_url="")
        client.failed.assert_not_called()

    def test_skips_when_no_apikey(self):
        _, client = _run([], seerr_apikey="")
        client.failed.assert_not_called()

    def test_returns_when_unreachable(self):
        client = _make_seerr_client(failed=None)
        _, c = _run(None, client=client)
        c.retry.assert_not_called()

    def test_returns_when_no_failed_requests(self):
        _, client = _run([])
        client.retry.assert_not_called()


class SeerrRetryTest(unittest.TestCase):

    def test_retries_one_request(self):
        reqs = [_req(1)]
        state, client = _run(reqs)
        client.retry.assert_called_once_with(1)
        self.assertEqual(state["__seerr__"]["1"], 1)

    def test_increments_counter_on_successive_runs(self):
        reqs = [_req(42)]
        state = {"__seerr__": {"42": 1}}
        _, client = _run(reqs, state=state, seerr_max_tries=5)
        client.retry.assert_called_once_with(42)
        self.assertEqual(state["__seerr__"]["42"], 2)

    def test_skips_request_without_id(self):
        reqs = [{"media": {"mediaType": "movie"}}]
        state, client = _run(reqs)
        client.retry.assert_not_called()


class SeerrMaxTriesTest(unittest.TestCase):

    def test_stops_retrying_at_max_tries(self):
        reqs = [_req(7)]
        state = {"__seerr__": {"7": 3}}
        _, client = _run(reqs, state=state, seerr_max_tries=3)
        client.retry.assert_not_called()

    def test_retries_when_below_max_tries(self):
        reqs = [_req(7)]
        state = {"__seerr__": {"7": 2}}
        _, client = _run(reqs, state=state, seerr_max_tries=3)
        client.retry.assert_called_once_with(7)

    def test_zero_max_tries_means_unlimited(self):
        reqs = [_req(9)]
        state = {"__seerr__": {"9": 999}}
        _, client = _run(reqs, state=state, seerr_max_tries=0)
        client.retry.assert_called_once_with(9)


class SeerrCapTest(unittest.TestCase):

    def test_caps_at_seerr_max(self):
        reqs = [_req(i) for i in range(5)]
        _, client = _run(reqs, seerr_max=2)
        self.assertEqual(client.retry.call_count, 2)

    def test_zero_max_breaks_immediately(self):
        # acted >= SEERR_MAX is True when both are 0: no retries
        reqs = [_req(i) for i in range(4)]
        _, client = _run(reqs, seerr_max=0)
        client.retry.assert_not_called()


class SeerrDryRunTest(unittest.TestCase):

    def test_dry_run_does_not_call_retry(self):
        reqs = [_req(1)]
        _, client = _run(reqs, dry_run=True)
        client.retry.assert_not_called()

    def test_dry_run_respects_seerr_max(self):
        reqs = [_req(1), _req(2)]
        _, client = _run(reqs, seerr_max=1, dry_run=True)
        client.retry.assert_not_called()


class SeerrStateCleanupTest(unittest.TestCase):

    def test_removes_stale_state_key(self):
        reqs = [_req(1)]
        state = {"__seerr__": {"1": 0, "99": 2}}
        _run(reqs, state=state)
        self.assertNotIn("99", state["__seerr__"])
        self.assertIn("1", state["__seerr__"])


class SeerrRetryExceptionTest(unittest.TestCase):

    def test_retry_exception_is_swallowed(self):
        client = _make_seerr_client([_req(1)])
        client.retry.side_effect = RuntimeError("connection refused")
        state, _ = _run([_req(1)], client=client)
        tries = state.get("__seerr__", {})
        self.assertEqual(tries.get("1", 0), 0)


class SeerrNoneIdCleanupTest(unittest.TestCase):
    """Regression tests for the None-id state cleanup bug (A4).

    When a request has id=None, str(None)="None" was added to the `live` set,
    preventing cleanup of a state key literally named "None".
    """

    def test_none_id_does_not_pollute_live_set(self):
        """A request with id=None must not add 'None' to the live set,
        so stale state keys that happen to be named 'None' get cleaned up."""
        # Simulate: one real request, one with id=None, and a stale 'None' key in state
        reqs = [_req(1), {"id": None, "media": {}}]
        state = {"__seerr__": {"1": 0, "None": 3}}
        _run(reqs, state=state)
        # "None" must be cleaned up — it was not a real request id
        self.assertNotIn("None", state["__seerr__"])
        # "1" must stay (it's still in the live failed requests)
        self.assertIn("1", state["__seerr__"])

    def test_real_requests_with_none_id_in_mix_still_retry(self):
        """A None-id request is skipped but real requests around it still work."""
        reqs = [{"id": None, "media": {}}, _req(2), {"id": None, "media": {}}]
        state, client = _run(reqs)
        tries = state.get("__seerr__", {})
        # Only req #2 should have been retried
        self.assertEqual(tries.get("2", 0), 1)
        self.assertNotIn("None", tries)
