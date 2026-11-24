"""代码注释只讲"做什么"：不允许把过程、缘由、历史写进注释。

判据：注释与文档字符串只说明这段代码做什么、怎么用、会发生什么；
不写"为什么这样选"、不写排查过程、不写哪一轮改的、不写用户的反馈原话。
硬约束可以写，但要写成事实与后果（"键非空时删不掉，要递归"）。

`BASELINE` 是欠账清单：每个文件的命中数是**存量**，清干净就把那一行删掉。
数字不许变大 —— 清过的文件再脏回去会当场变红。当前清单为空，
所以这是一道**纯闸门**。逐条明细：`python tests/test_comment_style.py --report`。
"""
import ast
import io
import pathlib
import re
import sys
import tempfile
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = {
    '历史叙事': r'当初|当时|曾经|以前是|第一版|上一版|上一轮|那时候|一开始|早年',
    '踩坑叙事': r'踩过|踩了|教训|实测|犯过|撞上|卡了',
    '用户痕迹': r'用户点名|用户要求|用户提出|用户报|用户反馈|用户说|用户投诉',
    '缘由框': r'之所以|原因是|为什么|究其原因|原因：|这么做是因为|是为了',
    '版本标记': r'v0\.[0-9]|v1\.[0-9]',
    '历史叙事(en)': r'\bused to\b|\bpreviously\b|\boriginally\b|\bat first\b|\bno longer\b|'
                  r'\bhas always\b|\bthe reason\b|\bwhich is why\b|\bthat is why\b|'
                  r'\bturns out\b|\bwe found\b|\bwhy\b',
    '缘由框(en)': r'\bbecause\b|\bso that\b|\btherefore\b|\bhence\b|\bsince it\b',
}

BASELINE = {
    # 清单为空：仓库里已经没有"讲原因"的注释了。
}


def docstring_lines(source):
    """真文档字符串占的行号：模块/类/函数体里的**第一个**字符串语句。

    三引号字符串不等于文档字符串 —— 本仓库里有用三引号存 JS/HTML 模板的，
    那种字符串是数据，扫它会把数据里的英文单词当注释判据（已经误报过一次）。
    """
    lines = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            continue
        start = body[0].value.lineno
        end = getattr(body[0].value, 'end_lineno', start) or start
        lines.update(range(start, end + 1))
    return lines


def comment_lines(path):
    """只取注释与文档字符串（Python 用 tokenize + AST；其它语言按注释语法）。"""
    if path.suffix == '.py':
        out = []
        try:
            source = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            return out
        docstrings = docstring_lines(source)
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    out.append((token.start[0], token.string))
                elif token.type == tokenize.STRING and token.start[0] in docstrings:
                    for offset, line in enumerate(token.string.splitlines()):
                        out.append((token.start[0] + offset, line))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
        return out
    text = path.read_text(encoding='utf-8')
    out = []
    if path.suffix in ('.js', '.css'):
        for block in re.finditer(r'/\*.*?\*/', text, re.S):
            for offset, line in enumerate(block.group(0).splitlines()):
                out.append((text[:block.start()].count(chr(10)) + 1 + offset, line))
        for match in re.finditer(r'(?m)^\s*//.*$', text):
            out.append((text[:match.start()].count(chr(10)) + 1, match.group(0)))
    elif path.suffix == '.html':
        for block in re.finditer(r'<!--.*?-->', text, re.S):
            for offset, line in enumerate(block.group(0).splitlines()):
                out.append((text[:block.start()].count(chr(10)) + 1 + offset, line))
    return out


def scan(root=None):
    """返回 {相对路径: [(分类, 行号, 原文)]}。"""
    root = root or ROOT
    compiled = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}
    targets = []
    for pattern in ('saltlrc/**/*.py', 'saltlrc/web/*.js', 'saltlrc/web/*.css',
                    'saltlrc/web/*.html', 'tests/*.py', 'app_entry.py'):
        targets.extend(root.glob(pattern))
    found = {}
    for path in sorted(set(targets)):
        rel = path.relative_to(root).as_posix()
        if rel == 'tests/test_comment_style.py':
            continue
        hits = []
        for number, line in comment_lines(path):
            for name, regex in compiled.items():
                if regex.search(line):
                    hits.append((name, number, line.strip()[:90]))
        if hits:
            found[rel] = hits
    return found


class CommentStyleTests(unittest.TestCase):
    def test_no_file_gains_new_traces(self):
        """已经清过的文件必须保持干净；没清过的文件不许变多。"""
        found = scan()
        regressions = []
        for rel, hits in sorted(found.items()):
            allowed = BASELINE.get(rel, 0)
            if len(hits) > allowed:
                sample = '；'.join('%s@%d %s' % (kind, number, text)
                                   for kind, number, text in hits[:3])
                regressions.append('%s：%d 处（允许 %d） 例：%s' % (rel, len(hits), allowed, sample))
        self.assertEqual(regressions, [],
                         '这些文件的注释里出现了过程/缘由类说法：\n  ' + '\n  '.join(regressions))

    def test_the_baseline_does_not_drift_upwards(self):
        """欠账清单本身也要跟得上：清干净的文件要从清单里消失。"""
        found = scan()
        stale = [rel for rel in BASELINE if rel not in found]
        self.assertEqual(stale, [],
                         '这些文件已经清干净了，请把它们的名字从 BASELINE 里删掉：' + '、'.join(stale))

    def test_the_scanner_reads_every_kind_of_comment(self):
        """扫描器自身的守卫：六类目标文件都要读到，且判据要真的会命中。

        先在临时目录里造一份"处处违规"的样例：如果扫描器读不到某种注释，
        或某条正则写错了，这里会当场变红 —— 而不是等到整个仓库都干净了、
        主判据返回空集时，看上去一切正常。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'saltlrc' / 'sub').mkdir(parents=True)
            (root / 'saltlrc' / 'web').mkdir(parents=True)
            (root / 'tests').mkdir(parents=True)
            (root / 'app_entry.py').write_text(
                '# 之所以这样写：当初踩过坑\n'
                '# v0.13.2 起改为按行处理\n'
                'def f():\n'
                '    """It used to work because the path differed."""\n',
                encoding='utf-8')
            (root / 'saltlrc' / 'sub' / 'deep.py').write_text(
                '"""第一版遗留。"""\n', encoding='utf-8')
            (root / 'saltlrc' / 'web' / 'a.js').write_text(
                '// previously this lived elsewhere\nconst x = 1;\n', encoding='utf-8')
            (root / 'saltlrc' / 'web' / 'a.css').write_text(
                '/* 为什么要单独一块 */\n.x{color:red}\n', encoding='utf-8')
            (root / 'saltlrc' / 'web' / 'a.html').write_text(
                '<!-- 用户报过 -->\n<p>x</p>\n', encoding='utf-8')
            (root / 'tests' / 'test_a.py').write_text(
                '"""当初按行处理。"""\n', encoding='utf-8')

            found = scan(root)
            for rel in ('app_entry.py', 'saltlrc/sub/deep.py', 'saltlrc/web/a.js',
                        'saltlrc/web/a.css', 'saltlrc/web/a.html', 'tests/test_a.py'):
                self.assertIn(rel, found, '这份文件里的注释没被读到：' + rel)
                self.assertTrue(found[rel], '读到了注释但判据没命中：' + rel)
            kinds = {kind for hits in found.values() for kind, _, _ in hits}
            self.assertTrue({'历史叙事', '踩坑叙事', '用户痕迹', '缘由框', '版本标记',
                             '历史叙事(en)', '缘由框(en)'} - kinds == set(),
                            '有分类一条都没命中，正则可能写错了：%s' % sorted(kinds))

    def test_a_clean_file_produces_no_hit(self):
        """反方向：描述"做什么"的注释不许被判据抓出来。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'tests').mkdir()
            (root / 'tests' / 'test_a.py').write_text(
                '"""条形图数据：返回每个桶的计数。"""\n'
                '# 只保留非空桶\n'
                'def f(items):\n'
                '    return [x for x in items if x]\n',
                encoding='utf-8')
            self.assertEqual(scan(root), {}, '干净的注释被误判了')

    def test_triple_quoted_data_is_not_a_comment(self):
        """三引号字符串是数据不是注释：里面夹着 JS/HTML 时不许按注释判。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'saltlrc').mkdir()
            (root / 'saltlrc' / 'tpl.py').write_text(
                'TEMPLATE = """\n'
                '  function bad(error) { return {ok: false, why: error.message}; }\n'
                '  // previously this returned null\n'
                '"""\n'
                'def f():\n'
                '    """返回模板。"""\n'
                '    return TEMPLATE\n',
                encoding='utf-8')
            self.assertEqual(scan(root), {}, '把三引号数据当成注释了')

    def test_a_real_docstring_is_still_scanned(self):
        """反方向：真正的文档字符串要被扫到（上面那条不能把判据整没了）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'saltlrc').mkdir()
            (root / 'saltlrc' / 'doc.py').write_text(
                '"""当初是另一个写法。"""\n'
                'class K:\n'
                '    """第一版遗留。"""\n'
                '    def m(self):\n'
                '        """实测过。"""\n',
                encoding='utf-8')
            found = scan(root)
            self.assertIn('saltlrc/doc.py', found)
            self.assertEqual(len(found['saltlrc/doc.py']), 3,
                             '模块/类/方法的文档字符串应各命中一次：%s' % found)

    def test_the_scanner_reaches_the_real_tree(self):
        """扫描器真的走了整个仓库，而不是只扫到零星几个文件。"""
        seen = {rel for rel in
                (p.relative_to(ROOT).as_posix() for p in ROOT.glob('saltlrc/**/*.py'))}
        self.assertTrue(len(seen) > 20, '仓库里的 Python 文件数看起来不对：%d' % len(seen))
        self.assertTrue(comment_lines(ROOT / 'saltlrc' / 'server.py'),
                        'server.py 的注释一条都没读到')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--report':
        for rel, hits in sorted(scan().items(), key=lambda item: -len(item[1])):
            print('%4d  %s' % (len(hits), rel))
            for kind, number, text in hits[:40]:
                print('       %s:%d  %s' % (kind, number, text))
        raise SystemExit(0)
    unittest.main()
