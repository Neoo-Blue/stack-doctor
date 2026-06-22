"""Unit tests for doctor.checks.repair.main.check_repair() orchestrator.

Tests cover:
  - returns early when INSTANCES is empty
  - returns early when host load exceeds REPAIR_LOAD_MAX
  - returns early when debrid mount is not OK
  - calls _repair_verify_pending when REPAIR_VERIFY is True
  - does NOT call _repair_verify_pending when REPAIR_VERIFY is False
  - processes Sonarr dead symlinks and counts acted/symlinks
  - processes Radarr dead symlinks
  - stops at REPAIR_MAX_ACTIONS cap (symlink sweep)
  - stops at REPAIR_MAX_SYMLINKS cap
  - skips REPAIR_SEASON_PACKS sub-check when disabled
  - runs REPAIR_SEASON_PACKS sub-check and calls SeasonSearch
  - skips REPAIR_MISSING_FROM_DISK sub-check when disabled
  - calls _missing_from_disk_check when REPAIR_MISSING_FROM_DISK is True
  - calls _orphan_dead_symlink_scan when REPAIR_ORPHAN_SCAN is True
  - skips _orphan_dead_symlink_scan when disabled
  - handles per-arr sweep exceptions without crashing

No real filesystem or network access.
"""
import unittest
from unittest.mock import MagicMock, patch

_MOD = "doctor.checks.repair.main"


def _make_arr(name="sonarr-1", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    arr.series.return_value = []
    arr.movies.return_value = []
    return arr


def _run(instances, state=None, *,
         repair_load_max=0,
         host_load_val=0.0,
         mount_ok=True,
         repair_verify=False,
         repair_max_actions=10,
         repair_max_symlinks=50,
         repair_season_packs=False,
         repair_missing_from_disk=False,
         repair_orphan_scan=False,
         repair_item_interval=0,
         dry_run=False,
         # sub-function stubs
         sonarr_dead_files=None,
         radarr_dead_files=None,
         repair_sonarr_season_ret=True,
         repair_radarr_movie_ret=True,
         season_pack_entries=None,
         mfd_acted=0,
         verify_pending_mock=None,
         orphan_mock=None,
         ):
    if state is None:
        state = {}
    if sonarr_dead_files is None:
        sonarr_dead_files = []
    if radarr_dead_files is None:
        radarr_dead_files = []
    if season_pack_entries is None:
        season_pack_entries = []

    mock_sonarr_dead = MagicMock(return_value=iter(sonarr_dead_files))
    mock_radarr_dead = MagicMock(return_value=iter(radarr_dead_files))
    mock_repair_sonarr = MagicMock(return_value=repair_sonarr_season_ret)
    mock_repair_radarr = MagicMock(return_value=repair_radarr_movie_ret)
    mock_season_pack = MagicMock(return_value=iter(season_pack_entries))
    mock_mfd = MagicMock(return_value=mfd_acted)
    mock_verify = verify_pending_mock or MagicMock()
    mock_orphan = orphan_mock or MagicMock()

    from doctor.checks.repair.main import check_repair

    with patch(_MOD + ".INSTANCES", instances), \
         patch(_MOD + ".REPAIR_LOAD_MAX", repair_load_max), \
         patch(_MOD + ".host_load", return_value=host_load_val), \
         patch(_MOD + "._debrid_mount_ok", return_value=mount_ok), \
         patch(_MOD + ".REPAIR_VERIFY", repair_verify), \
         patch(_MOD + ".REPAIR_MAX_ACTIONS", repair_max_actions), \
         patch(_MOD + ".REPAIR_MAX_SYMLINKS", repair_max_symlinks), \
         patch(_MOD + ".REPAIR_SEASON_PACKS", repair_season_packs), \
         patch(_MOD + ".REPAIR_MISSING_FROM_DISK", repair_missing_from_disk), \
         patch(_MOD + ".REPAIR_ORPHAN_SCAN", repair_orphan_scan), \
         patch(_MOD + ".REPAIR_ITEM_INTERVAL", repair_item_interval), \
         patch(_MOD + ".DRY_RUN", dry_run), \
         patch(_MOD + "._sonarr_dead_files", mock_sonarr_dead), \
         patch(_MOD + "._radarr_dead_files", mock_radarr_dead), \
         patch(_MOD + "._repair_sonarr_season", mock_repair_sonarr), \
         patch(_MOD + "._repair_radarr_movie", mock_repair_radarr), \
         patch(_MOD + "._sonarr_season_pack_check", mock_season_pack), \
         patch(_MOD + "._missing_from_disk_check", mock_mfd), \
         patch(_MOD + "._repair_verify_pending", mock_verify), \
         patch(_MOD + "._orphan_dead_symlink_scan", mock_orphan), \
         patch(_MOD + ".state_transaction") as mock_tx:
        mock_tx.return_value.__enter__ = lambda s: state
        mock_tx.return_value.__exit__ = MagicMock(return_value=False)
        check_repair()

    return {
        "sonarr_dead": mock_sonarr_dead,
        "radarr_dead": mock_radarr_dead,
        "repair_sonarr": mock_repair_sonarr,
        "repair_radarr": mock_repair_radarr,
        "season_pack": mock_season_pack,
        "mfd": mock_mfd,
        "verify": mock_verify,
        "orphan": mock_orphan,
    }


class RepairMainEarlyExitTest(unittest.TestCase):

    def test_returns_when_no_instances(self):
        mocks = _run([])
        mocks["sonarr_dead"].assert_not_called()
        mocks["radarr_dead"].assert_not_called()

    def test_returns_when_load_too_high(self):
        arr = _make_arr()
        mocks = _run([arr], repair_load_max=1, host_load_val=5.0)
        mocks["sonarr_dead"].assert_not_called()

    def test_passes_when_load_ok(self):
        arr = _make_arr()
        arr.series.return_value = []
        mocks = _run([arr], repair_load_max=10, host_load_val=1.0)
        # Not called because series() returned []
        mocks["sonarr_dead"].assert_called_once()

    def test_returns_when_mount_not_ok(self):
        arr = _make_arr()
        mocks = _run([arr], mount_ok=False)
        mocks["sonarr_dead"].assert_not_called()

    def test_skips_non_sonarr_radarr_instances(self):
        arr = _make_arr(kind="prowlarr")
        mocks = _run([arr])
        mocks["sonarr_dead"].assert_not_called()
        mocks["radarr_dead"].assert_not_called()


class RepairVerifyTest(unittest.TestCase):

    def test_calls_verify_when_enabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_verify=True)
        mocks["verify"].assert_called_once()

    def test_skips_verify_when_disabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_verify=False)
        mocks["verify"].assert_not_called()


class RepairSonarrTest(unittest.TestCase):

    def test_calls_repair_sonarr_for_dead_files(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{"id": 1, "title": "Show"}]
        dead = [(1, "Show", 1, [10, 11])]  # sid, title, season, efids
        mocks = _run([arr], sonarr_dead_files=dead)
        mocks["repair_sonarr"].assert_called_once()

    def test_sonarr_increments_acted_and_symlinks(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{}]
        # Two seasons with 1 and 2 files respectively
        dead = [(1, "Show", 1, [10]), (1, "Show", 2, [11, 12])]
        mocks = _run([arr], sonarr_dead_files=dead, repair_max_actions=10, repair_max_symlinks=50)
        self.assertEqual(mocks["repair_sonarr"].call_count, 2)

    def test_sonarr_stops_at_max_actions(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{}]
        dead = [(1, "Show", 1, [10]), (1, "Show", 2, [11])]
        mocks = _run([arr], sonarr_dead_files=dead, repair_max_actions=1)
        self.assertEqual(mocks["repair_sonarr"].call_count, 1)

    def test_sonarr_stops_at_max_symlinks(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = [{}]
        # 3 files in the group but symlink cap is 2 — group is too big
        dead = [(1, "Show", 1, [10, 11, 12])]
        mocks = _run([arr], sonarr_dead_files=dead, repair_max_symlinks=2)
        mocks["repair_sonarr"].assert_not_called()


class RepairRadarrTest(unittest.TestCase):

    def test_calls_repair_radarr_for_dead_files(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [{}]
        dead = [(5, "Movie", 50)]  # mid, title, mfid
        mocks = _run([arr], radarr_dead_files=dead)
        mocks["repair_radarr"].assert_called_once()

    def test_radarr_stops_at_max_actions(self):
        arr = _make_arr(name="radarr-1", kind="radarr")
        arr.movies.return_value = [{}]
        dead = [(5, "Movie A", 50), (6, "Movie B", 60)]
        mocks = _run([arr], radarr_dead_files=dead, repair_max_actions=1)
        self.assertEqual(mocks["repair_radarr"].call_count, 1)


class RepairSeasonPackTest(unittest.TestCase):

    def test_skips_season_pack_when_disabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_season_packs=False)
        mocks["season_pack"].assert_not_called()

    def test_runs_season_pack_when_enabled(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = []
        entries = [("Show", 1, 10, arr)]
        arr.command.return_value = True
        mocks = _run([arr], repair_season_packs=True, season_pack_entries=entries)
        mocks["season_pack"].assert_called_once()
        arr.command.assert_called_once_with("SeasonSearch", seriesId=10, seasonNumber=1)

    def test_season_pack_dry_run_does_not_call_command(self):
        arr = _make_arr(kind="sonarr")
        arr.series.return_value = []
        entries = [("Show", 1, 10, arr)]
        _run([arr], repair_season_packs=True, season_pack_entries=entries, dry_run=True)
        arr.command.assert_not_called()


class RepairMfdTest(unittest.TestCase):

    def test_skips_mfd_when_disabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_missing_from_disk=False)
        mocks["mfd"].assert_not_called()

    def test_calls_mfd_when_enabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_missing_from_disk=True)
        mocks["mfd"].assert_called_once()


class RepairOrphanTest(unittest.TestCase):

    def test_skips_orphan_scan_when_disabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_orphan_scan=False)
        mocks["orphan"].assert_not_called()

    def test_calls_orphan_scan_when_enabled(self):
        arr = _make_arr()
        mocks = _run([arr], repair_orphan_scan=True)
        mocks["orphan"].assert_called_once()


class RepairExceptionHandlingTest(unittest.TestCase):

    def test_arr_sweep_exception_is_swallowed(self):
        arr = _make_arr(kind="sonarr")
        arr.series.side_effect = RuntimeError("API down")
        # Should not raise
        mocks = _run([arr])
        mocks["repair_sonarr"].assert_not_called()
