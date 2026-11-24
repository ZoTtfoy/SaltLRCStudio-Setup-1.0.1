"""Native desktop window for SaltLRC Studio (WebView2 host).

The editor is a web UI, but it does not have to *live in a browser*. This
module runs the existing local server on a background thread and points a
WebView2 window straight at it, so the packaged build opens as one ordinary
desktop application: no address bar, no tabs, no "which browser do you have"
question.

The HTTP server provides what the webview uses:

* ``/api/audio`` streams the track straight out of the Salt Player library
  with byte-range support, which is what lets ``<audio>`` seek instantly
  without loading the whole file.
* ``/api/open-folder`` shells out to ``explorer`` to reveal a file; the
  webview cannot call ``explorer`` directly.
* The jsdom page checks, the end-to-end insert test and the exe smoke test
  all drive the app over HTTP, so the transport is part of the test
  surface.

The port is bound on 127.0.0.1 only, so nothing is exposed to the network.
"""

import socket
import sys
import threading
import time

WINDOW_TITLE = "SaltLRC Studio"


class _ServerThread(threading.Thread):
    """Runs ``serve()`` with its browser-opening side effect switched off.

    ``serve()`` blocks in ``serve_forever()`` and installs a SIGINT handler
    path; running it on a daemon thread lets the webview own the main thread,
    which is required on Windows for both the message loop and COM.
    """

    def __init__(self, server, port):
        super().__init__(daemon=True, name="saltlrc-http")
        self.server = server
        self.port = port
        self.error = None
        self.ready = threading.Event()

    def run(self):
        try:
            self.server.serve_forever()
        except Exception as error:      # pragma: no cover - fatal path only
            self.error = error
        finally:
            self.ready.set()

    def stop(self):
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


def free_port(start=8765, tries=25):
    """First port free on the loopback interface.

    Binding is the only reliable probe on Windows: a port held by another
    process can still accept a connection attempt in some states, so checking
    "can I connect" is not the same question as "can I listen".
    """
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


def build_server(port, models=None, device="cpu", open_path=None):
    """The same ThreadingHTTPServer ``serve()`` builds, without serve_forever."""
    from .server import Handler, Jobs, ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # See the note in server.serve(): the default backlog of 5 is smaller than
    # one editor page load, and the overflow is refused outright.
    server.request_queue_size = 128
    server.jobs = Jobs(models, device)
    # Set here too (not only in `serve()`): the windowed launch never calls
    # `serve()`, so a `--open` argument would otherwise be dropped on the floor
    # in exactly the path the user reaches by double-clicking a project file.
    server.pending_open = open_path
    return server


def run_window(port=None, models=None, device="cpu", title=WINDOW_TITLE,
               width=1440, height=920, debug=False, open_path=None):
    """Open the editor in its own window. Returns a process exit code.

    Import of ``webview`` is deferred to here: the headless paths (``--worker``,
    the smoke test, ``saltlrc serve``) never pay for pythonnet.
    """
    try:
        import webview
    except ImportError as error:
        raise RuntimeError(
            "缺少独立窗口所需的依赖（pywebview / pythonnet）。\n"
            "请运行：pip install pywebview\n"
            "原始错误：%s" % error
        )

    chosen = port or free_port()
    if chosen is None:
        raise RuntimeError("找不到可用端口（8765-8789 都被占用）")

    server = build_server(chosen, models, device, open_path=open_path)
    url = "http://127.0.0.1:%d" % chosen
    thread = _ServerThread(server, chosen)
    thread.start()

    from . import diagnostics
    diagnostics.DIAGNOSTICS.write("独立窗口启动：%s" % url)

    window = webview.create_window(
        title,
        url,
        width=width,
        height=height,
        min_size=(960, 640),
        # The editor is a full application layout: a browser-style zoom or a
        # stray text selection drag would both fight the waveform canvas.
        text_select=False,
        confirm_close=False,
    )

    try:
        # ``gui='edgechromium'`` is explicit on purpose: pywebview would also
        # accept the legacy MSHTML engine on Windows, and the editor needs a
        # modern canvas + Web Audio implementation. WebView2 ships with
        # Windows 10/11, so this is the one that is actually present.
        webview.start(gui="edgechromium", debug=debug, private_mode=False)
    except Exception:
        # A failed start would otherwise leave the server holding the port with
        # no window to talk to it, and the next launch would silently move to
        # another port.
        thread.stop()
        raise
    finally:
        thread.stop()
        try:
            server.jobs.close()
        except Exception:
            pass
    return 0


def available():
    """True when a native window can be opened in this interpreter."""
    if sys.platform != "win32":
        return False
    try:
        import webview  # noqa: F401
    except ImportError:
        return False
    return True


def wait_for_server(url, timeout=20.0, interval=0.05):
    """Test helper: block until the loopback server answers."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/capabilities", timeout=2) as reply:
                if reply.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(interval)
    return False
