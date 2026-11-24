"""测试用的临时目录**只有一个出口** —— 这是这个文件唯一的职责。

`scratch()` 建目录、`isolate_localappdata()` 隔离 `LOCALAPPDATA`，
两者建的目录都在进程退出时由 `_cleanup()` 统一删掉。

`tests/test_hygiene.py` 有一条静态断言盯着"`mkdtemp` 只许出现在这个文件里"，
所以这条规矩不靠自觉。
"""

import atexit
import os
import shutil
import sys
import tempfile
import time

_DIRS = []


def scratch(prefix):
    """建一个临时目录，**进程退出时自动删掉**。

    用例里不需要再写 `addCleanup(shutil.rmtree, ...)` —— 清理只在这一处发生。
    """
    path = tempfile.mkdtemp(prefix=prefix)
    _DIRS.append(path)
    return path


def isolate_localappdata(prefix):
    """把 `LOCALAPPDATA` 指到一个临时目录 —— **只在还没被隔离过时才建新的**。

    用 `os.environ.setdefault(...)` 会每次调用都建一个目录（参数先求值），
    即使绝大多数调用并不需要（上一个测试文件已经指过了）。这里先判断再建。

    判据是路径名以 `saltlrc-` 开头 —— 测试代码用的前缀，
    真实用户环境里的 `%LOCALAPPDATA%` 不会长这样。
    """
    current = os.environ.get("LOCALAPPDATA", "")
    if os.path.basename(current.replace("/", "\\").rstrip("\\")).startswith("saltlrc-"):
        return current
    path = scratch(prefix)
    os.environ["LOCALAPPDATA"] = path
    return path


@atexit.register
def _cleanup():
    """进程退出时把这一轮建的全部临时目录删掉，**删不掉的要重试并出声**。

    重试前先关日志：`saltlrc/diagnostics.py` 的 `FileHandler` 会一直占着
    `<临时目录>/SaltLRCStudio/logs/app-<日期>.log`，而 **Windows 上被占用的文件删不掉** ——
    不先关，第一次 `rmtree` 会静默失败、目录留下来。
    被占用的是**同进程里没关的句柄**；`atexit` 是后进先出，
    `logging` 自己的 shutdown 排在后面，所以这里要显式先关一次。

    只写 `ignore_errors=True` 等于"清不掉也不说"，所以最后还剩几个就报几个
    （写到 stderr，不改退出码 —— 为收尾失败把测试判红不值得）。
    """
    try:
        import logging
        logging.shutdown()          # 先放开日志文件的句柄，否则 rmtree 一定失败
    except Exception:
        pass
    # 应用自己的日志句柄也要放：`diagnostics.DIAGNOSTICS._file` 是 `open("a")` 出来的，
    # 进程活多久它就开多久，而 **Windows 上被占用的文件删不掉** ——
    # 这才是"清了但没清掉"的真因（只剩一个 logs\app-<日期>.log 留下）。
    try:
        from saltlrc import diagnostics
        diagnostics.close()
    except Exception:
        pass
    for _attempt in range(5):
        for path in list(_DIRS):
            if not os.path.exists(path):
                _DIRS.remove(path)
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                _DIRS.remove(path)
        if not _DIRS:
            return
        time.sleep(0.4)                 # 让子进程的句柄先放开
    if _DIRS:
        print("警告：%d 个测试临时目录没能删掉（子进程可能还占着）：%s"
              % (len(_DIRS), "、".join(os.path.basename(p) for p in _DIRS[:5])), file=sys.stderr)
