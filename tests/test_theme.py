"""主题（深色 / 浅色）：颜色只在 style.css 里定义两套，其余地方一律读令牌。

判据分四层：
  * **结构**：令牌块之外不许出现颜色字面量；JS 里引用的令牌必须在两套里都有。
  * **完整性**：`:root` 里每个颜色令牌都要有浅色取值，浅色块里的名字必须真的存在。
  * **可读性**：正文/次要文字在两种主题下的对比度不得低于下限；波形字格与轨道底
    必须"看得出来分开了"（用感知色差，不用对比度 —— 对比度在浅色下量不出这件事）。
  * **行为**：换主题必须让缓存的静态层重画（`tests/web_theme_check.js`）。
"""
import colorsys
import math
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB = ROOT / 'saltlrc' / 'web'
CSS_PATH = WEB / 'style.css'


def node_binary():
    for candidate in (shutil.which('node'),
                      r'C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'):
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    return None


LITERAL = r'#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)|hsla?\([^)]*\)'
# 诊断浮层的颜色是刻意写死的（见 debug-panel.js / trace.js 顶部说明）
FIXED_DARK_JS = {'debug-panel.js', 'trace.js'}
# 正文 4.5（WCAG AA），形状/按钮等 UI 元素 3.0
TEXT_FLOOR = 4.5
UI_FLOOR = 3.0
CELL_DELTA_E = 8.0


def block_span(text, selector):
    """`selector{...}` 的 (起点, 终点)，用于取令牌块。"""
    match = re.search(re.escape(selector) + r'\s*\{', text)
    if not match:
        raise AssertionError('找不到令牌块：' + selector)
    depth, i = 1, match.end()
    while depth:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
        i += 1
    return match.start(), i


def tokens(text, selector):
    start, end = block_span(text, selector)
    return dict((name, value.strip()) for name, value
                in re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;]+);', text[start:end]))


def parse_colour(value):
    """#rgb / #rrggbb / rgb() / rgba() / hsl() / hsla() -> (r, g, b) 0..1。"""
    value = value.strip()
    if value.startswith('#'):
        body = value[1:]
        if len(body) == 3:
            body = ''.join(c * 2 for c in body)
        return [int(body[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    match = re.fullmatch(r'hsla?\(([^)]*)\)', value)
    if match:
        parts = match.group(1).replace('%', '').split()
        return list(colorsys.hls_to_rgb(float(parts[0]) / 360,
                                        float(parts[2]) / 100, float(parts[1]) / 100))
    match = re.fullmatch(r'rgba?\(([^)]*)\)', value)
    if match:
        parts = [p.strip() for p in match.group(1).split(',')]
        return [min(255.0, float(p)) / 255 for p in parts[:3]]
    raise AssertionError('不认识的取值：' + value)


def luminance(rgb):
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(parse_colour(a)), luminance(parse_colour(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def delta_e(a, b):
    """CIELAB 色差：用来判断"看得出来分开了"，对比度对底色深浅不敏感。"""
    def lab(rgb):
        r, g, b = rgb
        x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
        y = r * 0.2126 + g * 0.7152 + b * 0.0722
        z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

        def f(t):
            return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
        fx, fy, fz = f(x), f(y), f(z)
        return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(lab(parse_colour(a)),
                                                     lab(parse_colour(b)))))


class ThemeTokensTests(unittest.TestCase):
    def setUp(self):
        self.text = CSS_PATH.read_text(encoding='utf-8')
        self.dark = tokens(self.text, ':root')
        self.light = tokens(self.text, 'html[data-theme="light"]')

    def test_no_colour_literal_outside_the_token_blocks(self):
        """颜色只能写在两个令牌块里 —— 别处再写一个字面量就是第二个权威。"""
        masked = self.text
        for selector in (':root', 'html[data-theme="light"]'):
            start, end = block_span(masked, selector)
            masked = masked[:start] + re.sub(r'[^\n]', ' ', masked[start:end]) + masked[end:]
        offenders = ['%d 行：%s' % (number, line.strip()[:80])
                     for number, line in enumerate(masked.splitlines(), 1)
                     if re.search(LITERAL, line)]
        self.assertEqual(offenders, [],
                         '令牌块之外出现了颜色字面量：\n  ' + '\n  '.join(offenders[:6]))

    def test_every_colour_token_has_a_light_value(self):
        """深色里定义的每个颜色令牌，浅色都必须给一份 —— 漏一个就是浅色下的一块深斑。"""
        missing = [name for name, value in self.dark.items()
                   if re.fullmatch(LITERAL, value) and name not in self.light]
        self.assertEqual(missing, [], '浅色块里缺少这些令牌：' + '、'.join(missing))
        # 阴影不是"颜色字面量"但同样要跟着主题走
        for name in ('--shadow-1', '--shadow-2'):
            self.assertIn(name, self.dark)
            self.assertIn(name, self.light, name + ' 没有浅色取值')

    def test_the_light_block_defines_nothing_that_does_not_exist(self):
        """浅色块里的名字拼错时不会报错，只会静默不生效 —— 这里钉住名字必须存在。"""
        orphans = [name for name in self.light if name not in self.dark]
        self.assertEqual(orphans, [], '浅色块里有 :root 不认识的令牌：' + '、'.join(orphans))
        start, end = block_span(self.text, 'html[data-theme="light"]')
        self.assertIn('color-scheme:light', self.text[start:end].replace(' ', ''),
                      '浅色块缺 color-scheme:light（原生控件不会跟着变浅）')

    def test_every_token_used_from_javascript_is_defined_in_both_themes(self):
        """JS 读的令牌必须两套都有（读不到就静默不换色）。"""
        wanted = {}
        for path in sorted(WEB.glob('*.js')):
            if path.name in FIXED_DARK_JS:
                continue
            for name in re.findall(r"Theme\.token\('(--[a-z0-9-]+)'\)",
                                   path.read_text(encoding='utf-8')):
                wanted.setdefault(name, set()).add(path.name)
        self.assertTrue(wanted, '没有任何 JS 在读令牌 —— 判据失效了')
        for name, files in sorted(wanted.items()):
            self.assertIn(name, self.dark, '%s 用了但 :root 里没有：%s' % ('、'.join(files), name))
            self.assertIn(name, self.light, '%s 用了但浅色里没有：%s' % ('、'.join(files), name))

    def test_the_fixed_dark_overlays_say_so(self):
        """刻意写死深色的浮层要在文件里说明，免得被当成漏改。"""
        for name in sorted(FIXED_DARK_JS):
            head = (WEB / name).read_text(encoding='utf-8')[:800]
            self.assertIn('深色', head, name + ' 顶部没说明它为什么不用主题令牌')


class ThemeReadabilityTests(unittest.TestCase):
    def setUp(self):
        text = CSS_PATH.read_text(encoding='utf-8')
        self.pairs = {'dark': tokens(text, ':root'),
                      'light': tokens(text, 'html[data-theme="light"]')}

    def test_text_stays_readable_in_both_themes(self):
        # (前景, 背景, 下限)：正文按 WCAG 的 4.5，形状与按钮这类 UI 元素按 3.0
        pairs = [('--text', '--bg', TEXT_FLOOR), ('--text', '--panel', TEXT_FLOOR),
                 ('--text', '--panel2', TEXT_FLOOR), ('--dim', '--bg', TEXT_FLOOR),
                 ('--dim2', '--panel', TEXT_FLOOR), ('--ink-quiet', '--bg', TEXT_FLOOR),
                 ('--ink-caption', '--bg', TEXT_FLOOR),
                 ('--accent-ink', '--accent-deep', UI_FLOOR),
                 ('--ink-strong', '--bg', UI_FLOOR),
                 ('--wave-scrim-ink', '--wave-scrim', UI_FLOOR),
                 ('--wave-cursor', '--wave-bg', UI_FLOOR),
                 ('--wave-env-main', '--wave-track-bg', UI_FLOOR),
                 ('--wave-env-trans', '--wave-track-bg', UI_FLOOR)]
        problems = []
        for theme, table in sorted(self.pairs.items()):
            for fg, bg, floor in pairs:
                ratio = contrast(table[fg], table[bg])
                if ratio < floor:
                    problems.append('%s：%s 在 %s 上只有 %.2f（要 %.1f）'
                                    % (theme, fg, bg, ratio, floor))
        self.assertEqual(problems, [], '对比度不足：\n  ' + '\n  '.join(problems))

    def test_the_light_theme_is_not_worse_than_the_dark_one(self):
        """浅色不许明显比深色更难读 —— 这是浅色主题最典型的翻车点。

        判据是"同时明显下降且已经不够舒服"：已经很高的比值（十几倍）掉一点点
        并不影响阅读，一律按比例卡会把正常结果判红。
        """
        pairs = [('--text', '--bg'), ('--dim', '--bg'), ('--ink-quiet', '--bg'),
                 ('--wave-env-main', '--wave-track-bg'), ('--wave-cursor', '--wave-bg')]
        problems = []
        for fg, bg in pairs:
            dark = contrast(self.pairs['dark'][fg], self.pairs['dark'][bg])
            light = contrast(self.pairs['light'][fg], self.pairs['light'][bg])
            if light < dark * 0.8 and light < 4.5:
                problems.append('%s 在 %s 上：深色 %.2f → 浅色 %.2f' % (fg, bg, dark, light))
        self.assertEqual(problems, [], '浅色下明显更难读：\n  ' + '\n  '.join(problems))

    def test_lyric_cells_read_as_separate_blocks_in_both_themes(self):
        """字格与轨道底要"看得出来分开了"：用色差，不用对比度。"""
        problems = []
        for theme, table in sorted(self.pairs.items()):
            for cell in ('--wave-cell-main', '--wave-cell-trans'):
                gap = delta_e(table[cell], table['--wave-track-bg'])
                if gap < CELL_DELTA_E:
                    problems.append('%s：%s 与轨道底色差只有 %.1f' % (theme, cell, gap))
        self.assertEqual(problems, [], '字格与底色分不开：\n  ' + '\n  '.join(problems))

    def test_no_light_value_is_a_muddy_mid_tone(self):
        """浅色取值不许落在"既不像底也不像字"的中间地带。

        机械翻转明度就会这样：深色的青绿底翻过来是一块刺眼的中间色。
        描边是例外 —— 边框本来就该取中间调。
        """
        offenders = []
        for name, value in sorted(self.pairs['light'].items()):
            # 描边与聚焦环本来就该取中间调；字格是"色块"不是"底"，
            # 它的可见度由下面那条色差判据单独管。
            if ('edge' in name or 'ring' in name or 'cell' in name
                    or name.startswith('--line')):
                continue
            try:
                hue, light, sat = colorsys.rgb_to_hls(*parse_colour(value))
            except AssertionError:
                continue
            if 0.50 < light < 0.80 and sat > 0.25:
                offenders.append('%s=%s（明度 %.2f 饱和 %.2f）' % (name, value, light, sat))
        self.assertEqual(offenders, [], '浅色取值发浑：' + '、'.join(offenders))


class ThemeWiringTests(unittest.TestCase):
    def test_both_pages_settle_the_theme_before_the_first_paint(self):
        """首屏不能先画一帧深色再变浅色：两页都要在 <head> 里先定下来。"""
        for name in ('index.html', 'library.html'):
            text = (WEB / name).read_text(encoding='utf-8')
            head = text[:text.index('</head>')]
            self.assertIn('dataset.theme', head, name + ' 的 <head> 里没有定主题')
            self.assertIn("localStorage.getItem('saltlrc-theme')", head,
                          name + ' 没有从首屏缓存里读主题')

    def test_every_page_loads_the_theme_module_before_using_it(self):
        for name in ('index.html', 'library.html'):
            text = (WEB / name).read_text(encoding='utf-8')
            self.assertIn('<script src="/theme.js"></script>', text, name + ' 没加载 theme.js')
            self.assertLess(text.index('src="/theme.js"'),
                            text.index('src="/studio.js"') if 'studio.js' in text
                            else text.index('src="/library.js"'),
                            name + '：theme.js 必须在用它之前加载')

    def test_the_server_serves_the_theme_module(self):
        server = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        self.assertIn('"/theme.js": "theme.js"', server)

    def test_the_switch_is_wired_end_to_end(self):
        """复选框 → collectSettings → settings.py 默认值 → applySettings。"""
        settings = (ROOT / 'saltlrc' / 'settings.py').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertIn('"darkMode": True', settings)
        self.assertIn('darkMode: $(\'setDark\').checked', studio)
        self.assertIn('Theme.apply(features.darkMode === false', studio)
        self.assertIn('id="setDark"', html)
        web_dom = (ROOT / 'tests' / 'web_dom_check.js').read_text(encoding='utf-8')
        self.assertIn('setDark', web_dom, '页面级行为检查没有把主题开关算进"必须接线"的清单')


class ThemeBehaviourTests(unittest.TestCase):
    def test_theme_switching_behaviour(self):
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过主题行为检查')
        result = subprocess.run([node, str(pathlib.Path(__file__).parent / 'web_theme_check.js')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         '主题行为检查失败：\n' + (result.stdout + result.stderr))
