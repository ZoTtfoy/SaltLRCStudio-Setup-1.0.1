"""Frozen entry point for the packaged SaltLRC Studio executable.

Double-clicking the built .exe lands here. By default it opens the editor in
its **own window** (WebView2), which is what makes the packaged build feel like
an application instead of "a Python script that opens Chrome". ``--browser``
keeps the old behaviour for anyone who prefers their own browser.
``--worker`` is the internal switch used by the audio analysis subprocess (a
frozen build cannot run ``python -m``).
"""

import socket
import sys

START_PORT = 8765
PORT_TRIES = 25


def free_port(start=START_PORT, tries=PORT_TRIES):
    for port in range(start, start + tries):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return None


def alert(title, text):
    """The last resort for a windowed build: without a console, a silent exit
    looks exactly like a crash. Falls back to the log file only."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text)[:1000], str(title), 0x10)
        return
    except Exception:
        pass
    try:
        print(title + "：" + text)
    except Exception:
        pass


def open_path_from_argv(argv):
    """The project file this launch was asked to open, or None.

    Two shapes, one per caller. ``--open "<path>"`` is what the
    file association writes: an explicit flag survives a path that happens to
    start with a dash, and it cannot be confused with an option. A bare path is
    what "drag a file onto the exe" and Explorer's "send to" produce.

    Kept as a pure function of argv so it can be tested without launching
    anything — the parsing is the only part of this that can be wrong.
    """
    for index, argument in enumerate(argv):
        if argument == "--open":
            following = argv[index + 1] if index + 1 < len(argv) else ""
            return following or None
        if argument.startswith("--open="):
            return argument.split("=", 1)[1] or None
    for argument in argv:
        if argument.startswith("-"):
            continue
        return argument or None
    return None


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        if len(argv) < 2:
            return 2
        from saltlrc.worker import run
        return run(argv[1])

    # The packaged build has no console: mirror stdout/stderr into the journal
    # so errors are still visible on the debug page and in the log file.
    from saltlrc import diagnostics
    diagnostics.install_stdout_bridge()
    diagnostics.DIAGNOSTICS.write("SaltLRC Studio 启动（%s）" %
                                  ("打包版" if getattr(sys, "frozen", False) else "源码"))

    # 文件关联（`.saltproj` 的图标 + 双击打开）：启动时按设置调成"应有的样子"。
    # 放在**入口**而不是窗口层或服务层 —— 两扇门（独立窗口 / 浏览器）都要走到它，
    # 而进来的门只有一个。它自己不会抛异常，也从不阻塞启动。
    from saltlrc import assoc
    diagnostics.DIAGNOSTICS.write("工程文件关联：" + assoc.describe(assoc.ensure()))

    open_path = open_path_from_argv(argv)

    port = None
    models = None
    device = "cpu"
    for argument in argv:
        if argument.startswith("--port="):
            try:
                port = int(argument.split("=", 1)[1])
            except ValueError:
                port = None
        elif argument.startswith("--models="):
            models = argument.split("=", 1)[1] or None
        elif argument.startswith("--device="):
            device = argument.split("=", 1)[1] or "cpu"

    # --browser restores the old "start the server and hand off to the default
    # browser" path. --no-browser still means "serve, but open nothing" (the
    # smoke test drives the API directly and must not raise a window).
    want_window = "--browser" not in argv and "--no-window" not in argv
    headless = "--no-browser" in argv

    if want_window and not headless:
        from saltlrc import window
        if window.available():
            return run_window(port, models, device, open_path)
        # A missing WebView2 runtime or a non-Windows host must not be a dead
        # end: fall back to the browser, which needs nothing extra.
        diagnostics.DIAGNOSTICS.write("独立窗口不可用，改为在浏览器中打开")

    return _serve_in_browser(port, models, device,
                             open_browser=not headless, open_path=open_path)


def _serve_in_browser(port, models, device, open_browser, open_path=None):
    """The original headless path: serve on the loopback and optionally open a
    browser tab. Also the fallback when no native window can be created."""
    from saltlrc.server import serve as serve_headless

    if port is None:
        port = _free_port()
    if port is None:
        alert("SaltLRC Studio 无法启动",
              "找不到可用端口（%d-%d 都被占用）。\n\n%s" %
              (START_PORT, START_PORT + PORT_TRIES - 1, _logs_dir()))
        return 1
    serve = serve_headless
    try:
        serve(port=port, open_browser=open_browser, models=models, device=device,
              open_path=open_path)
    except KeyboardInterrupt:
        print("\n已停止。")
    except OSError as error:
        alert("SaltLRC Studio 启动失败", "%s\n\n诊断日志：%s" % (error, _logs_dir()))
        return 1
    except Exception:
        from saltlrc import diagnostics
        diagnostics.capture_error("启动时发生未预期的错误")
        alert("SaltLRC Studio 启动失败",
              "发生未预期的错误，详情见诊断日志：%s" % _logs_dir())
        raise
    return 0


def _free_port():
    for port in range(START_PORT, START_PORT + PORT_TRIES):
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return None


def _logs_dir():
    from saltlrc import diagnostics
    return diagnostics.logs_dir()


def run_window(port, models, device, open_path=None):
    """Start the packaged app in its own window; report failures loudly.

    A windowed build that raises before the window exists looks identical to a
    crash on double-click, and the HTTP fallback is useless if the failure is
    in the window layer itself (a missing WebView2 runtime, say).
    """
    from saltlrc import diagnostics
    from saltlrc import window as window_module

    if port is None:
        port = window_module.free_port()
    if port is None:
        alert("SaltLRC Studio 无法启动",
              "找不到可用端口（%d-%d 都被占用）。\n\n%s" %
              (START_PORT, START_PORT + PORT_TRIES - 1, diagnostics.logs_dir()))
        return 1
    try:
        return window_module.run_window(port=port, models=models, device=device,
                                        open_path=open_path)
    except RuntimeError as error:
        # Dependency missing: fall through to the browser rather than failing.
        diagnostics.DIAGNOSTICS.write("独立窗口启动失败，回退到浏览器：%s" % error)
        return _serve_in_browser(port, models, device, open_browser=True,
                                 open_path=open_path)
    except Exception:
        diagnostics.capture_error("独立窗口启动失败")
        alert("SaltLRC Studio 启动失败",
              "窗口无法打开，详情见诊断日志：%s" % diagnostics.logs_dir())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
