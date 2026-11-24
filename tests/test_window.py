"""Tests for the native window layer (``saltlrc.window``).

This module is the bridge between the browser UI and a real desktop app, and it
is the one place where a missing dependency is invisible at build time: the
WebView2 assemblies are loaded *by name* at runtime, so a bundle can pass every
HTTP test and still fail to open a window.

What is covered here is deliberately the part that can be verified without a
desktop session: port selection, that the server really runs off the main
thread (pywebview refuses a window on any other thread), clean shutdown, and
the import-time contract ``available()`` promises. Actually rendering a window
needs a desktop session and is covered by the manual probe / exe smoke run.
"""

import sys
import threading
import unittest
import urllib.error
import urllib.request

from saltlrc import window


class PortSelectionTests(unittest.TestCase):
    def test_returns_a_bindable_port(self):
        port = window.free_port(start=19200, tries=40)
        self.assertIsNotNone(port)
        self.assertGreaterEqual(port, 19200)
        self.assertLess(port, 19240)

    def test_skips_a_port_that_is_already_listening(self):
        import socket

        holder = socket.socket()
        holder.bind(('127.0.0.1', 0))
        holder.listen(1)
        taken = holder.getsockname()[1]
        try:
            # Binding is the authority: "can I connect" is a different question,
            # so the port must be skipped by bind() alone.
            port = window.free_port(start=taken, tries=3)
            self.assertNotEqual(port, taken)
        finally:
            holder.close()

    def test_reports_failure_instead_of_guessing(self):
        import socket

        holders = []
        base = 19300
        try:
            for offset in range(3):
                s = socket.socket()
                s.bind(('127.0.0.1', base + offset))
                s.listen(1)
                holders.append(s)
            self.assertIsNone(window.free_port(start=base, tries=3))
        finally:
            for s in holders:
                s.close()


class ServerThreadTests(unittest.TestCase):
    """The arrangement that pywebview forces on us: server off the main thread."""

    def setUp(self):
        self.port = window.free_port(start=19400, tries=40)
        self.assertIsNotNone(self.port, 'no free port in the test range')
        self.server = window.build_server(self.port)
        self.thread = window._ServerThread(self.server, self.port)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.port

    def tearDown(self):
        self.thread.stop()
        self.thread.join(timeout=10)
        try:
            self.server.jobs.close()
        except Exception:
            pass

    def test_server_answers_while_the_main_thread_is_free(self):
        # If serve() ran on the main thread this call could never return.
        self.assertTrue(window.wait_for_server(self.url, timeout=20))
        self.assertTrue(self.thread.is_alive(), 'server thread should be running')
        self.assertIsNot(self.thread, threading.current_thread())

    def test_thread_is_a_daemon_so_a_stuck_server_cannot_hang_exit(self):
        self.assertTrue(self.thread.daemon)

    def test_editor_page_and_bundle_are_served(self):
        with urllib.request.urlopen(self.url + '/studio.html', timeout=5) as reply:
            page = reply.read().decode('utf-8')
        self.assertIn('waveCanvas', page)
        self.assertIn('songStrip', page)
        for asset in ('/waveform.js', '/studio.js', '/document.js', '/style.css'):
            with urllib.request.urlopen(self.url + asset, timeout=5) as reply:
                self.assertEqual(reply.status, 200, asset)

    def test_capabilities_reports_the_current_version(self):
        import json
        from saltlrc import __version__

        with urllib.request.urlopen(self.url + '/api/capabilities', timeout=5) as reply:
            payload = json.loads(reply.read())
        self.assertEqual(payload['version'], __version__)

    def test_the_version_number_has_exactly_one_authority(self):
        """版本号只来自 `saltlrc/__init__.py`，其余几处必须跟着它。

        `pyproject.toml` 一直是 0.13.1，而 `__init__.py` 已经到 0.16.0 —— 三个
        大版本没人发现，因为它不影响任何运行时行为。可它是一个**交付事实**
        （用户看到的版本号、发布说明的标题、"你用的是哪版"），漂了就等于在
        归档里撒谎。所以这里把它钉住：改版本号必须三处一起改。
        """
        import re
        from pathlib import Path

        from saltlrc import __version__

        root = Path(__file__).resolve().parent.parent
        pyproject = (root / 'pyproject.toml').read_text(encoding='utf-8')
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
        self.assertIsNotNone(match, 'pyproject.toml 里没有 version')
        self.assertEqual(match.group(1), __version__, 'pyproject.toml 的版本号没跟上')

        usage = (root / 'dist' / '使用说明.txt')
        if usage.is_file():
            first = usage.read_text(encoding='utf-8').splitlines()[0].strip()
            # 形状要**完全**对：只断言"含有版本号"是不够的 —— 文件被按 ANSI 解码后
            # 又写回 UTF-8（双重编码）时，那一行里照样含有 "0.20.0"。
            self.assertEqual(first, '# 使用说明（v%s）' % __version__,
                             '使用说明首行必须是「# 使用说明（v<版本>）」，'
                             '乱码或格式漂了都说明它被错误地重写过')
        # 两份文档的**最新一条**必须就是当前版本 —— 版本号漂了的话，
        # 用户在归档里查到的"这一版改了什么"根本对不上他手上的 exe。
        for name in ('CHANGELOG.md', 'RELEASE_NOTES.md'):
            path = root / 'docs' / name
            if not path.is_file():
                continue
            newest = re.search(r'^## v([0-9]+\.[0-9]+\.[0-9]+)', path.read_text(encoding='utf-8'), re.M)
            self.assertIsNotNone(newest, name + ' 里没有版本条目')
            self.assertEqual(newest.group(1), __version__,
                             name + ' 最新一条应当是当前版本')

    def test_stop_releases_the_port(self):
        self.assertTrue(window.wait_for_server(self.url, timeout=20))
        self.thread.stop()
        self.thread.join(timeout=10)
        # A port left bound would make the next launch silently move elsewhere.
        with self.assertRaises((urllib.error.URLError, OSError, ConnectionError)):
            urllib.request.urlopen(self.url + '/api/capabilities', timeout=2)

    def test_request_queue_is_raised_above_the_default(self):
        # The default backlog of 5 is smaller than one editor page load.
        self.assertGreaterEqual(self.server.request_queue_size, 128)


class AvailabilityTests(unittest.TestCase):
    def test_available_is_a_bool(self):
        self.assertIsInstance(window.available(), bool)

    def test_available_only_claims_true_when_webview_imports(self):
        try:
            import webview  # noqa: F401
            importable = True
        except ImportError:
            importable = False
        expected = sys.platform == 'win32' and importable
        self.assertEqual(window.available(), expected)

    def test_run_window_message_names_the_missing_dependency(self):
        """When webview cannot be imported, the error must say how to fix it.

        The window layer is optional, so the failure has to be actionable rather
        than a bare ModuleNotFoundError buried in a windowed (console-less) run.
        """
        import builtins
        import importlib

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == 'webview' or name.startswith('webview.'):
                raise ImportError('simulated missing pywebview')
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            with self.assertRaises(RuntimeError) as caught:
                window.run_window(port=19500)
        finally:
            builtins.__import__ = real_import
        message = str(caught.exception)
        self.assertIn('pywebview', message)
        self.assertIn('pip install', message)


if __name__ == '__main__':
    unittest.main()
