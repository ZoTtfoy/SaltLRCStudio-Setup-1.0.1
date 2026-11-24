"""Front-end load regression: every page's scripts must load without
ReferenceError/TypeError in a DOM stub (Node), and the buttons that matter
must actually end up with a click handler.

This guards the whole class of "helper defined on one page, used on the other"
bugs — the word editor's 应用/取消 buttons died exactly that way: bind() lived
only in studio.js, so library.js aborted mid-load and everything after that
line (including the button wiring) never ran.
"""
import re
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'saltlrc' / 'web'
HERE = Path(__file__).resolve().parent
HARNESS = HERE / 'web_load_harness.js'

def page_scripts(page):
    """The <script src> list of a page, in document order.

    Read from the markup rather than written here again: the order is already
    stated in the page, and a second copy of it in the tests is exactly how a
    newly added script silently stops being loaded by the harness (splitting
    studio.js did precisely that — history.js was never evaluated).
    """
    text = (WEB / page).read_text(encoding='utf-8')
    return [m for m in re.findall(r'<script src="/([A-Za-z0-9_.-]+)"></script>', text)]


PAGES = {
    # /library (library.html)
    'library': {
        'page': 'library.html',
        'handlers': ['applyWords', 'closeWordEdit'],
        'run': 'web_word_editor_check.js',
    },
    # /studio.html (index.html) — also served at "/"
    'studio': {
        'page': 'index.html',
        'handlers': ['applyText', 'closeText', 'applyWords', 'closeWords', 'help', 'closeHelp',
                     'pickLibrary', 'libClose', 'playPause'],
        'run': None,
    },
}

for _entry in PAGES.values():
    _entry['scripts'] = page_scripts(_entry['page'])


def node_binary():
    for candidate in (shutil.which('node'),
                      r'C:\Users\Administrator\.workbuddy\binaries\node\versions\22.22.2-3\node.exe'):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _node_path():
    """jsdom 装在托管工作区里，跑 node 检查时得把它指给子进程。"""
    candidates = [os.environ.get('NODE_PATH', ''),
                  r'C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules']
    return os.pathsep.join(c for c in candidates if c)


class WebLoadTests(unittest.TestCase):
    def test_common_js_is_loaded_first_on_both_pages(self):
        for html in ('library.html', 'index.html'):
            text = (WEB / html).read_text(encoding='utf-8')
            self.assertIn('<script src="/common.js"></script>', text, html + ' 必须最先加载 common.js')

    def test_bind_and_download_live_in_common_js_only(self):
        common = (WEB / 'common.js').read_text(encoding='utf-8')
        self.assertIn('function bind(', common)
        self.assertIn('function download(', common)
        for name in ('library.js', 'studio.js'):
            source = (WEB / name).read_text(encoding='utf-8')
            self.assertNotIn('function bind(', source, name + ' 不应再定义 bind')
            self.assertNotIn('function download(', source, name + ' 不应再定义 download')

    def test_word_editor_markup_ids(self):
        html = (WEB / 'library.html').read_text(encoding='utf-8')
        for element_id in ('wordEditDialog', 'wordGrid', 'wordEnd', 'applyWords', 'closeWordEdit'):
            self.assertIn('id="' + element_id + '"', html, 'library.html 缺少 #' + element_id)

    def test_no_script_binds_an_element_declared_later_in_the_page(self):
        """A script must not bind an id that the parser has not reached yet.

        The 曲库 word editor's 应用/取消 sat on the SAME line as the
        ``<script src="/library.js">`` but AFTER it. A browser executes an
        external script synchronously at that point in the document, so
        ``document.getElementById('closeWordEdit')`` returns null, ``bind()``
        warns to a console the packaged window does not show and returns, and
        both buttons are never wired at all. Clicking them does literally
        nothing: the handler is never reached.

        The rule is purely about markup order, so it is read from the source
        rather than exercised. jsdom cannot see this: with
        ``resources:'usable'`` it fetches external scripts asynchronously and
        has already parsed the whole document by the time they run, silently
        repairing the defect. A harness that cannot fail this way is exactly the
        trap this check exists to close.
        """
        pattern = re.compile(r"(?:bind|[$])\(\s*'([A-Za-z0-9_]+)'")
        for page in ('library.html', 'index.html', 'debug.html'):
            html = (WEB / page).read_text(encoding='utf-8')
            for script in re.findall(r'<script src="/([A-Za-z0-9_.-]+)"></script>', html):
                path = WEB / script
                if not path.is_file():
                    continue
                # The earliest tag is the strictest reading of the rule.
                script_at = html.index('<script src="/%s"></script>' % script)
                source = path.read_text(encoding='utf-8')
                for line_no, line in enumerate(source.splitlines(), 1):
                    # Top-level statements are written flush left; anything
                    # indented runs inside a function, i.e. after load.
                    if not line.strip() or line[:1].isspace():
                        continue
                    for element_id in pattern.findall(line):
                        declared = html.find('id="%s"' % element_id)
                        self.assertGreaterEqual(
                            declared, 0,
                            '%s:%d 绑定了 #%s，但 %s 里没有这个元素'
                            % (script, line_no, element_id, page))
                        where = html[:declared].count('\n') + 1
                        self.assertLess(
                            declared, script_at,
                            '%s:%d 绑定 #%s，但 #%s 写在 %s 第 %d 行（在 '
                            '<script src="/%s"> 之后）——浏览器先跑脚本、后解析该元素，'
                            'bind() 拿到 null 后静默返回，这个控件是死的'
                            % (script, line_no, element_id, element_id, page, where, script))

    def test_a_closed_dialog_can_never_be_rendered(self):
        """A closed <dialog> must be display:none even if a rule sets `display`.

        `dialog.wordBox{display:flex}` (the tall cell grid needs flex between a
        fixed header and a pinned action bar) is an AUTHOR rule, and author rules
        outrank the user-agent sheet **whatever the specificity**, so it silently
        beats the engine's own `dialog:not([open]){display:none}`. A closed word
        editor then keeps rendering: absolutely positioned at its place in the
        document, horizontally centred, with no ::backdrop — a stray panel glued
        to the bottom of the page, while `close()` appears to do nothing.

        The compensation has to be `!important`; otherwise it silently depends on
        coming after every other `display:` on a dialog in the file — the same
        order-dependence as a script binding an element written after it.

        This is asserted from the stylesheet rather than exercised: jsdom applies
        no cascade at all and would pass either way.
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        display_rules = [line.strip() for line in css.splitlines()
                         if line.strip().startswith('dialog')
                         and 'display:' in line
                         and ':not([open])' not in line]
        self.assertTrue(display_rules,
                        '至少有一条给 dialog 设置 display 的规则，否则这条检查没有意义')
        self.assertIsNotNone(
            re.search(r'dialog:not\(\[open\]\)\{display:none\s*!important\}', css),
            '关闭状态的对话框必须 display:none !important；'
            '当前给 dialog 设 display 的规则有：%s' % display_rules)

    def test_word_editing_reconciles_boundaries_instead_of_failing(self):
        """Word edits must go through the shared reconciliation, not raw assignment.

        Raw assignment of the last word's time past the line end lets
        validateEdit reject the result: the word does not move and the message
        never mentions the line end. The boundaries are handled in one place
        (``Doc.moveWordStart`` / ``Doc.fitLineEnd``) and the outcome is guaranteed
        legal.

        The behaviour itself is covered by tests/web_document_check.js; this test
        only pins the wiring — "the UI stopped calling the helper" is not
        something the helper's own tests can notice.
        """
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertIn('Doc.moveWordStart(', studio, '逐字拖动必须走 Doc.moveWordStart')
        self.assertIn('Doc.fitLineEnd(', studio, '应用逐字时间必须走 Doc.fitLineEnd')
        # The raw assignment is what makes the tail a dead end.
        self.assertNotIn('else track.words[word].start = time;', studio,
                         '不应再直接赋值逐字时间（越过句尾会被整笔拒绝）')

    def test_both_pages_reference_an_icon_that_exists(self):
        """Every page must point at a favicon, and that file must be served.

        The reference and the file are checked together on purpose: a dangling
        icon link is invisible in the UI (the browser just shows its own default)
        and would only be noticed by looking at the tab, which is exactly the
        kind of thing that ships broken — same failure shape as the licence
        notices that pointed at files nobody had ever shipped.
        """
        for html in ('index.html', 'library.html'):
            text = (WEB / html).read_text(encoding='utf-8')
            self.assertIn('<link rel="icon"', text, html + ' 缺少 favicon 声明')
            self.assertIn('href="/favicon.png"', text, html + ' favicon 路径必须是 /favicon.png')
        icon = WEB / 'favicon.png'
        self.assertTrue(icon.is_file(), 'favicon.png 必须真的存在于 saltlrc/web/')
        # A real icon, not a placeholder: PNG magic + a size worth shipping.
        self.assertEqual(icon.read_bytes()[:8], b'\x89PNG\r\n\x1a\n', 'favicon.png 不是 PNG')
        self.assertGreater(icon.stat().st_size, 2000, 'favicon.png 太小，八成是占位图')

    def test_the_executable_is_built_with_the_app_icon(self):
        """The build must hand PyInstaller the .ico.

        On Windows the window and taskbar icon is read from the executable's own
        resources — pywebview has no ``icon`` parameter — so this one flag is the
        only thing that makes the app recognisable in the taskbar, and nothing in
        the UI would reveal its absence.

        It is asserted on build.ps1 rather than on SaltLRCStudio.spec: the spec
        is a by-product that PyInstaller REGENERATES on every build, so adding the
        icon there looks right, does nothing, and is silently overwritten.
        That is exactly the mistake this test exists to prevent.
        """
        script = (ROOT / 'packaging' / 'build.ps1').read_text(encoding='utf-8')
        match = re.search(r'--icon\s+"([^"]+)"', script)
        self.assertIsNotNone(match, 'build.ps1 没有把 --icon 传给 PyInstaller')
        icon = ROOT / match.group(1).replace('\\', '/')
        self.assertTrue(icon.is_file(), 'build.ps1 指向的图标不存在：' + match.group(1))
        self.assertEqual(icon.read_bytes()[:4], b'\x00\x00\x01\x00', '不是合法的 .ico')

    def test_the_lyric_panel_keeps_one_size(self):
        """The preview panel must not resize as the lyrics come and go.

        Reported as "请固定歌词显示窗口的大小，不要因为没有歌词显示就突然缩小".
        Every row reserved its height EXCEPT the current line, so before a song's
        first lyric (and between lines) that row collapsed to nothing and the
        panel shrank — dragging the waveform and the table up with it. A long
        line grew it just as visibly.

        The assertion is "the size does not depend on the content": every row
        states a fixed `height`, and no row relies on `min-height` alone. jsdom
        applies no layout, so this is read from the stylesheet — the same reason
        the word-seam checks are.

        （面板是**常驻**的一整块 —— 用户看过收成细带的版本之后点名要改回来，
        "本来权重很高的地方现在反而看起来没什么权重了"。）
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        for scope in ('#preview', '#karaokeInner'):
            for row in ('dim', 'now', 'trans'):
                rule = re.search(r'%s\s*\.prow\.%s\{([^}]*)\}' % (re.escape(scope), row), css)
                self.assertIsNotNone(rule, '缺少 %s .prow.%s 的规则' % (scope, row))
                self.assert_fixed_height(rule.group(1), '%s .prow.%s' % (scope, row))
        # The legacy light-theme block set min-height on the same panel: a second
        # authority over the very thing being fixed.
        self.assertNotIn('#preview{text-align:center;background:#f0f5f2', css,
                         '旧主题里那条 #preview 规则应已删除（它对高度是第二处权威）')

    def assert_fixed_height(self, body, what):
        """写死了 plain height（line-height / min-height 都不算数）。"""
        plain = re.search(r'(?<!min-)(?<!line-)height\s*:\s*([^;]+)', body)
        self.assertIsNotNone(
            plain, '%s 必须写明固定 height，否则缺内容时会塌陷（line-height 不算）' % what)
        self.assertNotIn('min-height', body,
                         '%s 只写了 min-height：内容一多就会把面板撑高' % what)
        self.assertNotEqual(plain.group(1).strip(), 'auto',
                            '%s 的 height 不能是 auto（会随内容变）' % what)

    def test_both_sliders_reach_their_ends(self):
        """音量与缩放滑块的极左/极右必须是 0 和 100。

        Reported as "我需要这两个滑块到极左和极右的时候为 0 和 100，而不是留有
        部分空间无法拖动". Two separate causes:

        * the zoom slider ran 20–400 px/s, so its ends read "20" and "400" — the
          slider and the number box were two different scales. It is a 0–100
          percentage now, with the px/s value still in the box;
        * the native range track is INSET from the element box, so the fill
          stopped short of the edges — space you could see but not drag into.
          The track is drawn by us now, filled from ``--fill``.
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        for element_id, name in (('volume', '音量'), ('zoom', '缩放')):
            tag = re.search(r'<input id="%s"[^>]*>' % element_id, html).group(0)
            self.assertIn('min="0"', tag, name + '滑块最小值必须是 0')
            self.assertIn('max="100"', tag, name + '滑块最大值必须是 100')
        # 自绘轨道：两端由 --fill 决定，原生内缩的轨道已换掉。
        self.assertRegex(css, r'input\[type=range\]::-webkit-slider-runnable-track',
                         '滑块轨道必须自绘（原生轨道两端内缩，填充到不了边）')
        self.assertIn('--fill', css, '填充比例要由 --fill 控制，否则 0/100 落不到两端')
        self.assertIn('appearance:none', css.replace(' ', ''), '需去掉原生外观才能自绘轨道')
        # 填充比例只有一个写者。
        self.assertIn('function paintRange(', studio)
        self.assertIn('paintRange(slider)', studio, '音量变化要刷新填充条')
        self.assertIn('paintRange($(\'zoom\'))', studio, '缩放变化要刷新填充条')

    def test_every_bar_has_a_stable_name(self):
        """界面上每一"栏"都要有可引用的名字，便于后续按名字改动。

        Asked for as "完成应用窗口结构优化，将每行应用栏命名并给我一份".
        The names are also asserted here so a later edit cannot quietly drop one.
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        for bar in ('appHeader', 'appNav', 'statusLine', 'queuePane', 'tabBar',
                    'wavePane', 'lyricPreview', 'transportBar', 'songStrip', 'waveFrame',
                    'editDock', 'lyricTable', 'alignPane', 'projectPane'):
            self.assertIn('data-bar="%s"' % bar, html, '缺少命名栏位：' + bar)

    def test_toolbar_controls_are_not_left_floating(self):
        """编辑坞里的控件只允许由 flex 行摆放，不许带绝对定位属性。

        `#aiTranslate` / `#aiTranslateAll` 一旦被 `position:absolute` + `top` / `right`
        钉住，就会被抽出文档流、在工具栏里只留下一条空白缝 —— 看不到也点不到。
        因此编辑坞里的控件不得写 `position:absolute/fixed`，也不得出现
        `top:` / `right:`（只有绝对定位才需要它们）。
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        dock = html[html.index('id="editDock"'):html.index('</div>', html.index('id="editDock"'))]
        in_dock = re.findall(r'id="([A-Za-z0-9_]+)"', dock)
        offenders = []
        for element_id in in_dock:
            for rule in re.findall(r'#%s[^{]*\{([^}]*)\}' % re.escape(element_id), css):
                body = rule.replace(' ', '')
                if 'position:absolute' in body or 'position:fixed' in body:
                    offenders.append('#%s 被绝对定位' % element_id)
        self.assertEqual(offenders, [],
                         '编辑坞里的控件被抽出了文档流（会飘到别处、点不到）：%s' % offenders)
        # 单独盯住这两个按钮的定位属性。
        for element_id in ('aiTranslate', 'aiTranslateAll'):
            for rule in re.findall(r'#%s[^{]*\{([^}]*)\}' % element_id, css):
                body = rule.replace(' ', '')
                self.assertNotIn('position:absolute', body, '#%s 不该绝对定位' % element_id)
                self.assertNotIn('top:', body, '#%s 不该有 top（那是绝对定位才需要的）' % element_id)

    def test_the_app_bar_is_the_dock(self):
        """顶部那一行应用级按钮就是 dock：`position:sticky;top:0`，滚动时不动。

        用户："请你将最上面一行应用级的按钮做成dock"。停靠的判据是**始终可见**。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        nav = html[html.index('id="appNav"'):html.index('</nav>', html.index('id="appNav"'))]
        for element_id in ('pickLibrary', 'files', 'folder', 'demo', 'help', 'settings',
                           'save', 'embedAudio', 'export', 'zip', 'encoding'):
            self.assertIn('id="%s"' % element_id, nav, '#%s 应在顶部 dock 内' % element_id)
        rule = re.search(r'#appNav\{([^}]*)\}', css).group(1).replace(' ', '')
        self.assertIn('position:sticky', rule, '顶部这条必须是粘性停靠')
        self.assertIn('top:0', rule, '停靠位置在窗口顶部')

    def test_the_translate_buttons_the_import_and_export_entries_live_in_the_right_bars(self):
        """**导入/导出的入口必须在顶部 dock；改这首歌的内容留在编辑坞。**

        判据是"始终可见"：dock 承载流水线的入口与出口，因此
          * **导入/导出/保存**（pickLibrary·files·folder·save·export·zip）→ `#appNav`；
          * **改这首歌的内容**（撤销/重做/添加行/整体偏移/检查/翻译）→ `#editDock`。

        `#editDock` 只存在于「波形编辑」标签页，`#export` 留在里面时切到别的标签页
        就够不着 —— 所以导出只能放在 `#appNav`。

        AI 翻译两个按钮的作用是翻译当前已加载的歌词，因此与「检查歌词」同栏、
        不在 `#appNav` 里；按钮的 `title` 要写明"翻译当前已加载的歌词"。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        nav = html[html.index('id="appNav"'):html.index('</nav>', html.index('id="appNav"'))]
        dock = html[html.index('id="editDock"'):html.index('</div>', html.index('id="editDock"'))]

        # ① 导入/导出的全部入口都在顶部 dock 里（任何标签页都够得着）
        for element_id in ('pickLibrary', 'files', 'folder', 'save', 'export', 'zip'):
            self.assertIn('id="%s"' % element_id, nav,
                          '#%s 是导入/导出的入口，必须在顶部 dock 里' % element_id)
        # ② 导出**不许**再留在编辑坞（它只存在于「波形编辑」标签页，切页就没了）
        self.assertNotIn('id="export"', dock,
                         '导出歌词必须只在顶部 dock：editDock 跟着「波形编辑」标签页消失')

        # ③ 改这首歌的内容留在编辑坞，且顺序稳定：检查歌词 → AI 翻译
        order = re.findall(r'id="(check|aiTranslate|aiTranslateAll)"', dock)
        self.assertEqual(order, ['check', 'aiTranslate', 'aiTranslateAll'],
                         '编辑坞顺序应为：检查歌词 → AI 翻译（本行/缺失行）')
        for element_id in ('aiTranslate', 'aiTranslateAll'):
            self.assertNotIn('id="%s"' % element_id, nav, '翻译按钮不在顶部 dock 里')
        # 作用必须写在提示里（"翻译当前已加载的歌词"），否则下次改动又会放错组。
        for element_id in ('aiTranslate', 'aiTranslateAll'):
            pattern = r'id="%s"[^>]*title="([^"]*)"' % element_id
            title = re.search(pattern, html)
            self.assertIsNotNone(title, '#%s 缺少 title' % element_id)
            self.assertIn('当前已加载的歌词', title.group(1),
                          '#%s 的 title 要写清"翻译当前已加载的歌词"' % element_id)

    def test_the_translate_buttons_are_not_in_the_lyric_preview(self):
        """翻译按钮不得出现在歌词预览面板内。"""
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        preview = html[html.index('id="preview"'):html.index('id="preview"') + 1200]
        self.assertNotIn('aiTranslate', preview, '翻译按钮不应再留在歌词预览面板里')

    def test_the_check_button_says_what_it_is_for(self):
        """「校验」两个字看不出干什么 —— 它必须是"检查能不能导出"这件可读的事。

        Reported as "导出歌词旁边的校验是干什么用的？". The button runs /api/validate
        and lists what the exporter would refuse, but the label said nothing about
        that, and its findings appear on ANOTHER tab with no word spoken on this
        one — a click that changes nothing on screen.
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        tag = re.search(r'<button id="check"[^>]*>([^<]*)</button>', html)
        self.assertIsNotNone(tag)
        self.assertEqual(tag.group(1), '检查歌词', '按钮文案要说清是"检查歌词"')
        self.assertIn('title=', tag.group(0), '要有一句说明它做什么、结果在哪')
        # 点了必须有反馈（结果在别的页 → 至少状态行要出声）
        self.assertIn('function checkOutLoud(', studio)
        self.assertIn("bind('check', checkOutLoud)", studio, '接线要走会出声的那个')
        self.assertRegex(studio, r"switchPane\('project'\)", '有错误时应切到列出问题的那一页')

    def test_option_toggles_are_switches_not_boxes(self):
        """可选项统一成开关（滑轨 + 圆点），一处 CSS 覆盖全部页面与弹窗。

        用户："将所有可选项目UI优化成开关样式"。做法是给 `input[type=checkbox]`
        写开关样式（而不是逐个改标记）：工具栏、设置面板、曲库页的选项都命中它，
        以后新增的可选项自动就是开关。

        例外要**显式**保留为方框：队列勾选 / 对齐候选 —— 那是"挑条目"不是"开关"。
        清单也在这里钉住，防止哪天有人把某类勾选漏出例外（会变成开关挤在列表里）。
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        rule = re.search(r'(?<![\w.#-])input\[type=checkbox\]\{([^}]*)\}', css).group(1)
        body = rule.replace(' ', '')
        self.assertIn('appearance:none', body, '要自绘开关就必须去掉原生外观')
        self.assertIn('border-radius:9px', body, '开关是胶囊形')
        self.assertIn('width:34px', body)
        self.assertIn('height:18px', body)
        # 通用 `input{padding:8px}` 会把开关撑大，所以这里必须清零 padding
        self.assertIn('padding:0', body, '必须清掉通用 input 的 padding，否则开关被撑成 50x34')
        self.assertIn('input[type=checkbox]::after', css, '要有圆点（::after）')
        self.assertIn('input[type=checkbox]:checked', css, '要有选中态')
        self.assertIn('translateX(16px)', css, '选中时圆点要滑到右边')
        # 例外：挑条目的方框必须还原成原生外观。
        # 判据用**剥掉注释**的副本 —— 注释里提一句 `#proposals` 就会满足它。
        # 贴唱列表只有「试听」按钮，没有勾选框，所以它不是例外。
        bare = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
        for scope in ('.pick', '#queue', '#matchCandidates'):
            self.assertIn(scope, bare, '例外的勾选作用域要写明：' + scope)
        self.assertNotIn('#proposals', bare,
                         '#proposals 已随 v0.22.0 的重构删除，不该再有规则（也没了勾选框）')
        self.assertIn('appearance:auto', css, '例外项要还原成原生方框')
        # 页面里确实有可选项会用上它
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertGreaterEqual(html.count('type="checkbox"'), 8,
                                '可选项数量骤降？开关样式应覆盖全部可选项')

    def test_only_the_one_rule_may_size_a_switch(self):
        """开关的盒子只能由那一条规则决定：别的规则不许再量它的宽高。

        用户："帮我优化一下设置里的开关按钮，他们有的与文字打架了"。
        真因不是配色也不是间距，而是**第二个权威**（本项目头号老病）：
        `.setgrid input[type=checkbox]{width:auto;padding:0}` 是"复选框还是原生方框"
        那个年代留下的，优先级 0,2,1 压过基础规则 0,1,1 →
        设置面板里的开关轨道塌回默认宽度（约 13px），
        而圆点照样按 `left:2px + translateX(16px)` 走 → **圆点跑到轨道外面、压在文字上**。
        文案越长，圆点出界得越明显。

        按**不变式**验，不按症状验：
          ① 只有基础规则能写开关的宽/高/padding/圆角；
          ② 凡是给 `input` 写几何的通用规则，都必须排除勾选框（`:not([type=checkbox])`）——
             今天没命中也算数，因为 0,2,1 随时会盖掉 34px（"以后在工具栏加个开关"就会踩到）；
          ③ 圆点选中的位置必须**留在轨道里**（几何自洽，这条才是"打架"的本质）。
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        bare = re.sub(r'/\*.*?\*/', '', css, flags=re.S)

        # ① + ② 逐条规则扫：选择器会不会命中勾选框、里面有没有几何属性。
        #    "安全"的只有三种：基础开关规则本身、圆点（::after，由 ③ 管几何）、
        #    以及**裸 `input`**（优先级 0,0,1，本来就压不过基础规则）。
        #    任何带类/属性/后代前缀的 `input` 规则（0,2,1）都会盖掉 34px —— 除非排除勾选框。
        exempt = ('#queue', '#matchCandidates', '.pick')
        geometry = re.compile(r'(?<![\w-])(width|height|padding|border-radius)\s*:')
        offenders = []
        for match in re.finditer(r'([^{}]+)\{([^}]*)\}', bare):
            selector, body = match.group(1).strip().replace('\n', ' '), match.group(2)
            for part in selector.split(','):
                part = part.strip()
                if 'input' not in part or any(name in part for name in exempt):
                    continue
                if 'type=' in part and 'type=checkbox' not in part:
                    continue                      # 明确写了别的 type，命不中
                if part in ('input[type=checkbox]', 'input[type=checkbox]::after'):
                    continue                      # 权威规则本身与圆点
                if ':not([type=checkbox])' in part:
                    continue                      # 已经主动排除勾选框
                if part == 'input':
                    continue                      # 裸 input：压不过基础规则
                if not geometry.search(body):
                    continue                      # 不写盒子，无害
                offenders.append(part + ' （比基础规则优先级高，会盖掉开关的盒子）')
        self.assertEqual(offenders, [],
                         '开关的盒子出现了第二个权威，圆点会跑出轨道压到文字上：%s' % offenders)

        # ③ 几何自洽：轨道宽 ≥ 圆点左边界 + 位移 + 圆点宽。
        track = re.search(r'(?<![\w.#-])input\[type=checkbox\]\{([^}]*)\}', bare).group(1)
        knob = re.search(r'input\[type=checkbox\]::after\{([^}]*)\}', bare).group(1)
        width = float(re.search(r'(?<!min-)(?<!max-)width:\s*([\d.]+)px', track).group(1))
        left = float(re.search(r'left:\s*([\d.]+)px', knob).group(1))
        size = float(re.search(r'(?<!min-)(?<!max-)width:\s*([\d.]+)px', knob).group(1))
        slide = float(re.search(r'translateX\(([\d.]+)px\)', bare).group(1))
        self.assertLess(left + slide, width, '圆点选中的位置已经出界（左边界越过了轨道右端）')
        self.assertLessEqual(left + slide + size, width,
                             '圆点选中的位置越过了轨道右端 → 它会压在文字上：'
                             '轨道 %.0f，圆点在 %.0f~%.0f' % (width, left + slide, left + slide + size))

        # 设置面板里功能开关那一块：每个开关**整行**，别挤在两列网格里折三行。
        self.assertIn('grid-column:1/-1', re.search(r'\.setgrid label\.inline\{([^}]*)\}',
                                                    bare).group(1),
                      '功能开关应当整行显示（一列只有三百多像素，长说明会折成三行）')

    def test_the_word_cells_are_visibly_separated(self):
        """The word-timing cells must not read as one strip.

        Reported as "绿色提示框还是连在一起，能不能分开" → "逐字编辑器的字格 / 没有间距、
        粘在一起". The grid gap was 6px with the cell fill and the page tone one step
        apart, which is enough to measure and not enough to see: the cells read as a
        single bar. The gap is what this pins — a later "tidy up the CSS" pass that
        drops it back to a token value would silently undo the fix.
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        match = re.search(r'\.wordGrid\{([^}]*)\}', css)
        self.assertIsNotNone(match, '.wordGrid 规则不见了')
        body = match.group(1)
        gap = re.search(r'gap:\s*(\d+)px(?:\s+(\d+))?', body)
        self.assertIsNotNone(gap, '.wordGrid 没有设置间距')
        vertical = int(gap.group(1))
        horizontal = int(gap.group(2) or gap.group(1))
        self.assertGreaterEqual(vertical, 18, '字格纵向间距太小，会粘成一条')
        self.assertGreaterEqual(horizontal, 18, '字格横向间距太小，会粘成一条')
        # One BASE rule, not two: a second bare `.wordGrid{…}` block is how the value
        # drifted before. Scoped rules (e.g. `dialog.wordBox .wordGrid{…}`) are
        # legitimate and must not be counted as duplicates — counting the raw
        # substring flagged exactly that.
        base = [line for line in css.splitlines() if line.strip().startswith('.wordGrid{')]
        self.assertEqual(len(base), 1, '.wordGrid 基础规则只应有一条')

    def test_the_footer_claims_the_waveform_interaction_as_original(self):
        """The footer must present the waveform interaction as this project's own.

        The footer must not credit another product for the interaction design in
        the one place users actually read. The project's own THIRD_PARTY_NOTICES
        already states the frontend is independently implemented and that no
        implementation was copied, so "原创" is the accurate wording — and the
        attribution itself stays documented in the notices file, which is where a
        licence obligation belongs (not in a user-facing tagline).
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        footer = html.split('<footer>', 1)[1].split('</footer>', 1)[0]
        self.assertNotIn('King LRC', footer, 'footer 不应把波形交互归功于其它产品')
        self.assertNotIn('KingLRC', footer, 'footer 不应把波形交互归功于其它产品')
        self.assertIn('原创', footer, 'footer 应写明波形交互为原创设计')

    def test_third_party_notices_do_not_point_at_missing_files(self):
        """A notice may not claim to ship a licence file that is not there.

        THIRD_PARTY_NOTICES.md referenced `licenses/…-MIT.txt` and three others,
        none of which has ever existed in any delivery. A licence notice that
        points at absent files is worse than a plain attribution: it looks like
        the obligation was handled when it was not.

        The file itself is a distribution artifact rather than a source module,
        so its absence is not a failure — there is simply nothing to check. That
        keeps this test meaningful when the source tree is copied out on its own
        (the delivery ships it alongside, but a bare source checkout need not).
        """
        notices_path = ROOT / 'THIRD_PARTY_NOTICES.md'
        if not notices_path.is_file():
            self.skipTest('THIRD_PARTY_NOTICES.md 不在本目录（源码树可独立分发）')
        notices = notices_path.read_text(encoding='utf-8')
        referenced = re.findall(r'licenses/[A-Za-z0-9._-]+', notices)
        missing = [name for name in referenced if not (ROOT / name).is_file()]
        self.assertEqual(missing, [], 'notices 指向了不存在的许可文件：' + ', '.join(missing))

    def test_folder_export_is_written_by_the_backend(self):
        # showDirectoryPicker could create the file and then refuse the write
        # (0-byte export). The folder is now chosen and written by the backend.
        source = (WEB / 'library.js').read_text(encoding='utf-8')
        self.assertNotIn('window.showDirectoryPicker', source, '不应再使用浏览器文件夹接口（会写出 0 字节）')
        self.assertIn("api('export-folder'", source, '导出文件夹必须走后端 /api/export-folder')

    def test_the_native_media_bar_is_not_used(self):
        """The browser's own <audio controls> bar must not be rendered.

        It is painted by the browser, ignores our CSS, and follows the OS/browser
        colour scheme — on this dark page it came out as a bright light-themed
        band (#f1f3f4, 40px tall, full pane width) across the middle of the
        editor, which is what the user reported as "this shouldn't appear".
        Its size in the stylesheet matched the measured band exactly
        (audio{height:40px}), which is how it was identified.

        Asserting the ABSENCE of the attribute is the point: a styled-but-present
        native bar would still be drawn by the browser in its own colours.
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('controls', html.split('<audio', 1)[1].split('>', 1)[0],
                         'audio 标签内不能再有 controls 属性')
        self.assertRegex(html, r'<audio id="audio"\s*>', 'audio 元素应保留但不带 controls')
        # The replacement transport must exist, or playback loses its mouse UI.
        for element_id in ('playPause', 'playTime'):
            self.assertIn('id="' + element_id + '"', html, 'index.html 缺少 #' + element_id)

    def test_native_widgets_follow_the_dark_theme(self):
        """`color-scheme: dark` must be declared.

        It is the only lever that makes the browser draw ITS OWN widgets —
        media bars, form controls, scrollbars, focus rings — in the dark palette.
        Without it, anything native we did not replace would come back light.
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        self.assertRegex(css, r'color-scheme\s*:\s*dark', 'style.css 必须声明 color-scheme:dark')
        # And the audio element itself must stay hidden, since a native bar
        # cannot be styled into our palette.
        self.assertRegex(css, r'audio\s*\{[^}]*display\s*:\s*none', 'audio 必须隐藏')

    def test_wide_screen_grid_accounts_for_splitter_handle(self):
        # splitter.js 把 10px 把手插成 main 的第三个子元素。不变量是
        # **网格项数 == 轨道数**，两个断点各管一半：
        #   * ≥1250px：三轨（左栏 / 10px 把手 / minmax(0,1fr)）；
        #   * <1250px：两轨（把手 display:none，不再是网格项）。
        # 模板多一轨，`section` 就会掉进那条 10px 的轨、整个编辑器被挤成一条；
        # 少一轨则大屏下把手会吞掉 1fr 列。宽度只能来自 `--split-left`
        # （分栏拖动只改它，不许内联写模板）。
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        parts = css.split('@media(min-width:1250px)')
        self.assertTrue(len(parts) > 1, 'style.css 缺少 min-width:1250px 媒体查询')
        self.assertRegex(parts[1], r'grid-template-columns:[^;{}]*10px\s+minmax\(0,1fr\)',
                         '大屏网格必须给 splitter 手柄留出 10px 列')
        self.assertIn('var(--split-left)', parts[1],
                      '左栏宽度要来自变量 —— 分栏拖动只许改它，不许内联写模板')
        base = re.search(r'(?m)^main\{([^}]*)\}', css)
        self.assertIsNotNone(base, 'style.css 里找不到基础 main 规则')
        self.assertNotIn('10px minmax', base.group(1),
                         '基础（窄屏）模板只有两轨：把手在这以下是 display:none，'
                         '多出来的那一轨会让 section 掉进去')

    def test_document_layer_loads_before_the_pages_that_use_it(self):
        # document.js is layer 0 (mirror of saltlrc/document.py + edits.py). If
        # it loads after studio.js, the whole editor dies with "Doc is not
        # defined" — the same class of bug as the missing bind().
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<script src="/document.js"></script>', html)
        self.assertLess(html.index('/document.js'), html.index('/studio.js'),
                        'document.js 必须在 studio.js 之前加载')

    def test_studio_has_no_private_copy_of_the_document_rules(self):
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        for signature in ('function validateEdit(', 'function trackStart(', 'function shiftTrack('):
            self.assertNotIn(signature, studio, signature + ' 应只在 document.js 中定义一处')
        self.assertIn('const {trackStart, shiftTrack, validateEdit} = Doc;', studio)

    def test_document_layer_matches_the_server_rules(self):
        # Same fixtures as tests/test_edits.py on the Python side.
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过文档层行为检查')
        result = subprocess.run(
            [node, str(HARNESS), '--run=' + str(HERE / 'web_document_check.js'), str(WEB / 'document.js')],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         '前端文档层与服务端规则不一致：\n' + (result.stdout + result.stderr))

    def test_project_file_ui_and_rules(self):
        # The owned .saltproj format replaces the bare JSON dump: save must go
        # through the packer and the page needs 打开工程 / 内嵌音频 controls.
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        # 打开工程不再有独立控件：入口是顶部的文件选择器（importFiles 按扩展名分派）。
        for element_id in ('files', 'embedAudio', 'save', 'export'):
            self.assertIn('id="' + element_id + '"', html, 'index.html 缺少 #' + element_id)
        self.assertIn('.saltproj', html, '文件选择器必须接受 .saltproj')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertIn('Doc.packProject(', studio)
        self.assertIn('Doc.unpackProject(', studio)
        self.assertNotIn('JSON.stringify({version: 2', studio, '旧的裸 JSON 导出应已替换为 .saltproj')

    def test_opening_a_project_has_exactly_one_entry(self):
        """One entry point, not two.

        Reported as "上面dock栏的打开工程功能也和下面的打开工程冲突". The dock's
        file picker and a separate 打开工程 button both led here — same action,
        two controls, and the hidden <input> to go with the second one.
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertNotIn('id="openProject"', html, '不应再有独立的「打开工程」按钮')
        self.assertNotIn('id="projectFile"', html, '不应再有第二个工程文件选择器')
        self.assertNotIn("bind('openProject'", studio, 'studio.js 不应再为它接线')
        # The one entry: the dock picker, whose handler dispatches .saltproj.
        self.assertIn('id="files"', html)
        self.assertIn(r'/\.saltproj$/i.test(name)', studio,
                      '顶部文件选择器必须按扩展名分派到 openProjectFile')

    def test_lyric_export_and_project_save_are_different_jobs(self):
        """导出歌词 writes lyrics only; 保存工程 is the one that can carry audio.

        Reported as "导出当前歌词与保存工程…保存歌词时应当不内嵌音频". The embed
        checkbox sat between the two buttons with nothing saying which it
        belonged to, and the export went through a page blob download — which
        saves nothing inside the packaged window ("现在导出功能不可用").
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')

        # Both go through the backend's Save-As dialog, not a blob download.
        export_block = studio[studio.index("bind('export'"):studio.index("bind('zip'")]
        self.assertIn("api('save-as'", export_block, '导出歌词必须走后端另存为')
        self.assertNotIn('download(', export_block, '导出歌词不应再走页面的 blob 下载')
        self.assertNotIn('audioFiles', export_block, '导出歌词不得涉及音频')
        self.assertNotIn('embedAudio', export_block, '导出歌词不得读「工程内嵌音频」')

        save_block = studio[studio.index('async function saveProject'):studio.index('async function openProjectFile')]
        # 保存工程走 /api/save-binary（原始字节）：服务端仍弹同一个「另存为」
        # 对话框，请求体按原始字节处理、不当作 JSON 解析（内嵌音频不走 base64，
        # 直接发原始字节）。
        self.assertIn("apiBinary('/api/save-binary?", save_block,
                      '保存工程要走另存为那一个出口（现在走原始字节版）')
        self.assertNotIn('download(', save_block, '保存工程不应走页面的 blob 下载')
        self.assertIn('packProject(', save_block)
        self.assertIn('packContainer(', save_block, '内嵌音频要走容器形态（不带 base64）')
        self.assertIn('embedAudio', save_block, '内嵌音频只属于保存工程')

        # And the checkbox says whose it is.
        self.assertIn('工程内嵌音频', html)
        # Manual pan must suspend auto-follow.
        self.assertIn('followSuspended', (WEB / 'waveform.js').read_text(encoding='utf-8'))
        self.assertIn('height:44px', (WEB / 'style.css').read_text(encoding='utf-8'),
                      '贴条应加高到 44px')

    def test_the_splitter_only_supplies_a_css_variable(self):
        """拖动分栏只能写 CSS 变量 —— **不许写内联的 grid-template-columns**。

        **内联样式压过所有样式表规则，包括 @media**（与优先级无关）。样式表只在
        窗口 ≥1250px 时用三轨布局，并在这以下把 `#splitter` 设成 `display:none` ——
        若脚本同时内联写三轨模板：窗口一到 1250px 以下，把手被移出布局
        （不再是网格项），模板却仍是三轨 → **`section`（整个编辑器）掉进那个
        10px 的轨道**。

        判据（四条，全是静态事实）：
          1. 分栏脚本里不许出现内联列定义；
          2. 样式表里 `main` 的列定义只许用 `var(--split-left)`（宽度只有一个来源）；
          3. 两边用的是**同一个变量名**（各写一套就会漂）；
          4. 切轨数的那条 @media 与隐藏把手的那条必须成对 —— 少了任何一条，
             网格项与轨道数就对不上。
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')
        splitter = (WEB / 'splitter.js').read_text(encoding='utf-8')
        # **先剥注释再判**：注释里会提到 `gridTemplateColumns` 这种字样 ——
        # 不剥的话断言比的是"文件里出现过这个词"，而不是"代码里这么写了"。
        bare = re.sub(r'(?s)/\*.*?\*/', '', splitter)
        bare = re.sub(r'(?m)//.*$', '', bare)

        self.assertNotIn('gridTemplateColumns', bare,
                         '分栏不许内联写 grid-template-columns：内联样式压过 @media，'
                         '窗口变窄时网格项与轨道数会对不上（整个编辑器会被挤进 10px 那条轨）')
        self.assertIn('--split-left', splitter, '分栏宽度必须通过 CSS 变量交给样式表')
        # **每一处**主网格的列定义都要读那个变量 —— 只查"某个地方出现过"
        # 会漏掉"基础规则写死、宽屏规则读变量"这种半吊子改法（拖分栏在窄屏就失效）。
        columns = [value for value in re.findall(r'grid-template-columns:([^;}]+)', css)
                   if 'minmax(0,1fr)' in value]
        self.assertTrue(len(columns) >= 2, '主网格应当有基础与宽屏两处列定义：%s' % columns)
        for value in columns:
            self.assertIn('var(--split-left)', value,
                          '主网格的列定义必须读 --split-left（否则拖分栏在那一档失效）：' + value)

        # 断点成对：宽屏给把手一条轨，窄屏把把手藏掉（并回到两轨）。
        self.assertRegex(css, r'@media\(min-width:1250px\)\s*\{\s*main\{[^}]*'
                              r'10px\s+minmax\(0,1fr\)',
                         '≥1250px 才有多出来的把手轨')
        self.assertRegex(css, r'@media\(max-width:1249px\)\s*\{\s*#splitter\{display:none\}',
                         '≤1249px 必须把把手藏起来 —— 与上一条必须成对，否则轨道数对不上')

    def test_no_inline_style_fights_a_media_query(self):
        """**同一个属性不许既被 `@media` 控制、又被 JS 内联写。**

        内联样式压过 `@media`，于是断点**静默失效**：窗口一跨过 1250px，
        网格项数与轨道数就对不上，整个编辑器被挤进那条 10px 的轨。

        判据：把"`@media` 里出现过的属性"与"JS 里内联写过的属性"比一遍。
        交集必须为空 —— 唯一的例外是两个**浮动面板**的 `display`
        （调试面板 / 追踪面板，它们不在任何 `@media` 里，所以这里是允许的），
        而"它们不在任何 @media 里"这件事**由断言本身去查**，不是靠人记。

        两边的注释都先剥掉，否则断言会被注释里的字样骗到。
        """
        css = (WEB / 'style.css').read_text(encoding='utf-8')

        # ① @media 块里出现过的属性（按花括号配对取整段）
        media_props = set()
        for match in re.finditer(r'@media[^{]*\{', css):
            depth = 0
            for index in range(match.end() - 1, len(css)):
                if css[index] == '{':
                    depth += 1
                elif css[index] == '}':
                    depth -= 1
                    if depth == 0:
                        media_props |= set(re.findall(r'([a-z-]+)\s*:', css[match.end() - 1:index + 1]))
                        break
        self.assertIn('grid-template-columns', media_props, '夹具失效：没解析到 @media 里的属性')

        # ② JS 里内联写过的属性（剥注释）
        inline = {}
        for path in sorted(WEB.glob('*.js')):
            text = re.sub(r'(?s)/\*.*?\*/', '', path.read_text(encoding='utf-8'))
            text = re.sub(r'(?m)//.*$', '', text)
            for name in re.findall(r'\.style\.([A-Za-z][A-Za-z0-9]*)', text):
                kebab = re.sub(r'([A-Z])', lambda m: '-' + m.group(1).lower(), name).lstrip('-')
                inline.setdefault(kebab, set()).add(path.name)
            for name in re.findall(r'''\.style\.setProperty\(\s*['"]([^'"]+)''', text):
                inline.setdefault(name.lstrip('-'), set()).add(path.name)

        clash = {name: sorted(files) for name, files in inline.items() if name in media_props}
        # 唯一的例外：两个浮动面板的 `display`（调试面板 / 追踪面板）。
        # 这个例外**不是靠人记的** —— 下面先断言它们不在任何 @media 里，
        # 证明了它们与断点无关，才把 display 从交集里去掉。
        media_heads = [block.split('{')[0] for block in css.split('@media')[1:]]
        for panel in ('debugPanel', 'tracePanel'):
            self.assertFalse(any(panel in head for head in media_heads),
                             '@media 不许控制浮动面板 %s —— 否则它就与这条检查的例外冲突了' % panel)
        clash.pop('display', None)
        self.assertEqual(clash, {},
                         '这些属性既被 @media 控制、又被 JS 内联写 —— 内联会静默压掉断点：%s' % clash)

    def test_word_editor_is_structured_not_a_textarea(self):
        # The studio word editor is now card-based (one card per unit, tap
        # mode with Enter); the raw "秒 | 文字" textarea is gone.
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        for element_id in ('wordChips', 'tapMode', 'wordEndTime', 'wordAutoSplit', 'wordSpread', 'wordEndNow'):
            self.assertIn('id="' + element_id + '"', html, 'index.html 缺少 #' + element_id)
        self.assertNotIn('id="wordText"', html, '裸文本编辑器应已移除')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertNotIn("$('wordText')", studio, 'studio.js 不应再引用 wordText')
        self.assertIn('function wordTap(', studio)
        self.assertIn('function renderWordChips(', studio)
        # The routes live in saltlrc/api/ since the transport split, so look
        # where they now live rather than assuming one file holds them all.
        routes = "\n".join((ROOT / 'saltlrc' / 'api' / ('%s.py' % name)).read_text(encoding='utf-8')
                           for name in ('lyrics', 'project', 'system', 'align', 'login', 'debug'))
        self.assertIn('/api/tokenize', routes, '自动分词需要后端 tokenize 端点')

    def test_editor_owns_the_library_and_the_help(self):
        # The app opens into the editor ("/" → index.html); the library is a
        # dialog inside it, and all hint text lives in the help dialog.
        server = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        self.assertIn('"/": "index.html"', server, '“/” 必须直接进编辑器')
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        for element_id in ('pickLibrary', 'libraryDialog', 'libSearch', 'libList', 'libClose',
                           'help', 'helpDialog', 'closeHelp'):
            self.assertIn('id="' + element_id + '"', html, 'index.html 缺少 #' + element_id)
        self.assertNotIn('<summary>操作提示</summary>', html, '操作提示应统一收进帮助菜单')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        for name in ('function openLibraryDialog', 'async function openFromLibrary',
                     'function renderLibrary', 'function resetEditor'):
            self.assertIn(name, studio, 'studio.js 缺少 ' + name)
        # Pan limit must come from the waveform, never from `duration || 0`.
        self.assertIn('wave.maxOffset()', studio)
        self.assertNotIn('const duration = wave.duration || 0', studio)
        wave = (WEB / 'waveform.js').read_text(encoding='utf-8')
        self.assertIn('maxOffset()', wave)
        self.assertIn('this.maxOffset()', wave)

    def test_every_page_script_loads_without_reference_errors(self):
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过前端加载冒烟')
        for page, config in PAGES.items():
            with self.subTest(page=page):
                paths = [str(WEB / name) for name in config['scripts']]
                for path in paths:
                    self.assertTrue(Path(path).is_file(), '缺少脚本：' + path)
                command = [node, str(HARNESS), '--check-handlers=' + ','.join(config['handlers'])]
                if config['run']:
                    command.append('--run=' + str(HERE / config['run']))
                command += paths
                result = subprocess.run(command, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0,
                                 page + ' 页面脚本加载失败：\n' + (result.stdout + result.stderr))

    def test_the_transport_owns_the_speed_and_the_pause_point(self):
        """倍速 + 「从暂停点继续」的两条规则，各自只有一个权威。

        用户："我需要倍速功能，还有每次播放后暂停，再次播放从暂停点开始，
        并加一个这个选项的开关在设置里"。

        行为由 `tests/web_transport_check.js`（27 项）验；这里钉的是**接线与单一权威**，
        因为"某处又自己算了一遍"是这类改动的典型退化方式：

        * 合法档位只在 studio.js 的 `SPEEDS` 里定义 → HTML 的 `#speed` 不许写死选项；
        * `playbackRate` 与 `defaultPlaybackRate` 必须一起写 —— 换音源时浏览器会把前者
          重置回后者，只写一个的话每选一首歌倍速就悄悄回到 1×；
        * 「下次从哪播放」只能由 `playbackStart()` 一处决定；
        * 设置里的开关要真的进 `collectSettings`（否则勾了保存不了）。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        settings = (ROOT / 'saltlrc' / 'settings.py').read_text(encoding='utf-8')

        # 倍速控件在走带条里，而且选项由 JS 生成。
        bar = html[html.index('id="transportBar"'):html.index('id="audioInfo"')]
        self.assertIn('id="speed"', bar, '倍速下拉应在走带条里（用的时候就在手边）')
        self.assertNotIn('<option', bar, '档位不许在 HTML 里再写一遍：合法范围只有 SPEEDS 一处')
        self.assertIn('const SPEEDS = [', studio)
        self.assertIn('fillSpeedOptions()', studio, '下拉的选项要由 SPEEDS 生成')
        self.assertIn('restoreSpeed()', studio, '倍速要被记住')

        # 两个 rate 一起写：这条是浏览器行为，不是风格问题。
        block = studio[studio.index('function applySpeed('):studio.index('function restoreSpeed(')]
        self.assertIn('audio.playbackRate = rate', block)
        self.assertIn('audio.defaultPlaybackRate = rate', block,
                      '不写 defaultPlaybackRate：换音源（load()）会把它重置回 1×')
        self.assertIn('preservesPitch', block, '慢放不该变调')

        # 位置只有一个权威。
        self.assertIn('let playbackMark = null;', studio)
        self.assertIn('function playbackStart(audio)', studio)
        self.assertIn('const anchor = playbackAnchor(audio);', studio,
                      '按播放时必须问这唯一的权威，而不是信元素自己的读数')
        # 写位置的地方就是这几个显式动作 + 暂停点（函数定义本身也算一次，共 7 处）。
        self.assertEqual(studio.count('markPlaybackPosition('), 6,
                         '写播放位置的调用点数量变了：新增/删除要连着这里的数字与注释一起改')
        for reason in ('定位到', '跳到第', '重选同一首', '暂停于', '试听贴唱结果'):
            self.assertIn(reason, studio, '记位置时要写清是谁改的（排查用）：' + reason)

        # 设置里的开关：存在、回显、保存三处都要有。
        self.assertIn('id="setResume"', html, '设置里要有这个开关')
        self.assertIn("$('setResume').checked", studio)
        self.assertEqual(studio.count("resumeAfterPause: $('setResume').checked"), 1,
                         '开关必须进 collectSettings，否则勾了存不下来')
        self.assertIn('"resumeAfterPause": True', settings,
                      '默认必须是"从暂停点继续"（用户点名要的行为）')

    def test_repeat_playback_wins_over_resume(self):
        """「反复播放」和「从暂停点继续」是同一个手势的两种答案 —— 优先级写死一处。

        用户："我还需要在任意时间轴的任意位置（指针指定）播放后暂停再次播放时跳回播放前
        的时间轴位置继续播放，人话就是反复播放，另外这两个功能一定会冲突，请你做好逻辑运算"。

        冲突是真的（同一个动作、两种期望），所以定成显式优先级：**反复优先**。
        `playbackAnchor()` 是唯一做这个判断的地方 —— 两条规则在别处各判一次的话，
        早晚会漂移（本项目头号老病）。这里钉接线，行为在 `web_transport_check.js`。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        settings = (ROOT / 'saltlrc' / 'settings.py').read_text(encoding='utf-8')

        # 两个槽位 + 一个汇合点：位置（暂停点/显式点）和"这一段起点"分开存，
        # 判断只在 playbackAnchor() 里做一次。
        self.assertIn('let playbackMark = null;', studio, '位置槽位')
        self.assertIn('let segmentStart = null;', studio, '这一段起点的槽位')
        self.assertIn('function playbackAnchor(audio)', studio, '两条规则只在这一处汇合')
        self.assertIn('const anchor = playbackAnchor(audio);', studio,
                      '按播放必须问这个唯一的汇合点')
        # playbackStart 只能被 playbackAnchor 调用 —— 多一处就是第二个判断者。
        self.assertEqual(studio.count('playbackStart('), 2,
                         'playbackStart 应当只出现在定义与 playbackAnchor 里')

        # 显式换位置必须作废上一段（"任意位置、指针指定"靠的就是这条）。
        self.assertIn('if (freshSegment) segmentStart = null;', studio)
        explicit = [line for line in studio.splitlines()
                    if 'markPlaybackPosition(' in line and 'true);' in line]
        self.assertEqual(len(explicit), 3,
                         '显式换位置的 3 个调用点（定位到 / 跳到第N行 / 试听贴唱结果）'
                         '都要传 freshSegment=true，否则换了位置还回到旧起点')

        # 「反复」是走带条上的实时控件 + 设置里的持久入口。
        bar = html[html.index('id="transportBar"'):html.index('id="audioInfo"')]
        self.assertIn('id="repeat"', bar, '「反复」开关应在走带条里（一边听一边开关）')
        self.assertIn('id="setRepeat"', html, '设置里也要有（持久入口）')
        self.assertEqual(studio.count("repeatSegment: $('setRepeat').checked"), 1,
                         '开关必须进 collectSettings，否则勾了存不下来')
        self.assertIn("api('settings/save', {features: {repeatSegment: on}})", studio,
                      '走带条那个开关要立刻存（局部更新，别碰表单里别的字段）')
        self.assertIn('"repeatSegment": False', settings, '默认关：先保持"继续往下听"的直觉')
        # 优先级写在代码里，也写在设置界面上 —— 用户看得到才不会以为是 bug。
        self.assertIn('优先于上一条', html)

    def test_space_toggles_playback_not_the_focused_control(self):
        """空格=播放/暂停；「有焦点的控件」不许抢走它。

        用户："现在因为是网页端结构，空格键会有改变选定按键状态的功能，请帮我修复这个bug"。
        网页里空格天生还有第二个含义：**激活当前有焦点的控件**。点一次「反复」之后焦点留在
        那个勾选框上，按空格切换的就是它。行为由 `web_transport_check.js` 验（57 项），
        这里钉接线与"单一权威"——原来那段判定在**两处**各写了一遍、而且把 checkbox/select
        也算成"正在输入"，所以这个 bug 一直没被改掉。
        """
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')

        # 判定只有一个：`isTypingTarget`，而且必须只认文本输入。
        self.assertNotIn("el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'", studio,
                         '内联的"是不是输入框"判定应当已被 isTypingTarget 取代')
        self.assertEqual(studio.count('isTypingTarget('), 4,
                         'isTypingTarget 的调用点：定义 1 + 守卫 1 + 方向键 1 + 动作块 1')
        body = studio[studio.index('function isTypingTarget('):studio.index('function keyboardFocus(')]
        self.assertIn('TEXT_INPUT_TYPES.includes(type)', body, '只把文本类 input 当输入框')
        for kind in ('checkbox', 'radio', 'range', 'file'):
            self.assertNotIn("'" + kind + "'", body,
                             '勾选框/单选/滑块/文件都不是"正在打字"，不能算进去')

        # 无障碍那一半：Tab 走过来的焦点要把空格让给控件。
        self.assertIn('function keyboardFocus(', studio)
        self.assertIn("el.matches(':focus-visible')", studio)
        self.assertIn('if (keyboardFocus(el)) return;', studio)

        # 兜底那一半：鼠标点完非输入控件就把焦点放掉（不依赖浏览器是否让我们取消激活）。
        click = studio[studio.index("document.addEventListener('click'"):]
        click = click[:click.index('}, true);')]
        self.assertIn('isTypingTarget(el) || keyboardFocus(el)', click, '两种焦点都要保住')
        self.assertIn('el.blur()', click, '其余情况必须主动放掉焦点')

    def test_the_fit_pane_has_exactly_one_entry_and_a_preview(self):
        """「识别与对齐」页：一个「智能贴唱」按钮 + 一个**可选**预览。

        运行逻辑：导入歌词 → 按本地模型智能贴唱 → 可选把结果同步预览到波形编辑界面。
        粒度为整行，分析为纯本机能量，预览可播放核对。

        这里钉的是**界面事实**：只有一套算法与一套控件，行内逐字细化在逐字编辑器里；
        行为由 `web_align_check.js` 与 `tests/test_fit.py` 验。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        pane = html[html.index('id="alignPane"'):html.index('id="projectPane"')]

        # ① 一个入口：旧按钮/旧控件都不许回来
        for gone in ('id="analyze"', 'id="globalAlign"', 'id="refineWords"',
                     'id="cancelAnalysis"', 'id="applyAlignment"', 'id="engine"',
                     'id="sourceLanguage"', 'id="radius"', 'id="proposals"'):
            self.assertNotIn(gone, html, gone + ' 应随重构收掉（旧的三种做法并成一个）')
        for gone in ('function analyze(', 'function globalAlign(', 'function refineWords(',
                     'function renderProposals(', 'function engineReady(',
                     'function updateEngineControls('):
            self.assertNotIn(gone, studio, gone + ' 应随重构删除')

        # ② 新控件：按钮 + 预览开关 + 应用/丢弃 + 轨道/最大移动量
        for element_id in ('smartFit', 'fitPreview', 'fitApply', 'fitDiscard',
                           'saveTranscript', 'sourceTrack', 'maxShift', 'fitState',
                           'fitReport', 'fitList'):
            self.assertIn('id="%s"' % element_id, pane, '贴唱页缺少 #' + element_id)

        # ③ 预览是**可选**的：开关默认开，关掉就只看原稿
        self.assertRegex(pane, r'<input id="fitPreview"[^>]*checked',
                         '预览开关默认应该是打开的（用户要的就是"能播放着核对"）')
        self.assertIn('function fitPreviewOn()', studio)

        # ④ 时间轴读数只有一个来源：贴唱预览时读**贴后**的那一份
        self.assertIn('function timelineGroups(', studio)
        self.assertEqual(studio.count('timelineGroups(item)'), 2,
                         'preview() 与 activeRow() 必须都从这里取 groups')
        self.assertIn('wave.setFit(', studio)
        self.assertIn('setFit(lines)', (WEB / 'waveform.js').read_text(encoding='utf-8'))
        # 预览画在**覆盖层**，不注册命中区（它不是可拖的把手）
        wave = (WEB / 'waveform.js').read_text(encoding='utf-8')
        self.assertIn('paintFit(ctx, height)', wave)
        self.assertNotIn('fitLines', wave[wave.index('blockAt('):wave.index('blockAt(') + 900],
                         '贴唱预览不许进命中区')

        # ⑤ 行内逐字细化搬进了逐字编辑器（能力没丢，只是换了地方）
        self.assertIn('id="wordFromAudio"', html)
        self.assertIn("bind('wordFromAudio'", studio)
        self.assertIn("api('align/words'", studio)

    def test_waveform_block_drag(self):
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过波形拖拽行为检查')
        result = subprocess.run([node, str(HERE / 'web_block_drag_check.js')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         '波形拖块行为检查失败：\n' + (result.stdout + result.stderr))

    def test_the_wave_tip_escapes_untrusted_lyric_text(self):
        """歌词文本是**不可信输入**（联网/文件里来），提示条只能显示它、不能当标签执行。

        判据是真的渲染一遍看有没有生出元素，而不是"字符串里有没有 <"。
        """
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过提示条转义检查')
        env = dict(os.environ, NODE_PATH=_node_path())
        result = subprocess.run([node, str(HERE / 'web_tip_check.js')],
                                capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(result.returncode, 0,
                         '提示条转义检查失败：\n' + (result.stdout + result.stderr))

    def test_waveform_tick_grab_and_drawing(self):
        """刻度线：挤在一起时抓的是最近的那条，而且"能抓的那个"画得更粗更清楚。"""
        node = node_binary()
        if not node:
            self.skipTest('node 不可用，跳过刻度线检查')
        result = subprocess.run([node, str(HERE / 'web_tick_check.js')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0,
                         '刻度线检查失败：\n' + (result.stdout + result.stderr))

    def test_the_project_library_has_one_hook_per_rule(self):
        """本地工程库的三条规则各自只有一个权威。

        需求："每导入一个音频/歌词文件就新建一个工程；第一次打开问保存路径；
        退出时问两次（保留 / 删除）"。这里钉住的是**接线位置**：
        同一条规则只允许有一个写入口：

        * 新条目建工程：只挂在 repaintQueue()（队列变化的唯一出口）；
        * 文档写盘：只挂在 history.js 的 persist()（每次改动的唯一出口）；
        * 删除：只由服务端 projects.discard_session() 做，页面不拼路径。

        行为不在这里测（jsdom 的 web_dom_check 测首次启动的弹窗与"没路径就不建"，
        tests/test_projects.py 测边界与不可逆删除）。
        """
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        history = (WEB / 'history.js').read_text(encoding='utf-8')
        projects_js = (WEB / 'projects.js').read_text(encoding='utf-8')
        html = (WEB / 'index.html').read_text(encoding='utf-8')

        # ① 建工程只挂在一处：队列的那个唯一出口。
        repaint = studio[studio.index('function repaintQueue('):]
        repaint = repaint[:repaint.index('\n}\n')]
        self.assertIn('Project.trackAll(queue)', repaint,
                      '建工程的挂钩必须在 repaintQueue() 里（所有导入路径都汇到那儿）')
        self.assertEqual(studio.count('Project.trackAll('), 1,
                         '建工程的挂钩只应有一处；分头挂在各条导入路径上，'
                         '就是"修了 A 条路，用户走的是 B 条路"的形状')
        self.assertNotIn('Project.track', history, 'history.js 不该自己建工程')

        # ② 写盘只挂在 persist()。
        persist = history[history.index('function persist('):]
        persist = persist[:persist.index('\n}\n')]
        # persist() 必须把**是哪一条变了**告诉写盘层：无参调用会让写盘层
        # 只能靠"扫全队列取指纹"来猜是哪一条变了。
        self.assertIn('Project.sync(item)', persist,
                      '写盘挂钩必须在 persist() 里，而且要把变的那一条传下去')
        self.assertEqual(studio.count('Project.sync('), 0, 'studio.js 不应再开第二条写盘路')

        # ③ 页面不自己删文件、不自己拼路径：只有服务端那一个入口。
        self.assertIn("api('projects/discard'", projects_js)
        self.assertNotIn('rmdir', projects_js)
        self.assertNotIn('unlink', projects_js)
        self.assertNotIn('mkdir', projects_js)

        # 退出：两次确认必须发生在任何 await 之前（jsdom 检查在 click() 之后
        # 立刻读 localStorage，会话必须已经清掉）。
        exit_block = studio[studio.index("$('exitApp').onclick"):]
        exit_block = exit_block[:exit_block.index('\n};\n')]
        # 注释里也会出现这些标识符，先去掉注释再比 —— 否则断言比的是文字位置，
        # 而不是代码顺序（注释里提到一句就假红，正是"量得出来≠看得出来"那一类）。
        code = "\n".join(line for line in exit_block.splitlines()
                         if not line.strip().startswith('//'))
        self.assertLess(code.index('Project.decideExit()'), code.index('forgetSession()'),
                        '两次确认要排在清会话之前')
        self.assertLess(code.index('forgetSession()'), code.index('await '),
                        'forgetSession 必须在第一个 await 之前跑（否则它变成异步的）')
        self.assertIn('Project.finishExit(', code)

        # 「稍后再说」要被当回事：同一个会话不再重复弹。
        self.assertIn('if (this.asked && !force) return;', projects_js)

    def test_every_edit_hands_remember_the_entry_not_a_label(self):
        """每个编辑入口都必须把**歌曲条目**交给 remember()，而不是一个标签。

        规则：`remember(item = current(), label = '编辑', next = null)`
        三个参数都有默认值，所以 `remember('改歌词文字')` **不会报错** ——
        它会在那个字符串上建时间轴（给原始值赋属性静默失败）、拿到 undefined，
        然后在 `line.steps` 上抛 TypeError。后果是最坏的一种：**文档已经被改过，
        却既没进历史（撤不回来）也没落盘（工程还是旧的）**，而且异常把后面的
        `refresh()` 一起带走，界面停在旧样子。

        这类缺陷只在"真的点一下"的路径上现形，所以静态检查是唯一能提前拦住它的地方；
        行为侧（改一次文字 → 历史里多一步 → 能撤销 → 页面不抛异常）由
        `tests/web_insert_e2e_check.js` 验。

        判据刻意不看"参数个数"而看**第一个参数的形状**：标识符或 `current()`。
        """
        total = 0
        for name in ('studio.js', 'history.js', 'projects.js', 'library.js', 'common.js'):
            source = (WEB / name).read_text(encoding='utf-8')
            # 注释里也会出现 `remember(...)`（本节开头就写着两个），先剥注释再判，
            # 否则断言比的是文字位置而不是代码。
            bare = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
            bare = re.sub(r'(?m)^\s*//.*$', '', bare)
            calls = []
            for match in re.finditer(r'\bremember\s*\(', bare):
                if bare[max(0, match.start() - 9):match.start()] == 'function ':
                    continue          # 函数声明本身不算调用
                # 取**第一层**的第一个参数：参数里可能有 current() 这样的括号，
                # 所以不能简单地切到下一个 ')'（会把 `item = current()`
                # 当成参数）。
                depth, i, chunk = 1, match.end(), []
                while i < len(bare) and depth:
                    char = bare[i]
                    if char == '(':
                        depth += 1
                    elif char == ')':
                        depth -= 1
                        if not depth:
                            break
                    if depth == 1 and char == ',':
                        break
                    chunk.append(char)
                    i += 1
                calls.append(''.join(chunk).strip())
            total += len(calls)
            for first in calls:
                self.assertFalse(first[:1] in "'\"`",
                                 '%s：remember() 的第一个参数是标签（%s）—— '
                                 '必须是歌曲条目，否则这次编辑既不进历史也不落盘'
                                 % (name, first[:24]))
                self.assertRegex(first, r'^[A-Za-z_$][\w$]*(\(\))?$',
                                 '%s：remember() 的第一个参数看起来不是条目：%s' % (name, first[:24]))

        # 总的调用数要对得上（studio.js 里现在有 9 处，加别的文件更多）。
        # 数量用来兜住"正则失效 → 一条都没检查 → 全绿"这种情况。
        self.assertGreaterEqual(total, 8,
                                '只找到 %d 处 remember() 调用，正则大概失效了' % total)

        # 守卫本身要在：传错时必须出声，而不是静默地丢编辑记录。
        history = (WEB / 'history.js').read_text(encoding='utf-8')
        self.assertIn('function assertEntry(', history)
        self.assertIn("assertEntry(item, 'remember')", history)
        self.assertIn("assertEntry(item, 'timeline')", history)
        self.assertIn('Trace?.fault?.', history[history.index('function assertEntry('):
                                              history.index('function assertEntry(') + 900])

        # 「载入」这一步必须在**载入时**就建好。时间轴原本是懒创建的（只在
        # remember/goTo 里建），于是"载入后第一个动作是改文字、而那次改动先抛了
        # 异常"时，「载入」快照会把改动含进去 —— 那一步就永远撤不回去了。
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        body = studio[studio.index('\nfunction render() {'):]
        body = body[:body.index('\n}\n')]
        self.assertIn('renderHistory()', body,
                      'render() 必须刷新历史面板，顺带把「载入」快照在建档时就固定下来')

        # 「选中的行」越界时不许拿它拼提示。
        insert = studio[studio.index('async function insertRow('):]
        insert = insert[:insert.index('\nanchorRow')] if '\nanchorRow' in insert else insert[:2000]
        self.assertIn('anchorRow < groups.length', insert,
                      'insertRow() 要先判定锚点还在不在，越界就当"没选中"')

    def test_the_project_dialog_is_declared_before_its_script(self):
        """弹窗必须在脚本之前 —— 否则 bind() 拿到 null，按钮直接是死的。

        `<script src>` 会阻塞解析：只有它**前面**的元素才存在。所以这条顺序是硬规则，
        而且只能静态验（jsdom 会异步取脚本，悄悄把它修好）。
        """
        html = (WEB / 'index.html').read_text(encoding='utf-8')
        script_at = html.index('<script src="/projects.js"></script>')
        self.assertLess(html.index('id="projectDialog"'), script_at,
                        '工程弹窗必须写在 /projects.js 之前')
        for element_id in ('projectPath', 'projectState', 'projectBrowse', 'projectLater', 'projectUse'):
            self.assertIn('id="%s"' % element_id, html, 'index.html 缺少 #' + element_id)
            self.assertLess(html.index('id="%s"' % element_id), script_at,
                            '#%s 必须写在 /projects.js 之前' % element_id)
        # 与 library.js 的 const 顺序同理：projects.js 必须在 studio.js 之前，
        # 否则 studio.js 顶层引用 Project 会触发 TDZ。
        self.assertLess(script_at, html.index('<script src="/studio.js"></script>'),
                        'projects.js 必须在 studio.js 之前加载')
        self.assertIn('<script src="/projects.js"></script>', html)

    def test_the_project_root_lives_in_one_place_per_session(self):
        """「本次会话」= 一次进程运行，所以记录由 serve() 开新会话时清。

        放在页面启动时清是错的：刷新一下页面（后端还活着）就会把已经建好的工程
        变成"无人认领"，退出时的"删除本次会话的工程"就漏掉了它们。
        """
        server = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        serve = server[server.index('def serve('):]
        self.assertIn('projects.start_session()', serve, 'serve() 必须开一次新会话')
        # 而且它只清记录、不删文件。
        projects_py = (ROOT / 'saltlrc' / 'projects.py').read_text(encoding='utf-8')
        start = projects_py[projects_py.index('def start_session():'):]
        # start_session 是文件里最后一个函数，所以走到末尾即可（不靠"后面还有函数"）。
        start = start.split('\n\ndef ')[0]
        self.assertNotIn('delete(', start, '开新会话绝不能删文件')
        self.assertNotIn('_rmtree', start, '开新会话绝不能删文件')
        # 路由只允许传工程 id，路径由 L2 生成。
        routes = (ROOT / 'saltlrc' / 'api' / 'projects.py').read_text(encoding='utf-8')
        self.assertNotIn('os.path', routes, '路由层不该拼路径')
        self.assertNotIn('os.remove', routes)


if __name__ == '__main__':
    unittest.main()
