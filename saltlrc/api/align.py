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
from .. import fit
from ..alignment import apply_proposals, match_transcript, source_track
from ..acoustic import apply_words, global_offset, refine_words
from ..fit import fit_lines
from ..jobs import Jobs


def handle(self, path, data):
    if self.path == "/api/align/fit":
        # 智能贴唱：纯本机能量分析 + 一条单调路径，**一次给出整首行级提案**。
        # 它取代了旧的"分析当前音频 + 整首行首对齐"两个按钮（同一件事的两种做法），
        # 而且不落盘：返回的是一份**贴好的文档**，由页面决定预览还是应用。
        document = Document.from_dict(data["document"])
        field = data.get("field", "auto")
        field = source_track(document) if field == "auto" else field
        self.send(200, fit_lines(document, base64.b64decode(data["audio"], validate=True),
                                 field, int(data.get("max_shift", fit.MAX_SHIFT_MS))))
        return True
    if self.path == "/api/align/start":
        self.send(202, self.jobs().start(data, base64.b64decode(data["audio"], validate=True)))
        return True
    if self.path == "/api/align/offset":
        document = Document.from_dict(data["document"])
        field = data.get("field", "auto")
        field = source_track(document) if field == "auto" else field
        result = global_offset(document, base64.b64decode(data["audio"], validate=True),
                               field, int(data.get("radius", 4000)))
        self.send(200, result)
        return True
    if self.path == "/api/align/words":
        document = Document.from_dict(data["document"])
        field = data.get("field", "auto")
        field = source_track(document) if field == "auto" else field
        self.send(200, refine_words(document, base64.b64decode(data["audio"], validate=True), field))
        return True
    if self.path == "/api/align/words/apply":
        document = Document.from_dict(data["document"])
        updated = apply_words(document, data["result"]["proposals"], data["indices"])
        self.send(200, {"document": updated.to_dict(), "report": validate(updated)})
        return True
    if self.path == "/api/align/status":
        self.send(200, self.jobs().status(data["id"]))
        return True
    if self.path == "/api/align/cancel":
        self.send(200, self.jobs().cancel(data["id"]))
        return True

    return False
