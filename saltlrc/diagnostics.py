"""In-process diagnostic journal: log lines, request history, captured errors.

The packaged build runs without a console window, so this journal is the only
place where errors and the user's recent actions are visible. It also keeps a
plain-text log file on disk so a crash report can be exported after the fact.
"""

import hashlib
import os
import sys
import time
import traceback
from collections import deque
from pathlib import Path

# 调试页的门：口令**只从环境变量来**，没有设置就是关闭（接口一律 403）。


def debug_token():
    """调试口令的散列；没有设置 SALTLRC_DEBUG_PASSWORD 时返回 None（接口关闭）。"""
    password = os.environ.get("SALTLRC_DEBUG_PASSWORD")
    if not password:
        return None
    return hashlib.sha256(("saltlrc:" + password).encode("utf-8")).hexdigest()[:20]


def debug_enabled():
    """调试接口是否打开（有没有设置口令）。"""
    return debug_token() is not None


def check_password(password):
    return debug_token() == hashlib.sha256(("saltlrc:" + (password or "")).encode("utf-8")).hexdigest()[:20]


def logs_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = Path(base) / "SaltLRCStudio" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class Diagnostics:
    """Ring buffers + a plain log file. Never raises: diagnostics must not be
    able to take the server down."""

    def __init__(self, lines=800, requests=400, errors=120):
        self.started = time.time()
        self.log_lines = deque(maxlen=lines)
        self.requests = deque(maxlen=requests)
        self.errors = deque(maxlen=errors)
        self._file = None

    # ---- log file -------------------------------------------------------
    def _open(self):
        if self._file is not None:
            return self._file
        try:
            path = logs_dir() / ("app-%s.log" % time.strftime("%Y%m%d"))
            self._file = path.open("a", encoding="utf-8", errors="replace")
            self._file.write("\n==== session %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            self._file = None
        return self._file

    # ---- writers --------------------------------------------------------
    def write(self, text):
        for line in str(text).splitlines():
            line = line.rstrip()
            if not line:
                continue
            self.log_lines.append("%s %s" % (time.strftime("%H:%M:%S"), line))
        handle = self._open()
        if handle:
            try:
                handle.write(str(text).rstrip() + "\n")
                handle.flush()
            except OSError:
                self._file = None

    def request(self, method, path, status, seconds):
        self.requests.append({"time": time.strftime("%H:%M:%S"), "method": method,
                              "path": str(path)[:200], "status": status,
                              "ms": round(seconds * 1000, 1)})

    def error(self, message, detail=None):
        entry = {"time": time.strftime("%H:%M:%S"), "message": str(message)[:500],
                 "traceback": (detail or "").strip()[-4000:]}
        self.errors.append(entry)
        self.write("[error] %s%s" % (message, "\n" + detail if detail else ""))

    # ---- readers --------------------------------------------------------
    def snapshot(self):
        return {
            "started": self.started,
            "uptime": round(time.time() - self.started),
            "log": list(self.log_lines),
            "requests": list(self.requests),
            "errors": list(self.errors),
            "log_file": str(logs_dir() / ("app-%s.log" % time.strftime("%Y%m%d"))),
            "log_dir": str(logs_dir()),
        }


    # ---- 收尾 -----------------------------------------------------------
    def close(self):
        """放开日志文件的句柄。

        `self._file` 是**整个进程生命周期**都开着的（`open("a")`），
        而 Windows 上**被占用的文件删不掉** —— 不放开的话，
        `logs\\app-<日期>.log` 会让"删掉这个配置目录"的收尾静默失败。
        """
        handle, self._file = self._file, None
        if handle:
            try:
                handle.close()
            except OSError:
                pass


DIAGNOSTICS = Diagnostics()


def close():
    """模块级的收尾入口（见 `Diagnostics.close`）。"""
    DIAGNOSTICS.close()


def install_stdout_bridge():
    """Mirror stdout/stderr into the journal when there is no console.

    A windowed PyInstaller build has no usable stdout; without this, print()
    either vanishes or raises. When a console does exist, output still reaches
    it — we only add a second sink.
    """
    class Bridge:
        def __init__(self, original):
            self.original = original

        def write(self, text):
            try:
                DIAGNOSTICS.write(text)
            except Exception:
                pass
            if self.original is not None:
                try:
                    self.original.write(text)
                except Exception:
                    pass

        def flush(self):
            if self.original is not None:
                try:
                    self.original.flush()
                except Exception:
                    pass

        def isatty(self):
            return False

    if type(sys.stdout).__name__ != "Bridge":
        sys.stdout = Bridge(sys.stdout)
    if type(sys.stderr).__name__ != "Bridge":
        sys.stderr = Bridge(sys.stderr)


def capture_error(error):
    """Record an exception with its traceback, without ever raising."""
    try:
        DIAGNOSTICS.error(error, traceback.format_exc())
    except Exception:
        pass
