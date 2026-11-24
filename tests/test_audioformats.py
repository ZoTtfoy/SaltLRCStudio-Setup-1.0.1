"""音频格式：**只能有一个权威**（`saltlrc/audioformats.py`）。

"什么算音频文件"只有 `audioformats.py` 一份清单 —— 别的文件里再出现一串
音频扩展名就是第二份清单，迟早与它漂移。所以这里既验**行为**（页面拿到的
清单来自服务端），也验**静态事实**（没有第二份清单藏在别的文件里）。
"""
import re
import unittest
from pathlib import Path

from saltlrc import audioformats, batch, library, server

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'saltlrc' / 'web'


class CatalogueTests(unittest.TestCase):
    def test_the_two_python_callers_share_one_list(self):
        self.assertEqual(set(library.AUDIO_EXTENSIONS), set(audioformats.extensions()),
                         'library.py 又有了自己的一份清单')
        self.assertEqual(set(batch.AUDIO), set(audioformats.extensions()),
                         'batch.py 又有了自己的一份清单（它以前就少 4 种）')

    def test_decodable_and_unknown_partition_the_list(self):
        everything = audioformats.extensions()
        self.assertEqual(audioformats.decodable() | audioformats.unknown(), everything)
        self.assertEqual(audioformats.decodable() & audioformats.unknown(), frozenset(),
                         '一个格式不能既"能解码"又"不能解码"')
        self.assertTrue(audioformats.unknown(), '至少要有一个"列出来但解不了"的例子，否则这条分栏就是摆设')

    def test_mime_types_are_sane(self):
        self.assertEqual(audioformats.mime_for('a.MP3'), 'audio/mpeg', '大小写不该影响判断')
        self.assertEqual(audioformats.mime_for('a.flac'), 'audio/flac')
        self.assertEqual(audioformats.mime_for('没有扩展名'), 'application/octet-stream')
        self.assertEqual(audioformats.mime_for('a.xyz'), 'application/octet-stream')
        for extension in audioformats.extensions():
            self.assertTrue(audioformats.mime_for('x' + extension).startswith('audio/'),
                            extension + ' 的 MIME 不是 audio/*')

    def test_an_unsupported_format_says_what_to_do(self):
        """列出来 ≠ 能用：解不了的格式要给一句**可执行**的话。

        用户导入 .ape 时，以前要等"播放没声音、波形是空的"才发觉。
        """
        for extension in audioformats.unknown():
            note = audioformats.unsupported_note('x' + extension)
            self.assertTrue(note, extension + ' 没有给出说明')
            self.assertIn('mp3', note, '要告诉用户转成什么格式')
        for extension in audioformats.decodable():
            self.assertIsNone(audioformats.unsupported_note('x' + extension),
                              '能用的格式不该每次都唠叨一句')

    def test_the_server_serves_the_right_type(self):
        """`/api/audio` 的 MIME 来自同一张表。"""
        source = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        self.assertIn('audioformats.mime_for(', source, '/api/audio 必须用统一的那张表')
        self.assertNotIn('AUDIO_TYPES', source, 'server.py 里不该再有第二份 MIME 表')

    def test_the_page_asks_the_server_instead_of_hardcoding(self):
        """页面不自己写死扩展名清单 —— 它从 `/api/capabilities` 拿。

        只有 Python 那一侧存在清单**一次**；页面里再写一遍就是第四份定义。
        """
        capabilities = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        self.assertIn('capabilities["audio_extensions"]', capabilities)
        self.assertIn('capabilities["audio_notes"]', capabilities,
                      '连"怎么办"那句话也要下发，页面只负责显示')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertIn('audioExtensions()', studio)
        # 旧写法：一条把 9 种扩展名写死在正则里的判定（还少了 aiff）。
        self.assertIsNone(re.search(r'\\\.\(mp3\|flac\|m4a\|wav', studio),
                          'studio.js 里不该再有写死的音频扩展名正则')

    def test_no_second_audio_list_hides_anywhere_else(self):
        """**静态**兜底：别的文件里不许再出现"一串音频扩展名"。

        判据是"同一行里出现两个以上音频扩展名字面量" —— 正常代码里不会这样写，
        而一份扩展名清单一定是这样写的。这样将来谁再抄一份，这里立刻红。
        """
        extensions = ['.mp3', '.flac', '.m4a', '.wav', '.ogg', '.opus', '.aac', '.ape', '.wv', '.aiff']
        offenders = []
        for folder, pattern in ((ROOT / 'saltlrc', '*.py'), (WEB, '*.js')):
            for path in folder.rglob(pattern):
                if path.name in ('audioformats.py', 'notify.js'):
                    continue
                text = path.read_text(encoding='utf-8')
                for number, line in enumerate(text.splitlines(), 1):
                    if line.strip().startswith(('#', '//')):
                        continue                      # 注释里举例说明不算
                    hits = [one for one in extensions if one in line]
                    if len(hits) >= 2:
                        offenders.append('%s:%d %s' % (path.name, number, line.strip()[:70]))
        self.assertEqual(offenders, [],
                         '这些地方又写了一份音频扩展名清单（请改用 saltlrc/audioformats.py）：\n  '
                         + '\n  '.join(offenders))


if __name__ == '__main__':
    unittest.main()
