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
    if self.path == "/api/parse":
        raw = base64.b64decode(data["bytes"], validate=True)
        if data.get("encoding"):
            text, note = raw.decode(data["encoding"]), data["encoding"]
        else:
            text, note = decode(raw)
        doc = parse(text)
        doc.notes.append(note)
        self.send(200, {"document": doc.to_dict(), "report": validate(doc)})
        return True
    if self.path == "/api/zip":
        result = io.BytesIO()
        used = set()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
            for entry in data["entries"]:
                name = entry["name"].replace("\\", "/")
                if name.startswith("/") or any(p in ("..", ".", "") for p in name.split("/")) or ":" in name:
                    raise ValueError("非法导出路径")
                target = str(Path(entry["name"]).with_suffix(_extension(entry))).replace("\\", "/")
                if target.startswith("/") or any(p in ("..", ".", "") for p in target.split("/")) or ":" in target:
                    raise ValueError("非法导出路径")
                if target.casefold() in used:
                    raise ValueError("导出文件名冲突：" + target)
                used.add(target.casefold())
                archive.writestr(target, to_bytes(_render(Document.from_dict(entry["document"]), entry),
                                                 _encoding(entry)))
        self.send(200, result.getvalue(), "application/zip")
        return True

    # ---- online / manual lyric workflow ----
    if self.path == "/api/search":
        keyword = (data.get("query") or "").strip()
        if not keyword:
            raise ValueError("请输入搜索关键词")
        provider = data.get("provider") or providers.DEFAULT_PROVIDER
        candidates = providers.search(provider, keyword,
                                      int(data.get("limit", 12)),
                                      providers.credential(provider, data.get("cookie")))
        self.send(200, {"candidates": candidates, "provider": provider,
                               "label": providers.label(provider)})
        return True
    if self.path == "/api/fetch":
        song_id = data.get("song_id")
        if not song_id:
            raise ValueError("缺少歌曲 ID")
        provider = data.get("provider") or providers.DEFAULT_PROVIDER
        payload = providers.lyric(provider, song_id,
                                  providers.credential(provider, data.get("cookie")),
                                  drop_credits=data.get("drop_credits", True))
        payload.setdefault("provider", provider)
        payload.setdefault("label", providers.label(provider))
        self.send(200, payload)
        return True
    if self.path == "/api/open-folder":
        target = Path(str(data.get("path") or ""))
        if not target.is_absolute():
            raise ValueError("路径必须是绝对路径")
        if not target.exists():
            raise ValueError("路径不存在：" + target.name)
        subprocess.Popen(["explorer", "/select,", str(target)])
        self.send(200, {"status": "opened", "path": str(target)})
        return True
    if self.path == "/api/audio/match":
        provider = data.get("provider") or providers.DEFAULT_PROVIDER
        self.send(200, inspect.match(data.get("name", ""), data.get("duration"),
                                            data.get("db"), bool(data.get("search", True)),
                                            provider, int(data.get("limit", 8)),
                                            providers.credential(provider,
                                                                 data.get("cookie"))))
        return True
    if self.path == "/api/manual":
        self.send(200, wordtime.parse_manual(data.get("main_text", ""),
                                                    data.get("translation_text", ""),
                                                    drop_credits=data.get("drop_credits", True)))
        return True
    if self.path == "/api/tokenize":
        # Display units for the word-timing editor (no timings). "smart"
        # applies the rule for the line's language; `lang` lets the editor
        # pass the track's language it already detected, which is better
        # evidence than one short line. The reply says which rule ran so
        # the UI never implies "this is Japanese" when it was a guess.
        units, info = wordtime.tokenize_info(str(data.get("text") or ""),
                                             str(data.get("mode") or "smart"),
                                             data.get("lang") or None)
        self.send(200, {"units": units, "info": info})
        return True

    return False
