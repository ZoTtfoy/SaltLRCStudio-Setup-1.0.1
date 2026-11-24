"""原生文件夹 / 另存为对话框：测的是 **ctypes 那层管道**，不是对话框本身。

危险点是 `SHBrowseForFolderW` 返回的那个指针（PIDL）：ctypes 不声明 `restype` 时
按 `c_int` 收返回值，**指针被截断到 32 位**，下一次调用拿着残缺地址去读，
抛 `OSError: access violation reading 0xFFFFFFFFE2480920`
（高 32 位全是 F：那是指针被截断后**符号扩展**回来的形状）。

这里的做法是把 DLL 换成假的（`folders._dlls` 这个接缝），
于是结构体布局、签名声明、指针传递、缓冲区解码全都能在没有窗口的情况下被测到；
假函数还会**模拟 ctypes 的默认行为**（没声明 `restype` 就按 `c_int` 截断），
所以"忘了写签名"这个 bug 会在测试里重现，而不是只靠一句注释提醒。
"""

import ctypes
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from saltlrc import folders

# 一个像真实 PIDL 的 64 位指针：低 32 位不要是 0，高位也不是 0。
# 截断成 32 位会变成 0xABCDEF00（负数），符号扩展后是 0xFFFFFFFFABCDEF00 ——
# 那正是日志里那个故障地址的形状。
from _scratch import isolate_localappdata, scratch
PIDL = 0x000001F2ABCDEF00
FOREGROUND = 0x00000000000A1B2C
LONG_PATH = "D:\\音乐\\歌词\\Salt Player 工程\\日本語のフォルダ"

UNSET = object()   # 代表"从来没被赋值过"，与"显式赋成 None"区别开

class FakeFunc:
    """冒充一个 ctypes 函数：记录 argtypes/restype 和每次调用的参数。"""

    def __init__(self, value=None):
        self.argtypes = UNSET
        self.restype = UNSET
        self.value = value
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if callable(self.value):
            return self.value(*args)
        # ctypes 的默认行为：没声明 restype 就当成 c_int 收返回值。
        # 假函数也这么做，否则"签名忘了写"这个 bug 在测试里根本不会发生。
        if self.restype is UNSET and isinstance(self.value, int):
            low = self.value & 0xFFFFFFFF
            return low - (1 << 32) if low >= 0x80000000 else low
        return self.value

    @property
    def declared(self):
        # `restype = None` 对 void 函数是**正确**的写法，所以不能用"是不是 None"
        # 来判断有没有声明 —— 要判断的是有没有被赋值过。
        return self.argtypes is not UNSET and self.restype is not UNSET

def fills(path, ok=1):
    """让 `SHGetPathFromIDListW` 往输出缓冲区里写一个路径（缓冲区是最后一个参数）。"""

    def writer(*args):
        args[-1].value = path
        return ok

    return writer

def writes_path_through_spec(path, ok=1):
    """让 `GetSaveFileNameW` 写出用户选的完整文件名。

    它是**通过结构体里的 `lpstrFile` 指针**写回来的（Windows 就是这么做的），
    所以这里也用 `memmove` 真的写进那段内存 —— 这样"缓冲区是否被正确传给了
    Windows""容量是否够"才真的被测到，而不是靠假函数假装成功。
    """

    def writer(spec_arg):
        spec = spec_arg._obj
        raw = ctypes.create_unicode_buffer(path, spec.nMaxFile)
        ctypes.memmove(spec.lpstrFile, raw, ctypes.sizeof(raw))
        return ok

    return writer

class FakeDlls:
    """一整套假 DLL；默认行为 = 用户选了一个中文长路径。

    窗口枚举也必须建模：`owner_window()` 会 `EnumWindows` 找本进程的
    主窗口。默认给两台窗口 —— **别人家的**（更大、在前台）和我们自己的 —— 这样
    "对话框的 owner 到底跟着谁走"才是真的被测到，而不是假设。
    """

    def __init__(self, path=LONG_PATH, browse=PIDL, get_path=fills(LONG_PATH), windows=None):
        self.shell32 = SimpleNamespace(SHBrowseForFolderW=FakeFunc(browse),
                                       SHGetPathFromIDListW=FakeFunc(get_path))
        self.ole32 = SimpleNamespace(CoInitializeEx=FakeFunc(0),
                                     CoUninitialize=FakeFunc(None),
                                     CoTaskMemFree=FakeFunc(None))
        foreign = FOREGROUND            # 点按钮那一瞬间前台可能是别的程序
        own = 0x1001
        self.windows = windows if windows is not None else {
            foreign: {"visible": True, "owner": 0, "pid": os.getpid() + 1, "rect": (0, 0, 4000, 4000)},
            own: {"visible": True, "owner": 0, "pid": os.getpid(), "rect": (0, 0, 1000, 800)},
        }
        self.own_window = own

        def window_info(hwnd):
            return self.windows.get(hwnd, {})

        def enumerate_windows(callback, lparam):
            for hwnd in list(self.windows):
                callback(hwnd, lparam)
            return 1

        def thread_process(hwnd, out_pid):
            out_pid._obj.value = window_info(hwnd).get("pid", 0)
            return 1

        def window_rect(hwnd, out_rect):
            left, top, right, bottom = window_info(hwnd).get("rect", (0, 0, 0, 0))
            out_rect._obj.left, out_rect._obj.top = left, top
            out_rect._obj.right, out_rect._obj.bottom = right, bottom
            return 1

        self.user32 = SimpleNamespace(AllowSetForegroundWindow=FakeFunc(1),
                                      GetForegroundWindow=FakeFunc(foreign),
                                      SetForegroundWindow=FakeFunc(1),
                                      IsWindowVisible=FakeFunc(lambda hwnd: int(bool(window_info(hwnd).get("visible")))),
                                      GetWindow=FakeFunc(lambda hwnd, which: window_info(hwnd).get("owner", 0)),
                                      GetWindowThreadProcessId=FakeFunc(thread_process),
                                      GetWindowRect=FakeFunc(window_rect),
                                      EnumWindows=FakeFunc(enumerate_windows))
        self.comdlg32 = SimpleNamespace(GetSaveFileNameW=FakeFunc(writes_path_through_spec(path)))

class PickFolderTests(unittest.TestCase):
    def setUp(self):
        self.dlls = FakeDlls()
        self.patch = mock.patch.object(folders, "_dlls", lambda: self.dlls)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_chinese_path_comes_back_intact(self):
        """选中的中文路径必须原样回来，且那个 64 位句柄没有被截断。

        指针被截断时的形状见模块开头的说明。
        """
        self.assertEqual(folders.pick_folder(), LONG_PATH)

    def test_the_64_bit_handle_is_passed_on_unchanged(self):
        """核心断言：PIDL 必须**原样**传给后面两个调用。

        指针被截断到 32 位时，这条断言会看到 0xABCDEF00（甚至负数）——
        那正是崩溃现场。
        """
        folders.pick_folder()
        get_path = self.dlls.shell32.SHGetPathFromIDListW
        free = self.dlls.ole32.CoTaskMemFree
        self.assertEqual(get_path.calls[0][0], PIDL, "拿到的不是同一个句柄 → 截断了")
        self.assertEqual(free.calls[0][0], PIDL, "释放的必须是同一个句柄，否则漏内存")

    def test_the_handle_return_type_is_declared_as_a_pointer(self):
        """一句直接的、指向根因的断言：返回值必须声明成指针，不能是默认的 c_int。"""
        folders.pick_folder()
        self.assertIs(self.dlls.shell32.SHBrowseForFolderW.restype, ctypes.c_void_p)

    def test_every_win32_call_has_a_declared_signature(self):
        """通用规则：用到的每个 Win32 函数都要写 argtypes + restype。

        单个 restype 漏了就是一次访问冲突，而 ctypes 不会给任何提示 ——
        所以这条按"清单"断言，将来新增调用也在覆盖范围内。
        """
        folders.pick_folder()
        folders.save_file("x.lrc")
        missing = []
        for dll in (self.dlls.shell32, self.dlls.ole32, self.dlls.user32, self.dlls.comdlg32):
            for name, func in vars(dll).items():
                if not func.declared:
                    missing.append(name)
        self.assertEqual(missing, [], "这些调用没声明签名（restype 缺了会截断指针）")

    def test_the_dialog_is_owned_by_our_own_window_not_whatever_is_in_front(self):
        """对话框的 owner 必须是**本进程自己的窗口**，不能是前台窗口。

        点按钮那一瞬间前台未必是我们 —— WebView 的焦点、刚点过别的程序，都会让
        owner 变成别人家，而**对话框的 z 序跟着 owner 走**：owner 不是我们时它可以
        掉到应用窗口**后面**（状态行也不变，因为代码在等那个看不见的模态框）。

        假 DLL 特意给了一台**更大、在前台**的别人家窗口：按前台挑就会选中它。
        """
        folders.pick_folder()
        info = self.dlls.shell32.SHBrowseForFolderW.calls[0][0]._obj
        self.assertEqual(info.hwndOwner, self.dlls.own_window)
        self.assertNotEqual(info.hwndOwner, FOREGROUND,
                            '又拿"当时的前台窗口"当 owner 了 —— 那会让对话框掉到后面')
        self.assertTrue(info.pszDisplayName, "输出缓冲区必须传进去（不能是空指针）")
        self.assertNotEqual(info.lpszTitle, None)
        # 新式对话框（可缩放 + 带编辑框，能直接粘路径）
        self.assertTrue(info.ulFlags & folders.BIF_NEWDIALOGSTYLE)
        self.assertTrue(info.ulFlags & folders.BIF_EDITBOX)
        self.assertTrue(info.ulFlags & folders.BIF_RETURNONLYFSDIRS)

    def test_cancelling_returns_none_and_frees_nothing(self):
        self.dlls.shell32.SHBrowseForFolderW.value = 0
        self.assertIsNone(folders.pick_folder())
        self.assertEqual(self.dlls.shell32.SHGetPathFromIDListW.calls, [])
        self.assertEqual(self.dlls.ole32.CoTaskMemFree.calls, [], "没有句柄就不要去释放")

    def test_an_unconvertible_path_is_reported_not_treated_as_a_cancel(self):
        """用户**选了一个位置**、只是系统转不出路径时，不能静默变成"已取消"。

        那会让用户以为自己点错了，反复点 —— 而事情其实已经发生了。
        断言它同时是 `PickerError` 与 `ValueError`：后者才让服务端把它变成
        "400 + 原文"（用户看得懂），而不是一句 500 服务器内部错误。
        """
        self.dlls.shell32.SHGetPathFromIDListW.value = fills("", ok=0)
        with self.assertRaises(folders.PickerError) as caught:
            folders.pick_folder()
        self.assertIsInstance(caught.exception, ValueError)
        self.assertIn("路径过长", str(caught.exception))
        # 失败了也必须把句柄还回去。
        self.assertEqual(self.dlls.ole32.CoTaskMemFree.calls[0][0], PIDL)

    def test_cancelling_without_a_name_still_returns_none(self):
        """取消（对话框返回 0）时不能报错，也不能去读那个没被填过的缓冲区。"""
        self.dlls.comdlg32.GetSaveFileNameW.value = lambda *args: 0
        self.assertIsNone(folders.save_file("晴天.lrc"))

class SaveFileTests(unittest.TestCase):
    def setUp(self):
        self.dlls = FakeDlls()
        self.patch = mock.patch.object(folders, "_dlls", lambda: self.dlls)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_the_dialog_starts_in_the_songs_own_folder_not_somewhere_random(self):
        """对话框的初始位置：**这首歌自己的文件夹**优先，否则「文档」。

        不设 `lpstrInitialDir` 时 Windows 会自己挑（进程当前目录、上次用过的位置、
        甚至解包目录），用户会找不到东西在哪。
        """
        folder = scratch("saltlrc-初始目录-")
        lyric = os.path.join(folder, "晴天.lrc")
        with open(lyric, "w", encoding="utf-8") as handle:
            handle.write("[00:01]晴天")
        self.assertEqual(folders.initial_dir(lyric), folder, "给了文件就该用它的目录")
        self.assertEqual(folders.initial_dir(folder), folder, "给了目录就原样用")
        self.assertEqual(folders.initial_dir("N:\\没有这个盘\\x.lrc"), folders.documents_dir(),
                         "路径无效时要退回「文档」，绝不能拿它去试")
        self.assertEqual(folders.initial_dir(None), folders.documents_dir())
        self.assertTrue(os.path.isdir(folders.documents_dir()), "「文档」目录必须真的存在")

    def test_a_chinese_file_name_comes_back_intact(self):
        target = "D:\\我的歌词\\晴天.lrc"
        self.dlls.comdlg32.GetSaveFileNameW.value = writes_path_through_spec(target)
        self.assertEqual(folders.save_file("晴天.lrc"), target)

    def test_the_struct_is_filled_the_way_windows_requires(self):
        """`lStructSize` 不写 Windows 直接拒绝；缓冲区容量必须和实际缓冲一致。"""
        filters = (("歌词文件", "*.lrc;*.yrc"), ("全部文件", "*.*"))
        folders.save_file("晴天.lrc", filters=filters)
        spec = self.dlls.comdlg32.GetSaveFileNameW.calls[0][0]._obj
        self.assertEqual(spec.lStructSize, ctypes.sizeof(self.openfilename()))
        self.assertEqual(spec.nMaxFile, 2048)
        self.assertEqual(spec.hwndOwner, self.dlls.own_window)
        self.assertTrue(spec.lpstrFile, "文件名缓冲区必须传进去")
        self.assertEqual(spec.lpstrDefExt, "lrc", "默认扩展名要跟着文件名走")
        # 初始目录必须显式给，否则 Windows 自己挑，对话框开在哪儿不确定
        self.assertTrue(spec.lpstrInitialDir, "必须设 lpstrInitialDir，否则对话框开在哪儿全看系统心情")
        self.assertTrue(spec.Flags & folders.OFN_OVERWRITEPROMPT, "要问要不要覆盖")
        # 类型筛选是 "标签\0通配符\0…\0\0"：里面的 NUL 是**数据**，所以不能像普通
        # 字符串那样读（读 `spec.lpstrFilter` 只会在第一个 NUL 处停下）——
        # 要按已知长度把原始宽字符读出来比。
        expected = "".join("%s\0%s\0" % pair for pair in filters) + "\0"
        raw = ctypes.wstring_at(spec.lpstrFilter, len(expected))
        self.assertEqual(raw, expected)
        self.assertTrue(raw.endswith("\0\0"), "末尾必须有双 NUL，否则 Windows 会读过头")

    @staticmethod
    def openfilename():
        return folders._win32(FakeDlls()).OPENFILENAMEW

    def test_the_struct_sizes_match_the_real_windows_layout(self):
        """结构体布局错一个字的宽度，Windows 就会读到垃圾 —— 用真实尺寸锚住。

        这两个数字是 x64 上 Win32 头文件给出的值；改动字段类型时会在这里变红。
        """
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("这些偏移是 x64 的；32 位下布局本来就不同")
        once = folders._win32(FakeDlls())
        self.assertEqual(ctypes.sizeof(once.BROWSEINFOW), 64)
        self.assertEqual(ctypes.sizeof(once.OPENFILENAMEW), 152)
        # 关键偏移：Windows 按这些位置读 lStructSize / lpstrFile / nMaxFile，
        # 错一个字段类型就会把别的数据当成它们。
        self.assertEqual(once.OPENFILENAMEW.lpstrFile.offset, 48)
        self.assertEqual(once.OPENFILENAMEW.nMaxFile.offset, 56)
        self.assertEqual(once.BROWSEINFOW.hwndOwner.offset, 0)
        self.assertEqual(once.BROWSEINFOW.ulFlags.offset, 32)

class PathSafetyTests(unittest.TestCase):
    def test_a_picked_folder_with_a_trailing_space_is_still_usable(self):
        """真实路径里的空格/中文/日文都要能一路走到 os.makedirs。

        这条把"对话框返回值"和"真的能建目录"接起来 —— 只看返回值相等是不够的。
        """
        base = scratch("saltlrc-中文 目录-")
        folder = os.path.join(base, "歌词 工程")
        dlls = FakeDlls(path=folder, get_path=fills(folder))
        with mock.patch.object(folders, "_dlls", lambda: dlls):
            picked = folders.pick_folder()
        self.assertEqual(picked, folder)
        os.makedirs(picked, exist_ok=True)
        self.assertTrue(os.path.isdir(folder))

class RealWin32Tests(unittest.TestCase):
    """用**真的** shell32 验证这套调用方式，全程不弹任何对话框。

    这台机器上不许开窗口，所以不直接测对话框。这套调用里
    `SHBrowseForFolderW` 之后拿 PIDL 去 `SHGetPathFromIDListW` 的那一步
    **可以脱离对话框单独验**：自己造一个 PIDL 再走一遍同样的签名。
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("只在 Windows 上有 shell32 / ole32")
        self.shell32 = ctypes.windll.shell32
        self.ole32 = ctypes.windll.ole32

    def test_a_pidl_round_trips_through_the_declared_signatures(self):
        """真实的 PIDL → 路径，走的就是崩溃那天那条路（只是没有对话框）。"""
        from ctypes import wintypes

        self.shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)]
        self.shell32.SHParseDisplayName.restype = ctypes.c_long
        self.shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
        self.shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        self.ole32.CoTaskMemFree.restype = None

        target = os.environ.get("WINDIR") or "C:\\Windows"
        pidl = ctypes.c_void_p()
        attributes = wintypes.ULONG()
        self.assertEqual(
            self.shell32.SHParseDisplayName(target, None, ctypes.byref(pidl), 0,
                                            ctypes.byref(attributes)), 0,
            "没拿到 PIDL")
        self.assertTrue(pidl.value, "PIDL 是空指针 → 签名声明有问题")
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            self.assertTrue(self.shell32.SHGetPathFromIDListW(pidl, buffer),
                            "按声明的方式传 PIDL 必须能换回路径")
            self.assertEqual(os.path.normcase(buffer.value), os.path.normcase(target))
        finally:
            self.ole32.CoTaskMemFree(pidl)

    def test_a_truncated_pointer_looks_exactly_like_the_addresses_in_the_log(self):
        """把日志里的地址反推一遍：它就是"低 32 位 + 符号扩展"。

        这条不调用任何会解引用的东西 —— 拿到残值之后**绝不去读它**，
        读它会把进程打崩。这里只比较数值形状。
        """
        # 真分配一块，确认高 32 位在本机确实是有效的（所以截断一定会丢东西）。
        self.ole32.CoTaskMemAlloc.argtypes = [ctypes.c_size_t]
        self.ole32.CoTaskMemAlloc.restype = ctypes.c_void_p
        self.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        pointer = self.ole32.CoTaskMemAlloc(64)
        self.assertTrue(pointer, "拿不到内存")
        try:
            low = pointer & 0xFFFFFFFF
            # ctypes 收 c_int 时对返回值做的事：只留低 32 位，再按有符号解释。
            truncated = ctypes.c_int(low).value & (2 ** 64 - 1)
            self.assertNotEqual(truncated, pointer, "按 c_int 收就该丢掉高位")
            self.assertEqual(truncated & 0xFFFFFFFF, low, "只丢高位，低 32 位一模一样")
        finally:
            self.ole32.CoTaskMemFree(pointer)

        # 日志里真实出现过的两个地址（app-20260923 / app-20260926）：
        # 它们各自都等于"某个 64 位指针的低 32 位按 c_int 符号扩展回来"。
        for logged in (0xFFFFFFFFE2480920, 0x0000000017DECC20):
            low = logged & 0xFFFFFFFF
            self.assertEqual(ctypes.c_int(low).value & (2 ** 64 - 1), logged,
                             "0x%X 的形状与「指针被截断」完全吻合" % logged)

if __name__ == "__main__":
    unittest.main()
