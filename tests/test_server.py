import base64
import io
import re
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
import zipfile
from http.server import ThreadingHTTPServer
from saltlrc import diagnostics
from saltlrc import credentials, settings as settings_store
from saltlrc.server import Handler

from _scratch import isolate_localappdata, scratch
ROOT = pathlib.Path(__file__).resolve().parent.parent
# Keep the suite off the developer's real settings/credentials files.
isolate_localappdata('saltlrc-server-')


class JournalFileTests(unittest.TestCase):
    """运行日志的文件句柄要能放开 —— 否则占着它的目录删不掉。

    `Diagnostics._file` 是 `open("a")` 出来的、活到进程结束，而 **Windows 上被占用的
    文件删不掉** —— 不调 `diagnostics.close()`，临时目录清理就会**静默**剩下一个
    `logs\\app-<日期>.log`。`logging.shutdown()`
    管不到它，因为这个句柄不是 logging 的。
    """

    def test_closing_the_journal_releases_the_log_file(self):
        from saltlrc import diagnostics
        diagnostics.DIAGNOSTICS.write("测试写入一句")
        path = diagnostics.logs_dir() / ("app-%s.log" % time.strftime("%Y%m%d"))
        self.assertTrue(path.is_file(), "写一句之后日志文件应该存在")
        self.assertIsNotNone(diagnostics.DIAGNOSTICS._file, "应该已经被打开")

        if os.name == "nt":
            # 句柄还开着时 Windows 会拒绝删除 —— 这正是"清了但没清掉"的机制
            with self.assertRaises(OSError):
                path.unlink()

        diagnostics.close()
        self.assertIsNone(diagnostics.DIAGNOSTICS._file)
        path.unlink()                       # 放开之后就能删掉了
        self.assertFalse(path.exists())

    def test_writing_after_close_reopens_the_file(self):
        """放开只是"放手"，不是"关掉这个功能" —— 之后还要能继续写。"""
        from saltlrc import diagnostics
        diagnostics.close()
        diagnostics.DIAGNOSTICS.write("关掉之后再写一句")
        path = diagnostics.logs_dir() / ("app-%s.log" % time.strftime("%Y%m%d"))
        self.assertTrue(path.is_file())
        self.assertIn("关掉之后再写一句", path.read_text(encoding="utf-8", errors="replace"))
        diagnostics.close()


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, route, body):
        req = urllib.request.Request(self.url + '/api/' + route, json.dumps(body).encode(), {'Content-Type': 'application/json'})
        return urllib.request.urlopen(req)

    def test_import_edit_export_zip(self):
        raw = '[00:01]你好[00:02]月亮[00:03]\n[00:01]こんにちは'
        with self.post('parse', {'bytes': base64.b64encode(raw.encode()).decode()}) as response:
            doc = json.load(response)['document']
        doc['groups'][0]['translation']['text'] = '月が綺麗ね'
        with self.post('export', {'document': doc}) as response:
            self.assertIn('[00:01.000]月が綺麗ね', json.load(response)['text'])
        with self.post('zip', {'entries': [{'name': 'album/song.spl', 'document': doc}]}) as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(archive.namelist(), ['album/song.lrc'])
        with self.assertRaises(urllib.error.HTTPError):
            self.post('zip', {'entries': [{'name': '../song.lrc', 'document': doc}]})

    def test_edit_endpoint_inserts_deletes_and_sorts(self):
        # Structural edits run through saltlrc/edits.py so the browser cannot
        # produce a project the exporter would reject.
        raw = '[00:01.000]你好\n[00:03.000]月亮'
        with self.post('parse', {'bytes': base64.b64encode(raw.encode()).decode()}) as response:
            doc = json.load(response)['document']
        with self.post('edit', {'op': 'insert', 'document': doc, 'index': 1}) as response:
            inserted = json.load(response)['document']
        self.assertEqual([g['start'] for g in inserted['groups']], [1000, 2000, 3000])
        with self.post('edit', {'op': 'delete', 'document': inserted, 'index': 1}) as response:
            back = json.load(response)['document']
        self.assertEqual([g['start'] for g in back['groups']], [1000, 3000])
        inserted['groups'][2]['start'] = 1500   # [1000, 2000, 1500] -> out of order
        with self.post('edit', {'op': 'sort', 'document': inserted}) as response:
            sorted_doc = json.load(response)['document']
        self.assertEqual([g['start'] for g in sorted_doc['groups']], [1000, 1500, 2000])

    def test_edit_endpoint_rejects_bad_requests(self):
        raw = '[00:01.000]你好\n[00:03.000]月亮'
        with self.post('parse', {'bytes': base64.b64encode(raw.encode()).decode()}) as response:
            doc = json.load(response)['document']
        with self.assertRaises(urllib.error.HTTPError):
            self.post('edit', {'op': 'teleport', 'document': doc})
        # A line past the end of the audio is refused, not silently clamped in
        # a way that would put two lines on top of each other.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post('edit', {'op': 'insert', 'document': doc, 'index': 2,
                               'start': 9000, 'duration': 4000})
        self.assertIn('音频时长', caught.exception.read().decode('utf-8'))

    def test_tokenize_endpoint(self):
        # The word editor's 自动分词 uses this: display units with no timings,
        # and the concatenation must reproduce the text exactly.
        with self.post('tokenize', {'text': '月が綺麗だね'}) as response:
            payload = json.load(response)
        self.assertTrue(len(payload['units']) >= 1)
        self.assertEqual(''.join(payload['units']), '月が綺麗だね')
        with self.post('tokenize', {'text': 'The Nights'}) as response:
            units = json.load(response)['units']
        self.assertEqual(''.join(units), 'The Nights')

    def test_tokenize_endpoint_applies_the_language_rule(self):
        # Chinese splits one character each, and the reply names the rule so the
        # editor can say which one ran instead of implying it always knows.
        with self.post('tokenize', {'text': '今天我们一起去', 'lang': 'zh'}) as response:
            payload = json.load(response)
        self.assertEqual(payload['units'], ['今', '天', '我', '们', '一', '起', '去'])
        self.assertEqual(payload['info']['lang'], 'zh')
        self.assertEqual(payload['info']['rule'], '中文 · 按字')

        # A caller-supplied language beats detection, which is what keeps a
        # kanji-only line in a Japanese song from being split as Chinese.
        with self.post('tokenize', {'text': '東京', 'lang': 'ja'}) as response:
            self.assertEqual(json.load(response)['units'], ['東京'])
        with self.post('tokenize', {'text': '東京', 'lang': 'zh'}) as response:
            self.assertEqual(json.load(response)['units'], ['東', '京'])

    def test_every_asset_a_page_references_is_actually_served(self):
        """Walk each page's own href/src references and fetch them.

        This is the general form of a bug found while adding the favicon: the
        static route map is an explicit whitelist, so `/favicon.png` was a 404
        even though the file was sitting in saltlrc/web/ — and the content-type
        lookup raised KeyError for the new extension, which is a 500 rather than
        a clean "not found". A page that references something the server will not
        serve is invisible until someone opens the dev tools, so the references
        are collected from the HTML and requested for real.
        """

        wanted = {}
        for page in ('index.html', 'library.html'):
            html = (ROOT / 'saltlrc' / 'web' / page).read_text(encoding='utf-8')
            for url in re.findall(r'(?:href|src)="(/[^"#?]+)"', html):
                wanted.setdefault(url, page)
        self.assertTrue(wanted, '没有从页面里找到任何本地资源引用')
        for url, page in sorted(wanted.items()):
            with self.subTest(url=url, page=page):
                try:
                    with urllib.request.urlopen(self.url + url) as response:
                        body = response.read()
                        self.assertEqual(response.status, 200)
                        self.assertTrue(body, url + ' 返回了空内容')
                except urllib.error.HTTPError as error:
                    self.fail('%s 引用了 %s，但服务端返回 %s' % (page, url, error.code))

    def test_the_favicon_is_served_as_a_png(self):
        with urllib.request.urlopen(self.url + '/favicon.png') as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get('Content-Type'), 'image/png; charset=utf-8')
            self.assertEqual(response.read()[:8], b'\x89PNG\r\n\x1a\n')

    def test_the_resume_after_pause_switch_round_trips(self):
        """「播放/暂停后再按播放，从暂停点继续」是服务端存的一个功能开关。

        默认必须是**开**；关掉之后要能存下来、能读回来。
        页面侧的开关与这条规则都由 settings.py 的 `features` 决定，所以这里验的是
        "这个键真的在服务端的白名单里" —— 白名单是显式的，漏加就等于开关点了没用。
        """
        with self.post('settings', {}) as response:
            self.assertIs(json.load(response)['features']['resumeAfterPause'], True,
                          '默认应当是"从暂停点继续"')
        with self.post('settings/save', {'features': {'resumeAfterPause': False}}) as response:
            self.assertIs(json.load(response)['features']['resumeAfterPause'], False)
        with self.post('settings', {}) as response:
            self.assertIs(json.load(response)['features']['resumeAfterPause'], False,
                          '关掉之后要能读回来（不是每次都被默认值盖掉）')
        with self.post('settings/save', {'features': {'resumeAfterPause': True}}) as response:
            self.assertIs(json.load(response)['features']['resumeAfterPause'], True)

    def test_settings_are_readable_and_the_key_is_not(self):
        with self.post('settings', {}) as response:
            exposed = json.load(response)
        self.assertEqual(exposed['translation']['api_key'], '')
        self.assertIn('has_key', exposed['translation'])
        self.assertIn('keys', exposed)
        self.assertIn('features', exposed)

    def test_settings_round_trip_without_echoing_the_key(self):
        with self.post('settings/save', {'translation': {
                'base_url': 'https://api.deepseek.com/v1', 'model': 'deepseek-chat',
                'api_key': 'sk-live-should-never-come-back'},
                'features': {'followPlayback': False},
                'keys': {'playPause': 'KeyP'}}) as response:
            saved = json.load(response)
        self.assertEqual(saved['translation']['api_key'], '')
        self.assertTrue(saved['translation']['has_key'])
        self.assertNotIn('sk-live-should-never-come-back', json.dumps(saved, ensure_ascii=False))
        with self.post('settings', {}) as response:
            again = json.load(response)
        self.assertEqual(again['translation']['model'], 'deepseek-chat')
        self.assertEqual(again['features']['followPlayback'], False)
        self.assertEqual(again['keys']['playPause'], 'KeyP')
        self.assertNotIn('sk-live-should-never-come-back', json.dumps(again, ensure_ascii=False))

    def test_translate_without_configuration_says_what_to_do(self):
        with self.post('settings/save', {'translation': {'api_key': '\u0000clear'}}) as response:
            json.load(response)
        with self.post('translate', {'probe': True}) as response:
            payload = json.load(response)
        self.assertTrue(payload.get('needs_setup'))
        self.assertIn('设置', payload['error'])

    def test_the_serve_layer_never_writes_to_the_real_settings_file(self):
        # The suite must not touch the developer's own settings (or key).
        self.assertEqual(os.path.dirname(settings_store.path()),
                         os.path.dirname(credentials.path()))

    def test_static_and_origin(self):
        with urllib.request.urlopen(self.url) as response:
            home = response.read()
        # "/" is the editor now: the app opens straight into the workbench.
        self.assertIn(b'waveCanvas', home)
        self.assertIn(b'SaltLRC', home)
        with urllib.request.urlopen(self.url + '/library') as response:
            self.assertIn(b'libraryState', response.read())
        req = urllib.request.Request(self.url, headers={'Origin': 'https://example.com'})
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(req)
        self.assertEqual(result.exception.code, 403)

    def debug_post(self, route, body, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Debug-Token'] = token
        req = urllib.request.Request(self.url + '/api/debug/' + route,
                                     json.dumps(body).encode(), headers)
        return urllib.request.urlopen(req)

    def test_debug_page_is_password_gated(self):
        """口令只从环境变量来：没设置时调试接口整个关闭，而不是"默认口令也能进"。"""
        saved = os.environ.pop('SALTLRC_DEBUG_PASSWORD', None)
        try:
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.debug_post('auth', {'password': 'definitely-wrong'})
            self.assertEqual(result.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as result:
                urllib.request.urlopen(self.url + '/api/debug/state')
            self.assertEqual(result.exception.code, 403)
            self.assertIn('未启用', json.load(result.exception)['error'])
            os.environ['SALTLRC_DEBUG_PASSWORD'] = 'correct-horse'
            with self.debug_post('auth', {'password': 'correct-horse'}) as response:
                token = json.load(response)['token']
            self.assertEqual(token, diagnostics.debug_token())
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        self.url + '/api/debug/state',
                        headers={'X-Debug-Token': token})) as response:
                    state = json.load(response)
            except urllib.error.HTTPError as error:
                self.fail('state 403，body=' + error.read().decode('utf-8', 'replace'))
            self.assertIn('errors', state)
            self.assertIn('requests', state)
            self.assertIn('log_dir', state)
        finally:
            if saved is not None:
                os.environ['SALTLRC_DEBUG_PASSWORD'] = saved
            else:
                os.environ.pop('SALTLRC_DEBUG_PASSWORD', None)

    def test_lyric_read_only_accepts_lrc_files(self):
        """`/api/lyric` 只读 .lrc：不限后缀的话，同源脚本能把任意本地文件读走。"""
        with self.assertRaises(urllib.error.HTTPError) as result:
            urllib.request.urlopen(self.url + '/api/lyric?path=' +
                                   urllib.parse.quote(r'C://Windows//win.ini'))
        self.assertEqual(result.exception.code, 400)
        self.assertIn('.lrc', json.load(result.exception)['error'])

    def test_client_errors_and_requests_are_journalled(self):
        self.post('debug/client-error', {'message': 'boom', 'source': '/library'})
        self.assertTrue(diagnostics.DIAGNOSTICS.errors[-1]['message'].startswith('[前端] boom'))
        # Every API call is a recorded "last action" for the debug page.
        self.assertTrue(any(item['path'] == '/api/debug/client-error'
                            for item in diagnostics.DIAGNOSTICS.requests))

    def document(self, text='[00:01]你好[00:02]月亮[00:03]\n'):
        with self.post('parse', {'bytes': base64.b64encode(text.encode()).decode()}) as response:
            return json.load(response)['document']

    def test_export_folder_picks_and_writes_from_the_backend(self):
        from saltlrc import server as server_module
        doc = self.document()
        original = server_module.folders.pick_folder
        try:
            with tempfile.TemporaryDirectory() as folder:
                server_module.folders.pick_folder = lambda *a, **k: folder
                with self.post('export-folder', {'document': doc, 'name': 'song.lrc'}) as response:
                    data = json.load(response)
                target = pathlib.Path(folder) / 'song.lrc'
                self.assertEqual(data['status'], 'written')
                self.assertEqual(data['path'], str(target))
                self.assertTrue(target.is_file())
                self.assertGreater(target.stat().st_size, 0, '不得再写出 0 字节文件')
                self.assertIn('你好', target.read_text(encoding='utf-8'))

            server_module.folders.pick_folder = lambda *a, **k: None
            with self.post('export-folder', {'document': doc, 'name': 'song.lrc'}) as response:
                self.assertEqual(json.load(response)['status'], 'cancelled')
        finally:
            server_module.folders.pick_folder = original

    def test_export_folder_falls_back_when_picker_is_unavailable(self):
        from saltlrc import server as server_module
        from saltlrc import folders as folders_module
        doc = self.document()
        original = server_module.folders.pick_folder

        def broken(*args, **kwargs):
            raise RuntimeError('本机文件夹选择仅支持 Windows')

        server_module.folders.pick_folder = broken
        try:
            with self.post('export-folder', {'document': doc, 'name': 'song.lrc'}) as response:
                data = json.load(response)
            self.assertEqual(data['status'], 'written')
            self.assertTrue(data['fallback'])
            self.assertTrue(pathlib.Path(data['path']).is_file())
            self.assertEqual(os.path.dirname(data['path']), folders_module.export_dir())
        finally:
            server_module.folders.pick_folder = original

    def test_export_folder_strips_path_traversal(self):
        from saltlrc import server as server_module
        doc = self.document()
        original = server_module.folders.pick_folder
        try:
            with tempfile.TemporaryDirectory() as folder:
                server_module.folders.pick_folder = lambda *a, **k: folder
                with self.post('export-folder', {'document': doc, 'name': '..\\..\\evil.lrc'}) as response:
                    data = json.load(response)
                self.assertEqual(data['path'], str(pathlib.Path(folder) / 'evil.lrc'),
                                 '客户端给的文件名必须被当成纯文件名处理')
        finally:
            server_module.folders.pick_folder = original

    def test_save_errors_are_human_readable(self):
        raw = '[00:01]你好[00:02]月亮[00:03]\n'
        with self.post('parse', {'bytes': base64.b64encode(raw.encode()).decode()}) as response:
            doc = json.load(response)['document']
        # A missing parent directory surfaces as a 400 (not a bare 500) whose
        # message explains the folder is gone.
        missing = str(pathlib.Path(tempfile.gettempdir()) / 'saltlrc-no-such-dir' / 'song.lrc')
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.post('save', {'document': doc, 'path': missing})
        self.assertEqual(result.exception.code, 400)
        self.assertIn('歌曲目录不存在', result.exception.read().decode())
        # A denied write (e.g. C:\ system folders) must say so in Chinese instead
        # of leaking "[WinError 5]".
        from saltlrc import server as server_module
        original = server_module.library.write_lyric

        def denied(*args, **kwargs):
            raise PermissionError(13, 'Access is denied', 'C:\\song.lrc')

        server_module.library.write_lyric = denied
        try:
            target = str(pathlib.Path(tempfile.gettempdir()) / 'saltlrc-denied.lrc')
            with self.assertRaises(urllib.error.HTTPError) as result:
                self.post('save', {'document': doc, 'path': target})
            self.assertEqual(result.exception.code, 400)
            body = result.exception.read().decode()
            self.assertIn('没有写入权限', body)
            self.assertIn('导出到其他文件夹', body)
        finally:
            server_module.library.write_lyric = original
