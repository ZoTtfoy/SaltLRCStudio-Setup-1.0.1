"""Tests for audio identification and lyric-source resolution (offline)."""

import tempfile
import unittest
from pathlib import Path

from saltlrc import inspect


class ParseFilenameTests(unittest.TestCase):
    def test_artist_dash_title(self):
        parsed = inspect.parse_filename("D:/Music/あたらよ - また夏を追う.mp3")
        self.assertEqual(parsed["artist"], "あたらよ")
        self.assertEqual(parsed["title"], "また夏を追う")

    def test_track_number_and_brackets_are_stripped(self):
        parsed = inspect.parse_filename("07. Porter Robinson - Shelter (Piano version).flac")
        self.assertEqual(parsed["artist"], "Porter Robinson")
        self.assertEqual(parsed["title"], "Shelter")

    def test_title_only(self):
        parsed = inspect.parse_filename("summertime.mp3")
        self.assertEqual(parsed["title"], "summertime")
        self.assertEqual(parsed["artist"], "")

    def test_bracketed_annotations_are_stripped(self):
        # "(MONACA)" is an annotation, and dropping it only improves matching.
        parsed = inspect.parse_filename("神前暁,髙田龍一（MONACA) - Fluorite Eye's Song.mp3")
        self.assertEqual(parsed["artist"], "神前暁,髙田龍一")
        self.assertEqual(parsed["title"], "Fluorite Eye's Song")

    def test_empty_name_is_safe(self):
        self.assertEqual(inspect.parse_filename("")["title"], "")


def track(title, artist, path, duration=200000, has_lyric=False):
    return {"title": title, "artist": artist, "album": "", "path": path,
            "lyric_path": str(Path(path).with_suffix(".lrc")), "has_lyric": has_lyric,
            "duration": duration, "readable": True, "id": path}


class FindTrackTests(unittest.TestCase):
    def setUp(self):
        self.tracks = [
            track("また夏を追う", "あたらよ", "D:\\Music\\あたらよ - また夏を追う.mp3", 287582, True),
            track("Shelter", "Porter Robinson", "D:\\Music\\Porter Robinson - Shelter.mp3", 219010),
        ]

    def test_exact_file_name_wins(self):
        found, how = inspect.find_track(self.tracks, "あたらよ - また夏を追う.mp3", 287582)
        self.assertEqual(how, "exact")
        self.assertEqual(found["title"], "また夏を追う")

    def test_fuzzy_needs_matching_duration(self):
        found, how = inspect.find_track(self.tracks, "wav/また夏を追う.wav", 287000)
        self.assertEqual(how, "fuzzy")
        self.assertEqual(found["title"], "また夏を追う")

    def test_fuzzy_rejects_duration_mismatch(self):
        found, _ = inspect.find_track(self.tracks, "また夏を追う.mp3", 60000)
        self.assertIsNone(found)

    def test_unknown_file_is_not_matched(self):
        found, how = inspect.find_track(self.tracks, "完全に知らない曲.mp3", 200000)
        self.assertIsNone(found)
        self.assertIsNone(how)


class MatchTests(unittest.TestCase):
    def test_local_lyric_short_circuits_the_network(self):
        with tempfile.TemporaryDirectory() as root:
            audio = Path(root) / "あたらよ - また夏を追う.mp3"
            audio.write_bytes(b"x")
            audio.with_suffix(".lrc").write_text("[00:01.000]梅雨明け\n", encoding="utf-8")
            result = inspect.match(str(audio), 287582, db_path=None, search=False)
            # db_path None means "read the real library"; force an empty one instead.
        self.assertIn("notes", result)
        self.assertEqual(result["candidates"], [])

    def test_search_disabled_reports_no_candidates(self):
        result = inspect.match("unknown song.mp3", 100000, search=False)
        self.assertEqual(result["auto"], None)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["parsed"]["title"], "unknown song")


if __name__ == "__main__":
    unittest.main()
