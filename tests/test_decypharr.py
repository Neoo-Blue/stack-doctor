"""Unit tests for the FUSE mount health check (decypharr.py)."""
import errno
import os
import tempfile
import threading
import time
import unittest

from doctor.checks.decypharr import (
    _is_fuse_errno,
    _mount_registered,
    _probe_statvfs,
    _probe_mount,
    _record_fuse_result,
    _fuse_strikes,
    _FuseStatus,
)


class IsFuseErrnoTest(unittest.TestCase):
    """_is_fuse_errno identifies FUSE-dead errors by errno and message."""

    def test_eio_errno(self):
        self.assertTrue(_is_fuse_errno(OSError(5, "Input/output error")))

    def test_enotconn_errno(self):
        self.assertTrue(_is_fuse_errno(OSError(107, "Transport endpoint is not connected")))

    def test_enxio_errno(self):
        self.assertTrue(_is_fuse_errno(OSError(6, "No such device or address")))

    def test_message_transport(self):
        self.assertTrue(_is_fuse_errno(OSError(0, "transport endpoint is not connected")))

    def test_message_socket(self):
        self.assertTrue(_is_fuse_errno(OSError(0, "Socket not connected")))

    def test_ordinary_enoent(self):
        self.assertFalse(_is_fuse_errno(OSError(errno.ENOENT, "No such file or directory")))

    def test_ordinary_eperm(self):
        self.assertFalse(_is_fuse_errno(OSError(errno.EPERM, "Operation not permitted")))

    def test_non_oserror(self):
        self.assertFalse(_is_fuse_errno(ValueError("nope")))


class MountRegisteredTest(unittest.TestCase):
    """_mount_registered reads /proc/mounts to find a FUSE mount for a path."""

    def test_nonexistent_path_not_under_fuse(self):
        # A completely invented path cannot be under any real FUSE mount.
        self.assertFalse(_mount_registered("/this/path/definitely/does/not/exist/xyz"))

    def test_zurg_path_registered(self):
        # /mnt/zurg is the actual FUSE mount inside the container;
        # /mnt/zurg/__all__ is a subdirectory – both should register as True.
        # If this test runs outside the container or without the mount, skip it.
        try:
            with open("/proc/mounts") as f:
                text = f.read()
            if "/mnt/zurg" not in text:
                self.skipTest("/mnt/zurg not mounted in this environment")
        except Exception:
            self.skipTest("cannot read /proc/mounts")
        self.assertTrue(_mount_registered("/mnt/zurg"))
        self.assertTrue(_mount_registered("/mnt/zurg/__all__"))

    def test_ancestor_walk(self):
        """A child of a FUSE mount should return True even without exact match."""
        # We'll temporarily fake /proc/mounts by monkey-patching the open call.
        import builtins
        original_open = builtins.open
        fake_mounts = "rclone /mnt/fake fuse.rclone rw 0 0\n"
        class FakeFile:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def __iter__(self): return iter(fake_mounts.splitlines(keepends=True))
        def fake_open(path, *a, **kw):
            if path == "/proc/mounts":
                return FakeFile()
            return original_open(path, *a, **kw)
        builtins.open = fake_open
        try:
            self.assertTrue(_mount_registered("/mnt/fake/subdir/deep"))
            self.assertTrue(_mount_registered("/mnt/fake"))
            self.assertFalse(_mount_registered("/mnt/other"))
        finally:
            builtins.open = original_open


class ProbeStatvfsTest(unittest.TestCase):
    """_probe_statvfs returns OK for real accessible paths."""

    def test_real_path_ok(self):
        status, _ = _probe_statvfs("/tmp", timeout=5)
        self.assertEqual(status, _FuseStatus.OK)

    def test_nonexistent_path_unknown(self):
        status, detail = _probe_statvfs("/no/such/path/xyz", timeout=5)
        self.assertEqual(status, _FuseStatus.UNKNOWN)
        self.assertTrue(len(detail) > 0)

    def test_timeout_returns_hung(self):
        """Monkey-patch os.statvfs to block; probe should return HUNG after timeout."""
        original = os.statvfs
        barrier = threading.Event()
        def _blocking(path):
            barrier.wait(10)
            return original(path)
        os.statvfs = _blocking
        try:
            t0 = time.monotonic()
            status, _ = _probe_statvfs("/tmp", timeout=1)
            elapsed = time.monotonic() - t0
            self.assertEqual(status, _FuseStatus.HUNG)
            self.assertLess(elapsed, 5)
        finally:
            os.statvfs = original
            barrier.set()


class StrikeCounterTest(unittest.TestCase):
    """_record_fuse_result increments / resets the strike counter correctly."""

    def setUp(self):
        _fuse_strikes.reset()

    def test_ok_resets_strikes(self):
        _fuse_strikes.value = 3
        strikes, act = _record_fuse_result(_FuseStatus.OK)
        self.assertEqual(strikes, 0)
        self.assertFalse(act)
        self.assertEqual(_fuse_strikes.value, 0)

    def test_empty_resets_strikes(self):
        _fuse_strikes.value = 2
        strikes, act = _record_fuse_result(_FuseStatus.EMPTY)
        self.assertEqual(strikes, 0)
        self.assertFalse(act)

    def test_dead_increments_strikes(self):
        s, a = _record_fuse_result(_FuseStatus.DEAD)
        self.assertEqual(s, 1)
        self.assertFalse(a)  # default DECY_FUSE_STRIKES=2, 1 hit is not enough

    def test_consecutive_dead_triggers_action(self):
        from doctor.config import DECY_FUSE_STRIKES
        for i in range(DECY_FUSE_STRIKES - 1):
            strikes, act = _record_fuse_result(_FuseStatus.DEAD)
            self.assertFalse(act, "should not act on strike %d/%d" % (i + 1, DECY_FUSE_STRIKES))
        strikes, act = _record_fuse_result(_FuseStatus.DEAD)
        self.assertTrue(act, "should act after %d consecutive failures" % DECY_FUSE_STRIKES)

    def test_reset_between_failures_prevents_action(self):
        _record_fuse_result(_FuseStatus.DEAD)    # strike 1
        _record_fuse_result(_FuseStatus.OK)      # reset
        strikes, act = _record_fuse_result(_FuseStatus.DEAD)  # strike 1 again
        self.assertEqual(strikes, 1)
        self.assertFalse(act)

    def test_hung_also_increments(self):
        s, _ = _record_fuse_result(_FuseStatus.HUNG)
        self.assertEqual(s, 1)

    def test_unknown_also_increments(self):
        s, _ = _record_fuse_result(_FuseStatus.UNKNOWN)
        self.assertEqual(s, 1)

    def test_unmounted_also_increments(self):
        s, _ = _record_fuse_result(_FuseStatus.UNMOUNTED)
        self.assertEqual(s, 1)


class ProbeMountTest(unittest.TestCase):
    """_probe_mount integration tests using real local filesystem."""

    def setUp(self):
        _fuse_strikes.reset()

    def test_nonexistent_path_unmounted(self):
        # Completely invented path with no matching FUSE ancestor -> UNMOUNTED
        status, detail = _probe_mount("/no/such/mount/point/xyz/abc", read_timeout=5)
        self.assertEqual(status, _FuseStatus.UNMOUNTED)

    def test_statvfs_layer_works_on_tmp(self):
        status, detail = _probe_statvfs("/tmp", timeout=5)
        self.assertEqual(status, _FuseStatus.OK, detail)

    def test_read_layer_with_real_file(self):
        """Layer 3: create a real .mkv, confirm _read_file returns OK."""
        from doctor.checks.decypharr import _read_file
        with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as fh:
            fh.write(b"\x00" * 65536)
            fpath = fh.name
        try:
            status, detail = _read_file(fpath, timeout=5)
            self.assertEqual(status, _FuseStatus.OK, detail)
        finally:
            os.unlink(fpath)

    def test_read_layer_nonexistent_file_unknown(self):
        from doctor.checks.decypharr import _read_file
        status, detail = _read_file("/no/such/file.mkv", timeout=5)
        self.assertEqual(status, _FuseStatus.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
