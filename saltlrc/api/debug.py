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
from pathlib import Path

from .. import (diagnostics, folders, library, inspect, providers, wordtime,
                trace, translator, login, credentials, edits)
from .. import settings as settings_store
from ..sources import netease
from ..document import Document
from ..parser import parse
from ..repair import decode, to_bytes
from ..validator import validate
from ..exporter import export
from ..bilingual import shift as shift_track
from ..language import identify
from ..alignment import apply_proposals, match_transcript, source_track
from ..acoustic import apply_words, global_offset, refine_words
from ..jobs import Jobs


def handle(self, path, data):
    if path == "/api/debug/auth":
        if not diagnostics.debug_enabled():
            self.send(403, {"error": "调试接口未启用：设置环境变量 SALTLRC_DEBUG_PASSWORD 后重启程序"})
            return True
        if diagnostics.check_password(data.get("password")):
            self.send(200, {"token": diagnostics.debug_token()})
            return True
        diagnostics.DIAGNOSTICS.write("调试密码错误")
        self.send(403, {"error": "密码不正确"})
        return True
    if path == "/api/debug/client-error":
        # Browser-side failures are invisible without a console, so the
        # pages forward them here for the debug view. Deliberately not
        # gated: the main pages have no token, and it is loopback-only.
        diagnostics.DIAGNOSTICS.error("[前端] " + str(data.get("message", "未知错误"))[:200],
                                     str(data.get("stack") or data.get("source") or ""))
        self.send(200, {"status": "recorded"})
        return True
    if not self._debug_allowed() and path.startswith("/api/debug/"):
        if not diagnostics.debug_enabled():
            self.send(403, {"error": "调试接口未启用：设置环境变量 SALTLRC_DEBUG_PASSWORD 后重启程序"})
        else:
            self.send(401, {"error": "需要调试密码"})
        return True
    if path == "/api/debug/bundle":
        target = self._write_debug_bundle()
        self.send(200, {"path": str(target), "bytes": target.stat().st_size})
        return True
    if path == "/api/debug/open-folder":
        folder = str(diagnostics.logs_dir())
        subprocess.Popen(["explorer", folder])
        self.send(200, {"status": "opened", "path": folder})
        return True
    if path == "/api/exit":
        diagnostics.DIAGNOSTICS.write("收到退出请求，程序即将结束")
        threading.Thread(target=self._shutdown_soon, daemon=True).start()
        self.send(200, {"status": "exiting"})
        return True

    # ---- Netease login. Three ways in, all returning a cookie. ----
    #
    # Every route below replies with the *saved credential status*, never
    # the cookie itself: the browser has no use for the value (the server
    # attaches it on the way out) and shipping it to the page would put a
    # reusable session token in the DOM and in any screenshot.

    return False
