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
                trace, translator, login, credentials, edits, assoc)
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
    if self.path == "/api/trace":
        # The page reports what the user did and what it observed. Kept in
        # a core module (not in the browser) so the sequence survives a
        # reload, is analysable without a browser, and can be exported for
        # a bug report. Rebasing the page's relative clock onto ours lives
        # in trace.py (L1) — this route only forwards.
        events = data.get("events")
        if isinstance(events, list):
            trace.record_batch(events[:200])
        elif data.get("event"):
            trace.record_batch([data["event"]])
        self.send(200, trace.snapshot())
        return True
    if self.path == "/api/trace/clear":
        trace.clear()
        self.send(200, {"ok": True})
        return True
    if self.path == "/api/settings":
        # The API key is write-only from the page's point of view: the
        # reply says whether one is stored, never what it is.
        self.send(200, settings_store.public())
        return True
    if self.path == "/api/settings/save":
        # Reply with the MASKED view, never the stored record: returning
        # update()'s result would hand the API key straight back to the
        # page, which is the one thing this store exists to prevent.
        settings_store.update(data)
        self.send(200, settings_store.public())
        return True
    if self.path == "/api/assoc":
        # 「.saltproj 现在是谁的、图标指向哪里」——只读事实，用来在设置里说实话。
        self.send(200, assoc.status())
        return True
    if self.path == "/api/assoc/apply":
        # 源码运行时 install() 会返回 supported=False + 一句原因，而不是抛异常：
        # 由用户主动触发：失败时返回可读原因（不抛异常）。
        self.send(200, assoc.install())
        return True
    if self.path == "/api/assoc/remove":
        self.send(200, assoc.remove())
        return True

    return False
