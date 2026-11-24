"""Local JSON files under %LOCALAPPDATA%\\SaltLRCStudio.

Two properties every store in this app needs, defined once here:

* **Atomic writes.** A half-written file silently loses whatever it held — a
  login that stops working, a setting that will not stick. Every save goes to a
  temp file on the same filesystem and is then renamed over the target.
* **Corrupt data never raises.** The caller is usually in the middle of an
  unrelated request; a crash there looks like a bug in that request. A bad file
  is ignored (and left in place so it can be inspected) rather than thrown.
"""

import json
import os
import tempfile
import time

APP_DIR = "SaltLRCStudio"


def config_dir():
    # `SALTLRC_CONFIG_DIR` overrides the config directory. Without it, a test
    # that sets the project save path writes the real
    # %LOCALAPPDATA%\SaltLRCStudio\project-root.json.
    override = os.environ.get("SALTLRC_CONFIG_DIR")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_DIR)


def app_path(name):
    return os.path.join(config_dir(), name)


def read_json(path, fallback):
    """Parse ``path``, or return ``fallback`` if it is missing or unusable."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def _atomic(path, prefix, write):
    """Replace ``path`` atomically, filling the temp file via ``write(stream)``.

    Both writers below differ *only* in how they turn a value into text; the
    dangerous part (temp file beside the target, fsync, rename, cleanup on
    failure) lives here once.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # The temp file must sit next to the target: renaming across filesystems is
    # not atomic, and on Windows it fails outright.
    handle, temp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temp, path)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return path


# Windows 上 `os.replace` 会在**目标正被另一个线程/进程打开**的那一瞬间失败：
# 报 WinError 5（拒绝访问）或 32（另一个程序正在使用此文件）。这两个都是**瞬时**的，
# 退避几毫秒重试即可（并发建工程时偶发）。
# 只重试这两种错误码：真正的"没有权限"（只读目录）重试多少次都一样，必须原样抛出去。
_TRANSIENT_WINERRORS = frozenset({5, 32})


def _replace(temp, path, attempts=5):
    for attempt in range(attempts):
        try:
            os.replace(temp, path)
            return
        except OSError as error:
            transient = getattr(error, "winerror", None) in _TRANSIENT_WINERRORS
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def atomic_write_json(path, data, prefix=".store-"):
    """Replace ``path`` with ``data`` atomically. Returns the path written."""
    return _atomic(path, prefix,
                   lambda stream: json.dump(data, stream, ensure_ascii=False, indent=2))


def atomic_write_text(path, text, prefix=".store-"):
    """Replace ``path`` with UTF-8 ``text`` atomically. Returns the path written.

    A project file (``.saltproj``) is built by the page and must come back as the
    exact same character sequence, so it goes to disk verbatim — no
    ``json.dump`` round trip.
    """
    return _atomic(path, prefix, lambda stream: stream.write(text))
