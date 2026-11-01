"""check_plexstale keeps Plex's list honest without losing anything: only entries whose library path is really gone are
dropped, a mount problem drops nothing, and Plex's delete permission is on only while dropping."""
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import doctor


def media(i, *files):
    return {"id": str(i), "files": list(files)}


class PickTest(unittest.TestCase):
    def setUp(self):
        self.live = {"/lib/live.mkv"}
        self.exists = lambda f: f in self.live

    def pick(self, items, old_enough=lambda f: True):
        return doctor._plexstale_pick(items, self.exists, old_enough)

    def test_dead_version_beside_a_live_one_is_stale(self):
        stale, ghosts = self.pick([{"media": [media(1, "/lib/live.mkv"), media(2, "/lib/old.mkv")]}])
        self.assertEqual([m["id"] for _, m in stale], ["2"])
        self.assertEqual(ghosts, [])

    def test_grace_period_protects_a_fresh_gap(self):
        stale, ghosts = self.pick([{"media": [media(1, "/lib/live.mkv"), media(2, "/lib/old.mkv")]}], lambda f: False)
        self.assertEqual((stale, ghosts), ([], []))

    def test_item_with_no_version_left_is_a_ghost(self):
        stale, ghosts = self.pick([{"media": [media(2, "/lib/old.mkv")]}])
        self.assertEqual(stale, [])
        self.assertEqual(len(ghosts), 1)

    def test_media_with_one_live_part_is_kept(self):
        self.assertEqual(self.pick([{"media": [media(1, "/lib/live.mkv", "/lib/gone-cd2.mkv")]}]), ([], []))

    def test_media_without_a_path_is_never_dead(self):
        self.assertEqual(self.pick([{"media": [media(1), media(2, "/lib/old.mkv")]}]), ([], []))


LIBRARY = """<MediaContainer><Video ratingKey="7" title="Film">
  <Media id="1"><Part file="{live}"/></Media><Media id="2"><Part file="{dead}"/></Media>
</Video></MediaContainer>"""


class FakePlex:
    def __init__(self, url, token):
        self.calls = FakePlex.calls

    def _get(self, path, token=None, timeout=15):
        if path == "/library/sections":
            return ET.fromstring('<MediaContainer><Directory key="1" type="movie"/></MediaContainer>')
        return ET.fromstring(FakePlex.xml)

    def _send(self, method, path):
        self.calls.append((method, path))
        return 200


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.live = os.path.join(self.dir, "live.mkv")
        open(self.live, "w").close()
        self.state = os.path.join(self.dir, "state.json")
        FakePlex.calls = []
        FakePlex.xml = LIBRARY.format(live=self.live, dead=os.path.join(self.dir, "old.mkv"))

    def run_check(self, **overrides):
        settings = dict(PLEXSTALE_STATE=self.state, PLEXSTALE_GRACE=0, PLEXSTALE_DRY=False, DRY_RUN=False, Plex=FakePlex)
        settings.update(overrides)
        patches = [patch.object(doctor, k, v) for k, v in settings.items()]
        for p in patches:
            p.start()
        try:
            doctor.check_plexstale()
        finally:
            for p in patches:
                p.stop()

    def test_drops_the_dead_version_and_turns_deletion_back_off(self):
        self.run_check(PLEXSTALE_MAX_MISSING=0.9)
        self.assertEqual(FakePlex.calls, [("PUT", "/:/prefs?allowMediaDeletion=1"),
                                          ("DELETE", "/library/metadata/7/media/2"),
                                          ("PUT", "/:/prefs?allowMediaDeletion=0")])

    def test_too_many_gone_means_a_mount_problem_and_drops_nothing(self):
        self.run_check(PLEXSTALE_MAX_MISSING=0.05)
        self.assertEqual(FakePlex.calls, [])

    def test_dry_run_touches_nothing(self):
        self.run_check(PLEXSTALE_MAX_MISSING=0.9, PLEXSTALE_DRY=True)
        self.assertEqual(FakePlex.calls, [])

    def test_a_failed_drop_still_turns_deletion_off(self):
        class Failing(FakePlex):
            def _send(self, method, path):
                self.calls.append((method, path))
                if method == "DELETE":
                    raise OSError("plex down")
                return 200
        with self.assertRaises(OSError):
            self.run_check(PLEXSTALE_MAX_MISSING=0.9, Plex=Failing)
        self.assertEqual(FakePlex.calls[-1], ("PUT", "/:/prefs?allowMediaDeletion=0"))


if __name__ == "__main__":
    unittest.main()
