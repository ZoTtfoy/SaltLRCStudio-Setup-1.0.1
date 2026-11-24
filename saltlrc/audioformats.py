"""音频格式的**唯一权威**：扩展名 → MIME，以及"应用内能不能解码"。

`FORMATS` 是唯一的清单：`library.py`、`batch.py`、`server.py` 与页面都读它，
页面侧从 `/api/capabilities` 拿（`audio_extensions`），不在 JS 里另写一份。

`decodable` 说的是"浏览器（WebView2 / Chromium）能不能 `decodeAudioData`"：
它决定本机能否分析音频、能否在应用内播放。**列出来 ≠ 能用**：
Monkey's Audio（`.ape`）与 WavPack（`.wv`）、AIFF 在 Chromium 里都没有解码器 ——
它们仍然算"音频文件"（扫描曲库时要算），但导入时由 `unsupported_note()` 给出
"这个格式应用内用不了、怎么办"的说明。
"""
from pathlib import Path

# 扩展名（小写，带点）→ (MIME, 应用内能否解码)
FORMATS = {
    ".mp3":  ("audio/mpeg",  True),
    ".m4a":  ("audio/mp4",   True),
    ".aac":  ("audio/aac",   True),
    ".flac": ("audio/flac",  True),
    ".wav":  ("audio/wav",   True),
    ".ogg":  ("audio/ogg",   True),
    ".opus": ("audio/ogg",   True),
    ".aiff": ("audio/aiff",  False),
    ".ape":  ("audio/x-ape", False),
    ".wv":   ("audio/x-wavpack", False),
}

# 遇到不认识的扩展名时，服务端按这个回
DEFAULT_TYPE = "application/octet-stream"


def extensions():
    """所有**算音频**的扩展名（扫描曲库、导入文件夹都用这一份）。"""
    return frozenset(FORMATS)


def decodable():
    """其中**应用内能解码**的那些（能分析、能播放）。"""
    return frozenset(ext for ext, (_, ok) in FORMATS.items() if ok)


def unknown():
    """算音频、但应用内解码不了的那些 —— 导入时要给出可读理由。"""
    return frozenset(ext for ext, (_, ok) in FORMATS.items() if not ok)


def mime_for(name, default=DEFAULT_TYPE):
    return FORMATS.get(suffix_of(name), (default, False))[0]


def suffix_of(name):
    return Path(str(name)).suffix.lower()


def is_audio(name):
    return suffix_of(name) in FORMATS


def can_decode(name):
    return FORMATS.get(suffix_of(name), (DEFAULT_TYPE, False))[1]


def unsupported_note(name):
    """解码不了时给用户的一句话（含"那我要怎么做"）；能用就给 ``None``。

    只在**不认识**或**认识但解不了**时返回内容 —— 认得的格式一律返回 None。
    """
    suffix = suffix_of(name)
    if not suffix:
        return None
    if suffix not in FORMATS:
        return None
    if FORMATS[suffix][1]:
        return None
    return ("%s 这个格式浏览器里没有解码器（应用内无法试听、也无法做音频分析）。"
            "把它转成 mp3 / flac / wav 就能全功能使用 —— 歌词编辑本身不受影响，"
            "导入歌词照样能编辑和导出。" % suffix)
