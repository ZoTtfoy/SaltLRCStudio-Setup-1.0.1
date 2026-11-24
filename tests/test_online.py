"""Offline tests for the online-lyric pipeline: crypto, parsing, word timing,
provider ranking and library file handling. No network access is required."""

import base64
import tempfile
import unittest
from pathlib import Path

from saltlrc import crypto, library, providers, wordtime
from saltlrc import lrctext
from saltlrc.sources import netease
from saltlrc.exporter import export
from saltlrc.validator import validate

PLAIN_LRC = "[00:14.105]梅雨明けの空滲む街\n[00:19.772]窓から透ける街灯は\n[00:24.694]\n"
JSON_LRC = ('{"t":0,"c":[{"tx":"作词: "},{"tx":"ひとみ"}]}\n'
            '{"t":14105,"c":[{"tx":"梅","t":0},{"tx":"雨","t":240},{"tx":"明","t":480}]}\n')


class CryptoTests(unittest.TestCase):
    def test_fips_vector(self):
        # ecb_encrypt() applies PKCS#7 for the Netease envelope, so compare the
        # first block of a full 16-byte input against the FIPS-197 vector.
        self.assertEqual(crypto.ecb_encrypt(b"\x00" * 16, b"\x00" * 16)[:16].hex(),
                         "66e94bd4ef8a2c3b884cfa59ca342b2e")
        self.assertEqual(
            crypto.ecb_encrypt(b"\x32\x43\xf6\xa8\x88\x5a\x30\x8d\x31\x31\x98\xa2\xe0\x37\x07\x34",
                               b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c")[:16].hex(),
            "3925841d02dc09fbdc118597196a0b32")

    def test_cbc_differs_from_ecb_and_is_invertible_shape(self):
        key, iv = b"0CoJUm6Qyw8W8jud", b"0102030405060708"
        data = b"saltlrc-test-payload"
        cipher = crypto.cbc_encrypt(data, key, iv)
        self.assertEqual(len(cipher), 32)
        self.assertNotEqual(cipher, crypto.ecb_encrypt(data, key))

    def test_envelopes_carry_expected_fields(self):
        self.assertIn("params", crypto.eapi("/api/song/lyric/v1", {"id": 1}))
        envelope = crypto.weapi({"id": 1})
        self.assertIn("params", envelope)
        self.assertEqual(len(envelope["encSecKey"]), 256)


class ParseTests(unittest.TestCase):
    def test_plain_lines_and_empty_dropped(self):
        parsed = lrctext.parse_lrc(PLAIN_LRC)
        self.assertEqual([item["time"] for item in parsed["lines"]], [14105, 19772])

    def test_json_lines_keep_word_timings(self):
        parsed = lrctext.parse_lrc(JSON_LRC)
        self.assertTrue(parsed["word_level"])
        body = parsed["lines"][-1]
        self.assertEqual(body["text"], "梅雨明")
        self.assertEqual([w["time"] for w in body["words"]], [14105, 14345, 14585])

    def test_credits_removed(self):
        result = lrctext.clean_lines(lrctext.parse_lrc(JSON_LRC)["lines"])
        self.assertEqual([item["text"] for item in result], ["梅雨明"])


class WordTimeTests(unittest.TestCase):
    def test_tokenize_preserves_every_character(self):
        # Regression: a dropped space makes an English line fail the
        # "word texts must rebuild the line" check and blocks its export.
        for mode in ("char", "word", "smart"):
            for text in ("梅雨 abc def", "Hey, once upon a younger night", " leading", "trailing ",
                         "a  b", "纯中文没有空格", "今夜は月が綺麗だね", "スマイル de スマイル"):
                with self.subTest(mode=mode, text=text):
                    self.assertEqual("".join(wordtime.tokenize(text, mode)), text)

    def test_smart_mode_groups_words_and_kana(self):
        # English: whole words (trailing space rides along with its word),
        # never one cell per letter.
        self.assertEqual(wordtime.tokenize("I can feel your love", "smart"),
                         ["I ", "can ", "feel ", "your ", "love"])
        # Japanese: kanji absorbs its okurigana (up to 3 kana); standalone kana
        # stay whole up to 5, longer runs split every 3; katakana follows suit.
        self.assertEqual(wordtime.tokenize("今夜は月が綺麗だね", "smart"),
                         ["今夜は", "月が", "綺麗だね"])
        self.assertEqual(wordtime.tokenize("ありがとう", "smart"), ["ありがとう"])
        self.assertEqual(wordtime.tokenize("ひとりぼっちの夜", "smart"),
                         ["ひとり", "ぼっち", "の", "夜"])
        self.assertEqual(wordtime.tokenize("エンターテインメント", "smart"),
                         ["エンタ", "ーテイ", "ンメン", "ト"])
        # The granular modes keep working for people who want them.
        self.assertEqual(wordtime.tokenize("梅雨", "char"), ["梅", "雨"])
        self.assertIn("abc ", wordtime.tokenize("梅雨 abc def", "word"))

    def test_smart_mode_estimates_word_timings(self):
        main = [{"time": 1000, "text": "I can feel your love", "words": []},
                {"time": 6000, "text": "今夜は月が綺麗だね", "words": []}]
        doc = wordtime.build(main, [], 60000)
        self.assertEqual([w.text for w in doc.groups[0].main.words],
                         ["I ", "can ", "feel ", "your ", "love"])
        self.assertEqual([w.text for w in doc.groups[1].main.words],
                         ["今夜は", "月が", "綺麗だね"])
        for group in doc.groups:
            self.assertEqual("".join(w.text for w in group.main.words), group.main.text)
            starts = [w.start for w in group.main.words]
            self.assertTrue(all(b > a for a, b in zip(starts, starts[1:])))
        report = validate(doc, duration=60000)
        self.assertEqual(report["errors"], [])

    def test_tokenize_splits_cjk_per_character(self):
        self.assertEqual(wordtime.tokenize("梅雨", "char"), ["梅", "雨"])

    def test_latin_words_stay_whole_in_word_mode(self):
        self.assertIn("abc ", wordtime.tokenize("梅雨 abc def", "word"))

    def test_english_line_builds_and_validates(self):
        main = [{"time": 1000, "text": "Hey, once upon a younger night", "words": []},
                {"time": 5000, "text": "When all our shadows disappeared", "words": []}]
        doc = wordtime.build(main, [], 60000)
        self.assertEqual([g.main.text for g in doc.groups],
                         ["Hey, once upon a younger night", "When all our shadows disappeared"])
        for group in doc.groups:
            self.assertEqual("".join(w.text for w in group.main.words), group.main.text)
        report = validate(doc, duration=60000)
        self.assertEqual(report["errors"], [])
        # Stripping the inline timestamps must give the line back untouched.
        # 逐字时间戳写在 <> 里，行首的 [] 是句首 —— 两类都要能剥掉。
        import re
        first = export(doc).split("\n")[0]
        self.assertTrue(first.startswith("[00:01.000]"), first)
        self.assertEqual(re.sub(r"[\[<]\d{2}:\d{2}\.\d{3}[\]>]", "", first),
                         "Hey, once upon a younger night")

    def test_distribute_is_monotonic_and_bounded(self):
        words = wordtime.distribute(1000, 3000, "梅雨明け", "char")
        self.assertEqual(words[0].start, 1000)
        starts = [w.start for w in words]
        self.assertTrue(all(b > a for a, b in zip(starts, starts[1:])))
        self.assertLess(starts[-1], 3000)
        self.assertEqual("".join(w.text for w in words), "梅雨明け")

    def test_build_never_overflows_next_line(self):
        main = [{"time": 1000, "text": "梅雨明けの空滲む街", "words": []},
                {"time": 5000, "text": "窓から透ける街灯は", "words": []}]
        translation = [{"time": 1000, "text": "梅雨过后 天空氤氲的街道", "words": []},
                       {"time": 5000, "text": "透过窗户洒入的街灯", "words": []}]
        doc = wordtime.build(main, translation, 60000)
        self.assertEqual(len(doc.groups), 2)
        self.assertLess(doc.groups[0].main.end, doc.groups[1].start)
        self.assertTrue(all(g.translation.start == g.start for g in doc.groups))
        report = validate(doc, duration=60000)
        self.assertEqual(report["errors"], [])

    def test_export_pairs_translation_with_same_start(self):
        main = [{"time": 1000, "text": "梅雨明け", "words": []}]
        translation = [{"time": 1000, "text": "梅雨过后", "words": []}]
        text = export(wordtime.build(main, translation, 30000))
        lines = text.strip().split("\n")
        self.assertTrue(lines[0].startswith("[00:01.000]<00:01.000>梅"))
        self.assertTrue(lines[0].endswith(">"), lines[0])
        self.assertEqual(lines[1], "[00:01.000]梅雨过后")

    def test_translation_without_timestamp_option(self):
        main = [{"time": 1000, "text": "梅雨明け", "words": []}]
        translation = [{"time": 1000, "text": "梅雨过后", "words": []}]
        text = export(wordtime.build(main, translation, 30000), translation_timestamp=False)
        self.assertEqual(text.strip().split("\n")[1], "梅雨过后")

    def test_manual_plain_translation_pairs_by_order(self):
        parsed = wordtime.parse_manual("[00:01]梅雨明け\n[00:03]窓から\n", "梅雨过后\n透过窗户")
        self.assertEqual([item["text"] for item in parsed["translation"]], ["梅雨过后", "透过窗户"])
        self.assertEqual(parsed["translation"][0]["time"], 1000)

    def test_manual_requires_timed_main(self):
        with self.assertRaises(ValueError):
            wordtime.parse_manual("梅雨明け\n窓から", "")

    def test_manual_row_count_mismatch_is_reported(self):
        with self.assertRaises(ValueError):
            wordtime.parse_manual("[00:01]梅雨明け\n[00:03]窓から\n", "只有一行")

    def test_sanitize_neutralises_tag_like_text(self):
        self.assertNotIn("[", wordtime.sanitize("[00:01]不是时间"))


class ProviderTests(unittest.TestCase):
    def test_score_prefers_exact_title_artist_and_duration(self):
        track = {"title": "スピカ", "artist": "ロクデナシ", "duration": 165000}
        good = {"title": "スピカ", "artist": "ロクデナシ", "duration": 165000}
        poor = {"title": "別の曲", "artist": "他人", "duration": 300000}
        self.assertGreater(providers.score(good, track), providers.score(poor, track))

    def test_best_match_flags_low_confidence(self):
        track = {"title": "スピカ", "artist": "ロクデナシ", "duration": 165000}
        weak = [{"title": "別の曲", "artist": "他人", "duration": 300000}]
        _, value, confident = providers.best_match(weak, track)
        self.assertFalse(confident)
        self.assertLess(value, 45)


class ValidatorItemsTests(unittest.TestCase):
    def test_items_are_structured_and_locatable(self):
        doc = wordtime.build([{"time": 1000, "text": "梅雨明け", "words": []},
                              {"time": 5000, "text": "窓から", "words": []}], [], 60000)
        # Break the first line's word timeline: the error must point at line 0.
        # （"首个逐字时间与句首不同"只是提示，所以要造一个**真的错误**：
        #   让逐字文字与这一行的文字对不上 —— 那会让导出**悄悄只写其中一份**。
        #   用改文字而不是改时间，不依赖分词器在这句话上切出几个单元。）
        doc.groups[0].main.words[0].text = 'X'
        report = validate(doc, duration=60000)
        self.assertIn("items", report)
        errors = [item for item in report["items"] if item["level"] == "error"]
        self.assertTrue(errors)
        self.assertEqual(errors[0]["index"], 0)
        # message 里不该再带"第 N 组："前缀（行号由 index 单独携带）。
        # 判据写成**前缀模式**，不能用"第"这个字当代理 —— 文案里可能出现"第一个字"。
        self.assertNotRegex(errors[0]["message"], r"^第 \d+ 组：")
        self.assertTrue(errors[0]["message"].strip())
        # Global notes have no line to point at.
        global_items = [item for item in report["items"] if item["index"] is None]
        self.assertTrue(global_items)

    def test_clean_document_has_no_error_items(self):
        doc = wordtime.build([{"time": 1000, "text": "Hey, once upon a younger night", "words": []},
                              {"time": 5000, "text": "When all our shadows disappeared", "words": []}], [], 60000)
        report = validate(doc, duration=60000)
        self.assertEqual([i for i in report["items"] if i["level"] == "error"], [])


class ExportFormatTests(unittest.TestCase):
    def _doc(self):
        return wordtime.build([{"time": 1000, "text": "梅雨明け", "words": []},
                               {"time": 5000, "text": "窓から", "words": []}],
                              [{"time": 1000, "text": "梅雨过后", "words": []}], 30000)

    def test_yrc_is_json_lines_with_offsets_and_durations(self):
        import json
        text = export(self._doc(), fmt="yrc")
        lines = [json.loads(line) for line in text.strip().split("\n")]
        self.assertEqual([line["t"] for line in lines], [1000, 5000])
        first = lines[0]["c"]
        self.assertEqual(first[0]["t"], 0)
        self.assertEqual("".join(c["tx"] for c in first), "梅雨明け")
        self.assertTrue(all(c["d"] > 0 for c in first))
        # Character offsets must equal the accumulated durations.
        cursor = 0
        for char in first:
            self.assertEqual(char["t"], cursor)
            cursor += char["d"]

    def test_yrc_drops_translation(self):
        text = export(self._doc(), fmt="yrc")
        self.assertNotIn("梅雨过后", text)

    def test_yrc_round_trips_through_the_parser(self):
        parsed = lrctext.parse_lrc(export(self._doc(), fmt="yrc"))
        self.assertTrue(parsed["word_level"])
        self.assertEqual(parsed["lines"][0]["text"], "梅雨明け")

    def test_unknown_format_rejected(self):
        with self.assertRaises(ValueError):
            export(self._doc(), fmt="krc")

    def test_bom_only_when_requested(self):
        from saltlrc.repair import to_bytes
        self.assertFalse(to_bytes("あ", "utf-8").startswith(b"\xef\xbb\xbf"))
        self.assertTrue(to_bytes("あ", "utf-8-bom").startswith(b"\xef\xbb\xbf"))
        self.assertEqual(to_bytes("あ", "gb18030"), "あ".encode("gb18030"))

    def test_write_lyric_honours_encoding(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "song.lrc"
            library.write_lyric(str(target), "梅雨", encoding="utf-8-bom")
            self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))


class LibraryTests(unittest.TestCase):
    def test_lyric_path_matches_audio_name(self):
        self.assertEqual(library.lyric_path_for("D:\\Music\\song.mp3"), "D:\\Music\\song.lrc")

    def test_write_is_non_destructive_until_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "song.lrc"
            self.assertEqual(library.write_lyric(str(target), "first"), "written")
            self.assertEqual(library.write_lyric(str(target), "second"), "skipped")
            self.assertEqual(target.read_text(encoding="utf-8"), "first")
            self.assertEqual(library.write_lyric(str(target), "third", overwrite=True), "written")
            self.assertEqual(target.read_text(encoding="utf-8"), "third")
            self.assertTrue(Path(str(target) + ".bak").exists())

    def test_local_path_conversion(self):
        self.assertEqual(library._to_local_path("file:///D:/Music/song.mp3"), "D:\\Music\\song.mp3")
        self.assertEqual(library._to_local_path("file:///D:/Music/a%20b.mp3"), "D:\\Music\\a b.mp3")


if __name__ == "__main__":
    unittest.main()
