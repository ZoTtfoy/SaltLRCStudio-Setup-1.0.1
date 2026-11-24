"""Read the Salt Player for Windows (SPW) music library.

SPW keeps its library in a Room/SQLite database under %APPDATA%. The database
runs in WAL mode, so we copy the main file together with its ``-wal``/``-shm``
sidecars before opening it: that reads a consistent snapshot without locking
the player out of its own file.
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path
from . import audioformats

# 音频扩展名只有一处权威（saltlrc/audioformats.py）。
AUDIO_EXTENSIONS = audioformats.extensions()

DATABASE_NAME = "spw.db"
APPDATA_SUBDIR = os.path.join("Salt Player for Windows", DATABASE_NAME)


def default_db_path():
    """Locate spw.db in the usual per-user locations."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    candidates = []
    if base:
        candidates.append(os.path.join(base, APPDATA_SUBDIR))
    home = Path.home()
    candidates.append(str(home / "AppData" / "Roaming" / "Salt Player for Windows" / DATABASE_NAME))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0] if candidates else ""


def _to_local_path(value):
    if not value:
        return ""
    if value.startswith("file://"):
        rest = value[len("file://"):]
        rest = urllib.parse.unquote(rest)
        rest = rest.lstrip("/")
        if re.match(r"^[A-Za-z]:", rest):
            return rest.replace("/", "\\")
        return "/" + rest
    return value


def local_path(value):
    """Public wrapper: turn a library file:/// URI into a real path."""
    return _to_local_path(value)


def _snapshot(db_path):
    handle = tempfile.mkdtemp(prefix="saltlrc-db-")
    target = os.path.join(handle, DATABASE_NAME)
    try:
        shutil.copyfile(db_path, target)
    except OSError as error:
        shutil.rmtree(handle, ignore_errors=True)
        raise RuntimeError("无法读取曲库数据库：%s" % error) from error
    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.isfile(side):
            try:
                shutil.copyfile(side, target + suffix)
            except OSError:
                pass
    return handle, target


def read_library(db_path=None, only_missing_lyrics=False):
    """Return the track list known to Salt Player."""
    path = db_path or default_db_path()
    if not path or not os.path.isfile(path):
        raise RuntimeError("未找到 Salt Player 曲库数据库：%s" % (path or "(未配置)"))
    handle, snapshot = _snapshot(path)
    tracks = []
    try:
        connection = sqlite3.connect(snapshot)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, title, artist, album, path, duration, readable FROM Track"
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            audio = _to_local_path(row["path"] or "")
            if not audio:
                continue
            lyric = lyric_path_for(audio)
            has_lyric = os.path.isfile(lyric)
            if only_missing_lyrics and has_lyric:
                continue
            tracks.append({
                "id": row["id"],
                "title": row["title"] or "",
                "artist": row["artist"] or "",
                "album": row["album"] or "",
                "path": audio,
                "lyric_path": lyric,
                "has_lyric": has_lyric,
                "duration": int(row["duration"] or 0),
                "readable": bool(row["readable"]),
            })
    finally:
        shutil.rmtree(handle, ignore_errors=True)
    tracks.sort(key=lambda item: (item["artist"], item["album"], item["title"]))
    return {"db_path": path, "tracks": tracks}


def lyric_path_for(audio_path):
    """Salt Player matches a lyric file that has the same name as the audio."""
    return str(Path(audio_path).with_suffix(".lrc"))


def read_lyric(path):
    if not path or not os.path.isfile(path):
        return None
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def write_lyric(path, text, overwrite=False, backup=True, encoding="utf-8"):
    """Write a lyric file next to the audio. Returns a status string."""
    from .repair import to_bytes
    target = Path(path)
    if not target.parent.is_dir():
        raise ValueError("歌曲目录不存在：%s（歌曲文件可能已被移动，请在 Salt Player 里重新扫描曲库）" % target.parent)
    if target.exists():
        if not overwrite:
            return "skipped"
        if backup:
            try:
                target.replace(Path(str(target) + ".bak"))
            except OSError:
                pass
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(to_bytes(text, encoding))
    temporary.replace(target)
    return "written"


def cache_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = Path(base) / "SaltLRCStudio"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "matches.json"


def load_cache():
    try:
        return json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data):
    try:
        cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        return False
    return True
