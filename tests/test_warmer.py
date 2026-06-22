"""Unit tests for doctor.checks.warmer.

Tests cover the core logic that can be exercised without a real filesystem or
Plex server:

  _host_path():
    - returns the path unchanged when no WARM_PATH_MAP
    - rewrites the prefix when WARM_PATH_MAP = "a:b" and path starts with a
    - returns the path unchanged when path does not start with the map prefix

  _limit_parts():
    - returns all files when WARM_PARTS <= 0
    - truncates to WARM_PARTS when set

  _warm_record():
    - increments _warm_count[0]
    - appends an entry to _warm_recent
    - caps _warm_recent at 80 entries

  _warm_file():
    - skips (returns False) when host load exceeds WARM_LOAD_MAX
    - skips (returns False) when file was warmed within WARM_COOLDOWN
    - skips (returns False) when os.path.getsize raises
    - returns True and records state when file is read successfully
    - returns False when the read thread times out (is_alive=True)
    - reads head bytes and optionally tail bytes based on config

  warm_cycle():
    - skips the cycle when host load exceeds WARM_LOAD_MAX
    - calls _warm_file for each target up to WARM_MAX_CYCLE
    - stops after WARM_MAX_CYCLE successful warms

  _warm_targets():
    - returns empty list when no sessions and no on-deck sources
    - adds next-ep targets when "next" is in WARM_SOURCES and episode is near end
    - skips next-ep when remaining time exceeds WARM_NEXT_NEAR_END
    - adds ondeck targets when sessions is empty and ondeck is enabled
    - skips ondeck when sessions are active (someone is watching)

Patching strategy: module-level config constants are patched on the warmer
module directly.  Filesystem calls (os.path.getsize, open, threading.Thread)
are patched per-test.  The Plex client is always mocked.
"""
import threading
import time
import unittest
from unittest.mock import MagicMock, patch, mock_open

_MOD = "doctor.checks.warmer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_warmer_state():
    """Reset module-level mutable state between tests."""
    import doctor.checks.warmer as w
    w._warm_state.clear()
    w._warm_last_ondeck[0] = 0.0
    w._warm_count[0] = 0
    w._warm_recent.clear()


def _make_plex(sessions=None, ondeck=None, recent=None, parts=None, leaves=None):
    p = MagicMock()
    p.sessions.return_value = sessions or []
    p.ondeck.return_value = ondeck or []
    p.recent.return_value = recent or []
    p.parts.return_value = parts or []
    p.leaves.return_value = leaves or []
    return p


# ---------------------------------------------------------------------------
# _host_path
# ---------------------------------------------------------------------------

class HostPathTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_no_map_returns_unchanged(self):
        from doctor.checks.warmer import _host_path
        with patch(_MOD + ".WARM_PATH_MAP", ""):
            self.assertEqual(_host_path("/mnt/lib/file.mkv"), "/mnt/lib/file.mkv")

    def test_rewrites_matching_prefix(self):
        from doctor.checks.warmer import _host_path
        with patch(_MOD + ".WARM_PATH_MAP", "/mnt/lib:/data/lib"):
            self.assertEqual(_host_path("/mnt/lib/Show/ep.mkv"), "/data/lib/Show/ep.mkv")

    def test_non_matching_prefix_unchanged(self):
        from doctor.checks.warmer import _host_path
        with patch(_MOD + ".WARM_PATH_MAP", "/mnt/lib:/data/lib"):
            self.assertEqual(_host_path("/other/path/ep.mkv"), "/other/path/ep.mkv")

    def test_no_colon_in_map_returns_unchanged(self):
        from doctor.checks.warmer import _host_path
        with patch(_MOD + ".WARM_PATH_MAP", "nocolon"):
            self.assertEqual(_host_path("/mnt/lib/file.mkv"), "/mnt/lib/file.mkv")


# ---------------------------------------------------------------------------
# _limit_parts
# ---------------------------------------------------------------------------

class LimitPartsTest(unittest.TestCase):

    def test_returns_all_when_parts_zero(self):
        from doctor.checks.warmer import _limit_parts
        with patch(_MOD + ".WARM_PARTS", 0):
            files = ["a", "b", "c"]
            self.assertEqual(_limit_parts(files), files)

    def test_returns_all_when_parts_negative(self):
        from doctor.checks.warmer import _limit_parts
        with patch(_MOD + ".WARM_PARTS", -1):
            files = ["a", "b", "c"]
            self.assertEqual(_limit_parts(files), files)

    def test_truncates_to_warm_parts(self):
        from doctor.checks.warmer import _limit_parts
        with patch(_MOD + ".WARM_PARTS", 2):
            self.assertEqual(_limit_parts(["a", "b", "c"]), ["a", "b"])


# ---------------------------------------------------------------------------
# _warm_record
# ---------------------------------------------------------------------------

class WarmRecordTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_increments_warm_count(self):
        from doctor.checks.warmer import _warm_record
        _warm_record("Show S01E01.mkv", "cycle")
        import doctor.checks.warmer as w
        self.assertEqual(w._warm_count[0], 1)

    def test_appends_to_warm_recent(self):
        from doctor.checks.warmer import _warm_record
        _warm_record("Show.mkv", "ondeck")
        import doctor.checks.warmer as w
        self.assertEqual(len(w._warm_recent), 1)
        self.assertEqual(w._warm_recent[0]["title"], "Show.mkv")
        self.assertEqual(w._warm_recent[0]["why"], "ondeck")

    def test_caps_warm_recent_at_80(self):
        from doctor.checks.warmer import _warm_record
        for i in range(90):
            _warm_record(f"file{i}.mkv", "cycle")
        import doctor.checks.warmer as w
        self.assertEqual(len(w._warm_recent), 80)


# ---------------------------------------------------------------------------
# _warm_file
# ---------------------------------------------------------------------------

class WarmFileLoadGuardTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_skips_when_load_exceeds_max(self):
        from doctor.checks.warmer import _warm_file
        with patch(_MOD + ".WARM_LOAD_MAX", 1.0), \
             patch(_MOD + ".WARM_COOLDOWN", 0), \
             patch(_MOD + ".host_load", return_value=5.0), \
             patch(_MOD + ".WARM_PATH_MAP", ""):
            result = _warm_file("/some/file.mkv")
        self.assertFalse(result)

    def test_passes_when_load_under_max(self):
        from doctor.checks.warmer import _warm_file
        with patch(_MOD + ".WARM_LOAD_MAX", 10.0), \
             patch(_MOD + ".WARM_COOLDOWN", 0), \
             patch(_MOD + ".host_load", return_value=1.0), \
             patch(_MOD + ".WARM_PATH_MAP", ""), \
             patch(_MOD + ".WARM_HEAD_MB", 1), \
             patch(_MOD + ".WARM_TAIL_MB", 0), \
             patch(_MOD + ".WARM_READ_TIMEOUT", 30), \
             patch("os.path.getsize", return_value=10 << 20), \
             patch("builtins.open", mock_open(read_data=b"x" * 4096)):
            result = _warm_file("/some/file.mkv")
        self.assertTrue(result)


class WarmFileCooldownTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_skips_file_within_cooldown(self):
        import doctor.checks.warmer as w
        from doctor.checks.warmer import _warm_file
        # Seed state so file was "just" warmed
        w._warm_state["/some/file.mkv"] = time.time()
        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".WARM_COOLDOWN", 3600), \
             patch(_MOD + ".WARM_PATH_MAP", ""):
            result = _warm_file("/some/file.mkv")
        self.assertFalse(result)

    def test_warms_file_after_cooldown_expires(self):
        import doctor.checks.warmer as w
        from doctor.checks.warmer import _warm_file
        # Seed state with an old timestamp (cooldown expired)
        w._warm_state["/some/file.mkv"] = time.time() - 7200
        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".WARM_COOLDOWN", 3600), \
             patch(_MOD + ".WARM_PATH_MAP", ""), \
             patch(_MOD + ".WARM_HEAD_MB", 1), \
             patch(_MOD + ".WARM_TAIL_MB", 0), \
             patch(_MOD + ".WARM_READ_TIMEOUT", 30), \
             patch("os.path.getsize", return_value=10 << 20), \
             patch("builtins.open", mock_open(read_data=b"x" * 4096)):
            result = _warm_file("/some/file.mkv")
        self.assertTrue(result)


class WarmFileStatFailTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_returns_false_when_stat_fails(self):
        from doctor.checks.warmer import _warm_file
        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".WARM_COOLDOWN", 0), \
             patch(_MOD + ".WARM_PATH_MAP", ""), \
             patch("os.path.getsize", side_effect=OSError("no such file")):
            result = _warm_file("/nonexistent.mkv")
        self.assertFalse(result)


class WarmFileReadTimeoutTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_returns_false_when_read_times_out(self):
        from doctor.checks.warmer import _warm_file

        # Simulate a thread that never finishes (is_alive stays True)
        class HangingThread(threading.Thread):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.daemon = True
            def start(self): pass
            def join(self, timeout=None): pass
            def is_alive(self): return True

        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".WARM_COOLDOWN", 0), \
             patch(_MOD + ".WARM_PATH_MAP", ""), \
             patch(_MOD + ".WARM_HEAD_MB", 1), \
             patch(_MOD + ".WARM_TAIL_MB", 0), \
             patch(_MOD + ".WARM_READ_TIMEOUT", 1), \
             patch("os.path.getsize", return_value=10 << 20), \
             patch("threading.Thread", HangingThread):
            result = _warm_file("/some/file.mkv")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# warm_cycle
# ---------------------------------------------------------------------------

class WarmCycleTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def test_skips_cycle_when_load_too_high(self):
        from doctor.checks.warmer import warm_cycle
        with patch(_MOD + ".WARM_LOAD_MAX", 1.0), \
             patch(_MOD + ".host_load", return_value=5.0), \
             patch(_MOD + "._warm_targets") as mock_targets, \
             patch(_MOD + ".Plex"):
            warm_cycle()
            mock_targets.assert_not_called()

    def test_warms_up_to_max_cycle(self):
        from doctor.checks.warmer import warm_cycle
        targets = [("cycle", f"/lib/f{i}.mkv") for i in range(5)]
        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".host_load", return_value=0.0), \
             patch(_MOD + ".WARM_MAX_CYCLE", 2), \
             patch(_MOD + ".PLEX_URL", "http://plex"), \
             patch(_MOD + ".PLEX_TOKEN", "tok"), \
             patch(_MOD + ".Plex"), \
             patch(_MOD + "._warm_targets", return_value=targets), \
             patch(_MOD + "._warm_file", return_value=True) as mock_warm:
            warm_cycle()
        # Called for up to WARM_MAX_CYCLE=2 successful warms
        self.assertEqual(mock_warm.call_count, 2)

    def test_continues_past_failed_warms(self):
        """Failed warms (False) do not count against WARM_MAX_CYCLE."""
        from doctor.checks.warmer import warm_cycle
        # 4 targets: first 2 fail, next 2 succeed → should warm 2
        targets = [("cycle", f"/lib/f{i}.mkv") for i in range(4)]
        side_effects = [False, False, True, True]
        with patch(_MOD + ".WARM_LOAD_MAX", 0), \
             patch(_MOD + ".host_load", return_value=0.0), \
             patch(_MOD + ".WARM_MAX_CYCLE", 2), \
             patch(_MOD + ".PLEX_URL", "http://plex"), \
             patch(_MOD + ".PLEX_TOKEN", "tok"), \
             patch(_MOD + ".Plex"), \
             patch(_MOD + "._warm_targets", return_value=targets), \
             patch(_MOD + "._warm_file", side_effect=side_effects) as mock_warm:
            warm_cycle()
        self.assertEqual(mock_warm.call_count, 4)


# ---------------------------------------------------------------------------
# _warm_targets
# ---------------------------------------------------------------------------

class WarmTargetsTest(unittest.TestCase):

    def setUp(self):
        _reset_warmer_state()

    def _run_targets(self, plex, *, sources=None, load_max=0, max_cycle=99,
                     ondeck=True, ondeck_every=0, low_cache=False,
                     next_near_end=0, next_eps=1, recent_count=0, parts=0):
        from doctor.checks.warmer import _warm_targets
        with patch(_MOD + ".WARM_SOURCES", sources or []), \
             patch(_MOD + ".WARM_LOAD_MAX", load_max), \
             patch(_MOD + ".WARM_MAX_CYCLE", max_cycle), \
             patch(_MOD + ".WARM_ONDECK", ondeck), \
             patch(_MOD + ".WARM_ONDECK_EVERY", ondeck_every), \
             patch(_MOD + ".WARM_LOW_CACHE", low_cache), \
             patch(_MOD + ".WARM_NEXT_NEAR_END", next_near_end), \
             patch(_MOD + ".WARM_NEXT_EPS", next_eps), \
             patch(_MOD + ".WARM_RECENT_COUNT", recent_count), \
             patch(_MOD + ".WARM_PARTS", parts):
            return _warm_targets(plex)

    def test_empty_when_no_sources(self):
        plex = _make_plex()
        targets = self._run_targets(plex, sources=[])
        self.assertEqual(targets, [])

    def test_ondeck_added_when_no_sessions(self):
        plex = _make_plex(
            sessions=[],
            ondeck=[{"ratingKey": "rk1"}],
            parts=["/lib/file.mkv"],
        )
        plex.parts.return_value = ["/lib/file.mkv"]
        targets = self._run_targets(plex, sources=["ondeck"], ondeck=True, ondeck_every=0)
        reasons = [r for r, _ in targets]
        self.assertIn("ondeck", reasons)

    def test_ondeck_skipped_when_sessions_active(self):
        """On Deck is never pre-warmed while someone is actively watching."""
        plex = _make_plex(
            sessions=[{"ratingKey": "live", "type": "episode"}],
            ondeck=[{"ratingKey": "rk1"}],
        )
        targets = self._run_targets(plex, sources=["ondeck"], ondeck=True, ondeck_every=0)
        reasons = [r for r, _ in targets]
        self.assertNotIn("ondeck", reasons)

    def test_next_ep_added_when_episode_near_end(self):
        """next-ep sources added when remaining play time < WARM_NEXT_NEAR_END."""
        duration = 60 * 60 * 1000   # 60 min in ms
        offset   = 55 * 60 * 1000   # 55 min watched → 5 min remain
        session = {
            "type": "episode",
            "grandparentRatingKey": "show1",
            "ratingKey": "ep1",
            "duration": duration,
            "viewOffset": offset,
        }
        next_ep = {"ratingKey": "ep2"}
        plex = _make_plex(sessions=[session], leaves=[{"ratingKey": "ep1"}, next_ep])
        plex.parts.return_value = ["/lib/next.mkv"]
        targets = self._run_targets(plex, sources=["next"], next_near_end=10, next_eps=1)
        reasons = [r for r, _ in targets]
        self.assertIn("next-ep", reasons)

    def test_next_ep_skipped_when_too_much_remaining(self):
        """next-ep NOT added when remaining play time > WARM_NEXT_NEAR_END."""
        duration = 60 * 60 * 1000   # 60 min
        offset   = 10 * 60 * 1000   # only 10 min watched → 50 min remain
        session = {
            "type": "episode",
            "grandparentRatingKey": "show1",
            "ratingKey": "ep1",
            "duration": duration,
            "viewOffset": offset,
        }
        plex = _make_plex(sessions=[session], leaves=[{"ratingKey": "ep1"}, {"ratingKey": "ep2"}])
        plex.parts.return_value = ["/lib/next.mkv"]
        targets = self._run_targets(plex, sources=["next"], next_near_end=10, next_eps=1)
        reasons = [r for r, _ in targets]
        self.assertNotIn("next-ep", reasons)

    def test_duplicate_paths_deduplicated(self):
        """The same path appearing in multiple sources is only added once."""
        plex = _make_plex(
            sessions=[],
            ondeck=[{"ratingKey": "rk1"}, {"ratingKey": "rk2"}],
        )
        plex.parts.return_value = ["/lib/same.mkv"]  # both return the same path
        targets = self._run_targets(plex, sources=["ondeck"], ondeck=True, ondeck_every=0)
        paths = [p for _, p in targets]
        self.assertEqual(len(paths), len(set(paths)))


class WarmCyclemetadataTest(unittest.TestCase):
    """Tests for per-cycle metadata vars added in B4."""

    def _import_warmer(self):
        import importlib
        import doctor.checks.warmer as w
        importlib.reload(w)
        return w

    def test_last_cycle_ts_updated_after_cycle(self):
        import doctor.checks.warmer as w
        # Reset
        w._last_cycle_ts[0] = 0.0
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 0), \
             patch("doctor.checks.warmer._warm_targets", return_value=[]), \
             patch("doctor.checks.warmer.Plex"):
            before = time.time()
            w.warm_cycle()
            after = time.time()
        self.assertGreaterEqual(w._last_cycle_ts[0], before)
        self.assertLessEqual(w._last_cycle_ts[0], after)

    def test_last_cycle_warmed_count(self):
        import doctor.checks.warmer as w
        w._last_cycle_warmed[0] = 0
        targets = [("ondeck", "/fake/a.mkv"), ("ondeck", "/fake/b.mkv")]
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 0), \
             patch("doctor.checks.warmer.WARM_MAX_CYCLE", 10), \
             patch("doctor.checks.warmer._warm_targets", return_value=targets), \
             patch("doctor.checks.warmer._warm_file", return_value=True), \
             patch("doctor.checks.warmer.Plex"):
            w.warm_cycle()
        self.assertEqual(w._last_cycle_warmed[0], 2)

    def test_last_cycle_candidates_count(self):
        import doctor.checks.warmer as w
        w._last_cycle_candidates[0] = 0
        targets = [("ondeck", "/a"), ("ondeck", "/b"), ("next-ep", "/c")]
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 0), \
             patch("doctor.checks.warmer.WARM_MAX_CYCLE", 10), \
             patch("doctor.checks.warmer._warm_targets", return_value=targets), \
             patch("doctor.checks.warmer._warm_file", return_value=False), \
             patch("doctor.checks.warmer.Plex"):
            w.warm_cycle()
        self.assertEqual(w._last_cycle_candidates[0], 3)

    def test_last_cycle_skipped_load_true_when_load_exceeded(self):
        import doctor.checks.warmer as w
        w._last_cycle_skipped_load[0] = False
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 1.0), \
             patch("doctor.checks.warmer.host_load", return_value=5.0):
            w.warm_cycle()
        self.assertTrue(w._last_cycle_skipped_load[0])

    def test_last_cycle_skipped_load_false_when_load_ok(self):
        import doctor.checks.warmer as w
        w._last_cycle_skipped_load[0] = True
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 0), \
             patch("doctor.checks.warmer._warm_targets", return_value=[]), \
             patch("doctor.checks.warmer.Plex"):
            w.warm_cycle()
        self.assertFalse(w._last_cycle_skipped_load[0])

    def test_last_cycle_duration_is_nonnegative_float(self):
        import doctor.checks.warmer as w
        w._last_cycle_duration[0] = -1.0
        with patch("doctor.checks.warmer.WARM_LOAD_MAX", 0), \
             patch("doctor.checks.warmer._warm_targets", return_value=[]), \
             patch("doctor.checks.warmer.Plex"):
            w.warm_cycle()
        self.assertGreaterEqual(w._last_cycle_duration[0], 0.0)
        self.assertIsInstance(w._last_cycle_duration[0], float)
