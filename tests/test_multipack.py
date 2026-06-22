"""Unit tests for check_multipack helper functions."""
import unittest

from doctor.checks.multipack import (
    _pack_season_range,
    _incomplete_seasons_covered,
    _rank_packs,
)
from doctor.checks.missing_seasons import searched_series as _series_searched_by_missing_seasons


class PackSeasonRangeTest(unittest.TestCase):
    def test_standard_dash(self):
        self.assertEqual(_pack_season_range("Show S01-S05 BluRay"), (1, 5))

    def test_dot_separator(self):
        self.assertEqual(_pack_season_range("Show.S01.S03.1080p"), (1, 3))

    def test_single_digit_seasons(self):
        self.assertEqual(_pack_season_range("Show S1-S3 WEB"), (1, 3))

    def test_reversed_order_normalized(self):
        # S05-S01 should normalize to (1, 5)
        self.assertEqual(_pack_season_range("Show S05-S01 Pack"), (1, 5))

    def test_no_match_returns_none(self):
        self.assertIsNone(_pack_season_range("Show S01E01 Episode"))
        self.assertIsNone(_pack_season_range("Show Season 1 Complete"))
        self.assertIsNone(_pack_season_range(""))

    def test_two_season_range(self):
        self.assertEqual(_pack_season_range("Billions S01-S02 1080p"), (1, 2))

    def test_parentheses_in_title(self):
        self.assertEqual(
            _pack_season_range("The Last Ship (2014) S01-S05 (1080p BluRay x265)"),
            (1, 5)
        )

    def test_complete_keyword_prefix(self):
        self.assertEqual(_pack_season_range("Show Complete S01-S07 WEB"), (1, 7))


class IncompleteSeasonsCovers(unittest.TestCase):
    def test_full_overlap(self):
        # Pack S01-S05, all 5 seasons incomplete
        self.assertEqual(_incomplete_seasons_covered((1, 5), {1, 2, 3, 4, 5}), 5)

    def test_partial_overlap(self):
        # Pack S01-S06, only S06+S07 incomplete -> covers 1
        self.assertEqual(_incomplete_seasons_covered((1, 6), {6, 7}), 1)

    def test_no_overlap(self):
        # Pack S01-S04, only S05-S07 incomplete -> covers 0
        self.assertEqual(_incomplete_seasons_covered((1, 4), {5, 6, 7}), 0)

    def test_exact_match(self):
        # Pack S06-S07, exactly those two missing
        self.assertEqual(_incomplete_seasons_covered((6, 7), {6, 7}), 2)

    def test_wider_than_needed(self):
        # Pack S01-S07, only S03 and S05 incomplete -> covers 2
        self.assertEqual(_incomplete_seasons_covered((1, 7), {3, 5}), 2)

    def test_single_season_pack_matches(self):
        # Edge: S03-S03 range (shouldn't normally appear, but safe)
        self.assertEqual(_incomplete_seasons_covered((3, 3), {3, 5}), 1)


def _make_pack(title, quality_weight=1000):
    return {"title": title, "fullSeason": True, "qualityWeight": quality_weight}


class RankPacksTest(unittest.TestCase):
    def _titles(self, packs, incomplete):
        return [p["title"] for p, _pr, _cov in _rank_packs(packs, incomplete)]

    def test_zero_overlap_excluded(self):
        # S01-S04 pack is useless when only S05-S07 are missing
        packs = [_make_pack("Show S01-S04 1080p")]
        self.assertEqual(_rank_packs(packs, {5, 6, 7}), [])

    def test_most_coverage_first(self):
        # S01-S07 covers more missing seasons than S01-S06
        packs = [
            _make_pack("Show S01-S06 1080p"),
            _make_pack("Show S01-S07 1080p"),
        ]
        incomplete = {6, 7}
        titles = self._titles(packs, incomplete)
        self.assertEqual(titles[0], "Show S01-S07 1080p")  # covers both S06+S07

    def test_wider_pack_preferred_on_equal_coverage(self):
        # Both cover the same 1 missing season (S06), but S01-S07 is wider
        packs = [
            _make_pack("Show S01-S06 1080p"),
            _make_pack("Show S01-S07 1080p"),
        ]
        incomplete = {6}  # only S06 missing
        titles = self._titles(packs, incomplete)
        # Both cover 1 missing season; S01-S07 is wider -> comes first
        self.assertEqual(titles[0], "Show S01-S07 1080p")

    def test_quality_weight_tiebreaker(self):
        # Same coverage, same width -> higher qualityWeight wins
        packs = [
            _make_pack("Show S01-S05 WEB", quality_weight=800),
            _make_pack("Show S01-S05 BluRay", quality_weight=1200),
        ]
        incomplete = {3, 5}
        titles = self._titles(packs, incomplete)
        self.assertEqual(titles[0], "Show S01-S05 BluRay")

    def test_mixed_useful_and_useless(self):
        # One pack covers nothing, two cover different amounts
        packs = [
            _make_pack("Show S01-S02 1080p"),   # useless: only S05-S07 missing
            _make_pack("Show S01-S06 1080p"),   # covers S05+S06
            _make_pack("Show S01-S07 1080p"),   # covers S05+S06+S07
        ]
        incomplete = {5, 6, 7}
        titles = self._titles(packs, incomplete)
        self.assertEqual(len(titles), 2)        # S01-S02 excluded
        self.assertEqual(titles[0], "Show S01-S07 1080p")  # most coverage

    def test_empty_packs(self):
        self.assertEqual(_rank_packs([], {1, 2, 3}), [])

    def test_empty_incomplete(self):
        packs = [_make_pack("Show S01-S05 1080p")]
        self.assertEqual(_rank_packs(packs, set()), [])


class SeriesSearchedByMissingSeasons(unittest.TestCase):
    def test_parses_state_keys(self):
        state = {
            "__missing_seasons__": {
                "sonarr:17:1": 1000.0,
                "sonarr:17:2": 1000.0,
                "sonarr:42:3": 1000.0,
                "sonarr:99:1": 1000.0,
            }
        }
        ids = _series_searched_by_missing_seasons(state, "sonarr")
        self.assertEqual(ids, {17, 42, 99})

    def test_different_arr_name_ignored(self):
        state = {
            "__missing_seasons__": {
                "sonarr:17:1": 1000.0,
                "radarr:99:1": 1000.0,
            }
        }
        ids = _series_searched_by_missing_seasons(state, "sonarr")
        self.assertEqual(ids, {17})

    def test_empty_state(self):
        self.assertEqual(_series_searched_by_missing_seasons({}, "sonarr"), set())

    def test_no_missing_seasons_key(self):
        state = {"__multipack__": {}}
        self.assertEqual(_series_searched_by_missing_seasons(state, "sonarr"), set())


if __name__ == "__main__":
    unittest.main()
