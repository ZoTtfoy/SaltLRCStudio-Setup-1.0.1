"""Windows 原生文件对话框（零第三方依赖）。

选目录 + 写文件都放到后端，不经过浏览器的 File System Access API（showDirectoryPicker）：
写文件走的是「写入歌曲目录」同一段代码，权限失败会给出中文提示。

**所有 Win32 调用的签名必须显式声明**（`argtypes` + `restype`）：

    ctypes 不声明 `restype` 时按 `c_int` 收返回值，而 `SHBrowseForFolderW`
    返回的是一个**指针**（PIDL）。64 位 Windows 上这个指针被**截断到 32 位**，
    下一次调用（`SHGetPathFromIDListW`）拿着残缺地址去读 →
    `OSError: access violation reading 0xFFFFFFFFE2480920`。

判据很好认：**异常里的地址只有 32 位**（高 32 位要么是 0、要么是符号扩展）——
真实堆指针在 x64 上是 `0x000001F2xxxxxxxx` 这个形状。
`_dlls()` 是这条管道的接缝：`tests/test_folders.py` 拿假的 DLL 把
结构体布局、签名声明、指针传递、缓冲区解码整条测一遍，不需要开窗口。
"""

import ctypes
import os
import platform
import subprocess
import threading
from ctypes import wintypes      # 模块级：`owner_window()` 也要用（只 import 在 `_win32` 里会 NameError）

TIMEOUT = 180
_UNINITIALISED = 0x800401F0  # CO_E_NOTINITIALIZED

# 新式（可缩放、带编辑框）的文件夹对话框
BIF_RETURNONLYFSDIRS = 0x0001
BIF_EDITBOX = 0x0010
BIF_NEWDIALOGSTYLE = 0x0040

OFN_OVERWRITEPROMPT = 0x00000002
OFN_PATHMUSTEXIST = 0x00000800
OFN_NOCHANGEDIR = 0x00000008
OFN_EXPLORER = 0x00080000

COINIT_APARTMENTTHREADED = 0x2


class PickerError(ValueError):
    """系统对话框弹不出来 / 超时 / 选中的位置转不成路径。

    故意继承 :class:`ValueError`：这里说的每句话（"路径过长"、"等待超时"）都是
    给用户看的，而 `server.do_POST` 只把 ``ValueError/TypeError/...`` 转成
    400 + 原文，其它异常一律是 ``500 服务器内部错误``。
    这也是"添加中文路径报错"里那句刺眼的
    `exception: access violation reading 0x0000000017DECC20` 的来源 ——
    访问冲突是个 ``OSError``，被原样当成 400 的正文发给了页面。
    """


def export_dir():
    """Always-writable fallback folder when no picker is available."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "SaltLRCStudio", "exports")
    os.makedirs(folder, exist_ok=True)
    return folder


def documents_dir():
    """用户的「文档」目录 —— 对话框的**兜底初始位置**。

    不设 `lpstrInitialDir` 时，Windows 会自己挑一个（进程当前目录、
    上一次用过的位置、甚至解包目录）。这里给出一个用户一定能找到的目录。
    """
    home = os.path.expanduser("~")
    for name in ("Documents", "文档"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def initial_dir(preferred=None, win=None):
    """对话框该从哪个文件夹打开。**优先用户这首歌自己的目录**，否则退回「文档」。

    `preferred` 是"这首歌从哪来的"（曲库导入/打开工程时我们知道），
    传进来的可能是一个文件路径、也可能已经是目录。
    """
    if preferred:
        try:
            candidate = os.path.abspath(str(preferred))
            if os.path.isfile(candidate):
                candidate = os.path.dirname(candidate)
            if os.path.isdir(candidate):
                return candidate
        except (OSError, ValueError):
            pass
    return documents_dir()


def fallback_dir(preferred=None):
    """原生对话框用不了时，文件该落到哪儿 —— **一定可写、而且用户找得到**。

    不用 `export_dir()`（那是 `%LOCALAPPDATA%\\SaltLRCStudio\\exports`，藏得很深）：
    导出的东西是给用户拿去用的，落点必须是他自己能摸到的地方。
    """
    target = initial_dir(preferred)          # 这首歌的目录，否则「文档」
    if os.access(target, os.W_OK):
        return target
    return documents_dir() if os.access(documents_dir(), os.W_OK) else export_dir()


def reveal(path):
    """在资源管理器里选中这个文件。**只有用户点了才会调用**（它会开窗口）。

    导出落到兜底目录时，用它把文件的落点显示出来。
    """
    target = os.path.abspath(str(path))
    if not os.path.exists(target):
        raise PickerError("这个文件已经不在了：%s" % target)
    if platform.system() != "Windows":
        raise PickerError("打开所在文件夹仅支持 Windows")
    subprocess.Popen(["explorer", "/select," + target])
    return target


def _dlls():
    """本机可用的 Win32 DLL 集合。**测试的接缝** —— 换掉它就能整条链路地测。"""
    if platform.system() != "Windows":
        raise PickerError("本机文件夹选择仅支持 Windows")
    return ctypes.windll


def _win32(dlls=None):
    """本次调用要用的接口；**签名在这里统一声明一次**（见模块开头的说明）。"""
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        """`pszDisplayName` 声明成 `c_void_p` 而不是 `LPWSTR`：它是一个**输出缓冲**，
        我们传的是自己那个 `create_unicode_buffer` 的地址。指针对指针，
        和真实布局逐字节相同，同时省掉一次 `cast`。"""

        _fields_ = [("hwndOwner", wintypes.HWND),
                    ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", ctypes.c_void_p),
                    ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT),
                    ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM),
                    ("iImage", wintypes.INT)]

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [("lStructSize", wintypes.DWORD),
                    ("hwndOwner", wintypes.HWND),
                    ("hInstance", wintypes.HINSTANCE),
                    ("lpstrFilter", ctypes.c_void_p),
                    ("lpstrCustomFilter", ctypes.c_void_p),
                    ("nMaxCustFilter", wintypes.DWORD),
                    ("nFilterIndex", wintypes.DWORD),
                    ("lpstrFile", ctypes.c_void_p),
                    ("nMaxFile", wintypes.DWORD),
                    ("lpstrFileTitle", ctypes.c_void_p),
                    ("nMaxFileTitle", wintypes.DWORD),
                    # 和 lpstrFilter/lpstrFile 同一写法：声明成地址、我们传自己持有的
                    # 缓冲区**地址**（声明成 LPCWSTR 就没法塞缓冲区进去）。
                    ("lpstrInitialDir", ctypes.c_void_p),
                    ("lpstrTitle", wintypes.LPCWSTR),
                    ("Flags", wintypes.DWORD),
                    ("nFileOffset", wintypes.WORD),
                    ("nFileExtension", wintypes.WORD),
                    ("lpstrDefExt", wintypes.LPCWSTR),
                    ("lCustData", wintypes.LPARAM),
                    ("lpfnHook", ctypes.c_void_p),
                    ("lpTemplateName", wintypes.LPCWSTR),
                    ("pvReserved", ctypes.c_void_p),
                    ("dwReserved", wintypes.DWORD),
                    ("FlagsEx", wintypes.DWORD)]

    dlls = dlls or _dlls()
    shell32, ole32, user32, comdlg32 = dlls.shell32, dlls.ole32, dlls.user32, dlls.comdlg32

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    # SHBrowseForFolderW 返回 PIDL（指针）—— 不写 restype 就会被截断成 32 位。
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long      # HRESULT（wintypes 里没有这个名字）
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    # 找"本进程自己的主窗口"要用的四个 —— 全都要声明，理由同上。
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    comdlg32.GetSaveFileNameW.restype = wintypes.BOOL

    import types
    return types.SimpleNamespace(shell32=shell32, ole32=ole32, user32=user32, comdlg32=comdlg32,
                                 BROWSEINFOW=BROWSEINFOW, OPENFILENAMEW=OPENFILENAMEW,
                                 WNDENUMPROC=WNDENUMPROC)


GW_OWNER = 4


def owner_window(win):
    """给对话框当 owner 的那个窗口：**本进程自己的、可见的、最大的一个顶层窗口**。

    不用 `GetForegroundWindow()`：点按钮那一瞬间前台窗口未必是本进程的 ——
    WebView 的焦点、或者刚点过别的程序，都会让 owner 变成别人，
    而**对话框的 z 序跟着 owner 走**。owner 不是本进程窗口的时候，对话框可以掉到应用窗口
    **后面**（状态行也不会变，因为代码正在等那个模态框）。

    找不到就退回 `GetForegroundWindow()`。
    """
    best, best_area = None, -1
    try:
        pid = os.getpid()

        def visit(hwnd, _param):
            nonlocal best, best_area
            if not win.user32.IsWindowVisible(hwnd):
                return True
            if win.user32.GetWindow(hwnd, GW_OWNER):        # 只认顶层窗口
                return True
            owner_pid = wintypes.DWORD()
            win.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != pid:
                return True
            rect = wintypes.RECT()
            if not win.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area > best_area:
                best, best_area = hwnd, area
            return True

        win.user32.EnumWindows(win.WNDENUMPROC(visit), 0)
    except (OSError, AttributeError, ValueError) as error:
        # 只兜住"这一台机器上枚举不了"这一类；**不要把 NameError/TypeError 也吞掉** ——
        # 那会让"`wintypes` 没导入"这类错误被静默吃掉，枚举一次都不生效。
        diagnostics_note = error
        best = None
    if best:
        return best
    return win.user32.GetForegroundWindow()


def _run(worker):
    """在工作线程里跑对话框并把结果带回；异常原样抛给调用方。

    对话框要有自己的消息循环，而请求线程要在这里等结果，所以放到独立线程上跑；
    `CoInitializeEx` 也在这个线程上做（STA）。
    """
    result = {}
    done = threading.Event()

    def wrapped():
        win = None
        try:
            win = _win32()
            # 对话框要有自己的消息循环，所以必须在一个 STA 线程上跑。
            win.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            worker(win, result)
        except Exception as error:  # noqa: BLE001 - 原样交给调用方转成提示
            result["error"] = error
        finally:
            if win is not None:
                try:
                    win.ole32.CoUninitialize()
                except Exception:
                    pass
            done.set()

    threading.Thread(target=wrapped, daemon=True).start()
    if not done.wait(TIMEOUT):
        raise PickerError("等待系统对话框超时（%d 秒）；它可能被压在了别的窗口后面" % TIMEOUT)
    if "error" in result:
        raise result["error"]
    return result


def _prepare_foreground(win):
    """窗口持有前台时，对话框可能被压在应用窗口后面 —— 那和"没反应"一模一样。

    owner 由 `owner_window()` 定（本进程自己的窗口），这里再把它提到前台，
    两道一起用：只提前台不改 owner，是"提了又掉回去"；只改 owner 不提前台，
    在应用本身就没在前台时（用户刚点过别的程序）对话框还是可能开在别处。
    """
    try:
        win.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    try:
        hwnd = owner_window(win)
        if hwnd:
            win.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def pick_folder(title="SaltLRC Studio：选择存放歌词的文件夹", preferred=None):
    """弹原生文件夹对话框；用户取消返回 None。"""
    if platform.system() != "Windows":
        raise PickerError("本机文件夹选择仅支持 Windows")
    start = initial_dir(preferred)

    def worker(win, result):
        _prepare_foreground(win)
        info = win.BROWSEINFOW()
        display = ctypes.create_unicode_buffer(260)
        # 有主的对话框才是模态的（会禁用我们的窗口、跟着窗口居中）。
        # 没有 owner 的对话框可以掉到窗口后面，用户就以为按钮坏了。
        info.hwndOwner = owner_window(win)
        info.pszDisplayName = ctypes.cast(display, ctypes.c_void_p)
        info.lpszTitle = title
        info.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
        pidl = win.shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            result["path"] = None                     # 用户取消
            return
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            if not win.shell32.SHGetPathFromIDListW(pidl, buffer) or not buffer.value:
                # 用户**选了一个**位置，只是系统没能把它转成路径
                # （多半是超过 260 字符）。这不能当成"取消"—— 那等于静默失败。
                raise PickerError("系统没能把选中的位置转成路径：可能路径过长（超过 260 字符），"
                                   "或者那不是本机上的普通文件夹")
            result["path"] = buffer.value
        finally:
            # 不管成功还是失败都要释放，否则每失败一次就漏一段 shell 内存。
            win.ole32.CoTaskMemFree(pidl)

    return _run(worker).get("path")


def save_file(default_name, title="SaltLRC Studio：选择保存位置", filters=(("歌词文件", "*.lrc;*.yrc"),),
              preferred=None):
    """弹原生「另存为」对话框；用户取消返回 None。

    文件夹选择器不适合"存这一个文件"：还得自己起名字，扩展名也容易丢。
    GetSaveFileNameW 给出的是系统标准的对话框，名字预填、有类型筛选、会问要不要覆盖。

    由后端调：打包窗口里页面那层没有下载界面，blob 下载不会落盘。

    `preferred` = **这首歌从哪来的**（首选它，否则「文档」）。
    不设初始目录时 Windows 会自己挑一个（进程当前目录 / 上一次的位置 / 甚至解包目录）。
    """
    if platform.system() != "Windows":
        raise PickerError("本机另存为对话框仅支持 Windows")
    start = initial_dir(preferred)

    # "标签\0通配符\0标签\0通配符\0\0" —— 键值成对，双 NUL 结束。
    # 里面的 NUL 是**数据**不是结尾，所以显式建一个缓冲区自己拿着，
    # 不依赖 ctypes 对 `str → wchar*` 的隐式转换（那种转换的生命周期不在我们手里）。
    pattern = "\0".join("%s\0%s" % (label, wildcard) for label, wildcard in filters) + "\0"
    suffix = default_name.rsplit(".", 1)[-1] if "." in default_name else ""

    def worker(win, result):
        _prepare_foreground(win)
        buffer = ctypes.create_unicode_buffer(default_name, 2048)
        # 自己拿着这些缓冲区：结构体里存的是**地址**，对象一旦被回收，
        # Windows 就会写到已经释放的内存上（和指针被截断是同一类事故）。
        filter_buffer = ctypes.create_unicode_buffer(pattern)
        start_buffer = ctypes.create_unicode_buffer(start)
        spec = win.OPENFILENAMEW()
        spec.lStructSize = ctypes.sizeof(win.OPENFILENAMEW)   # 不写这个 Windows 直接拒绝
        spec.hwndOwner = owner_window(win)
        spec.lpstrFilter = ctypes.cast(filter_buffer, ctypes.c_void_p)
        spec.lpstrFile = ctypes.cast(buffer, ctypes.c_void_p)
        spec.nMaxFile = len(buffer)
        spec.lpstrInitialDir = ctypes.cast(start_buffer, ctypes.c_void_p)
        spec.lpstrTitle = title
        spec.lpstrDefExt = suffix
        spec.Flags = (OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                      | OFN_NOCHANGEDIR | OFN_EXPLORER)
        if not win.comdlg32.GetSaveFileNameW(ctypes.byref(spec)):
            result["path"] = None                             # 取消
            return
        result["path"] = buffer.value or None

    return _run(worker).get("path")
