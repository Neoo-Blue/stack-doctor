"""Tests for Arr.history_grabbed — timestamp comparison correctness.

These tests target the ISO-8601 timestamp comparison bug (A3) where string
lexicographic comparison fails when *arr API dates include milliseconds or
timezone offsets in a different format than the stored search_ts.
"""
from unittest.mock import patch
import unittest
from doctor.clients import Arr


def _make_arr(kind="sonarr"):
    arr = Arr.__new__(Arr)
    arr.name = "test"
    arr.kind = kind
    arr.base = "http://localhost/api/v3"
    arr.apikey = "test"
    return arr


def _make_rec(date, event_type="grabbed", episode_id=None):
    rec = {"eventType": event_type, "date": date, "sourceTitle": "Foo.S01E01"}
    if episode_id is not None:
        rec["episodeId"] = episode_id
    return rec


class HistoryGrabbedTimestampTest(unittest.TestCase):
    """history_grabbed must correctly identify records *after* since_ts across
    all common *arr date formats."""

    def _call(self, arr, records, since_ts, entity_ids=None):
        with patch.object(arr, "history", return_value=records):
            return arr.history_grabbed(1, since_ts, entity_ids)

    # --- Formats that must be recognised as AFTER the search timestamp ---

    def test_same_format_z_suffix_after(self):
        """Record with matching Z-suffix format, newer than search_ts."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T05:00:00Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNotNone(result)

    def test_milliseconds_after(self):
        """Record with milliseconds (.123Z), newer than plain search_ts."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T05:00:00.123Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNotNone(result)

    def test_plus_offset_after(self):
        """Record with +00:00 offset (common *arr format), newer than search_ts."""
        arr = _make_arr()
        # 05:00:00+00:00 is 5 minutes after 04:55:00Z — must be found
        rec = _make_rec("2026-06-23T05:00:00+00:00")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNotNone(result)

    def test_milliseconds_same_second_after(self):
        """A record at T04:55:30.5Z is newer than search_ts T04:55:30Z.
        String compare bug: ord('.')=46 < ord('Z')=90, so '30.5Z' < '30Z'
        lexicographically -> record is incorrectly skipped without the fix."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T04:55:30.500Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:30Z")
        self.assertIsNotNone(result)

    def test_plus_offset_with_ms_after(self):
        """Record with milliseconds AND +00:00 offset, newer than search_ts."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T05:00:00.456+00:00")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNotNone(result)

    # --- Formats that must be recognised as BEFORE/EQUAL and skipped ---

    def test_same_format_z_suffix_before(self):
        arr = _make_arr()
        rec = _make_rec("2026-06-23T04:50:00Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    def test_milliseconds_before(self):
        arr = _make_arr()
        rec = _make_rec("2026-06-23T04:50:00.999Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    def test_plus_offset_before(self):
        """04:50:00+00:00 is before 04:55:00Z — must be skipped."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T04:50:00+00:00")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    def test_equal_timestamp_is_skipped(self):
        """Equal timestamp must NOT be returned (strictly after only)."""
        arr = _make_arr()
        rec = _make_rec("2026-06-23T04:55:00Z")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    # --- Non-grabbed events always skipped ---

    def test_non_grabbed_event_skipped(self):
        arr = _make_arr()
        rec = _make_rec("2026-06-23T05:00:00Z", event_type="downloadFolderImported")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    # --- entity_ids filter (sonarr) ---

    def test_sonarr_entity_id_filter_match(self):
        arr = _make_arr(kind="sonarr")
        rec = _make_rec("2026-06-23T05:00:00Z", episode_id=42)
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z", entity_ids=[42])
        self.assertIsNotNone(result)

    def test_sonarr_entity_id_filter_no_match(self):
        arr = _make_arr(kind="sonarr")
        rec = _make_rec("2026-06-23T05:00:00Z", episode_id=99)
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z", entity_ids=[42])
        self.assertIsNone(result)

    def test_radarr_ignores_entity_ids(self):
        """Radarr does not filter by episode IDs; any grab after ts is returned."""
        arr = _make_arr(kind="radarr")
        rec = _make_rec("2026-06-23T05:00:00Z", episode_id=99)
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z", entity_ids=[42])
        self.assertIsNotNone(result)

    # --- Defensive fallback for unparseable dates ---

    def test_unparseable_date_falls_back_gracefully(self):
        """A record with a garbage date field should not crash; should be skipped."""
        arr = _make_arr()
        rec = _make_rec("not-a-date")
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)

    def test_missing_date_field_skipped(self):
        """A record with no date field should be skipped."""
        arr = _make_arr()
        rec = {"eventType": "grabbed", "sourceTitle": "Foo"}
        result = self._call(arr, [rec], "2026-06-23T04:55:00Z")
        self.assertIsNone(result)
