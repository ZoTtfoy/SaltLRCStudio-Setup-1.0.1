"""Layer 1 — structural edits (saltlrc/edits.py).

The fixtures are deliberately identical to tests/web_document_check.js: the
same numbers, the same expectations. If one side changes a rule, the other side
fails instead of silently drifting apart.
"""

import unittest

from saltlrc.document import Document, DocumentError, Group, Track, Word, check, clone, track_start
from saltlrc import edits


def fixture(starts=(1000, 3000, 9000)):
    return Document([Group(start, Track()) for start in starts])


def starts(doc):
    return [g.start for g in doc.groups]


class EditsTests(unittest.TestCase):
    def test_insert_between_two_lines_takes_the_midpoint(self):
        doc = fixture()
        edits.insert_group(doc, 1)
        self.assertEqual(starts(doc), [1000, 2000, 3000, 9000])
        self.assertEqual(doc.groups[1].main.text, '')
        self.assertEqual(doc.groups[1].main.words, [])

    def test_insert_before_the_first_line(self):
        doc = fixture()
        edits.insert_group(doc, 0)
        self.assertEqual(starts(doc)[0], 0)

    def test_insert_at_the_end_uses_the_gap(self):
        doc = fixture()
        edits.insert_group(doc, 3, duration=12000)
        self.assertEqual(starts(doc), [1000, 3000, 9000, 10000])

    def test_insert_at_the_end_is_clamped_into_the_audio(self):
        doc = fixture()
        edits.insert_group(doc, 3, duration=9500)
        self.assertEqual(starts(doc), [1000, 3000, 9000, 9499])

    def test_insert_refuses_when_there_is_no_room(self):
        doc = fixture((1000, 1001))
        with self.assertRaises(DocumentError) as caught:
            edits.insert_group(doc, 1)
        self.assertIn('中间插不进新行', str(caught.exception))

    def test_insert_refuses_past_the_audio_end(self):
        doc = fixture((1000, 8000))
        with self.assertRaises(DocumentError):
            edits.insert_group(doc, 2, start=9000, duration=5000)

    def test_insert_accepts_an_explicit_start(self):
        doc = fixture()
        edits.insert_group(doc, 1, start=1500)
        self.assertEqual(starts(doc), [1000, 1500, 3000, 9000])

    def test_insert_between_lines_stays_clear_of_the_previous_sung_span(self):
        """上一行写明了句尾时，新行的起点必须排在它后面。

        夹具里两行是 1000 与 3000，中点是 2000 —— 但上一行唱到 2500，
        所以新行只能落在 2501，不能落进"已经唱过的那一段"。
        """
        doc = fixture()
        doc.groups[0].main.end = 2500
        edits.insert_group(doc, 1)
        self.assertEqual(starts(doc), [1000, 2501, 3000, 9000])

    def test_insert_between_lines_takes_the_later_of_the_two_tracks(self):
        """两条轨道各有句尾时，按更晚的那个避让（与校验器口径一致）。"""
        doc = fixture()
        doc.groups[0].main.end = 2000
        doc.groups[0].translation.end = 2600
        edits.insert_group(doc, 1)
        self.assertEqual(starts(doc), [1000, 2601, 3000, 9000])

    def test_insert_at_the_end_lands_after_the_previous_end(self):
        doc = fixture()
        doc.groups[2].main.end = 11000
        edits.insert_group(doc, 3, duration=None)
        self.assertEqual(starts(doc), [1000, 3000, 9000, 12000])

    def test_insert_refuses_when_the_previous_line_sings_past_the_next(self):
        """上一行唱过了下一行的起点时，中间确实没有位置 —— 说清怎么办，不要硬塞。"""
        doc = fixture((1000, 3000))
        doc.groups[0].main.end = 5000
        with self.assertRaises(DocumentError) as caught:
            edits.insert_group(doc, 1)
        self.assertIn('中间插不进新行', str(caught.exception))
        self.assertIn('句尾往前收', str(caught.exception))

    def test_delete_keeps_the_order(self):
        doc = fixture()
        edits.delete_group(doc, 1)
        self.assertEqual(starts(doc), [1000, 9000])

    def test_delete_rejects_a_missing_line(self):
        with self.assertRaises(DocumentError):
            edits.delete_group(fixture(), 9)

    def test_sort_is_stable_and_leaves_one_note(self):
        doc = fixture((3000, 1000, 9000))
        edits.sort_groups(doc)
        self.assertEqual(starts(doc), [1000, 3000, 9000])
        self.assertEqual(len(doc.notes), 1)
        edits.sort_groups(doc)
        self.assertEqual(len(doc.notes), 1, 'already sorted -> no second note')


class InvariantTests(unittest.TestCase):
    """The rules an edit may never break (saltlrc/document.py)."""

    def test_line_starts_must_increase(self):
        doc = fixture((1000, 1000))
        with self.assertRaises(DocumentError) as caught:
            check(doc)
        self.assertIn('递增', str(caught.exception))

    def test_track_timeline_must_increase(self):
        doc = fixture((1000,))
        doc.groups[0].main.words = [Word(1000, 'a'), Word(900, 'b')]
        with self.assertRaises(DocumentError) as caught:
            check(doc)
        self.assertIn('递增', str(caught.exception))

    def test_first_word_sits_on_the_track_start(self):
        doc = fixture((1000,))
        doc.groups[0].main.words = [Word(1200, 'a')]
        with self.assertRaises(DocumentError) as caught:
            check(doc)
        self.assertIn('首个逐字时间', str(caught.exception))

    def test_timestamps_stay_inside_the_audio(self):
        doc = fixture((1000,))
        doc.groups[0].main.end = 50000
        with self.assertRaises(DocumentError) as caught:
            check(doc, 12000)
        self.assertIn('音频时长', str(caught.exception))

    def test_clone_is_a_deep_copy(self):
        doc = fixture()
        doc.groups[0].main.text = '你好'
        copy = clone(doc)
        copy.groups[0].main.text = '月亮'
        self.assertEqual(doc.groups[0].main.text, '你好')
        self.assertEqual(track_start(copy.groups[0]), 1000)


if __name__ == '__main__':
    unittest.main()
