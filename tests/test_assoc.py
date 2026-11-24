"""`.saltproj` 的文件关联：**只有一个写者，而且每一处都能验证**。

让 `.saltproj` 在资源管理器里有自己的图标，双击就用本程序打开。

这件事有两类容易坏掉的方式，这份测试就盯这两类：

1. **两个写者**：安装器（Inno Setup）和程序自己都去写注册表 —— 本项目第 1 条老病。
   所以这里静态断言 `installer.iss` **没有** `[Registry]`，关联只由
   `saltlrc/assoc.py` 写（HKCU，不需要管理员权限）。
2. **看起来成功的失败**：注册表写了一半、图标文件不在旁边、Windows 的"打开方式"
   被手动指定过 —— 这三种都会表现为"设了没反应"。`status()` 必须把它们**说出来**，
   而不是给一个沉默的勾选框。

**测试一个字节都不写用户真实的注册表**：`MemoryRegistry` 是个字典假实现，
但它**照 Windows 的规矩**（删非空键报错），所以"忘了递归删子键"这种错会当场变红。
"""

import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

from saltlrc import assoc, settings, window

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'saltlrc' / 'web'


class MemoryRegistry:
    """字典假实现 —— 但**行为对齐 Windows**，不是"怎么方便怎么来"。

    关键一条：``delete_key`` 只允许删空键。真实现是 ``winreg.DeleteKey``，
    它删不掉带子键的键；如果假实现宽容一点，`assoc.erase()` 里"忘了递归"
    就会**假绿**。
    """

    def __init__(self):
        self.keys = {}

    def _make(self, path):
        parts = path.split('\\')
        for index in range(1, len(parts) + 1):
            self.keys.setdefault('\\'.join(parts[:index]), {})

    def read(self, path, name=None):
        return self.keys.get(path, {}).get(name)

    def write(self, path, name, value):
        self._make(path)
        self.keys[path][name] = value

    def children(self, path):
        prefix = path + '\\'
        return sorted({rest[len(prefix):].split('\\')[0] for rest in self.keys
                       if rest.startswith(prefix)})

    def exists(self, path):
        return path in self.keys

    def delete_key(self, path):
        if self.children(path):
            raise OSError('键非空')          # 和 winreg.DeleteKey 一样
        self.keys.pop(path, None)

    def paths(self):
        return sorted(self.keys)


class FakeApp:
    """一个临时目录 + 里面的假 exe（真文件不需要存在）。"""

    def __enter__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.exe = os.path.join(self.dir.name, 'SaltLRCStudio.exe')
        return self

    def __exit__(self, *exc):
        self.dir.cleanup()

    def put_icon(self):
        Path(assoc.icon_file(self.exe)).write_bytes(b'\x00\x00\x01\x00')
        return assoc.icon_file(self.exe)


class AssociationShapeTests(unittest.TestCase):
    def setUp(self):
        self.reg = MemoryRegistry()

    def test_install_writes_exactly_what_the_shell_needs(self):
        with FakeApp() as app:
            app.put_icon()
            state = assoc.install(exe=app.exe, registry=self.reg, notify=False)
            icon = assoc.icon_file(app.exe)
        self.assertTrue(state['installed'])
        self.assertEqual(self.reg.read(r'Software\Classes\.saltproj'), assoc.PROGID)
        self.assertEqual(self.reg.read(r'Software\Classes\SaltLRCStudio.Project'),
                         assoc.TYPE_NAME)
        self.assertEqual(self.reg.read(r'Software\Classes\SaltLRCStudio.Project\DefaultIcon'),
                         '"%s"' % icon)
        command = self.reg.read(r'Software\Classes\SaltLRCStudio.Project\shell\open\command')
        self.assertIn(assoc.OPEN_FLAG, command)
        self.assertIn('"%1"', command, '双击时要能把选中的文件路径交进来')

    def test_the_icon_is_the_file_beside_the_exe(self):
        """图标值就是**一个文件路径** —— 这是"能直接看一眼验证"的那种设计。

        内嵌资源只能写 `"exe",N`，而 N 是序号还是资源 ID 在 Windows 上并不统一。
        """
        with FakeApp() as app:
            spec, present = assoc.icon_spec(app.exe)
            self.assertFalse(present)
            self.assertEqual(spec, '"%s",0' % os.path.abspath(app.exe),
                             '图标文件不在时应退回 exe 自身的图标，而不是指向一个不存在的文件')
            icon = app.put_icon()
            spec, present = assoc.icon_spec(app.exe)
            self.assertTrue(present)
            self.assertEqual(spec, '"%s"' % icon)

    def test_a_missing_icon_file_is_reported_not_hidden(self):
        with FakeApp() as app:
            state = assoc.install(exe=app.exe, registry=self.reg, notify=False)
        self.assertFalse(state['icon_ok'])
        self.assertTrue(any('图标文件不在' in one for one in state['problems']),
                        '图标退回程序图标这件事必须说出来：%s' % state['problems'])

    def test_install_is_idempotent_and_notices_a_moved_app(self):
        with FakeApp() as first, FakeApp() as second:
            first.put_icon()
            assoc.install(exe=first.exe, registry=self.reg, notify=False)
            once = self.reg.paths()
            again = assoc.install(exe=first.exe, registry=self.reg, notify=False)
            self.assertTrue(again['installed'])
            self.assertEqual(self.reg.paths(), once, '重复关联不该长出第二份键')
            # 程序换了位置（两个目录里都有图标，所以这里报的只能是"指向旧路径"）。
            second.put_icon()
            moved = assoc.status(exe=second.exe, registry=self.reg)
            self.assertFalse(moved['installed'],
                             '程序换过位置后必须能看出"指向的是旧路径" —— 启动时才谈得上自动重装')
            self.assertTrue(any('不在那个位置' in one for one in moved['problems']),
                            '而且要说清楚它指向的是旧路径：%s' % moved['problems'])
            # 启动时的 ensure() 正是靠这个判断把关联改过来的。
            original, assoc.exe_path = assoc.exe_path, lambda: second.exe
            try:
                repaired = assoc.ensure(registry=self.reg, notify=False)
            finally:
                assoc.exe_path = original
            self.assertTrue(repaired['installed'])
            self.assertEqual(self.reg.read(r'Software\Classes\SaltLRCStudio.Project\DefaultIcon'),
                             '"%s"' % assoc.icon_file(second.exe))

    def test_status_notices_a_manually_chosen_open_with(self):
        """Windows 的「打开方式」是**写在别处的第二个权威**，而且优先级更高。"""
        with FakeApp() as app:
            app.put_icon()
            assoc.install(exe=app.exe, registry=self.reg, notify=False)
            self.reg.write(r'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.saltproj\UserChoice',
                           'ProgId', 'SomeOtherApp.Proj')
            state = assoc.status(exe=app.exe, registry=self.reg)
        self.assertEqual(state['user_choice'], 'SomeOtherApp.Proj')
        self.assertEqual(state['user_choice_owner'], 'foreign')
        self.assertTrue(state['installed'], '我们的键仍然是写好的')
        self.assertFalse(state['effective'], '被别的程序接管时**不能**说已经生效')
        self.assertTrue(any('接管' in one for one in state['problems']),
                        '被系统忽略这件事必须说出来，否则表现就是"设了没反应"')

    def test_a_stale_choice_pointing_at_us_is_repaired(self):
        """`UserChoice` 落到 `Applications\\<我们的 exe>`，而那一支的命令指向别处的旧路径。

        那种状态下"图标贴好了、双击却去开一个不存在的文件"，而且因为 UserChoice
        优先级更高，我们刚写的 ProgID **完全不起作用**。
        """
        old = r'C:\Users\Someone\Desktop\SaltLRCStudio-v0.6.0\SaltLRCStudio.exe'
        apps = r'Software\Classes\Applications\SaltLRCStudio.exe'
        with FakeApp() as app:
            app.put_icon()
            self.reg.write(apps + r'\shell\open\command', None, '"%s" "%%1"' % old)
            self.reg.write(r'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts'
                           r'\.saltproj\UserChoice', 'ProgId', 'Applications\\SaltLRCStudio.exe')
            before = assoc.status(exe=app.exe, registry=self.reg)
            self.assertEqual(before['user_choice_owner'], 'stale',
                             '名字是我们的、命令不是 —— 这一种不能当成"别人接管"，也不能当成没事')
            self.assertFalse(before['effective'])
            self.assertTrue(any('旧路径' in one for one in before['problems']),
                            '要说清楚"双击会去开一个不存在的地方"：%s' % before['problems'])
            # 再点一次保存就该修好它。
            after = assoc.install(exe=app.exe, registry=self.reg, notify=False)
            self.assertEqual(after['user_choice_owner'], 'ours',
                             '修完之后双击该落到本程序（那一支的命令要指向当前 exe）')
            self.assertTrue(after['effective'], '修完之后必须真正生效')
        self.assertEqual(self.reg.read(apps + r'\shell\open\command'),
                         '"%s" --open "%%1"' % app.exe,
                         '修的是那一支的命令，不是把用户的「打开方式」删掉')
        self.assertEqual(self.reg.read(apps + r'\DefaultIcon'), '"%s"' % assoc.icon_file(app.exe),
                         '走这一支时 Windows 取它的 DefaultIcon，不给就退回程序图标')

    def test_install_does_not_invent_an_open_with_entry(self):
        """没有那一支就不建：`Applications\\<exe>` 是"打开方式"候选列表，
        不该被安装关联顺手加进去。"""
        with FakeApp() as app:
            app.put_icon()
            assoc.install(exe=app.exe, registry=self.reg, notify=False)
        self.assertFalse(self.reg.exists(r'Software\Classes\Applications\SaltLRCStudio.exe'))


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.reg = MemoryRegistry()

    def test_remove_deletes_the_whole_tree(self):
        """``winreg.DeleteKey`` 删不掉带子键的键。

        `SaltLRCStudio.Project` 那一支带 DefaultIcon 与 shell 两个子键，
        删除要从叶子往上递归，否则"取消关联"看起来成功了、键却还在。
        """
        with FakeApp() as app:
            app.put_icon()
            assoc.install(exe=app.exe, registry=self.reg, notify=False)
            self.assertIn(r'Software\Classes\SaltLRCStudio.Project\shell\open\command',
                          self.reg.paths())
            assoc.remove(registry=self.reg, notify=False)
        leftovers = [one for one in self.reg.paths() if 'SaltLRCStudio' in one]
        self.assertEqual(leftovers, [], '取消关联之后一个键都不该留下：%s' % leftovers)
        self.assertIsNone(self.reg.read(r'Software\Classes\.saltproj'))

    def test_remove_leaves_a_foreign_association_alone(self):
        """`.saltproj` 交给别的程序时，撤销**不许**动那一条。

        和工程库删除边界是同一条规矩：宁可不删，也不越界。
        """
        self.reg.write(r'Software\Classes\.saltproj', None, 'SomeOtherApp.Proj')
        with FakeApp() as app:
            app.put_icon()
            assoc.install(exe=app.exe, registry=self.reg, notify=False)
            self.reg.write(r'Software\Classes\.saltproj', None, 'SomeOtherApp.Proj')
            assoc.remove(registry=self.reg, notify=False)
        self.assertEqual(self.reg.read(r'Software\Classes\.saltproj'), 'SomeOtherApp.Proj',
                         '别人占着这个扩展名时，撤销不该把它拆掉')
        self.assertEqual([one for one in self.reg.paths() if 'SaltLRCStudio' in one], [])

    def test_remove_also_clears_a_choice_that_points_at_us(self):
        """撤销时 `UserChoice` 也必须清 —— 它优先级更高，留着会让"取消关联"不生效。

        指向**别人**时一个字节都不动（那一条在下面单独验）。
        """
        choice = r'Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.saltproj\UserChoice'
        original, assoc.exe_path = assoc.exe_path, lambda: None
        try:
            with FakeApp() as app:
                app.put_icon()
                assoc.exe_path = lambda: app.exe
                self.reg.write(r'Software\Classes\Applications\SaltLRCStudio.exe\shell\open\command',
                               None, '"%s" --open "%%1"' % app.exe)
                self.reg.write(choice, 'ProgId', 'Applications\\SaltLRCStudio.exe')
                assoc.install(exe=app.exe, registry=self.reg, notify=False)
                assoc.remove(registry=self.reg, notify=False)
                self.assertIsNone(self.reg.read(choice, 'ProgId'),
                                  '指向我们自己的 UserChoice 必须一起清掉，否则双击照旧会开本程序')

                # 指向别人时：留着。
                self.reg.write(choice, 'ProgId', 'SomeOtherApp.Proj')
                assoc.remove(registry=self.reg, notify=False)
                self.assertEqual(self.reg.read(choice, 'ProgId'), 'SomeOtherApp.Proj',
                                 '别人指定的打开方式不该被我们删掉')
        finally:
            assoc.exe_path = original


class SourceModeTests(unittest.TestCase):
    """源码运行时**一个字节都不许写注册表**。

    `sys.executable` 是 python.exe：把 `.saltproj` 关联到 python.exe 只会让双击
    弹出一个黑窗口。所以这条既是"没意义"，也是"必须有测试挡着"。
    """

    def test_exe_path_is_none_when_not_frozen(self):
        if getattr(sys, 'frozen', False):
            self.skipTest('这一条是为源码运行写的；打包后的测试进程本来就是冻结的')
        self.assertIsNone(assoc.exe_path())

    def test_install_refuses_without_an_exe(self):
        reg = MemoryRegistry()
        state = assoc.install(registry=reg, notify=False)
        self.assertFalse(state['supported'])
        self.assertTrue(state['reason'])
        self.assertEqual(reg.paths(), [], '没有 exe 路径时注册表必须一个键都没写')

    def test_ensure_never_raises_and_never_writes_in_source_mode(self):
        reg = MemoryRegistry()
        state = assoc.ensure(registry=reg, notify=False)
        self.assertFalse(state['supported'])
        self.assertEqual(reg.paths(), [])


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.reg = MemoryRegistry()
        self.original = settings.load
        self.addCleanup(lambda: setattr(settings, 'load', self.original))

    def _with_exe(self, exe):
        original = assoc.exe_path
        self.addCleanup(lambda: setattr(assoc, 'exe_path', original))
        assoc.exe_path = lambda: exe

    def test_the_setting_turns_it_off(self):
        with FakeApp() as app:
            app.put_icon()
            self._with_exe(app.exe)
            settings.load = lambda: {'features': {'associateProjectFiles': False}}
            state = assoc.ensure(registry=self.reg, notify=False)
            self.assertFalse(state['installed'])
            self.assertEqual(self.reg.paths(), [], '设置关掉了就不该写注册表')
            self.assertIn('关掉', state['reason'])

    def test_the_setting_turns_it_on(self):
        with FakeApp() as app:
            app.put_icon()
            self._with_exe(app.exe)
            settings.load = lambda: {'features': {'associateProjectFiles': True}}
            state = assoc.ensure(registry=self.reg, notify=False)
            self.assertTrue(state['installed'])
            # 再跑一次（每次启动都会跑）不该报错、也不该长出新键。
            before = self.reg.paths()
            self.assertTrue(assoc.ensure(registry=self.reg, notify=False)['installed'])
            self.assertEqual(self.reg.paths(), before)

    def test_ensure_reports_instead_of_raising(self):
        """关联坏了不能让程序起不来 —— 但也不能**静默**：要有一句可读的原因。"""
        self._with_exe(os.path.join(tempfile.gettempdir(), 'SaltLRCStudio.exe'))
        def boom():
            raise RuntimeError('注册表被组策略锁住了')
        settings.load = boom
        state = assoc.ensure(registry=self.reg, notify=False)
        self.assertFalse(state['supported'])
        self.assertIn('注册表被组策略锁住了', state['reason'])


class CommandContractTests(unittest.TestCase):
    """写进注册表的那条命令行，必须能被**我们自己发的入口**读懂。

    `command_for()` 与 `app_entry.open_path_from_argv()` 是一份契约的两半 ——
    两半各改各的，表现就是"双击没反应"，而且没有任何东西会报错。
    """

    def test_the_entry_point_reads_the_flag_the_association_writes(self):
        import app_entry

        exe = r'C:\Program Files\SaltLRC\SaltLRCStudio.exe'
        command = assoc.command_for(exe)
        self.assertIn('"%s"' % exe, command, '路径带空格时必须引起来')
        self.assertTrue(command.endswith('"%1"'))
        self.assertIn(assoc.OPEN_FLAG, command)
        # 关联里写的形状 → 入口必须能读出来（这里直接把 %1 换成一个真路径）。
        target = r'D:\歌词工程\20260927-某首歌\project.saltproj'
        argv = [exe, assoc.OPEN_FLAG, target]
        self.assertEqual(app_entry.open_path_from_argv(argv[1:]), target)

    def test_the_entry_point_reads_the_other_shapes_too(self):
        import app_entry

        path = r'D:\a b\project.saltproj'
        self.assertEqual(app_entry.open_path_from_argv(['--open', path]), path)
        self.assertEqual(app_entry.open_path_from_argv(['--open=' + path]), path)
        # 拖到 exe 上 / 发送到 —— 裸路径，没有标志。
        self.assertEqual(app_entry.open_path_from_argv([path]), path)
        self.assertIsNone(app_entry.open_path_from_argv([]))
        self.assertIsNone(app_entry.open_path_from_argv(['--browser', '--no-browser']),
                          '选项不能被当成路径')
        self.assertIsNone(app_entry.open_path_from_argv(['--port=8765']))


class OneWriterTests(unittest.TestCase):
    """静态事实：关联**只有一个写者**，图标**必须随 exe 交付**。"""

    def test_the_installer_does_not_register_the_association(self):
        iss = (ROOT / 'packaging' / 'installer.iss').read_text(encoding='utf-8')
        self.assertNotIn('[Registry]', iss,
                         '安装器不许自己写关联 —— 否则和程序运行时写的成了两个权威')
        self.assertIn('SaltLRCStudio-project.ico', iss,
                      '安装器必须把图标文件放进程序目录（关联指向它）')

    def test_the_release_ships_the_icon_next_to_the_exe(self):
        script = (ROOT / 'packaging' / 'release.ps1').read_text(encoding='utf-8')
        self.assertIn('SaltLRCStudio-project.ico', script)
        self.assertIn("Test-Path (Join-Path $dest 'SaltLRCStudio-project.ico')", script,
                      '复制了还要**验一遍**：少了它只是静默退回程序图标，没人会注意到')

    def test_the_page_never_tells_the_server_which_file_to_read(self):
        """页面只问"启动时带了什么"，**从不提交路径**。

        否则 `/api/pending-open` 就成了"让本机服务端读任意文件"的接口。
        """
        studio = (WEB / 'studio.js').read_text(encoding='utf-8')
        self.assertIn("fetch('/api/pending-open')", studio)
        self.assertNotIn('/api/pending-open?', studio,
                         'path 参数一律不许走查询串 —— 路径只能来自本进程的命令行')
        server = (ROOT / 'saltlrc' / 'server.py').read_text(encoding='utf-8')
        body = server.split('def _serve_pending_open')[1].split('def _translate')[0]
        self.assertIn('self.server.pending_open', body)
        self.assertNotIn('query', body, '这个路由不该从查询串里取任何东西')


class PendingOpenTransportTests(unittest.TestCase):
    """真的起一个服务：`/api/pending-open` 递字节，并且**只递一次**。"""

    @classmethod
    def setUpClass(cls):
        cls.holder = tempfile.TemporaryDirectory()
        cls.project = Path(cls.holder.name) / '我的工程.saltproj'
        cls.project.write_bytes(b'SaltLRC1\x00\x00\x00\x00payload')
        cls.server = window.build_server(0, open_path=str(cls.project))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.holder.cleanup()

    def url(self):
        return 'http://127.0.0.1:%d/api/pending-open' % self.port

    def get(self):
        with urllib.request.urlopen(self.url(), timeout=10) as reply:
            return reply.status, dict(reply.headers), reply.read()

    def test_it_hands_over_the_launch_argument_then_forgets_it(self):
        status, headers, body = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(body, self.project.read_bytes(), '必须是**原始字节**，不是 JSON 转义过的')
        # 中文文件名不能直接进 HTTP 头（只能是 latin-1）——
        # 所以发的是百分号编码，页面自己解回来。
        self.assertEqual(urllib.parse.unquote(headers['X-Project-Name']), self.project.name)

        again_status, _, again_body = self.get()
        self.assertEqual(again_status, 204, '一次性：刷新页面不该把磁盘上的旧内容再灌一遍')
        self.assertEqual(again_body, b'')

    def test_nothing_pending_is_a_quiet_204(self):
        """没带参数启动时不能报错 —— 页面每次启动都会问一次。"""
        with tempfile.TemporaryDirectory() as holder:
            other = window.build_server(0)
            port = other.server_address[1]
            thread = threading.Thread(target=other.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/api/pending-open' % port,
                                            timeout=10) as reply:
                    self.assertEqual(reply.status, 204)
            finally:
                other.shutdown()
                other.server_close()


if __name__ == '__main__':
    unittest.main()
