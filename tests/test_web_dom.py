"""Loads the live pages in jsdom and asserts every button is wired.

The failure mode this catches: a script error aborts the binding loop, so
buttons render but do nothing. Skipped when node or jsdom is unavailable.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from saltlrc.server import Handler

from _scratch import isolate_localappdata, scratch
HERE = pathlib.Path(__file__).parent

def _node():
    return shutil.which('node') or shutil.which('node.exe')

def _node_path():
    """Where npm packages may live; jsdom is an extra dev dependency."""
    candidates = [os.environ.get('NODE_PATH', ''),
                  str(pathlib.Path(_node() or '').parent / 'node_modules'),
                  r'C:\Users\Administrator\.workbuddy\binaries\node\workspace\node_modules']
    return os.pathsep.join(c for c in candidates if c)

class DomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        node = _node()
        if not node:
            raise unittest.SkipTest('node not available')
        env = dict(os.environ, NODE_PATH=_node_path())
        probe = subprocess.run([node, '-e', "require('jsdom')"], env=env,
                               capture_output=True)
        if probe.returncode != 0:
            raise unittest.SkipTest('jsdom not installed (npm i jsdom)')
        cls.node, cls.env = node, env
        # Every store in the app (settings, credentials, project roots) writes
        # under store.config_dir(). A loaded page now creates projects on import,
        # so without this redirect a test run would drop real工程 folders into
        # whatever folder the user configured — and could overwrite the user's
        # own project-root.json. Point the whole suite at a scratch directory.
        cls.scratch = scratch('saltlrc-config-')
        cls.env['SALTLRC_CONFIG_DIR'] = cls.scratch
        os.environ['SALTLRC_CONFIG_DIR'] = cls.scratch
        cls.addClassCleanup(os.environ.pop, 'SALTLRC_CONFIG_DIR', None)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        # jsdom requests every <script> of the page in parallel; the default
        # backlog of 5 refuses the overflow and the page half-loads with
        # "Could not load script" — a false failure. Same fix as serve().
        cls.server.request_queue_size = 128
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'server'):
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join()

    def setUp(self):
        # 每个用例都从"从未配置过"的临时配置目录开始。工程库的启动前提是
        # "第一次打开要问保存位置"，那是个**状态**条件：上一个用例把保存路径
        # 设过之后，这个用例的弹窗就不会出现 —— 变成一条取决于执行顺序的断言。
        for name in os.listdir(self.scratch):
            target = os.path.join(self.scratch, name)
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            else:
                try:
                    os.remove(target)
                except OSError:
                    pass

    def test_pages_render_and_bind_every_control(self):
        with urllib.request.urlopen(self.url + '/library', timeout=5):
            pass
        result = subprocess.run(
            [self.node, str(HERE / 'web_dom_check.js'), self.url],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         'jsdom check failed:\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_inserting_a_line_end_to_end(self):
        """The whole chain: button -> /api/edit -> saltlrc/edits.py -> table."""
        result = subprocess.run(
            [self.node, str(HERE / 'web_insert_e2e_check.js'), self.url],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '插入行端到端检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_autosave_only_touches_what_changed(self):
        """改一条只碰一条 —— 以及后台失败挂横幅、失败弹框。

        两个真热点都是 **O(队列)**：每次编辑对**整个队列**的文档做
        `JSON.stringify` 取指纹（300 首 1039ms），以及把整个队列拼成一个 JSON
        写进 localStorage。而每次编辑真正变的只有一条。

        **判据的关键在 `web_autosave_check.js` 里**：写盘请求数在新旧两种写法下
        **都是 1**（指纹扫完发现只有一条变了），单看它分辨不出差异 ——
        能分辨的是"文档级 JSON.stringify 的次数"（12 条队列下新写法 1 次、
        旧写法 13 次）。
        """
        folder = scratch('saltlrc-autosave-')
        result = subprocess.run(
            [self.node, str(HERE / 'web_autosave_check.js'), self.url, folder],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '自动落盘与应用内提示检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_the_local_project_library_end_to_end(self):
        """本地工程库整条链路：设路径 → 导入建工程 → 编辑落盘 → 退出问两次。

        需求原文："每导入一个音频/歌词文件的时候新建一个工程，将其保存在本地；
        第一次打开软件时询问用户保存路径；如果用户未保存退出时询问用户是否保存，
        询问二次，用户需确认两次；如果是则保留工程，如果否则删除本地已保存的文件。"

        这些都不是单测能覆盖的：它们全在**页面接线**上（挂钩挂在哪儿、按钮点了走哪条
        接口、两次确认的顺序）。所以这里在真页面里跑，并且真的读写磁盘 —— 保存路径
        用临时目录，绝不碰用户自己的文件夹。
        """
        folder = scratch('saltlrc-projects-')
        result = subprocess.run(
            [self.node, str(HERE / 'web_project_check.js'), self.url, folder],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '本地工程库端到端检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_a_late_match_reply_does_not_freeze_playback(self):
        """The timing hole behind the frozen playhead.

        A lookup that answers *after* select() emptied the <audio> element must
        not repaint from the reset currentTime, and a reply belonging to a
        selection the user has left must be dropped before it makes a request.
        This drives the real studio.js with a deferred /api/audio/match, so the
        ordering is reproduced on purpose rather than hoped for.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_late_match_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '迟到匹配回调检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_seeking_during_playback_keeps_rendering(self):
        """Clicking the waveform mid-playback must not stop the frame loop.

        A `seeked` event must not be handled like `pause`: one click on the
        waveform while the audio keeps playing must leave the playhead, the
        karaoke highlight and the follow-scroll all running.

        The loop's state is derived from the element (`syncPlaybackLoop()`)
        instead of from which event happened to fire.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_playback_seek_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '播放中跳转检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_the_transport_keeps_the_pause_point_and_the_speed(self):
        """倍速 + 「再次播放从暂停点开始」——走带条的两条规则。

        需求原文："我需要倍速功能，还有每次播放后暂停，再次播放从暂停点开始，
        并加一个这个选项的开关在设置里"。

        不能只靠浏览器：`<audio>` 自己的 `currentTime` 在换音源
        （`removeAttribute('src') + load()`）后会被清零，加载未就绪时也可能是 0 ——
        所以"下次从哪播放"由页面自己记着、由页面显式写回去。这个检查用可控的
        <audio> 把这句话钉住（`tests/web_transport_check.js` 27 项），
        包括"换歌时 playbackRate 会被浏览器重置"这条容易被忽略的浏览器行为。
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_transport_check.js')],
            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         '走带条检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_the_align_pane_buttons_actually_work(self):
        """「智能贴唱」页：点一下就能干活，干完能预览、能应用、能丢弃。

        页面上是一个「智能贴唱」按钮加一个**可选**的同步预览，所以这里钉的接线是：
        请求带上轨道与最大移动量、页内状态原样写出服务端说明、结果逐行列出、
        预览开着时时间轴读**贴后**那一份、应用写进文档且可撤销、
        贴唱后改过歌词不许应用、失败必须在页内出声。

        算法真值由 `tests/test_fit.py` 验、接口由 `tests/test_align_routes.py` 验，
        这里只盯页面接线（`tests/web_align_check.js`，26 项）。
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_align_check.js')],
            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         '识别与对齐检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_track_captions_are_pinned_and_nameless(self):
        """The two track captions must stay put and must not name a language.

        Two defects, and only the first is about wording:

        * the text said "主轨 · 中文" / "翻译轨 · 日语", but the main track is not
          necessarily Chinese nor the translation track Japanese, so naming a
          language is wrong, not just noise;
        * the caption must be painted OUTSIDE the cached layer, otherwise it is
          blitted at an offset like the content and slides sideways with the
          waveform — eventually off the canvas, leaving no caption at all.

        The assertion that matters is the invariance: the caption's x must be the
        same at every viewport offset. "Is the label drawn?" would pass with the
        scrolling behaviour still present.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_track_hint_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '轨道提示检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_keyboard_nudging_uses_the_shared_boundary_logic(self):
        """Arrow-key nudging must go through changeTime, not assign a time itself.

        changeTime routes a word move through ``Doc.moveWordStart``, which is what
        keeps a word from passing the one before it and carries the line end when
        the last word crosses it. A nudge that wrote ``words[i].start`` directly
        would look identical on screen and could produce a document the validator
        rejects.
        """
        studio = (HERE.parent / 'saltlrc' / 'web' / 'studio.js').read_text(encoding='utf-8')
        start = studio.index('function nudgeSelected(')
        body = studio[start:studio.index('\n}\n', start)]
        self.assertIn('changeTime(', body, '微调必须走 changeTime（统一边界处理）')
        self.assertNotIn('.start =', body, '微调不得直接写逐字时间')

    def test_the_library_word_editor_buttons_work(self):
        """取消 / 应用 on the 歌词获取页 must be reachable and effective.

        What matters is that the buttons are effective on the real page: 应用
        writes the values and closes, 取消 closes without touching the document,
        and a stale row refuses out loud instead of returning silently.

        The dialog is also asserted to cap its height with a non-shrinking action
        bar, so a tall cell grid cannot push both buttons below the viewport.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_library_word_check.js'), self.url],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '曲库逐字编辑器检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('library word-editor checks passed', result.stdout)

    def test_the_line_handles_go_through_the_shared_range_rule(self):
        """句首 and 句尾 must be clamped by Doc.tickRange, not assigned directly.

        Both handles are assigned directly and then judged by validateEdit, so
        crossing either one's neighbour discards the whole edit and leaves the
        handle put. The legal range is decided in ONE place, and every path must
        ask for it.

        The behaviour is covered by web_document_check.js; this pins the wiring —
        "the UI stopped asking the shared rule" is not something the rule's own
        tests can notice.
        """
        studio = (HERE.parent / 'saltlrc' / 'web' / 'studio.js').read_text(encoding='utf-8')
        start = studio.index('function changeTime(')
        body = studio[start:studio.index('\n}\n', start)]
        self.assertIn("Doc.tickRange(", body, '句首/整句拖动必须走 Doc.tickRange')
        self.assertIn('Doc.moveLineEnd(', body, '句尾必须走 Doc.moveLineEnd')
        self.assertNotIn('else if (kind === \'end\') track.end = time;', body,
                         '句尾不得再直接赋值（越过最后一个字会被整笔驳回）')

    def test_editing_word_timings_on_the_waveform_gives_feedback(self):
        """Word ticks must be aimable and must say what they are.

        A syllable tick needs a visible grab zone and numeric feedback next to the
        cursor, naming the word it belongs to.

        The check also guards that the pointer state does not live in ``hover``,
        which belongs to the table-row highlight: one field with two writers
        freezes the highlight.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_word_edit_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '逐字编辑检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('word-editing checks passed', result.stdout)

    def test_the_playing_line_is_pinned_to_the_top(self):
        """Each line change re-pins the playing row to the top of the table.

        The playing row is put at the top, not merely kept visible: a timing
        editor works on the lines below the playing one.

        jsdom has no layout engine, so every getBoundingClientRect() is zeros and
        an assertion there would be vacuous. This drives the real studio.js with
        explicit row geometry, so the scroll arithmetic is actually exercised.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_row_pin_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '当前行置顶检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_every_interaction_keeps_the_canvas_covered(self):
        """No interactive route may leave a strip of the canvas unpainted.

        An uncovered strip shows the background fill, which reads as a missing or
        shifted piece of waveform — so every route that moves the viewport is
        swept: strip drag/seek, wheel pan, zoom, splitter resize, rapid clicking,
        and drag start/end transitions.

        The gap is measured against the canvas's ACTUAL backing-store width:
        paint() clamps the pane to a 200px floor while viewportSeconds() uses the
        raw width, and that disagreement leaves a strip unpainted.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_cover_sweep_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '交互覆盖扫描失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_the_layer_blit_covers_the_whole_canvas(self):
        """No sliver of the canvas may be left uncovered by the layer blit.

        The layer is painted at a whole-second anchor and blitted at
        (anchor - offset), so it shifts by up to half a second. A layer exactly
        one viewport wide leaves a strip uncovered whenever `offset` is not a
        whole number — follow mode makes it fractional on every frame of playback.

        The invariant is deterministic, so it is asserted directly across zooms,
        offsets and drag state.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_layer_coverage_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '图层覆盖检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_table_interaction_does_not_rebuild_the_waveform(self):
        """Hovering/clicking/following rows must not repaint the static layer.

        The hover and playback outline are an overlay, like the playhead, so a
        hovered row, a clicked row or a playback row change does not repaint the
        whole waveform plus both lyric tracks.

        This asserts both the cost (no rebuild, bounded calls) and the truth
        (the outline is still drawn at the right position): those fail
        independently — deleting the drawing would satisfy the cost half.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_interaction_perf_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '表格交互性能检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)

    def test_dragging_the_timeline_stays_cheap(self):
        """A continuous drag must not rebuild the static layer every frame.

        The layer is painted at a coarse anchor and blitted, so panning does not
        change the layer's cache key and repaint the whole lyric/waveform layer
        every frame.

        This asserts the *work* rather than wall-clock time: timing assertions on
        a shared machine are flaky while draw-call counts are deterministic — and
        draw calls are what actually decide the frame rate.
        It also checks that hit testing still lines up, since blitting means the
        recorded hit positions are in layer space, not canvas space.
        """
        result = subprocess.run(
            [self.node, str(HERE / 'web_drag_perf_check.js')],
            env=self.env, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0,
                         '拖动性能检查失败：\n' + result.stdout + result.stderr)
        self.assertIn('0 failed', result.stdout)
