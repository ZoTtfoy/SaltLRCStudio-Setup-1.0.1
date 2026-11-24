"""Audio-evidence alignment: onset detection, global offset, word refinement.

The fixtures are synthesised tone bursts, so no real recording is needed and
the expected onsets are known exactly.
"""

import array
import io
import math
import unittest
import wave

from saltlrc.acoustic import apply_words, detect_onsets, envelope, global_offset, read_pcm, refine_words
from saltlrc.document import Document, Group, Track, Word

RATE = 16000
BURST_MS = 260


def wav_bytes(bursts, duration_ms):
    total = int(RATE * duration_ms / 1000)
    samples = array.array("h", [0]) * total
    for start_ms in bursts:
        from_index = int(RATE * start_ms / 1000)
        to_index = min(total, from_index + int(RATE * BURST_MS / 1000))
        for i in range(from_index, to_index):
            samples[i] = int(11000 * math.sin(i * 0.35))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    return buffer.getvalue()


def document(starts, text="あいう", words=None):
    groups = []
    for start in starts:
        track_words = [Word(start + i * 300, character) for i, character in enumerate(text)]
        if words is not None:
            track_words = words(start, text)
        groups.append(Group(start, Track(text, track_words, start + 3000, start),
                            Track("译文", [], None, start)))
    return Document(groups)


class OnsetTests(unittest.TestCase):
    def test_reads_only_16k_mono_pcm16(self):
        raw = wav_bytes([500], 2000)
        self.assertEqual(len(read_pcm(raw)) > 0, True)

    def test_onsets_land_on_burst_starts(self):
        onsets, _, _ = detect_onsets(envelope(read_pcm(wav_bytes([500, 1500, 2500], 3500))))
        self.assertEqual(len(onsets), 3)
        for found, expected in zip(onsets, (500, 1500, 2500)):
            self.assertLessEqual(abs(found - expected), 40)


class GlobalOffsetTests(unittest.TestCase):
    def test_recovers_a_constant_shift(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        # Lyrics sit 500 ms late; the recovery must move them back by -500.
        doc = document([1500, 2500, 3500])
        result = global_offset(doc, raw, "main", radius=1500, step=10)
        self.assertLessEqual(abs(result["delta"] + 500), 60)
        self.assertEqual(result["hits"], 3)
        self.assertEqual(result["lines"], 3)

    def test_reports_no_evidence_for_silence(self):
        doc = document([1000])
        with self.assertRaises(ValueError):
            global_offset(doc, wav_bytes([], 3000), "main", radius=500, step=10)


class RefineWordsTests(unittest.TestCase):
    def test_matching_onset_count_retimes_words(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        doc = document([1000])          # 3 units, 3 onsets inside the line
        result = refine_words(doc, raw, "main")
        entry = result["proposals"][0]
        self.assertEqual(entry["status"], "ready", entry["reason"])
        self.assertEqual([w["start"] for w in entry["words"]], [1000, 2000, 3000])

    def test_unit_count_mismatch_gets_approximate_alignment(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        doc = document([1000], text="あいうえお")   # 5 units, 3 onsets
        entry = refine_words(doc, raw, "main")["proposals"][0]
        self.assertEqual(entry["status"], "ready")
        self.assertEqual(entry["confidence"], "approx")
        self.assertIn("近似对齐", entry["reason"])
        times = [w["start"] for w in entry["words"]]
        self.assertEqual(times[0], 1000)
        self.assertTrue(all(b > a for a, b in zip(times, times[1:])))

    def test_lines_without_word_axis_get_one_generated(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        doc = Document([Group(1000, Track("la la la", [], 4000, 1000),
                              Track("译文", [], None, 1000))])
        result = refine_words(doc, raw, "main")
        entry = result["proposals"][0]
        self.assertEqual(entry["status"], "ready", entry["reason"])
        self.assertEqual(entry["units"], 3)
        apply_words(doc, result["proposals"], [0])
        self.assertEqual(len(doc.groups[0].main.words), 3)
        self.assertEqual("".join(w.text for w in doc.groups[0].main.words), "la la la")

    def test_source_track_prefers_the_word_axis(self):
        from saltlrc.alignment import source_track
        self.assertEqual(source_track(document([1000])), "main")
        empty = Document([Group(1000, Track("あ", [], 3000, 1000), Track("译文", [], None, 1000))])
        self.assertEqual(source_track(empty), "translation")
        bare = Document([Group(1000, Track("あ", [], 3000, 1000), Track("", [], None, 1000))])
        self.assertEqual(source_track(bare), "main")

    def test_missing_line_end_is_reported(self):
        raw = wav_bytes([1000, 2000], 3200)
        doc = document([1000])
        doc.groups[0].main.end = None
        entry = refine_words(doc, raw, "main")["proposals"][0]
        self.assertEqual(entry["status"], "skip")
        self.assertIn("句尾", entry["reason"])

    def test_apply_updates_only_selected_lines(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        doc = document([1000])
        result = refine_words(doc, raw, "main")
        apply_words(doc, result["proposals"], [0])
        self.assertEqual([w.start for w in doc.groups[0].main.words], [1000, 2000, 3000])

    def test_apply_rejects_edited_text(self):
        raw = wav_bytes([1000, 2000, 3000], 4200)
        doc = document([1000])
        result = refine_words(doc, raw, "main")
        doc.groups[0].main.text = "改过了"
        with self.assertRaises(ValueError):
            apply_words(doc, result["proposals"], [0])


if __name__ == "__main__":
    unittest.main()
