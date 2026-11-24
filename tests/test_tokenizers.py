"""Per-language word splitting (saltlrc/tokenizers).

One unit is what the word-timing editor puts a timestamp on, so "what is a unit"
is a language question:

    中文 按字      一个汉字一个字
    日语 按发音    漢字+送りがな 成一组，假名按可唱长度分组
    英语 按单词    一个单词一个单元
    韩语 按音节    一个谚文音节块
    未识别         混合规则（并如实标注是猜的）

The invariant that must hold for every rule is the character-preservation one:
joining the units has to reproduce the source text exactly. The exporter and the
validator both depend on it, and a violation makes a line unexportable.
"""

import unittest

from saltlrc import wordtime
from saltlrc.tokenizers import chinese, english, japanese, korean, mixed


def detector_for(code, method="test"):
    """A stand-in detector, so the dispatch can be tested without langid."""
    return lambda text: {"code": code, "confidence": 1.0, "method": method}


class RuleTests(unittest.TestCase):
    def test_chinese_splits_one_character_each(self):
        self.assertEqual(wordtime.tokenize("今天我们一起去", "smart", lang="zh"),
                         ["今", "天", "我", "们", "一", "起", "去"])

    def test_chinese_keeps_a_latin_run_whole(self):
        # 按字 must not cut "baby" into b/a/b/y: the rule is about Chinese
        # characters, and a Latin word is already the unit a listener hears.
        # The space rides with the unit before it (see merge_spaces) — spaces are
        # never a unit of their own, and the concatenation must still be exact.
        self.assertEqual(wordtime.tokenize("我爱你 baby", "smart", lang="zh"),
                         ["我", "爱", "你 ", "baby"])
        self.assertEqual(wordtime.tokenize("销量2024年", "smart", lang="zh"),
                         ["销", "量", "2024", "年"])

    def test_japanese_groups_kanji_with_its_okurigana(self):
        self.assertEqual(wordtime.tokenize("今夜は月が綺麗だね", "smart", lang="ja"),
                         ["今夜は", "月が", "綺麗だね"])
        # A kanji run longer than one breath is cut, and only up to three kana
        # are absorbed as okurigana.
        self.assertEqual(wordtime.tokenize("一生懸命がんばる", "smart", lang="ja"),
                         ["一生懸命", "がんばる"])

    def test_japanese_groups_kana_by_singable_length(self):
        self.assertEqual(wordtime.tokenize("ありがとう", "smart", lang="ja"), ["ありがとう"])
        self.assertEqual(wordtime.tokenize("ひとりぼっちの夜", "smart", lang="ja"),
                         ["ひとり", "ぼっち", "の", "夜"])
        self.assertEqual(wordtime.tokenize("エンターテインメント", "smart", lang="ja"),
                         ["エンタ", "ーテイ", "ンメン", "ト"])

    def test_english_splits_one_word_each(self):
        self.assertEqual(wordtime.tokenize("I can feel your love", "smart", lang="en"),
                         ["I ", "can ", "feel ", "your ", "love"])
        # The apostrophe is part of the word, not a break.
        self.assertEqual(wordtime.tokenize("don't stop", "smart", lang="en"),
                         ["don't ", "stop"])

    def test_korean_splits_one_syllable_each(self):
        self.assertEqual(wordtime.tokenize("사랑해요", "smart", lang="ko"),
                         ["사", "랑", "해", "요"])

    def test_unknown_language_uses_the_mixed_rule(self):
        self.assertEqual(wordtime.tokenize("月が綺麗", "smart", lang="und"),
                         japanese.regroup(list("月が綺麗")))

    def test_every_rule_preserves_every_character(self):
        samples = ["今天我们一起去", "我爱你 baby", "I can feel your love", "don't stop",
                   "今夜は月が綺麗だね", "エンターテインメント", "사랑해요", "月が綺麗",
                   " Hey, once upon a younger night ", "a  b", "纯中文没有空格",
                   "スマイル de スマイル", "123 456", "！？……"]
        for lang in ("zh", "ja", "en", "ko", "und"):
            for mode in ("smart", "char", "word"):
                for text in samples:
                    with self.subTest(lang=lang, mode=mode, text=text):
                        self.assertEqual(
                            "".join(wordtime.tokenize(text, mode, lang=lang)), text)


class DispatchTests(unittest.TestCase):
    def test_language_is_detected_from_the_line_when_not_given(self):
        units, info = wordtime.tokenize_info("今夜は月が綺麗だね")
        self.assertEqual(info["lang"], "ja")
        self.assertEqual(info["source"], "detect")
        self.assertEqual(units, ["今夜は", "月が", "綺麗だね"])

    def test_a_hint_wins_over_detection(self):
        # 月が綺麗 is short and kana-bearing, so detection says ja on its own; the
        # interesting case is a kanji-only line, where detection cannot tell 中文
        # from 日语 and the track's own language must decide.
        han_only = "東京"
        self.assertEqual(wordtime.tokenize(han_only, "smart", lang="zh"), ["東", "京"])
        self.assertEqual(wordtime.tokenize(han_only, "smart", lang="ja"), ["東京"])
        # ...and with no hint the ambiguous case is reported as such.
        _units, info = wordtime.tokenize_info(han_only, detector=detector_for("zh", "han-ambiguous"))
        self.assertEqual(info["lang"], "zh")

    def test_the_reply_names_the_rule_that_ran(self):
        _units, info = wordtime.tokenize_info("今天我们一起去", lang="zh")
        self.assertEqual(info["rule"], chinese.LABEL)
        _units, info = wordtime.tokenize_info("I can feel", lang="en")
        self.assertEqual(info["rule"], english.LABEL)
        _units, info = wordtime.tokenize_info("사랑", lang="ko")
        self.assertEqual(info["rule"], korean.LABEL)
        _units, info = wordtime.tokenize_info("???", lang="und")
        self.assertEqual(info["rule"], mixed.LABEL)

    def test_an_unknown_language_code_falls_back_instead_of_raising(self):
        self.assertEqual(wordtime.tokenize("abc", "smart", lang="xx"), ["abc"])

    def test_char_and_word_modes_ignore_the_language(self):
        # Someone who asks for one cell per character means it for every script.
        self.assertEqual(wordtime.tokenize("梅雨", "char", lang="ja"), ["梅", "雨"])
        self.assertEqual(wordtime.tokenize("梅雨", "word", lang="ja"), ["梅", "雨"])
        self.assertEqual(wordtime.tokenize("abc", "char", lang="ja")[:3], ["a", "b", "c"])

    def test_mixed_rule_is_not_a_second_copy_of_the_japanese_rules(self):
        # The fallback delegates to the Japanese grouping; two copies of a rule
        # is one copy too many, and they would drift apart.
        self.assertEqual(mixed.regroup(list("月が綺麗")), japanese.regroup(list("月が綺麗")))

    def test_word_level_distribution_follows_the_same_units(self):
        # The estimator must place timestamps on the same units the editor shows,
        # or the chips and the exported file disagree.
        doc = wordtime.build([{"time": 1000, "text": "今天我们一起去", "words": []}], [], 20000,
                             options={"unit": "smart", "lang": "zh"})
        self.assertEqual([w.text for w in doc.groups[0].main.words],
                         ["今", "天", "我", "们", "一", "起", "去"])


if __name__ == "__main__":
    unittest.main()
