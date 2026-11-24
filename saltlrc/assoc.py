"""Windows 文件关联：让 `.saltproj` 显示本程序的图标、双击就用本程序打开。

**单一写者。** 写注册表这件事有两个可能的来源：安装器（Inno Setup）和程序自己，
而这里定成**只有一个写者**：

* 程序运行时按 **``HKCU\\Software\\Classes``** 写 —— 不需要管理员权限，取消关联时
  只删自己写的那几个键，别人的关联一个都不动；
* 安装器只负责把图标文件放进程序目录（见 ``packaging/installer.iss`` 的 ``[Files]``）。

**图标用"exe 旁边的一个 .ico"，不内嵌进 exe 的资源。**
``DefaultIcon`` 需要一个能求值的路径。内嵌的话只能写 ``"exe",N``，而那个 ``N`` 到底是
**序号**（``ExtractIconEx`` 语义，0 起）还是**资源 ID**（PyInstaller 按 ``--icon``
顺序从 1 编号）在 Windows 上并不统一 —— 一个只能靠猜的值不该进注册表。
旁边放一个 ``.ico`` 是**可以直接看一眼验证**的：值就是那个文件的路径。

**双击打开的安全性。** 关联把命令行写成 ``"<exe>" --open "%1"``；页面**永远不提交
路径**，它只是问服务端要"这个进程启动时带的那个工程"的字节（``/api/pending-open``）。
"读哪个文件"由**本进程自己的命令行**决定，不由页面决定。

**失败绝不能让程序起不来**：``ensure()`` 是启动路径上的调用，它把所有异常都收成一句
可读的原因（并且写进日志），而不是抛出去。
"""

import os
import sys

EXT = ".saltproj"
PROGID = "SaltLRCStudio.Project"
TYPE_NAME = "SaltLRC 工程文件"
ICON_FILE = "SaltLRCStudio-project.ico"
OPEN_FLAG = "--open"

CLASSES = r"Software\Classes"
FILE_EXTS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"

_EXT_KEY = CLASSES + "\\" + EXT
_PROGID_KEY = CLASSES + "\\" + PROGID
_ICON_KEY = _PROGID_KEY + "\\DefaultIcon"
_COMMAND_KEY = _PROGID_KEY + "\\shell\\open\\command"


# --------------------------------------------------------------------- paths
def exe_path():
    """打包后自己的 exe 路径；**源码运行返回 None**。

    源码模式没有"固定的 exe 路径"可写进注册表 —— ``sys.executable`` 是 python.exe，
    把 ``.saltproj`` 关联到 python.exe 只会让双击弹出一个黑窗口。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return None


def icon_file(exe):
    return os.path.join(os.path.dirname(os.path.abspath(exe)), ICON_FILE)


def command_for(exe):
    """``shell\\open\\command`` 的值：把工程路径原样交给本程序。"""
    return '"%s" %s "%%1"' % (os.path.abspath(exe), OPEN_FLAG)


def icon_spec(exe):
    """``DefaultIcon`` 的值，外加"图标文件在不在"这一事实。

    图标文件不在时退回 exe 自身的图标（``"exe",0``）—— 比让资源管理器显示一个空白
    图标好，也比让注册表指向一个不存在的文件好；而"它退回去了"这件事会由 ``status()``
    报出来，不会静默。
    """
    path = icon_file(exe)
    if os.path.isfile(path):
        return '"%s"' % path, True
    return '"%s",0' % os.path.abspath(exe), False


# ------------------------------------------------------------------ registry
class WinRegistry:
    """HKCU 下的最小注册表读写。

    路径一律是 **HKCU 相对路径**（例如 ``Software\\Classes\\.saltproj``），这样
    测试可以塞一个字典假实现进来，整条逻辑就能在没有注册表的机器上跑。

    ``delete_key`` **只删空键** —— 这不是偷懒，而是刻意对齐 ``winreg.DeleteKey``：
    整棵树的删除由模块级的 :func:`erase` 递归完成，而测试里的假实现也照这条规矩
    来（删非空键就报错）。于是"忘了递归"这种错会**当场变红**，而不是在一个
    看起来成功的"取消关联"里悄悄留下半支键。
    """

    def read(self, path, name=None):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return value
        except OSError:
            return None

    def write(self, path, name, value):
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    def children(self, path):
        import winreg
        found = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                while True:
                    try:
                        found.append(winreg.EnumKey(key, len(found)))
                    except OSError:
                        break
        except OSError:
            return []
        return found

    def exists(self, path):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
                return True
        except OSError:
            return False

    def delete_key(self, path):
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)     # 非空会抛 OSError


def erase(registry, path):
    """整棵删掉。

    ``winreg.DeleteKey`` **删不掉带子键的键**，所以必须从叶子往上删。
    直接调 ``DeleteKey`` 时，``SaltLRCStudio.Project`` 那一支（有 ``DefaultIcon``
    与 ``shell`` 两个子键）删不掉，而它不抛异常 —— "取消关联"看起来是成功的，
    界面也只是重新读了一遍状态。
    """
    for child in registry.children(path):
        erase(registry, path + "\\" + child)
    try:
        registry.delete_key(path)
    except OSError:
        pass


def notify_shell():
    """告诉资源管理器"关联变了"，否则图标缓存不会立刻刷新。失败不算错。"""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
        return True
    except Exception:
        return False


def applications_key(exe):
    """``Software\\Classes\\Applications\\<exe 名>`` —— Windows 记"用某个程序打开"的地方。

    **它比我们写的 ProgID 优先级更高**：只要 ``UserChoice`` 落到这一支，
    Windows 就走它的 ``shell\\open\\command``、取它的 ``DefaultIcon``。

    这一支的命令可能指向一个已经不在的路径（例如旧的交付目录），于是
    "图标贴好了、双击却去开一个不存在的文件"。
    这一支属于**我们自己**（名字就是本程序），所以 ``install()`` 会把它修回来
    （只修不新建，见那里的注释）。
    """
    return CLASSES + "\\Applications\\" + os.path.basename(os.path.abspath(exe))


def choice_owner(registry, choice, exe):
    """``UserChoice`` 会把双击交给谁：``none`` / ``ours`` / ``stale`` / ``foreign``。

    只比对 ProgID 是不够的：``Applications\\<我们的 exe>`` 也是我们 —— 但它的命令
    可能指着旧的路径（``stale``）。

    判据必须是**完整的 exe 路径**而不是文件名：旧命令里的 exe 名与我们相同，
    只是目录不同 —— 按文件名比会得出"没事"。
    """
    if not choice:
        return "none"
    if str(choice).upper() == PROGID.upper():
        return "ours"
    here = os.path.abspath(exe)
    command = registry.read(CLASSES + "\\" + str(choice) + r"\shell\open\command") or ""
    if here.lower() in command.lower():
        return "ours"
    if os.path.basename(str(choice)).lower() == os.path.basename(here).lower():
        return "stale"
    return "foreign"


# --------------------------------------------------------------------- verbs
def status(exe=None, registry=None):
    """当前关联的**事实**：谁占着 ``.saltproj``、图标与命令分别指向哪里。"""
    target = os.path.abspath(exe) if exe else exe_path()
    reg = registry if registry is not None else WinRegistry()
    owner = reg.read(_EXT_KEY)
    icon = reg.read(_ICON_KEY)
    command = reg.read(_COMMAND_KEY)
    type_name = reg.read(_PROGID_KEY)
    user_choice = reg.read(FILE_EXTS + "\\" + EXT + "\\UserChoice", "ProgId")

    state = {
        "supported": bool(target),
        "reason": None if target else "源码运行：没有任何 exe 路径可写进注册表（打包后才生效）",
        "ext": EXT, "progid": PROGID, "type_name": TYPE_NAME,
        "exe": target, "owner": owner, "icon": icon, "command": command,
        "icon_file": icon_file(target) if target else None,
        "user_choice": user_choice, "user_choice_owner": "none",
        "installed": False, "effective": False, "problems": [],
    }
    if not target:
        return state

    wanted_icon, has_icon = icon_spec(target)
    wanted_command = command_for(target)
    state["icon_ok"] = has_icon
    state["installed"] = (owner == PROGID and icon == wanted_icon and command == wanted_command)

    if not has_icon:
        # 图标文件不在程序目录里 —— 退回程序自身的图标**不是错误**，但必须说出来：
        # 用户看到的是"图标不对"，而不是"文件少了"。
        state["problems"].append(
            "图标文件不在程序目录里（缺 %s）：现在退回程序自身的图标。"
            "把 %s 放回 %s 就能恢复" % (ICON_FILE, ICON_FILE,
                                        os.path.dirname(os.path.abspath(target))))
    elif icon and icon != wanted_icon:
        state["problems"].append(
            "注册表里现在指向 %s（程序已经不在那个位置了）：再点一次「保存」就会改成 %s"
            % (icon, wanted_icon))

    # Windows 的「打开方式」（`UserChoice`）是**写在别处的第二个权威**，而且优先级
    # 比我们写的 ProgID 更高。三种情况要分清楚，否则界面上会给出错的建议：
    #   ours    —— 就是我们（含 `Applications\<我们的 exe>`）；什么也不用做
    #   stale   —— 名字是我们的，但命令指着旧路径：双击会去开一个不存在的文件
    #   foreign —— 真的被别的程序接管了：只能让用户自己去改
    where = choice_owner(reg, user_choice, target)
    state["user_choice_owner"] = where
    if where == "stale":
        stale = reg.read(CLASSES + "\\" + str(user_choice) + r"\shell\open\command") or ""
        state["problems"].append(
            "Windows 的「打开方式」记着一份旧路径（%s），双击会去开它 —— "
            "而那个位置已经没有程序了。再点一次「保存」会自动把它改回本程序。" % (stale or "（读不到）"))
    elif where == "foreign":
        state["problems"].append(
            "这个文件类型的「打开方式」被别的程序接管了（%s），Windows 会优先用它；"
            "在文件的「打开方式 → 选择其他应用」里改回本程序即可" % user_choice)

    state["effective"] = state["installed"] and where in ("none", "ours")
    return state


def install(exe=None, registry=None, notify=True):
    """写入关联。**只写自己那几个键**，别人的一个都不动。"""
    target = os.path.abspath(exe) if exe else exe_path()
    if not target:
        return {"supported": False,
                "reason": "源码运行：没有任何 exe 路径可写进注册表（打包后才生效）"}
    reg = registry if registry is not None else WinRegistry()
    icon, _ = icon_spec(target)
    reg.write(_EXT_KEY, None, PROGID)
    reg.write(_PROGID_KEY, None, TYPE_NAME)
    reg.write(_ICON_KEY, None, icon)
    reg.write(_COMMAND_KEY, None, command_for(target))
    # `Applications\<我们的 exe>` 那一支：**只修不新建**。
    #
    # 它可能已经存在（选过「打开方式 → 本程序」，或者本程序自己写过），
    # 里面可能留着旧的绝对路径。因为 UserChoice 落到它时 Windows **不看我刚写的
    # ProgID**，那种状态下"图标贴好了、双击却打不开"。
    # 没有这一支时不主动创建：那是"打开方式"候选列表，不该由安装关联顺手加进去。
    apps = applications_key(target)
    if reg.exists(apps):
        reg.write(apps + r"\shell\open\command", None, command_for(target))
        # 图标也得给一份：走这一支时 Windows 取的是它的 DefaultIcon。
        reg.write(apps + r"\DefaultIcon", None, icon)
    if notify:
        notify_shell()
    return status(exe=target, registry=reg)


def remove(registry=None, notify=True):
    """撤销关联。

    ``.saltproj`` 那一支**只在它属于我们的时候**才删：万一用户把 ``.saltproj``
    交给了别的程序，这里必须留着那条关联不动（宁可不删，也不越界 —— 和工程库
    删除边界的同一条规矩）。
    """
    reg = registry if registry is not None else WinRegistry()
    if (reg.read(_EXT_KEY) or "") == PROGID:
        erase(reg, _EXT_KEY)
    erase(reg, _PROGID_KEY)
    # `UserChoice` 指向我们时**也得清掉**，否则"取消关联"根本不生效：
    # Windows 会继续按 UserChoice 打开本程序（真机上验证过 —— 那台机器的
    # UserChoice 落在 `Applications\SaltLRCStudio.exe` 上，而它优先级更高）。
    # 指向**别人**时一个字节都不动：那是别人的地盘。
    target = exe_path()
    choice = reg.read(FILE_EXTS + "\\" + EXT + "\\UserChoice", "ProgId")
    if target and choice_owner(reg, choice, target) in ("ours", "stale"):
        erase(reg, FILE_EXTS + "\\" + EXT + "\\UserChoice")
    if notify:
        notify_shell()
    return status(registry=reg)


def ensure(registry=None, notify=True):
    """启动时调用：按设置把关联调成"应该的样子"。**永不抛异常。**

    设置关了 = 不装（也不主动删 —— 删除是用户在设置里明确点的那一下）。
    """
    try:
        from . import settings as settings_store
        wanted = bool(settings_store.load()["features"].get("associateProjectFiles", True))
        target = exe_path()
        if not target:
            return {"supported": False,
                    "reason": "源码运行：跳过文件关联（没有任何 exe 路径可写进注册表）"}
        if not wanted:
            state = status(exe=target, registry=registry)
            state["reason"] = "设置里关掉了这一项，未做关联"
            return state
        current = status(exe=target, registry=registry)
        # 判据是 `effective` 而不是 `installed`：键写对了但被 UserChoice 顶掉
        # （比如它记着一份旧路径）时，"已经装好了"是句假话。
        if current["effective"]:
            return current
        return install(exe=target, registry=registry, notify=notify)
    except Exception as error:          # 关联失败不能让程序起不来
        try:
            from . import diagnostics
            diagnostics.DIAGNOSTICS.write("关联 .saltproj 失败：%s" % error)
        except Exception:
            pass
        return {"supported": False, "reason": "关联 .saltproj 失败：%s" % error}


def describe(state):
    """一行日志用的人话（启动时写进诊断日志）。"""
    if not state.get("supported"):
        return state.get("reason") or "不支持"
    if state.get("effective"):
        return "已关联并生效（图标 %s；双击 %s）" % (state.get("icon"), state.get("command"))
    if state.get("installed"):
        # 键写好了但系统不按它走 —— 必须说出来，否则读日志的人会以为已经好了。
        return "键已写入但没有真正生效（%s）" % (
            "；".join(state.get("problems") or []) or "被别的打开方式接管")
    return "未关联（%s）" % ("；".join(state.get("problems") or []) or "注册表里没有本程序的条目")
