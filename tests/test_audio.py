import io
import math
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path
from saltlrc.acoustic import onset_proposals
from saltlrc.alignment import match_transcript, apply_proposals
from saltlrc.language import detect, identify
from saltlrc.document import Document
from saltlrc.validator import validate
from saltlrc.parser import parse
from saltlrc.exporter import export
from saltlrc.jobs import Jobs
from saltlrc.bilingual import shift


def tone_audio():
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(b''.join(struct.pack('<h', round(6000 * math.sin(i * math.tau * 200 / 16000)))
                                 if 19200 <= i < 44800 else b'\0\0' for i in range(64000)))
    return raw.getvalue()


def document():
    return parse('[00:01]你好[00:01.500]月亮[00:02]\n[00:01]こんにちは\n[00:03]一起走[00:03.800]\n[00:03]帰ろう')


def transcript():
    return {'engine': 'fixture', 'duration_ms': 6000, 'language': 'ja', 'segments': [
        {'start': 1.2, 'end': 1.6, 'text': 'こん'}, {'start': 1.6, 'end': 2.3, 'text': 'にちは'},
        {'start': 3.2, 'end': 4.0, 'text': '帰ろう'}]}


class AudioTests(unittest.TestCase):
    def test_script_language_with_uncertainty(self):
        self.assertEqual(detect('こんにちは')['code'], 'ja')
        self.assertEqual(detect('안녕하세요')['code'], 'ko')
        self.assertTrue(detect('月色')['review'])
        self.assertEqual(detect('...')['code'], 'und')
        self.assertEqual(identify(document())['translation']['code'], 'ja')

    def test_match_original_lyrics_and_actual_chunk_boundaries(self):
        result = match_transcript(document(), transcript())
        self.assertEqual(result['field'], 'translation')
        first = result['proposals'][0]
        self.assertEqual(first['coverage'], 1)
        self.assertEqual(first['start'], 1200)
        self.assertEqual([w['start'] for w in first['words']], [1200, 1600])
        self.assertEqual(''.join(w['text'] for w in first['words']), 'こんにちは')

    def test_linked_apply_preserves_chinese_word_rhythm(self):
        doc = document()
        updated = apply_proposals(doc, match_transcript(doc, transcript()), [0, 1])
        self.assertEqual(updated.groups[0].start, 1200)
        self.assertEqual([w.start for w in updated.groups[0].main.words], [1200, 1700])
        self.assertEqual(updated.groups[0].translation.start, 1200)
        self.assertEqual(updated.groups[0].translation.end, 2300)
        self.assertEqual(doc.groups[0].start, 1000)
        self.assertEqual(export(updated).splitlines()[1], '[00:01.200]こんにちは')

    def test_independent_track_requires_sync_for_salt(self):
        """翻译轨没对齐时：**导出照常成功，但两条线要写成同一个句首**。

        这条要求**落实在写出来的文件里**（播放器靠"两行时间相同"配对），
        同时报一条提示让人可以去对齐。
        """
        doc = document()
        updated = apply_proposals(doc, match_transcript(doc, transcript()), [0], link=False)
        self.assertEqual(updated.groups[0].start, 1000)
        self.assertEqual(updated.groups[0].translation.start, 1200)
        # 这份夹具是**两组**（4 行），这里只盯第一组那一对。
        lines = export(updated).splitlines()
        self.assertTrue(lines[0].startswith('[00:01.000]'), lines[0])
        self.assertTrue(lines[1].startswith('[00:01.000]'),
                        '翻译按主轨句首写，播放器才配得成对：' + lines[1])
        self.assertTrue(any('双轴' in w for w in validate(updated)['warnings']),
                        '没对齐要**提示**（不是拦路）：' + str(validate(updated)['warnings']))
        self.assertIn('[00:01.200]こんにちは', export(updated, salt=False))

    def test_missing_chinese_words_not_invented(self):
        """没有逐字就是没有 —— **不许编**，但要写得出来（写成逐行形式）。

        判据是**文件里没有 `<>`** —— 贴着症状写，比"抛不抛"更贴题。
        """
        doc = document()
        doc.groups[0].main.words = []
        doc.groups[0].main.end = None
        updated = apply_proposals(doc, match_transcript(doc, transcript()), [0])
        self.assertEqual(updated.groups[0].main.words, [], '对齐不许替它编出逐字时间')
        lines = export(updated).splitlines()
        # 只盯**没有逐字的那一组**（夹具里第二组本来就带逐字，它当然还该有 `<>`）。
        self.assertEqual(lines[0], '[00:01.200]你好月亮', '没有逐字就写成逐行形式')
        self.assertNotIn('<', lines[0], '没有逐字就不该出现逐字标记（编出来的更糟）')
        self.assertIn('<', lines[2], '别的行已经写好的逐字**一个都不许少**')

    def test_unmatched_and_partial_require_review(self):
        t = transcript()
        t['segments'] = [{'start': 1, 'end': 2, 'text': 'こんばんは'}]
        result = match_transcript(document(), t)
        self.assertEqual(result['proposals'][0]['status'], 'review')
        self.assertEqual(result['proposals'][1]['start'], None)

    def test_stale_candidate_rejected(self):
        doc = document()
        result = match_transcript(doc, transcript())
        doc.groups[0].translation.text = '変更'
        with self.assertRaises(ValueError):
            apply_proposals(doc, result, [0])

    def test_repeat_chorus_is_monotonic(self):
        doc = parse('[00:01]你好\n[00:01]こんにちは\n[00:03]你好\n[00:03]こんにちは')
        t = transcript()
        t['segments'] = [{'start': 1.2, 'end': 2, 'text': 'こんにちは'}, {'start': 3.2, 'end': 4, 'text': 'こんにちは'}]
        result = match_transcript(doc, t)
        self.assertEqual([p['start'] for p in result['proposals']], [1200, 3200])

    def test_invalid_transcript(self):
        t = transcript()
        for start, end in [(3, 1), (-1, 2), (1, 1000), (float('nan'), 2)]:
            t['segments'] = [{'start': start, 'end': end, 'text': 'こんにちは'}]
            with self.assertRaises(ValueError):
                match_transcript(document(), t)

    def test_onset_uses_real_audio_not_lyrics_interpolation(self):
        result = onset_proposals(document(), tone_audio())
        self.assertEqual(result['proposals'][0]['start'], 1200)
        self.assertEqual(result['proposals'][0]['status'], 'review')
        self.assertEqual(result['proposals'][0]['words'], [])

    def test_silence_has_no_fake_alignment(self):
        raw = io.BytesIO()
        with wave.open(raw, 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(b'\0' * 64000)
        self.assertTrue(all(p['start'] is None for p in onset_proposals(document(), raw.getvalue())['proposals']))

    def test_project_roundtrip_and_offset_preserve_dual_tracks(self):
        doc = document()
        doc.groups[0].translation.start = 1200
        doc.alignment = {'engine': 'fixture'}
        copied = Document.from_dict(doc.to_dict())
        self.assertEqual(copied.to_dict(), doc.to_dict())
        self.assertEqual(shift(doc, 100).groups[0].translation.start, 1300)

    def test_real_worker_job_and_audio_cleanup(self):
        jobs = Jobs()
        try:
            created = jobs.start({'document': document().to_dict(), 'engine': 'acoustic'}, tone_audio())
            path = Path(jobs.entries[created['id']]['folder'].name)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = jobs.status(created['id'])
                if state['state'] != 'running':
                    break
                time.sleep(.1)
            self.assertEqual(state['state'], 'completed', state)
            self.assertEqual(state['result']['proposals'][0]['start'], 1200)
            self.assertFalse(path.exists())
        finally:
            jobs.close()

    def test_missing_model_is_clear_failure(self):
        with tempfile.TemporaryDirectory() as models:
            jobs = Jobs(models)
            with self.assertRaisesRegex(ValueError, '尚未就绪'):
                jobs.start({'document': document().to_dict(), 'engine': 'faster-whisper'}, tone_audio())


if __name__ == '__main__':
    unittest.main()
