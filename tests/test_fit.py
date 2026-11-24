"""智能贴唱（`saltlrc/fit.py`）：拿**构造得知道真值**的音频验识别与对齐。

贴唱的对错不能靠"返回了 200"来判断 —— 必须**知道正确答案**。
所以这里自己合成音频：哪一段有"人声"（带低频的浊音）、哪一段是纯伴奏（噪声/鼓），
时间点全在夹具里；然后检查

  * 识别：人声起点被找到、纯伴奏的鼓点**不**被当成人声起点；
  * 对齐：整首平移 1.5 秒的歌要被整体扳回去；本来就对的歌**不许乱动**；
  * 单调：后一行的句首永远晚于前一行（路径约束）；
  * 保守：没人唱的地方不硬贴（保持原时间并标出来）；
  * 句尾：从浊音结束处取，找不到就保留原值。
"""
import math
import struct
import unittest
from array import array

from saltlrc.document import Document, Group, Track, Word
from saltlrc.fit import analyse, fit_lines

RATE = 16000


def pcm16(samples):
    """浮点夹具 → **int16** 序列。

    `analyse` 的契约是 int16（就是 `acoustic.read_pcm` 的返回）。
    喂浮点会被再除一次 32768 → rms ≈ 0 → 一个起点都找不到：
    夹具不能替被测代码补全假设。
    """
    return array("h", [int(max(-1.0, min(1.0, value)) * 32767) for value in samples])


def wav(samples):
    frames = b"".join(struct.pack("<h", int(max(-1, min(1, value)) * 32767)) for value in samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16) +
            b"data" + struct.pack("<I", len(frames)) + frames)


def silence(seconds):
    return [0.0] * int(seconds * RATE)


def voice(seconds, f0=180.0, level=0.35):
    """"人声"：基频 + 两三次谐波（低频能量强 → 浊音度高），带一点起音包络。"""
    out = []
    for index in range(int(seconds * RATE)):
        t = index / RATE
        rise = min(1.0, t / 0.03)
        fall = min(1.0, (seconds - t) / 0.05)
        shape = max(0.0, min(rise, fall))
        value = (math.sin(2 * math.pi * f0 * t) +
                 0.5 * math.sin(2 * math.pi * 2 * f0 * t) +
                 0.25 * math.sin(2 * math.pi * 3 * f0 * t))
        out.append(level * shape * value / 1.75)
    return out


def drum(seconds, level=0.5, seed=7):
    """"鼓/镲"：确定性噪声脉冲（高频为主 → 浊音度低），用来验证不会被当成唱。"""
    out = []
    state = seed
    for index in range(int(seconds * RATE)):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        noise = (state / 0x3FFFFFFF) - 1.0
        decay = math.exp(-(index / RATE) * 40)
        out.append(level * decay * noise)
    return out


def build(tracks, total=30.0):
    """tracks: [(起始秒, 人声秒数, 结束后的伴奏秒数)]，拼成一整首。"""
    samples = silence(1.0)                      # 开头留一点静音
    plan = []
    for start, sing, rest in tracks:
        while len(samples) / RATE < start:
            samples += silence(0.1)
        plan.append((len(samples) * 1000 // RATE, int(sing * 1000)))
        samples += voice(sing)
        samples += silence(0.12)
        samples += drum(0.25) if rest else []
        samples += silence(max(0.0, rest - 0.25)) if rest else silence(0.5)
    while len(samples) / RATE < total:
        samples += silence(0.5)
    return wav(samples[:int(total * RATE)]), plan


def lyrics(plan, offset_ms, word_axis=True):
    """按 plan 造歌词；`offset_ms` 是故意加的错位（正数 = 歌词比声音晚）。"""
    groups = []
    for start, length in plan:
        text = "あいうえお"
        words = [Word(start + offset_ms + i * 200, text[i]) for i in range(len(text))] if word_axis else []
        track = Track(text=text, words=words, start=start + offset_ms,
                      end=start + offset_ms + length + 300)
        groups.append(Group(start=start + offset_ms, main=track,
                            translation=Track(text="", words=[], start=None, end=None)))
    return Document(groups=groups)


class AnalysisTests(unittest.TestCase):
    def test_vocal_onsets_are_found_and_drums_are_not(self):
        """识别：人声起点找得到；纯伴奏的鼓点不算人声起点。"""
        raw, plan = build([(2.0, 1.6, 1.2), (5.0, 1.4, 1.2)], total=12.0)
        info = analyse(array("h", struct.unpack("<%dh" % (len(raw) // 2 - 22), raw[44:])))
        found = [value for value, _ in [(o, None) for o in info["onsets"]]]
        self.assertTrue(found, "一个人声起点都没找到")
        for start, length in plan:
            near = [value for value in found if abs(value - start) <= 200]
            self.assertTrue(near, "第 %d 毫秒处的人声没被找到（找到的是 %s）" % (start, found[:12]))
        # 鼓点在每句之后 ~1.3~1.5s；那里不该有起点
        for start, length in plan:
            drum_at = start + length + 200
            self.assertFalse([v for v in found if abs(v - drum_at) <= 90],
                             "把伴奏鼓点当成了人声起点：%s" % found[:12])

    def test_there_is_evidence_at_all(self):
        raw, _ = build([(1.0, 2.0, 0.0)], total=8.0)
        info = analyse(array("h", struct.unpack("<%dh" % (len(raw) // 2 - 22), raw[44:])))
        self.assertGreater(info["frames"], 100)
        self.assertTrue(any(info["voiced"]), "浊音度判定全为假，说明代理失效")
        self.assertGreater(info["threshold"], 0)


class FitTests(unittest.TestCase):
    def setUp(self):
        self.raw, self.plan = build([(2.0, 2.2, 1.4), (6.5, 2.0, 1.4), (11.0, 2.4, 1.2)], total=16.0)

    def test_a_globally_late_lyric_is_pulled_back(self):
        """整首晚 1500ms：应当被整体**往前**扳回去（这正是"智能贴唱"的核心）。"""
        document = lyrics(self.plan, offset_ms=1500)
        result = fit_lines(document, self.raw, "main")
        for line, (start, _) in zip(result["lines"], self.plan):
            self.assertLess(line["moved_ms"], 0,
                            "歌词是晚的，应该往前扳：%s" % line)
            self.assertLessEqual(abs(line["start"] - start), 260,
                                 "行 %d 贴到了 %d，真值是 %d" % (line["index"], line["start"], start))

    def test_a_correct_lyric_is_left_alone(self):
        """本来就对的歌词不许乱动（动了就是"贴歪"）。"""
        document = lyrics(self.plan, offset_ms=0)
        result = fit_lines(document, self.raw, "main")
        for line, (start, _) in zip(result["lines"], self.plan):
            self.assertLessEqual(abs(line["moved_ms"]), 260,
                                 "行 %d 本来是对的，却被移动了 %d ms" % (line["index"], line["moved_ms"]))

    def test_the_path_is_strictly_increasing(self):
        """路径约束：后一行的句首必须**明显**晚于前一行。"""
        document = lyrics(self.plan, offset_ms=2500)     # 晚很多，逼它去找别的起点
        result = fit_lines(document, self.raw, "main")
        starts = [line["start"] for line in result["lines"]]
        self.assertEqual(starts, sorted(starts))
        for previous, current in zip(starts, starts[1:]):
            self.assertGreater(current - previous, 250,
                               "两行贴到了同一个起点上：%s" % starts)

    def test_a_small_nudge_inside_a_sung_passage_is_taken(self):
        """最常见的真实情况：歌词**大致齐**，只是每行差两百毫秒。

        这条专门盯住代价模型里的"离人声起点多远"那一项：如果没有它，
        "保持原时间"（代价只有一点点）就会永远赢过"贴到 200ms 外的那个人声起点"，
        于是**微调永远不会发生** —— 而微调恰恰是贴唱最常干的事。
        （反向验证时发现原来的用例覆盖不到这一点：晚 1500ms 那种情形是被
        "落在长段无人处"的惩罚推着走的，跟这一项无关。）
        """
        document = lyrics(self.plan, offset_ms=0)
        for group in document.groups:            # 每行往前挪 200ms，但仍在人声段里
            for track_name in ("main",):
                track = getattr(group, track_name)
                track.start -= 200
        result = fit_lines(document, self.raw, "main")
        for line, (start, _) in zip(result["lines"], self.plan):
            self.assertLessEqual(abs(line["start"] - start), 120,
                                 "行 %d 该被贴回 %d，结果落在 %d（%s）"
                                 % (line["index"], start, line["start"], line["reason"]))
            self.assertEqual(line["confidence"], "snapped", line)

    def test_dense_onsets_cannot_collapse_two_lines_onto_one_start(self):
        """快歌 + 写得很挤的歌词：路径约束不许两行挤到同一个人声起点上。

        夹具是合成的（12 个短促音节，每 280ms 一个；歌词每行只隔 100ms）——
        它模拟的是"整份时间轴都不可信"的真实现象：不设约束时，好几行都会
        觉得同一个最强的起点最划算。
        """
        samples = silence(1.0)
        truth = []
        for _ in range(12):
            truth.append(len(samples) * 1000 // RATE)
            samples += voice(0.15) + silence(0.13)
        samples += silence(2.0)
        raw = wav(samples)
        groups = []
        for i in range(6):
            at = truth[2] + 1200 + i * 100          # 故意挤成 100ms 一行
            groups.append(Group(start=at, main=Track(text="あ", words=[], start=at, end=at + 300),
                                translation=Track()))
        result = fit_lines(Document(groups=groups), raw, "main")
        starts = [line["start"] for line in result["lines"]]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts), "两行贴到了同一个起点：%s" % starts)
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 250, "两行贴得太近：%s" % starts)

    def test_a_line_with_nobody_singing_keeps_its_time(self):
        """没人唱的地方不硬贴：保持原时间，并且标成 kept。"""
        # 第三行放在一段长达 4 秒的纯伴奏里（真值里那里确实没有人声）
        raw, plan = build([(2.0, 1.5, 0.5), (12.0, 1.5, 0.5)], total=16.0)
        document = lyrics(plan, offset_ms=0)
        lonely = Group(start=7000, main=Track(text="没有人唱的一句", words=[],
                                              start=7000, end=9000),
                       translation=Track())
        document.groups.insert(1, lonely)
        result = fit_lines(document, raw, "main")
        middle = [line for line in result["lines"] if line["index"] == 1][0]
        self.assertEqual(middle["confidence"], "kept", middle)
        self.assertEqual(middle["start"], 7000, middle)

    def test_the_end_comes_from_the_audio_and_never_precedes_the_start(self):
        document = lyrics(self.plan, offset_ms=0)
        result = fit_lines(document, self.raw, "main")
        for line in result["lines"]:
            self.assertGreater(line["end"], line["start"], line)
            self.assertTrue(line["reason"], "每行都要有一句人话说明")

    def test_word_axis_moves_with_its_line(self):
        """整行移动时，行内逐字轴跟着一起走（否则字还留在旧时间上，行内就散了）。"""
        document = lyrics(self.plan, offset_ms=1500, word_axis=True)
        before = [word.start for word in document.groups[0].main.words]
        result = fit_lines(document, self.raw, "main")
        moved = result["lines"][0]["moved_ms"]
        after = [word.start for word in Document.from_dict(result["document"]).groups[0].main.words]
        self.assertEqual([value + moved for value in before], after)

    def test_group_start_moves_with_main_start(self):
        """句首只有一个事实：`group.start` 与 `main.start` 必须同时变。

        只改一个的话，时间轴（读 track.start）与表格/波形（读 group.start）会各说各话。
        （这一条是页面夹具先抓到的：应用贴唱后 group.start 还留在旧位置。）
        """
        document = lyrics(self.plan, offset_ms=1500)
        result = fit_lines(document, self.raw, "main")
        fitted = Document.from_dict(result["document"])
        for group in fitted.groups:
            self.assertEqual(group.start, group.main.start,
                             "group.start 与 main.start 必须一致：%s" % group)

    def test_a_silent_track_is_refused_out_loud(self):
        from saltlrc.fit import fit_lines as fit
        document = lyrics(self.plan, offset_ms=0)
        with self.assertRaises(ValueError) as caught:
            fit(document, wav(silence(6.0)), "main")
        self.assertIn("人声起点", str(caught.exception))

    def test_an_empty_lyric_is_refused_out_loud(self):
        empty = Document(groups=[])
        with self.assertRaises(ValueError) as caught:
            fit_lines(empty, self.raw, "main")
        self.assertIn("歌词行", str(caught.exception))

    def test_the_notice_explains_what_happened(self):
        document = lyrics(self.plan, offset_ms=1500)
        result = fit_lines(document, self.raw, "main")
        self.assertIn("人声起点", result["notice"])
        self.assertIn("预览", result["notice"], "要告诉用户下一步能预览")
        self.assertEqual(result["moved"] + result["weak"] + result["kept"], len(self.plan))


if __name__ == "__main__":
    unittest.main()
