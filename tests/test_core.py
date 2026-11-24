import tempfile
import unittest
from pathlib import Path
from saltlrc.parser import parse
from saltlrc.exporter import export
from saltlrc.bilingual import swap, shift
from saltlrc.validator import validate
from saltlrc.repair import decode
from saltlrc.batch import run_batch

SAMPLE = '[ti:测试]\n[00:01:25]月色[00:02.500]很美[00:04.000]\n[00:01.250]月が綺麗ね\n'


class CoreTests(unittest.TestCase):
    def test_salt_round_trip_and_translation_one_timestamp(self):
        text = export(parse(SAMPLE))
        # 逐行用 `[]`、逐字用 `<>`。翻译轨只有一个句首时间，不带逐字。
        self.assertEqual(text,
                         '[00:01.250]<00:01.250>月色<00:02.500>很美<00:04.000>\n'
                         '[00:01.250]月が綺麗ね\n')
        # 导出 → 回读 → 再导出必须一模一样：解析器与导出器共用同一个形状
        # （见 test_angle_words），读写闭环。
        self.assertEqual(export(parse(text)), text)
        self.assertEqual(text.splitlines()[1].count('['), 1)

    def test_salt_keeps_line_and_word_timestamps_apart(self):
        """Salt 逐字格式：**逐行用 `[]`、逐字用 `<>`** —— 两类时间戳同时存在。

        两类时间戳必须能分开读：全都写 `[]` 的话播放器分不出哪个是句首、哪个是某个字
        的时间，逐字就等于没有单独交代出去。一条行里两类信息同时在：

            [句首]<字1时间>字1<字2时间>字2…<句尾>

        这个形状由 `parser.parse` 定义（那里专门为 `<` 写了一条特例，
        注释写着 "Angle-bracket markers are word syntax"）。
        **翻译轨一个字都不改** —— 仍然只带与主句相同的那个句首时间。
        """
        doc = parse('[00:01.250]月色[00:02.500]很美[00:04.000]\n[00:01.250]月が綺麗ね\n')
        lines = export(doc).splitlines()
        self.assertEqual(lines[0], '[00:01.250]<00:01.250>月色<00:02.500>很美<00:04.000>')
        self.assertTrue(lines[0].startswith('[00:01.250]'),
                        '行首仍必须是 [句首] —— 只按行读的播放器就靠它')
        self.assertEqual(lines[0].count('['), 1, '这一行只该有一个 []（句首），其余是 <>')
        self.assertEqual(lines[0].count('<'), 3, '逐字 + 句尾都写在 <> 里')
        self.assertEqual(lines[1], '[00:01.250]月が綺麗ね', '翻译轨不动：只有一个句首')
        # 回读闭环 + 校验通过
        self.assertEqual(export(parse(export(doc))), export(doc), '导出 → 回读 → 再导出必须一致')
        self.assertFalse(validate(parse(export(doc)))['errors'])

    def test_angle_words(self):
        doc = parse('[00:01.00]<00:01.00>你好<00:02.00>月亮<00:03.00>\n[00:01.00]こんにちは')
        self.assertFalse(validate(doc)['errors'])
        self.assertEqual(doc.groups[0].main.words[0].start, 1000)

    def test_repeated_line_timestamps(self):
        doc = parse('[00:01.00][00:03.00]你好')
        self.assertEqual([g.start for g in doc.groups], [1000, 3000])
        self.assertEqual(export(doc, False), '[00:01.000]你好\n[00:03.000]你好\n')

    def test_language_swap_never_maps_timing(self):
        doc = parse('[00:01]月が[00:02]綺麗[00:03]\n[00:01]月色很美')
        self.assertEqual(doc.groups[0].main.text, '月色很美')
        self.assertEqual(doc.groups[0].main.words, [])
        self.assertTrue(doc.groups[0].translation.words)
        # 交换前：主轨（中文）没有逐字 → 导出成**逐行形式**（没有 `<>`）。
        # 判据是"逐字此时不在主轨上"，而不是"导出会失败"——
        # 这正是这条测试真正要盯的东西。
        self.assertNotIn('<', export(doc))
        swap(doc.groups[0])
        self.assertTrue(doc.groups[0].main.words)
        self.assertIn('<00:02.000>綺麗', export(doc), '交换后逐字跟着文字一起搬到了主轨')

    def test_translation_timing_removed(self):
        doc = parse(SAMPLE.replace('月が綺麗ね', '月が[00:02]綺麗ね[00:04]'))
        self.assertEqual(export(doc).splitlines()[1], '[00:01.250]月が綺麗ね')
        self.assertTrue(doc.groups[0].translation.words)

    def test_offset_once_and_atomic_shift(self):
        doc = parse('[offset:100]\n' + SAMPLE)
        self.assertEqual(doc.groups[0].start, 1350)
        self.assertEqual(parse(export(doc)).groups[0].start, 1350)
        before = doc.to_dict()
        with self.assertRaises(ValueError):
            shift(doc, -2000)
        self.assertEqual(before, doc.to_dict())

    def test_sort_dedup_and_third_track(self):
        """同一时间出现第三条歌词：**丢掉它并说出来**，但不拦导出。

        歌词文件里常常混着几行别的东西（例如网易云 YRC 版权信息），把它们当成
        **错误**会让整首歌永远导不出来。所以：能导出（第二条仍按翻译轨写出），
        被丢掉的那条在 `unresolved` 里一条条写着。
        """
        doc = parse('[00:02]后来\n[00:01]你好\n[00:01]你好\n[00:01]こんにちは\n[00:01]额外')
        self.assertEqual([g.start for g in doc.groups], [1000, 2000])
        self.assertTrue(doc.unresolved, "丢掉了东西就必须说出来")
        self.assertIn("额外", " ".join(doc.unresolved), "要说清丢的是哪一条")
        # 导出照做（不再因为"有一行看不懂/多了一条"就整首拒了）
        text = export(doc, False)
        self.assertIn("[00:01.000]你好", text)
        self.assertIn("[00:01.000]こんにちは", text, "第二条按翻译轨写出")
        # 而"多出来那一条"在校验里是**提示**，不是错误
        # （注意：这两行本身没有逐字时间，那仍然是 error —— 降级发生在**渲染**那一步，
        #  不在校验里；所以这里只断言"额外内容"这一条不再拦路）
        report = validate(doc)
        self.assertTrue(any("额外" in w for w in report["warnings"]), report["warnings"])
        self.assertFalse(any("额外" in e for e in report["errors"]), report["errors"])

    def test_a_netease_yrc_line_inside_an_lrc_is_parsed(self):
        """**混合格式**：版权信息那几行走网易云 YRC、正文走普通 LRC —— 两种都要认。

        YRC 版权行形如 `{"t":0,"c":[{"tx":"作曲: "},{"tx":"Grabbitz"},…]}`。
        这种行若被记成"未解析"，而校验又把未解析当**错误**，整首歌就永远导不出来。
        """
        text = ('{"t":0,"c":[{"tx":"作曲: ","t":0,"d":500},{"tx":"Grabbitz","t":500,"d":400}]}\n'
                '[00:22.549]Am I learning my lesson\n'
                '[00:29.737]\n')
        doc = parse(text)
        self.assertEqual(doc.unresolved, [], "YRC 行不该再被当成未解析")
        self.assertEqual([g.start for g in doc.groups], [0, 22549, 29737])
        credits = doc.groups[0].main
        self.assertEqual(credits.text, "作曲: Grabbitz")
        self.assertEqual([w.start for w in credits.words], [0, 500], "逐字时间要按 t/d 还原")
        self.assertEqual(credits.end, 900)
        # 空行（伴奏过门）是一行、没有逐字轴 —— 这是真实歌词里的常态
        self.assertEqual(doc.groups[2].main.text, "")
        self.assertEqual(doc.groups[2].main.words, [])

    def test_our_own_yrc_export_can_be_read_back(self):
        """**导出 → 回读**必须是同一个东西（以前回读完全不认自己的格式）。"""
        source = parse('[00:01]今晚[00:02]月色很美[00:04]\n[00:01]今夜は[00:02]月が綺麗だね[00:04]')
        body = export(source, fmt='yrc')
        again = parse(body)
        self.assertEqual(again.unresolved, [])
        self.assertEqual([g.start for g in again.groups], [g.start for g in source.groups])
        self.assertEqual([w.text for w in again.groups[0].main.words],
                         [w.text for w in source.groups[0].main.words])
        self.assertEqual([w.start for w in again.groups[0].main.words],
                         [w.start for w in source.groups[0].main.words])

    def test_yrc_units_on_the_same_millisecond_are_clamped(self):
        """YRC 里相邻单元可以同毫秒（快歌词）—— 解析时必须夹成严格递增。

        校验器要求逐字时间**严格递增**，所以这里就地夹紧（1ms 一格），
        而不是生成一份 `check()` 会拒绝的文档。
        """
        text = '{"t":100,"c":[{"tx":"快","t":0,"d":0},{"tx":"歌","t":0,"d":0},{"tx":"词","t":0,"d":0}]}\n'
        doc = parse(text)
        self.assertEqual(doc.unresolved, [])
        words = doc.groups[0].main.words
        self.assertEqual([w.start for w in words], [100, 101, 102])
        self.assertGreater(doc.groups[0].main.end, words[-1].start, "句尾必须晚于最后一个字")

    def test_a_song_with_only_plain_lrc_lines_still_exports(self):
        """整首没有逐字的歌词：**照常按 Salt 格式导出**（每行写成逐行形式）。

        没有逐字的行本来就写得出来（写成 `[句首]整行`），所以格式仍然是 Salt，
        不需要降级，文件里也就不存在"被截断的逐字"。
        """
        from saltlrc.api._render import _render_or_fallback
        # 四行都是**单时间戳**的普通 LRC 行（同一时间戳的两行会被合并成主轨+翻译轨，
        # 所以这里是 2 组）→ 一行逐字时间都没有
        doc = parse('[00:01]甲\n[00:01]こんにちは\n[00:03]乙\n[00:03]またね\n')
        self.assertEqual([g.main.words for g in doc.groups], [[], []])
        self.assertEqual(validate(doc)["errors"], [], "缺逐字不再拦路（它只是提示）")
        text, note = _render_or_fallback(doc, {"format": "salt", "salt": True})
        self.assertIn("[00:01.000]甲", text)
        self.assertIn("[00:01.000]こんにちは", text)
        self.assertIsNone(note, "没有降级：这份文件本来就是 Salt 格式")
        # YRC 也**不降级**：它是另一种格式（JSON），要 JSON 的人不该拿到 LRC。
        # 没有逐字的行它自己写得出来（整行当一个单元、时长用下一行的句首），
        # 所以判据是"写出来的是不是 JSON"（这才是不降级的证据）。
        yrc, yrc_note = _render_or_fallback(doc, {"format": "yrc"})
        self.assertIsNone(yrc_note)
        self.assertIn('"c":[', yrc, "必须仍是 YRC 的 JSON，而不是被换成 LRC")
        self.assertNotIn('[00:01.000]甲', yrc, "绝不能把 LRC 冒充成 YRC")

    def test_an_empty_line_renders_as_a_bare_timestamp(self):
        """空行（伴奏过门）在 Salt 逐字格式里写一个裸时间戳 —— 不是 `stamp(None)` 崩掉。

        注意空行的写法是 `[00:03]` **后面什么也没有**（真实歌词里就是这样，例如
        用户那份文件里的 `[00:29.737]`）；`[00:03]乙[00:04]` 是一行有字的歌词。
        """
        doc = parse('[00:01]甲[00:02]\n[00:03]\n')
        self.assertEqual(doc.groups[1].main.text, "")
        self.assertEqual(doc.groups[1].main.words, [])
        self.assertIn("[00:03.000]", export(doc, fmt='lrc'))
        # Salt 逐字分支对空行也要能写出（以前会 `stamp(None)` 抛异常 —— 只在
        # "校验被绕过后"才会露头的坑）
        doc2 = parse('[00:01]甲[00:01.500]乙[00:02]\n[00:03]\n')
        worded = export(doc2, fmt='salt')
        self.assertIn("[00:03.000]", worded)

    def test_no_silent_discard(self):
        for bad in ['未知内容', '[00:61]坏时间', '[00:01.1234]坏精度', '[00:01>括号错误']:
            with self.subTest(bad=bad):
                self.assertTrue(parse(bad).unresolved)

    def test_invalid_word_order_and_missing_end(self):
        """倒退的逐字时间仍然是**错误**；缺句尾**照常导出**（不写结尾标记）。

        「缺少显式句尾时间」是"某一行的问题"，降成提示：错误会让
        `_render_or_fallback` 把**整首**降级、把别的行已经写好的逐字一起截掉。
        缺句尾**写得出来**（不写结尾标记，播放器按"延续到下一行"处理），所以不拦路。
        倒退不一样：写出来就是坏文件，仍然是错误。
        """
        backwards = '[00:02]你好[00:01]月亮[00:03]'
        with self.assertRaises(ValueError):
            export(parse(backwards + '\n' + backwards[:7] + 'こんにちは'))

        no_end = '[00:01]你好[00:02]月亮'
        doc = parse(no_end + '\n' + no_end[:7] + 'こんにちは')
        text = export(doc)
        self.assertEqual(text.splitlines()[0], '[00:01.000]<00:01.000>你好<00:02.000>月亮',
                         '没有句尾就不写结尾标记（不编一个时间出来）')
        self.assertEqual(export(parse(text)), text, '往返仍要一模一样')
        self.assertFalse(validate(doc)['errors'])

    def test_short_units_and_audio_duration(self):
        doc = parse('[00:01]你[00:01.025]好[00:02]\n[00:01]こんにちは')
        self.assertTrue(validate(doc)['warnings'])
        self.assertTrue(validate(doc, duration=1500)['errors'])

    def test_equal_word_times_warn_but_dense_lines_are_writable(self):
        # Dense (rap-like) lines routinely land several words on the same
        # millisecond once a human edits them; that must stay a warning, not a
        # blocking error. Only backwards times are an error.
        equal = parse('[00:01]你[00:01.5]好[00:02]世[00:02.5]界[00:03]')
        group = equal.groups[0]
        self.assertGreaterEqual(len(group.main.words), 2)
        group.main.words[1].start = group.main.words[0].start
        report = validate(equal)
        self.assertFalse(report['errors'])
        self.assertTrue(any('相同时间' in w for w in report['warnings']))
        backwards = parse('[00:01]你[00:01.5]好[00:02]世[00:02.5]界[00:03]')
        group = backwards.groups[0]
        group.main.words[1].start = group.main.words[0].start - 1
        self.assertTrue(any('不能倒退' in e for e in validate(backwards)['errors']))

    def test_word_and_line_forms_mix_in_one_file(self):
        """**一份文件里两种形式混着来** —— 一行没逐字，不该把别的行的逐字截掉。

        若校验器把"缺逐字"当 **error**：`export(salt)` 整个抛出去 →
        `_render_or_fallback` 把**整首**降级成标准 LRC →
        **别的行已经写好的逐字（连同句尾）全被截掉**（3 行里 1 行没逐字，
        另外那行的逐字就没了）。

        按行决定写哪种形式：

            [句首]<字1>字1<字2>字2<句尾>    有逐字的行
            [句首]整行                      只有逐行的行
            [句首]                          空行（伴奏过门）

        夹具用**混合的文件本身**：这样的文件导进来就是"有的行有逐字、有的没有"
        （比如网易云的 YRC 版权行 + 普通 LRC 正文）。
        """
        source = ('[00:01.000]<00:01.000>月色<00:02.500>很美<00:04.000>\n'
                  '[00:01.000]月が綺麗ね\n'
                  '[00:05.000]风吹过\n'
                  '[00:05.000]風が吹く\n'
                  '[00:09.000]\n')
        doc = parse(source)
        self.assertEqual([bool(g.main.words) for g in doc.groups], [True, False, False],
                         '第 1 行有逐字、第 2 行只有逐行、第 3 行是空行')
        self.assertEqual(validate(doc)['errors'], [], '混着来不该报错')

        text = export(doc, fmt='salt')
        self.assertIn('[00:01.000]<00:01.000>月色<00:02.500>很美<00:04.000>', text,
                      '有逐字的那一行：逐字与句尾都要在')
        self.assertIn('[00:05.000]风吹过', text, '没逐字的那一行写成逐行形式')
        self.assertIn('[00:09.000]', text, '空行（过门）写成裸时间戳')
        self.assertIn('[00:05.000]風が吹く', text, '翻译轨照旧一行一个句首')
        self.assertNotIn('<00:05', text, '没有逐字的行不该被编出逐字时间')
        self.assertEqual(text, source, '混着进来的，原样混着出去')
        self.assertEqual(export(parse(text), fmt='salt'), text, '往返一致')

    def test_a_whole_track_missing_word_timings_is_one_message_not_forty(self):
        """整首缺逐字时间只报**一条**，而且要说清下一步怎么做。

        若一行一条：40 行的歌词就有 40 条「缺少主歌词逐字时间」，报告页变成一堵墙、
        侧栏显示"待处理 40 项"、导出报错是 40 行拼起来的。
        有用的信息只有一句：整首缺、共几行、下一步做什么。
        """
        lines = ['[00:%02d]第%d句' % (index, index) for index in range(1, 41)]
        doc = parse('\n'.join(lines) + '\n')
        report = validate(doc)
        # 缺逐字是**提示**而不是错误（不再拦路，导出时写成逐行形式）
        self.assertEqual(report['errors'], [], '缺逐字不该再拦着导出')
        self.assertEqual(len(report['warnings']), 1, report['warnings'][:4])
        message = report['warnings'][0]
        self.assertIn('共 40 行', message)
        self.assertIn('逐字', message)
        # 「那我要怎么做」必须写出来（本项目老规矩：约束类提示要给下一步）
        self.assertIn('按字数均分', message)
        # 汇总条仍要带行号，否则「定位」没地方可跳
        self.assertEqual(report['items'][0]['index'], 0)
        self.assertEqual(report['items'][0]['count'], 40)
        # 而且它说的是真的：换标准 LRC 导出确实不需要逐字时间
        self.assertEqual(validate(doc, strict=False)['errors'], [])
        self.assertTrue(export(doc, salt=False).strip())
        # 而且**不用换格式**：Salt 逐字格式现在也导得出（每行写成逐行形式）
        self.assertTrue(export(doc, fmt='salt').strip())

    def test_a_few_problem_lines_are_still_listed_one_by_one(self):
        """少数几行有问题时仍然逐条列出 —— 只有这样才能「定位到那一行」。"""
        # 第 4 行带逐字时间（有内层时间戳），前三行没有 ——
        # 所以缺逐字的是 3 行，正好落在"逐条列出"的阈值内。
        doc = parse('[00:01]甲[00:02]\n[00:03]乙[00:04]\n[00:05]丙[00:06]\n'
                    '[00:07]こ[00:07.500]ん[00:08.000]\n')
        for group in doc.groups[:3]:
            group.main.words = []
        report = validate(doc)
        # 缺逐字是**提示**，但"少数几行仍然逐条列出"这条行为没变
        #（只有这样才能「定位到那一行」）。
        self.assertEqual(len(report['warnings']), 3)
        self.assertTrue(report['warnings'][0].startswith('第 1 组：'), report['warnings'][0])
        self.assertEqual([item['index'] for item in report['items']], [0, 1, 2])
        for item in report['items']:
            self.assertNotIn('共', item['message'])

    def test_gap_preserved(self):
        # 空档（2s 收尾 → 3s 起头）必须在文件里看得见：**两个时间戳各写各的**，
        # 谁也不会被并成一个。
        #
        # 注意这份夹具有个坑：`[00:01]你好[00:02][00:03]月亮[00:04]` 是**一行**，
        # 解析器把中间那个没有文字的 `[00:02][00:03]` 读成"一个空字单元"
        # （不是两行之间的空档 —— 想表达空行得换行写）。所以判据是那两个时间戳相邻：
        doc = parse('[00:01]你好[00:02][00:03]月亮[00:04]\n[00:01]こんにちは')
        self.assertIn('<00:02.000><00:03.000>', export(doc))

    def test_encodings(self):
        self.assertEqual(decode(SAMPLE.encode('utf-16'))[0], SAMPLE)
        self.assertEqual(decode(SAMPLE.encode('utf-8-sig'))[0], SAMPLE)
        self.assertEqual(decode('中文'.encode('gb18030'))[0], '中文')

    def test_batch_non_destructive_and_failure_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'input'
            source.mkdir()
            (source / 'good.lrc').write_text(SAMPLE, encoding='utf-8')
            (source / 'bad.lrc').write_text('unparsed', encoding='utf-8')
            (source / 'good.mp3').write_bytes(b'placeholder')
            output = source / 'exports'
            rows = run_batch(source, output)
            self.assertEqual(sorted(r['status'] for r in rows), ['exported', 'failed'])
            saved = (output / 'good.lrc').read_bytes()
            again = run_batch(source, output)
            self.assertEqual(len(again), 2)
            self.assertTrue(all(r['status'] == 'failed' for r in again))
            self.assertEqual((output / 'good.lrc').read_bytes(), saved)
            self.assertEqual((source / 'good.lrc').read_text(encoding='utf-8'), SAMPLE)
            with self.assertRaises(ValueError):
                run_batch(source, source)


if __name__ == '__main__':
    unittest.main()
