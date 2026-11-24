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
from ._render import _fmt, _extension, _encoding, _render


def handle(self, path, data):
    if self.path == "/api/translate":
        self.send(200, self._translate(data))
        return True
    if self.path == "/api/build":
        document = wordtime.build(data.get("main") or [], data.get("translation") or [],
                                  data.get("duration"), data.get("options") or {},
                                  word_level=bool(data.get("word_level")))
        report = validate(document, duration=data.get("duration"))
        text = "" if report["errors"] else _render(document, data)
        self.send(200, {"document": document.to_dict(), "report": report, "text": text})
        return True
    if self.path == "/api/save":
        self.send(200, self._save(data))
        return True
    if self.path == "/api/export-folder":
        self.send(200, self._export_folder(data))
        return True
    if self.path == "/api/save-as":
        # 唯一的"另存为"出口：歌词导出与工程保存都走它 ——
        # 页面的 download() 在打包窗口里没有下载界面，点了什么都不会落盘。
        result = self._save_as(data)
        if result["status"] == "cancelled":
            self.send(200, {"status": "cancelled"})
        else:
            self.send(200, result)
        return True
    if self.path == "/api/reveal":
        # 「打开所在文件夹」—— 导出之后用户要能立刻找到那个文件。
        # **只有用户点了才会走到这里**（它会开一个资源管理器窗口）。
        self.send(200, {"status": "revealed", "path": folders.reveal(data.get("path"))})
        return True
    if self.path == "/api/batch":
        self.send(200, self._batch(data))
        return True

    if self.path == "/api/edit":
        doc = Document.from_dict(data["document"])
        # Structural edits (add / remove / reorder lines) run through
        # saltlrc.edits so the same invariants hold as in the browser.
        op = str(data.get("op") or "")
        if op == "insert":
            edits.insert_group(doc, int(data.get("index", len(doc.groups))),
                               data.get("start"), data.get("duration"),
                               int(data.get("gap", edits.DEFAULT_GAP)),
                               str(data.get("text") or ""), str(data.get("translation") or ""))
        elif op == "delete":
            edits.delete_group(doc, int(data["index"]))
        elif op == "sort":
            edits.sort_groups(doc)
        else:
            raise ValueError("未知的编辑操作：" + (op or "(空)"))
        self.send(200, {"document": doc.to_dict(),
                               "report": validate(doc, data.get("salt", True), data.get("duration"))})
        return True
    if self.path == "/api/language":
        doc = Document.from_dict(data["document"])
        self.send(200, identify(doc))
        return True
    if self.path == "/api/align/match":
        doc = Document.from_dict(data["document"])
        self.send(200, match_transcript(doc, data["transcript"], data.get("field", "auto")))
        return True
    if self.path == "/api/align/apply":
        doc = Document.from_dict(data["document"])
        updated = apply_proposals(doc, data["result"], data["indices"], bool(data.get("link", True)))
        self.send(200, {"document": updated.to_dict(), "report": validate(updated)})
        return True
    if self.path == "/api/shift":
        doc = Document.from_dict(data["document"])
        doc = shift(doc, data["delta"])
        self.send(200, {"document": doc.to_dict(), "report": validate(doc)})
        return True
    if self.path == "/api/validate":
        doc = Document.from_dict(data["document"])
        self.send(200, validate(doc, data.get("salt", True), data.get("duration")))
        return True
    if self.path == "/api/export":
        doc = Document.from_dict(data["document"])
        self.send(200, {"text": _render(doc, data),
                               "format": _fmt(data),
                               "encoding": _encoding(data),
                               "extension": _extension(data)})
        return True

    return False
