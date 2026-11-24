import base64
import io
import json
import os
import platform
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .document import Document
from . import edits
from .parser import parse
from .repair import decode
from .validator import validate
from .exporter import export
from .repair import to_bytes
from .bilingual import shift
from .language import identify
from .alignment import apply_proposals, match_transcript, source_track
from .acoustic import apply_words, global_offset, refine_words
from .jobs import Jobs
from . import diagnostics
from . import folders
from . import library, providers, wordtime, inspect
from .sources import netease
from . import credentials, login
from . import trace
from . import api
from .api._render import _fmt, _extension, _encoding, _render, _render_or_fallback
from . import settings as settings_store
from . import translator
from . import audioformats

WEB = Path(__file__).parent / "web"


STATIC = {
    # "/" is the editor: opening the app must land straight in the workbench.
    # The library page stays available at /library (and from the editor's
    # 「从曲库导入」 dialog, which is the normal way in).
    "/": "index.html",
    "/studio.html": "index.html",
    "/library": "library.html",
    "/library.js": "library.js",
    "/common.js": "common.js",
    # 应用内提示的唯一出口（失败弹框 / 后台失败横幅），每个页面都加载
    "/notify.js": "notify.js",
    "/history.js": "history.js",
    "/projects.js": "projects.js",
    "/document.js": "document.js",
    "/studio.js": "studio.js",
    "/debug-panel.js": "debug-panel.js",
    # 操作追踪：内置的诊断通道，每个页面都加载
    "/trace.js": "trace.js",
    # 主题：写 data-theme 并按主题读 CSS 令牌，每个页面都加载
    "/theme.js": "theme.js",
    "/splitter.js": "splitter.js",
    "/debug.html": "debug.html",
    "/waveform.js": "waveform.js",
    "/audio-tools.js": "audio-tools.js",
    "/style.css": "style.css",
    "/favicon.png": "favicon.png",
}

# Content type per extension. Kept as a module constant next to STATIC so adding
# a file to the map and forgetting its type is one edit away from being noticed —
# an unknown extension otherwise raises KeyError, which reaches the browser as a
# 500 rather than a missing-file message.
CONTENT_TYPES = {
    "html": "text/html",
    "js": "text/javascript",
    "css": "text/css",
    "png": "image/png",
    "ico": "image/x-icon",
    "svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler defaults to HTTP/1.0, which closes the connection
    # after every response. jsdom's resource loader can then report
    # "Could not load script" for a file that did arrive and run, and a real
    # browser has to open a fresh TCP connection for each of the editor's seven
    # scripts plus every audio range request. HTTP/1.1 + keep-alive fixes both.
    protocol_version = "HTTP/1.1"

    def jobs(self):
        if not hasattr(self.server, "jobs"):
            self.server.jobs = Jobs()
        return self.server.jobs

    def log_message(self, *args):
        pass

    def send(self, status, data, kind="application/json; charset=utf-8", headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        # Every response funnels through here, so this is where the user's last
        # actions get journalled for the debug page.
        if getattr(self, "path", "").startswith("/api/"):
            try:
                diagnostics.DIAGNOSTICS.request(self.command, self.path.split("?")[0], status,
                                                time.time() - self._started)
            except Exception:
                pass
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        # 附加头走这一个入口，别在别处自己拼响应 —— 那样就绕过了上面的日志。
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path, content_type):
        """Serve a local file with byte-range support so <audio> can seek."""
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        partial = False
        if range_header and range_header.startswith("bytes="):
            first, _, last = range_header[6:].partition("-")
            try:
                start = int(first)
                if last:
                    end = min(int(last), size - 1)
                end = min(end, size - 1)
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                # keep-alive (HTTP/1.1) needs a body with a length on every
                # response, including this one; without it the client waits
                # forever for the end of a zero-length body.
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        quoted = urllib.parse.quote(path.name)
        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{quoted}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def allowed(self):
        expected = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host") == expected and self.headers.get("Origin", f"http://{expected}") == f"http://{expected}"

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        self._started = time.time()
        try:
            self._handle_get()
        except Exception as error:
            # A windowed build has no console to print to, so the journal is
            # the only place a crash becomes visible.
            diagnostics.capture_error(error)
            try:
                self.send(500, {"error": "服务器内部错误：" + str(error)})
            except Exception:
                pass

    def _handle_get(self):
        if not self.allowed():
            return self.send(403, {"error": "仅允许本机同源请求"})
        route = urlparse(self.path)
        query = parse_qs(route.query)
        if route.path == "/api/capabilities":
            from . import __version__
            capabilities = self.jobs().capabilities()
            capabilities["providers"] = providers.names()
            # The full catalogue drives the source dropdown; hardcoding the list
            # in the page would make adding a source a change in three places
            # instead of one.
            capabilities["provider_catalogue"] = providers.catalogue()
            # 音频格式清单也下发：页面不再自己写死一份正则 —— 那是另一份
            # "什么算音频"的定义，容易少几种（导入文件夹时会漏掉）。
            capabilities["audio_extensions"] = sorted(audioformats.extensions())
            capabilities["audio_unknown"] = sorted(audioformats.unknown())
            # 连"怎么办"那句话也一起下发：页面上只显示，不自己编（一处权威）。
            capabilities["audio_notes"] = {ext: audioformats.unsupported_note("x" + ext)
                                           for ext in sorted(audioformats.unknown())}
            capabilities["version"] = __version__
            return self.send(200, capabilities)
        if route.path == "/api/credentials":
            # Never returns a cookie: only whether one is stored and a redacted
            # label. The status call must be safe to render in the UI.
            return self.send(200, credentials.status())
        if route.path == "/api/pending-open":
            return self._serve_pending_open()
        if route.path == "/api/audio":
            try:
                data = library.read_library()
            except (RuntimeError, OSError, sqlite3.Error) as error:
                return self.send(400, {"error": str(error)})
            wanted = query.get("track_id", [""])[0]
            track = next((t for t in data["tracks"] if str(t["id"]) == wanted), None)
            if not track:
                return self.send(404, {"error": "曲库里没有这首歌"})
            path = Path(library.local_path(track["path"]))
            if not path.is_file():
                return self.send(404, {"error": "音频文件不在本机或已被移动：" + path.name})
            # 「什么算音频 / 它的 MIME 是什么」只有一个权威（audioformats）——
            # 第二份表会让少的那几种退成 octet-stream，浏览器不认。
            kind = audioformats.mime_for(path)
            return self.send_file(path, kind)
        if route.path == "/api/library":
            try:
                data = library.read_library(query.get("db", [None])[0],
                                            query.get("missing", ["0"])[0] == "1")
            except (RuntimeError, OSError, sqlite3.Error) as error:
                return self.send(400, {"error": str(error)})
            keyword = (query.get("keyword", [""])[0] or "").strip().lower()
            if keyword:
                data["tracks"] = [t for t in data["tracks"]
                                  if keyword in (t["title"] + t["artist"] + t["album"]).lower()]
            return self.send(200, data)
        if route.path == "/api/debug/state":
            if not self._debug_allowed():
                if not diagnostics.debug_enabled():
                    return self.send(403, {"error": "调试接口未启用：设置环境变量 SALTLRC_DEBUG_PASSWORD 后重启程序"})
                return self.send(401, {"error": "需要调试密码"})
            from . import __version__
            state = diagnostics.DIAGNOSTICS.snapshot()
            state.update({"version": __version__, "python": sys.version.split()[0],
                          "frozen": bool(getattr(sys, "frozen", False)),
                          "port": self.server.server_port, "platform": platform.platform()})
            return self.send(200, state)
        if route.path == "/api/lyric":
            path = (query.get("path", [""])[0] or "").strip()
            if not path:
                return self.send(400, {"error": "缺少歌词路径"})
            # 这条接口唯一的用途是把曲库扫描到的 .lrc 读进来。不限后缀的话，
            # 同源页面（被注入的脚本也在这个源里）能把任意本地文件 —— 包括
            # 凭据与设置 —— 原样读走。
            if Path(path).suffix.lower() != ".lrc":
                return self.send(400, {"error": "只能读取 .lrc 歌词文件"})
            try:
                if Path(path).stat().st_size > 2_000_000:
                    return self.send(400, {"error": "歌词文件超过 2MB，不读取"})
            except OSError:
                return self.send(404, {"error": "该歌曲还没有歌词文件"})
            text = library.read_lyric(path)
            if text is None:
                return self.send(404, {"error": "该歌曲还没有歌词文件"})
            return self.send(200, {"path": path, "text": text})
        name = STATIC.get(route.path)
        if not name:
            return self.send(404, {"error": "Not found"})
        kind = CONTENT_TYPES.get(name.rsplit(".", 1)[1].lower(), "application/octet-stream")
        self.send(200, (WEB / name).read_bytes(), kind + "; charset=utf-8")

    def _serve_pending_open(self):
        """「启动时带着的那个工程」—— 双击 `.saltproj` / 命令行 `--open` 的落地处。

        **路径来自本进程自己的命令行**，页面永远不能提交路径（页面只问"给我启动时
        带的东西"）。所以这里没有"按页面给的路径读文件"这条路。

        **一次性**：读走之后立刻清掉。刷新页面不会重复打开 —— 刷新后队列本来就会
        从会话快照恢复，那才是"接着改"该走的路；再从磁盘灌一份旧内容回去，
        反而会把刚改的东西顶掉。
        """
        path = getattr(self.server, "pending_open", None)
        self.server.pending_open = None
        if not path:
            return self.send(204, b"")
        try:
            raw = Path(path).read_bytes()
        except OSError as error:
            return self.send(400, {"error": "打不开这个工程文件：%s（%s）" % (error, path)})
        diagnostics.DIAGNOSTICS.write("打开命令行指定的工程：%s" % path)
        # 文件名可能是中文，而 HTTP 头只能是 latin-1 → 百分号编码后再发。
        return self.send(200, raw, "application/octet-stream",
                         {"X-Project-Name": urllib.parse.quote(Path(path).name)})

    def _translate(self, data):
        """Fill AI translations into a document.

        The request carries the document (the editor owns it); the reply carries
        the document back with translations written in. The API key stays here:
        the browser asks for a translation, it never handles the credential.
        """
        options, problem = settings_store.translate_options()
        if options is None:
            return {"error": problem, "needs_setup": True}
        if data.get("probe"):
            # 设置 → 测试连接: translate something tiny so the answer reflects a
            # real round trip (auth, URL, model) rather than the stored shape.
            try:
                sample = translator.translate_line("Hello", options)
            except translator.TranslationError as error:
                return {"error": str(error)}
            return {"ok": True, "sample": sample}
        document = Document.from_dict(data["document"])
        try:
            report = translator.fill(document, options,
                                     index=data.get("index"),
                                     scope=str(data.get("scope") or "line"),
                                     only_missing=bool(data.get("only_missing")))
        except translator.TranslationError as error:
            return {"error": str(error)}
        return {"document": document.to_dict(), "report": report}

    def _debug_allowed(self):
        provided = self.headers.get("X-Debug-Token") or ""
        return provided == diagnostics.debug_token()

    def _finish_login(self, state, cookie):
        """Persist a successful login and report only its status.

        A confirmed login with no cookie is rejected rather than stored: saving
        it would look like a success in the UI while silently leaving the user
        anonymous, and the discrepancy would only surface much later as lyrics
        that mysteriously lack word timings.
        """
        if state != login.QR_CONFIRMED:
            return self.send(200, {"state": state})
        if not cookie:
            raise ValueError("登录已确认但没有拿到凭据，请重试")
        credentials.put(providers.DEFAULT_PROVIDER, cookie)
        status = credentials.status()
        status["state"] = login.QR_CONFIRMED
        return self.send(200, status)

    def _write_debug_bundle(self):
        """Zip the journal so a bug report can be attached and sent as-is."""
        state = diagnostics.DIAGNOSTICS.snapshot()
        from . import __version__
        environment = {
            "version": __version__, "python": sys.version, "platform": platform.platform(),
            "frozen": bool(getattr(sys, "frozen", False)), "port": self.server.server_port,
            "uptime": state["uptime"], "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        target = diagnostics.logs_dir() / ("saltlrc-diagnostics-%s.zip" % time.strftime("%Y%m%d-%H%M%S"))
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("environment.json", json.dumps(environment, ensure_ascii=False, indent=1))
            archive.writestr("log.txt", "\n".join(state["log"]))
            archive.writestr("requests.json", json.dumps(state["requests"], ensure_ascii=False, indent=1))
            archive.writestr("errors.json", json.dumps(state["errors"], ensure_ascii=False, indent=1))
        diagnostics.DIAGNOSTICS.write("已导出诊断包：" + str(target))
        return target

    @staticmethod
    def _shutdown_soon():
        time.sleep(0.4)
        os._exit(0)

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        self._started = time.time()
        if not self.allowed():
            return self.send(403, {"error": "仅允许本机同源请求"})
        path = self.path.split("?")[0]
        try:
            # Read the body first: the debug routes need it too, and reading it
            # here keeps the size limit applied to every single route.
            # With keep-alive the body MUST be consumed even when it is
            # rejected — otherwise the unread bytes are parsed as the next
            # request on the same connection and every later call desyncs.
            size = int(self.headers.get("Content-Length", "0"))
            # /api/save-as can carry a .saltproj with the audio embedded, which is
            # larger than the lyric payloads — the same request the user just
            # asked the app to make by ticking 内嵌音频.
            limits = {"/api/align/start": 64 * 1024 * 1024,
                      "/api/align/offset": 64 * 1024 * 1024,
                      "/api/align/words": 64 * 1024 * 1024,
                      "/api/save-as": 128 * 1024 * 1024,
                      # 工程容器里是**音频原始字节**（内嵌音频时）—— 一首 30MB 的无损歌
                      # 就是 30MB 的体，不是 base64 之后的 40MB。上限给宽一点。
                      "/api/save-binary": 256 * 1024 * 1024}
            limit = limits.get(path, 16 * 1024 * 1024)
            if size <= 0:
                # 空体单独说清楚：否则它会落进下面那条"超过大小限制"，
                # 而 0 字节既不空也不大 —— 那句话自相矛盾。
                raise ValueError("请求没有内容（Content-Length 为 0）")
            if size > limit:
                self.rfile.read(min(size, limit))
                raise ValueError("请求超过大小限制；音频分析最多 20 分钟")
            if path == "/api/save-binary":
                # **原始字节**的出口：工程文件 / 内嵌音频走这里。
                # 不走 JSON，省掉"编码成 base64 → 再解析回对象"这两趟搬运。
                # 参数（文件名、起始目录）都很短，走查询串即可。
                query = parse_qs(urlparse(self.path).query)
                self.send(200, self._save_binary(self.rfile.read(size), query))
                return
            data = json.loads(self.rfile.read(size))
            if api.dispatch(self, path, data):
                return
            self.send(404, {"error": "Not found"})
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, LookupError, OSError) as error:
            diagnostics.capture_error(error)
            self.send(400, {"error": str(error)})
        except netease.ProviderError as error:
            diagnostics.capture_error(error)
            self.send(502, {"error": str(error)})
        except Exception as error:
            diagnostics.capture_error(error)
            self.send(500, {"error": "服务器内部错误：" + str(error)})

    # ------------------------------------------------------------- helpers
    def _save(self, data):
        document = Document.from_dict(data["document"])
        target = Path(str(data.get("path") or ""))
        if target.suffix.lower() not in (".lrc", ".yrc"):
            raise ValueError("歌词文件必须以 .lrc 或 .yrc 结尾")
        if not target.is_absolute():
            raise ValueError("歌词路径必须是绝对路径")
        text, degraded = _render_or_fallback(document, data)
        try:
            status = library.write_lyric(str(target), text, overwrite=bool(data.get("overwrite")),
                                         backup=bool(data.get("backup", True)), encoding=_encoding(data))
        except PermissionError as error:
            # write_lyric writes through a .tmp sibling first; report the real name.
            name = str(error.filename or target)
            if name.endswith(".tmp"):
                name = name[:-4]
            raise ValueError(
                "没有写入权限：%s。系统目录（C 盘根目录、Windows、Program Files）需要管理员权限；"
                "请把歌曲移到普通文件夹，或改用页面上的「导出到其他文件夹」。"
                % name) from error
        if status == "written" and data.get("track_id"):
            cache = library.load_cache()
            cache[str(data["track_id"])] = {
                "provider": data.get("provider") or providers.DEFAULT_PROVIDER,
                "song_id": data.get("song_id"), "title": data.get("title", "")}
            library.save_cache(cache)
        result = {"status": status, "path": str(target), "bytes": len(text.encode("utf-8"))}
        if degraded:
            result["degraded"] = degraded
        return result

    def _export_folder(self, data):
        """Pick a folder natively and write there from the backend.

        The browser's File System Access API could create the file and then
        fail to write it (a 0-byte stub), so both steps happen server-side.
        """
        name = self._export_name(data)
        text = self._export_text(data)
        fallback = None
        try:
            folder = folders.pick_folder()
        except Exception as error:  # noqa: BLE001 - 任何异常都不能把用户的一批文件白导一次
            # 原生对话框用不了时退回一个一定可写的目录，**并且把原因说出来**
            # （页面会把这段文字显示出来）。这里只降级"文件写到哪"，
            # 不降级"有没有告诉用户"：访问冲突之类也要照样报出来，
            # 否则文件悄悄落在别处，只留一句英文异常。
            diagnostics.capture_error(error)
            folder = folders.export_dir()
            fallback = "本机文件夹选择不可用（%s），这批文件已改存到 %s" % (error, folder)
        if folder is None:
            return {"status": "cancelled"}
        result = self._write_out(Path(folder) / name, text, data)
        result["fallback"] = fallback
        return result

    def _save_as(self, data):
        """导出/保存的**唯一出口**：读 L0 文档 → 打包文本 → 选路径 → 写盘。

        这四步就是它的流程：
            读取已编辑的歌词 → 打包成 LRC → 用户选择保存路径 → 保存文件

        四步里有三步可能失败，**每一步都要能说出话来**：

        * **打包**（`_export_text`）：校验不过会抛带中文原因的 `ValueError`；
        * **选路径**：原生「另存为」。**对话框用不了不再等于这次导出作废** ——
          退回一个一定可写、而且用户找得到的目录（这首歌自己的目录，否则「文档」），
          并把**原因**和**落点**一起报给页面（`fallback` 字段）；
          只有用户自己点了「取消」才算 `cancelled`；
        * **写盘**（`_write_out`）：权限/占用会抛带中文原因的 `ValueError`。

        `dry_run` 只做"打包"这一步，把将写出的文本和默认落点返回，**不开任何窗口** ——
        本机不许弹窗，所以这是**唯一能在交付的 exe 上验证导出内容**的口子。
        """
        if data.get("bytes_base64") is not None:
            # 二进制（批量导出 ZIP）。也走这一个出口 —— 页面那层没有下载界面，
            # blob 链接不会落盘。
            suffix = str(data.get("suffix") or "")
            if suffix != ".zip":
                raise ValueError("不支持的文件类型：" + (suffix or "(空)"))
            name = self._safe_name(data, suffix)
            target = self._choose_target(name, data, ("歌词文件", "*.lrc;*.yrc"))
            if target is None:
                return {"status": "cancelled"}
            path, note = target
            result = self._write_blob(Path(path),
                                      base64.b64decode(data["bytes_base64"], validate=True))
            result["name"] = Path(path).name
            if note:
                result["fallback"] = note
            return result
        # 「原文文本」这一支的扩展名：默认 .saltproj（保存工程），白名单里也允许 .json
        # （保存分析结果）。**扩展名仍由服务端决定** —— 调用方只是从白名单里挑一个，
        # 免得"另存为"变成"随便写什么后缀"。
        suffix = str(data.get("suffix") or ".saltproj")
        if suffix not in (".saltproj", ".json"):
            raise ValueError("不支持的文件类型：" + suffix)
        ready_made = data.get("text") is not None
        name = self._safe_name(data, suffix) if ready_made else self._export_name(data)
        degraded = None
        if ready_made:
            text = str(data["text"])
        else:
            # ② 打包：**导不出来时降级成标准双语 LRC，而不是拒了**（用户要的是文件）
            text, degraded = _render_or_fallback(Document.from_dict(data["document"]), data)

        if data.get("dry_run"):
            # 只打包、不选路径、不写盘 —— 交付的 exe 上也能验（不开窗口）
            result = {"status": "dry-run", "name": name, "text": text,
                      "bytes": len(text.encode("utf-8")),
                      "lines": len(text.rstrip("\n").splitlines()),
                      "directory": folders.fallback_dir(data.get("preferred"))}
            if degraded:
                result["degraded"] = degraded
            return result

        target = self._choose_target(name, data, self._save_filters(name))
        if target is None:
            return {"status": "cancelled"}
        path, note = target
        result = self._write_out(Path(path), text, data)
        result["name"] = Path(path).name
        result["lines"] = len(text.rstrip("\n").splitlines())
        if note:
            result["fallback"] = note
        if degraded:
            result["degraded"] = degraded
        return result

    def _save_binary(self, raw, query):
        """把**原始字节**存到用户选的位置（工程文件 / 内嵌音频的容器）。

        与 `/api/save-as` 的区别只有一处：那边把请求体当 JSON 解析，于是 30MB 音频
        要先 base64 成 40MB 文本、再由服务端解析回对象 —— 同一份数据搬三趟
        （浏览器编码、JSON 解析、落盘）。走这里只搬一趟：字节进来，字节落地。

        参数走查询串（都很短）：`name`（文件名，用来选扩展名与对话框默认名）、
        `preferred`（这首歌自己的文件夹，没有就让服务端退回「文档」）。
        """
        if not raw:
            raise ValueError("请求体是空的（没有内容可写）")
        # 只取**文件名**（basename）：调用方给什么路径都没用，写哪儿由用户在
        # 「另存为」对话框里定 —— 与 `_safe_name` 同一条边界，只是这里没有 JSON 体。
        name = Path(str((query.get("name") or [""])[0])).name
        if not name:
            raise ValueError("缺少文件名")
        suffix = Path(name).suffix.lower()
        if suffix not in (".saltproj", ".lrc", ".yrc"):
            raise ValueError("不支持的文件类型：" + (suffix or "(空)"))
        preferred = (query.get("preferred") or [None])[0]
        target = self._choose_target(name, {"preferred": preferred}, self._save_filters(name))
        if target is None:
            return {"status": "cancelled"}
        path, note = target
        result = self._write_blob(Path(path), raw)
        result["name"] = Path(path).name
        if note:
            result["fallback"] = note
        return result

    def _choose_target(self, name, data, filters):
        """选路径这一步：原生「另存为」→（失败就兜底）→ 返回 (路径, 说明)。

        **"对话框弹不出来"必须与"用户点了取消"分开**：
          * 用户取消 → 返回 None（页面说"已取消导出"，这是正常操作）；
          * 对话框失败/超时 → 写进日志 + L1 追踪，然后退回一个一定可写的目录。
        两者混在一起就是"点了没反应"。
        """
        try:
            picked = folders.save_file(name, title="SaltLRC Studio：另存为",
                                       filters=filters, preferred=data.get("preferred"))
        except Exception as error:  # noqa: BLE001 - 兜底只能降级"写到哪"，不能降级"说不说"
            diagnostics.capture_error(error)
            # 写进 L1 追踪（`error` 是"可观察结果"，所以它也算这一次手势有了结果）
            trace.record("error", "另存为对话框不可用", "%s" % error)
            folder = folders.fallback_dir(data.get("preferred"))
            note = ("系统「另存为」对话框用不了（%s），已改存到 %s —— "
                    "可以点「打开所在文件夹」直接看" % (error, folder))
            return str(Path(folder) / name), note
        if not picked:
            return None
        return picked, None

    @staticmethod
    def _write_blob(target, blob):
        """Write a binary file (the batch ZIP), reporting trouble in Chinese."""
        try:
            with open(target, "wb") as handle:
                handle.write(blob)
        except PermissionError as error:
            raise ValueError(
                "没有写入权限：%s。请换一个普通文件夹（如「文档 / 下载」），"
                "系统目录需要管理员权限。" % (error.filename or target)) from error
        except OSError as error:
            raise ValueError("写不进去：%s（%s）" % (target, error)) from error
        return {"status": "written", "path": str(target), "bytes": len(blob)}

    @staticmethod
    def _safe_name(data, suffix):
        name = Path(str(data.get("name") or "")).name
        if not name:
            raise ValueError("缺少文件名")
        if Path(name).suffix.lower() != suffix:
            name += suffix
        return name

    @staticmethod
    def _save_filters(name):
        if Path(name).suffix.lower() == ".saltproj":
            return (("SaltLRC 工程", "*.saltproj"), ("所有文件", "*.*"))
        if Path(name).suffix.lower() == ".json":
            return (("分析结果 JSON", "*.json"), ("所有文件", "*.*"))
        return (("歌词文件", "*.lrc;*.yrc"), ("所有文件", "*.*"))

    def _export_name(self, data):
        """The name to write: the client's stem + the format's own extension.

        The extension comes from ``_extension(data)`` — the same function the
        renderer uses — so a file can never be named ``.lrc`` while holding YRC
        JSON. Clients send the stem only; the format decides the rest.
        """
        stem = Path(str(data.get("name") or "")).stem.strip()
        if not stem:
            raise ValueError("缺少文件名")
        return stem + _extension(data)

    def _export_text(self, data):
        return _render(Document.from_dict(data["document"]), data)

    def _write_out(self, target, text, data):
        """Write one file, reporting permission trouble in plain Chinese."""
        try:
            status = library.write_lyric(str(target), text, overwrite=True,
                                         backup=False, encoding=_encoding(data))
        except PermissionError as error:
            raise ValueError(
                "没有写入权限：%s。请换一个普通文件夹（如「文档 / 下载」），"
                "系统目录需要管理员权限。" % (error.filename or target)) from error
        return {"status": status, "path": str(target),
                "bytes": len(text.encode("utf-8"))}

    def _batch(self, data):
        tracks = data.get("tracks") or []
        if not tracks:
            raise ValueError("没有选择任何歌曲")
        if len(tracks) > 60:
            raise ValueError("一次最多处理 60 首，请分批执行")
        cookie = data.get("cookie") or None
        provider = data.get("provider")
        overwrite = bool(data.get("overwrite"))
        auto = bool(data.get("auto", True))
        results = []
        for track in tracks:
            entry = {"track_id": track.get("id"), "title": track.get("title"), "artist": track.get("artist"),
                     "path": track.get("lyric_path"), "status": "failed", "detail": ""}
            try:
                keyword = track.get("query") or " ".join(x for x in (track.get("title"), track.get("artist")) if x)
                candidates = providers.search(provider, keyword, 10, cookie)
                ranked = sorted(((providers.score(c, track), c) for c in candidates),
                                key=lambda item: -item[0])
                if not ranked:
                    entry["detail"] = "没有搜索结果"
                elif auto and ranked[0][0] < 45:
                    entry["status"] = "needs_review"
                    entry["detail"] = "最佳匹配评分 %.0f，低于阈值，需人工确认" % ranked[0][0]
                    entry["candidates"] = candidates[:5]
                else:
                    # Several uploads of the same song exist; some carry no lyric
                    # at all. Walk the confident ones instead of giving up.
                    tried = []
                    for value, candidate in ranked[:3]:
                        if value < 45:
                            break
                        payload = providers.lyric(provider, candidate["id"], cookie)
                        if payload["main"]:
                            document = wordtime.build(payload["main"], payload["translation"],
                                                      track.get("duration"), data.get("options") or {},
                                                      word_level=payload["word_level"])
                            report = validate(document, duration=track.get("duration"))
                            if report["errors"]:
                                entry["detail"] = report["errors"][0]
                                break
                            text = _render(document, data)
                            status = library.write_lyric(str(Path(track["lyric_path"]).with_suffix(_extension(data))),
                                                         text, overwrite=overwrite, encoding=_encoding(data))
                            entry["status"] = status
                            entry["detail"] = "匹配「%s / %s」评分 %.0f" % (
                                candidate["title"], candidate["artist"], value)
                            entry["song_id"] = candidate["id"]
                            if status == "written" and track.get("id"):
                                cache = library.load_cache()
                                cache[str(track["id"])] = {
                                    "provider": provider or providers.DEFAULT_PROVIDER,
                                    "song_id": candidate["id"], "title": candidate["title"]}
                                library.save_cache(cache)
                            break
                        tried.append(candidate["title"])
                    if not entry["detail"]:
                        entry["detail"] = ("网易云上这些匹配版本都没有歌词（%s），可手动添加"
                                           % "、".join(tried[:3]))
            except netease.ProviderError as error:
                entry["detail"] = str(error)
            except (ValueError, TypeError, KeyError, OSError) as error:
                entry["detail"] = str(error)
            results.append(entry)
        summary = {key: sum(1 for r in results if r["status"] == key)
                   for key in ("written", "skipped", "needs_review", "failed")}
        return {"results": results, "summary": summary}


def serve(port=8765, open_browser=True, models=None, device="cpu", open_path=None):
    import webbrowser
    from . import __version__, projects
    # "本次会话" = 这一次进程运行 —— 所以会话记录在这里清，且**只清记录不删文件**。
    # 放在这里而不是页面启动时：刷新一下页面（进程还活着）不该把已经建好的工程
    # 变成"无人认领"，那样退出时的"删除本次会话的工程"就会漏掉它们。
    projects.start_session()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    # The listen backlog defaults to 5. The editor page pulls seven scripts at
    # once and the audio element opens range requests in parallel, so a burst
    # of connections can exceed it and the extra ones are refused outright —
    # the page then loads with a "Could not load script" error and half the
    # buttons dead. Raise it well above any realistic burst.
    server.request_queue_size = 128
    server.jobs = Jobs(models, device)
    # 双击 .saltproj（关联写的是 `--open "%1"`）时带着的工程路径。
    # 它属于**这个进程**，不属于页面 —— 页面只能问"启动时带了什么"，
    # 不能自己指一个路径让服务端去读（见 Handler._serve_pending_open）。
    server.pending_open = open_path
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"SaltLRC Studio v{__version__}: {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.jobs.close()
        server.server_close()
