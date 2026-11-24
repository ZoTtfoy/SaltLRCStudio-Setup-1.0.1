"""发行文档的约定：**能直接给外人看**。

约定（三条）：

1. **只讲"做什么 / 怎么用 / 会发生什么"**；不写选择过程、不写排查过程、
   不写来源署名这类痕迹。
2. **手册就是手册**：`使用说明.txt` 里不出现版本史章节，也没有被注释掉的历史正文。
3. **内部档案不随交付**：`docs/CHANGELOG.md`（含排查过程）留在源码里，
   交付目录只放 `docs/RELEASE_NOTES.md`（每版改了什么）。

这几条测试守的是同一件事：它们坏掉的方式是**静默**的 —— 文档里混进一段选择过程、
或者把内部档案又塞回交付，都不会有任何东西报错，直到文件被发给别人。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / 'README.md'
RELEASE_NOTES = ROOT / 'docs' / 'RELEASE_NOTES.md'
CHANGELOG = ROOT / 'docs' / 'CHANGELOG.md'
USAGE = ROOT / 'dist' / '使用说明.txt'
INDEX = ROOT / 'dist' / '目录说明.txt'
RELEASE_PS1 = ROOT / 'packaging' / 'release.ps1'

SHIPPED = (README, RELEASE_NOTES, USAGE, INDEX,
           ROOT / 'docs' / 'ARCHITECTURE.md',
           ROOT / 'docs' / 'WINDOW_STRUCTURE.md',
           ROOT / 'docs' / 'AUDIO_ALIGNMENT.md')

# 过程与缘由类痕迹。**只列真正不该出现在发行文档里的说法**：
# "当前用户"（指注册表范围）这类正常用词不在其中；**版本号也不在其中** ——
# 更新日志的版本标题本来就是它的内容。
TRACES = ('用户点名', '用户提出', '用户要求', '用户报', '用户反馈', '教训',
          '踩过', '考古', '我当初', '为什么当初', '实测')

COMMENT = re.compile(r'<!--.*?-->', re.S)
STALE_TITLE = re.compile(r'^#\s+SaltLRC Studio v(?!\d+\.\d+\.\d+$)', re.M)


class ShippedDocumentTests(unittest.TestCase):
    def setUp(self):
        # 交付出去的 `源码\` 里**没有**发行文档（README / 使用说明 / 目录说明 在交付目录的根上），
        # 所以裸源码树里这一整类检查跳过 —— 和其它测试对 `dist/`、`installer.iss` 的处理
        # 同一套约定（"源码树可以独立分发"）。
        # ⚠️ 守卫必须在 `setUp` 里：写成"某一个测试里 skipTest"的话，**其余测试照样会去读文件**
        # 然后 FileNotFoundError。
        missing = [path.name for path in SHIPPED if not path.is_file()]
        if missing:
            self.skipTest('发行文档不在（裸源码树）：缺 ' + '、'.join(missing))

    def test_the_shipped_docs_carry_the_current_version(self):
        from saltlrc import __version__

        self.assertIn('# SaltLRC Studio v%s' % __version__, README.read_text(encoding='utf-8'),
                      'README 标题必须是当前版本')
        self.assertIsNone(STALE_TITLE.search(README.read_text(encoding='utf-8')),
                          'README 正文里出现了旧版本标题')
        for path, prefix in ((USAGE, '# 使用说明（v'), (INDEX, 'SaltLRC Studio v')):
            first = path.read_text(encoding='utf-8').splitlines()[0].strip()
            self.assertTrue(first.startswith(prefix) and __version__ in first,
                            '%s 首行必须带当前版本号，现在是：%s' % (path.name, first))

    def test_the_manual_is_a_manual(self):
        """`使用说明.txt` 里不出现版本史，也没有被注释掉的历史正文。"""
        text = USAGE.read_text(encoding='utf-8')
        self.assertNotIn('<!--', text, '手册里不该有注释块（历史正文已另行归档）')
        for marker in ('这一版改了什么', '上一版改了什么', '古早版本遗产'):
            self.assertNotIn(marker, text, '手册里不该出现版本史章节：' + marker)

    def test_shipped_docs_avoid_process_and_rationale_traces(self):
        offenders = []
        for path in SHIPPED:
            text = path.read_text(encoding='utf-8')
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith(('<!--', '>', '====')):
                    continue
                for trace in TRACES:
                    if trace in line:
                        offenders.append('%s:%d 含「%s」 → %s' % (path.name, number, trace, line.strip()[:60]))
        self.assertEqual(offenders, [],
                         '发行文档里出现了过程/缘由类说法：\n  ' + '\n  '.join(offenders))

    def test_comments_are_well_formed(self):
        """剥完注释不许残留标记 —— HTML 注释不能嵌套，写坏了会静默露出来。"""
        for path in SHIPPED:
            text = path.read_text(encoding='utf-8')
            if text.count('<!--') != text.count('-->'):
                self.fail('%s：注释标记不配对' % path.name)
            if '<!--' in COMMENT.sub('', text) or '-->' in COMMENT.sub('', text):
                self.fail('%s：剥完注释还残留标记' % path.name)


class DeliveryContractTests(unittest.TestCase):
    """交付目录里放什么，与 `release.ps1` 实际复制什么必须一致。"""

    def setUp(self):
        if not RELEASE_PS1.is_file() or not INDEX.is_file():
            self.skipTest('发布脚本或目录说明不在（源码树可独立分发）')
        self.script = RELEASE_PS1.read_text(encoding='utf-8')
        self.index = INDEX.read_text(encoding='utf-8')

    def test_every_shipped_file_is_listed_in_the_index(self):
        """`release.ps1` 复制进去的每个文件，都要在《目录说明》里出现。

        判断用"交付后的名字"（`DestName`），因为交付目录里用的是中文名。
        """
        names = set()
        for line in self.script.splitlines():
            if 'Copy-Item' not in line or 'DestDir' in line:
                continue
            dest = re.search(r"DestName:\s*\"([^\"]+)\"", line)
            if dest:
                names.add(dest.group(1))
                continue
            source = line.split('Join-Path')[1] if 'Join-Path' in line else ''
            parts = re.findall(r"'([^']+)'", line)
            if parts:
                names.add(parts[-1].split('\\')[-1])
        # 《目录说明》自己不必在《目录说明》里再列一遍。
        names = {name for name in names
                 if name and name not in ('$dest', '图标源文件', '源码', '目录说明.txt')}
        missing = sorted(name for name in names if name not in self.index)
        self.assertEqual(missing, [],
                         '这些文件会进交付目录，但《目录说明》里没有：%s' % missing)

    def test_the_internal_changelog_is_not_shipped(self):
        self.assertIn('RELEASE_NOTES.md', self.script, '交付要放发行用的更新日志')
        self.assertNotIn("docs\\CHANGELOG.md", self.script,
                         '内部档案（含排查过程）不随交付发出')
        self.assertTrue(CHANGELOG.is_file(), '内部档案本身要留在源码里')


if __name__ == '__main__':
    unittest.main()
