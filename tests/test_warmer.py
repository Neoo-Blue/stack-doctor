"""The warmer sizes each warm from the title's bitrate and what the link is doing, and never competes with a viewer."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import doctor

MB = 1 << 20
GB = 1 << 30


class HeadSizeTest(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(doctor, "WARM_START_S", 5), patch.object(doctor, "WARM_RUNWAY_S", 30),
                  patch.object(doctor, "WARM_LOAD_CALM", 4.0), patch.object(doctor, "WARM_HEAD_MB", 24),
                  patch.object(doctor, "WARM_HEAD_MAX_MB", 300), patch.object(doctor, "WARM_BULK_HEAD_MB", 40),
                  patch.object(doctor, "WARM_BULK_SECONDS", 8)):
            p.start()
            self.addCleanup(p.stop)

    def test_low_bitrate_stays_at_the_floor(self):
        self.assertEqual(doctor._warm_head_bytes(3 * GB, 0.4 * MB, 8, 15 * MB, 1), 24 * MB)

    def test_slower_pickup_and_busy_host_ask_for_more(self):
        base = doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 15 * MB, 1)
        self.assertGreater(doctor._warm_head_bytes(60 * GB, 8 * MB, 16, 15 * MB, 1), base * 1.5)
        self.assertGreater(doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 15 * MB, 8), base * 1.4)

    def test_load_stretch_is_capped(self):
        self.assertEqual(doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 15 * MB, 8),
                         doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 15 * MB, 40))

    def test_fetch_below_bitrate_covers_the_shortfall(self):
        self.assertGreater(doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 5 * MB, 1),
                           doctor._warm_head_bytes(60 * GB, 8 * MB, 8, 15 * MB, 1) + 80 * MB)

    def test_never_past_the_ceiling_or_the_file(self):
        self.assertEqual(doctor._warm_head_bytes(60 * GB, 30 * MB, 30, 5 * MB, 20), 300 * MB)
        self.assertEqual(doctor._warm_head_bytes(10 * MB, 8 * MB, 8, 15 * MB, 1), 10 * MB)

    def test_bulk_head_scales_with_bitrate_within_its_bounds(self):
        self.assertEqual(doctor._warm_bulk_head(2 * MB), 40 * MB)
        self.assertEqual(doctor._warm_bulk_head(8 * MB), 64 * MB)
        self.assertEqual(doctor._warm_bulk_head(30 * MB), 80 * MB)


class LearnTest(unittest.TestCase):
    def setUp(self):
        p = patch.dict(doctor._warm_live, {"pickup": 8.0, "tput": 15 * MB})
        p.start()
        self.addCleanup(p.stop)

    def test_cache_hit_teaches_nothing(self):
        doctor._warm_learn("0.000 0.001 16777216\n")
        self.assertEqual(doctor._warm_live, {"pickup": 8.0, "tput": 15 * MB})

    def test_cold_read_moves_both_estimates(self):
        doctor._warm_learn("13.000 16.000 33554432\n")
        self.assertAlmostEqual(doctor._warm_live["pickup"], 9.0)
        self.assertLess(doctor._warm_live["tput"], 15 * MB)


class ReaderTest(unittest.TestCase):
    def test_reads_ranges_and_reports_each(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b"x" * 5000)
            f.flush()
            out = subprocess.run([sys.executable, "-c", doctor._WARM_READER, f.name, "4000:1000", "0:100"],
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual([line.split()[2] for line in out.stdout.splitlines()], ["1000", "100"])
        doctor._warm_learn(out.stdout)

    def test_a_missing_file_fails(self):
        out = subprocess.run([sys.executable, "-c", doctor._WARM_READER, "/nonexistent/file", "0:1"], capture_output=True)
        self.assertNotEqual(out.returncode, 0)


class WedgedReadTest(unittest.TestCase):
    """A FIFO blocks the reader on open, the same way a wedged FUSE read never returns."""

    def setUp(self):
        self.fifo = os.path.join(tempfile.mkdtemp(), "wedged")
        os.mkfifo(self.fifo)
        for p in (patch.object(doctor, "WARM_LOAD_MAX", 0), patch.object(doctor, "WARM_COOLDOWN", 3600)):
            p.start()
            self.addCleanup(p.stop)

    def test_viewer_starting_kills_a_speculative_warm_and_allows_a_retry(self):
        viewer = threading.Event()
        threading.Timer(1, viewer.set).start()
        with patch.object(doctor, "WARM_READ_TIMEOUT", 60), patch.object(doctor, "_viewer_playing", viewer.is_set):
            started = time.time()
            self.assertFalse(doctor._warm_file(self.fifo, "season"))
        self.assertLess(time.time() - started, 8)
        self.assertNotIn(doctor._host_path(self.fifo), doctor._warm_state)
        self.assertNotIn(doctor._host_path(self.fifo), doctor._warm_sizes)

    def test_a_title_the_viewer_opened_is_not_killed_for_them(self):
        with patch.object(doctor, "WARM_READ_TIMEOUT", 2), patch.object(doctor, "_viewer_playing", return_value=True):
            started = time.time()
            self.assertFalse(doctor._warm_file(self.fifo, "detail-page"))
        self.assertGreaterEqual(time.time() - started, 2)
        self.assertIn(doctor._host_path(self.fifo), doctor._warm_state, "a wedged file sits out its cooldown")
        doctor._warm_state.pop(doctor._host_path(self.fifo))


class ViewerPlayingTest(unittest.TestCase):
    def playing(self, *states):
        videos = [ET.fromstring(f'<Video><Player state="{s}"/></Video>') for s in states]
        doctor._viewing.update(ts=0.0, on=False)

        class Fake:
            def __init__(self, url, token):
                pass

            def sessions(self):
                return videos
        with patch.object(doctor, "PLEX_URL", "http://plex"), patch.object(doctor, "Plex", Fake):
            return doctor._viewer_playing()

    def test_nobody_watching(self):
        self.assertFalse(self.playing())

    def test_playing_and_buffering_count(self):
        self.assertTrue(self.playing("playing"))
        self.assertTrue(self.playing("paused", "buffering"))

    def test_a_session_left_paused_holds_nothing_back(self):
        self.assertFalse(self.playing("paused"))


if __name__ == "__main__":
    unittest.main()
