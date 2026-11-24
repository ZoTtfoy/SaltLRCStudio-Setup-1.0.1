"""Kuwo Music lyric provider.

Two endpoints, each with its own format:

* **The search endpoint does not return JSON.** ``search.kuwo.cn/r.s`` answers
  with a JavaScript-object literal: unquoted keys, single-quoted strings, and
  bare values such as ``level:p``. :func:`parse_pseudo_json` scans the text
  character by character and rewrites a word as a key only when it is followed
  by a colon.
* **The lyric endpoint is HTTPS only.** Plain HTTP on ``m.kuwo.cn`` answers 301.
  The search host is the opposite case and is called over HTTP.

Durations arrive in seconds and the per-line times as seconds-as-strings; both
are converted at this boundary, so the rest of the app only sees milliseconds.
"""

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..lrctext import CREDIT
from ._http import TIMEOUT, ProviderError, request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

# HTTP: the search host does not answer on HTTPS.
SEARCH = "http://search.kuwo.cn/r.s"
# HTTPS: plain HTTP answers 301 here, and the redirect drops the musicId.
LYRIC = "https://m.kuwo.cn/newh5/singles/songinfoandlrc"

# "MUSIC_51685512" is how Kuwo itself labels a track id.
MUSIC_ID = re.compile(r"MUSIC_(\d+)")

# A pseudo-JSON key: a bare word immediately followed by a colon. The lookahead
# keeps the colon out of the match; "a:b" inside a string is left intact.
BARE_KEY = re.compile(r"([A-Za-z_$][\w$]*)(?=\s*:)")


class _Scanner:
    """Minimal reader that walks a JS-object literal.

    Only the constructs Kuwo actually emits are supported: objects, arrays,
    single- and double-quoted strings, bare words, numbers, and the three
    literals ``true`` / ``false`` / ``null``. Anything else raises.
    """

    def __init__(self, text):
        self.text = text
        self.at = 0

    def error(self, message):
        raise ValueError("%s（位置 %d）" % (message, self.at))

    def skip(self):
        while self.at < len(self.text) and self.text[self.at] in " \t\r\n":
            self.at += 1

    def parse(self):
        self.skip()
        value = self.value()
        self.skip()
        if self.at != len(self.text):
            self.error("末尾有多余内容")
        return value

    def value(self):
        self.skip()
        if self.at >= len(self.text):
            self.error("内容意外结束")
        char = self.text[self.at]
        if char == "{":
            return self.object()
        if char == "[":
            return self.array()
        if char in "'\"":
            return self.string(char)
        return self.word()

    def object(self):
        self.at += 1                     # consume "{"
        out = {}
        self.skip()
        if self.at < len(self.text) and self.text[self.at] == "}":
            self.at += 1
            return out
        while True:
            self.skip()
            if self.at >= len(self.text):
                self.error("对象没有闭合")
            if self.text[self.at] in "'\"":
                key = self.string(self.text[self.at])
            else:
                start = self.at
                while self.at < len(self.text) and self.text[self.at] not in ": \t\r\n":
                    self.at += 1
                key = self.text[start:self.at]
                if not key:
                    self.error("对象的键为空")
            self.skip()
            if self.at >= len(self.text) or self.text[self.at] != ":":
                self.error("键 " + repr(key) + " 后面缺少冒号")
            self.at += 1
            out[key] = self.value()
            self.skip()
            if self.at < len(self.text) and self.text[self.at] == ",":
                self.at += 1
                continue
            if self.at < len(self.text) and self.text[self.at] == "}":
                self.at += 1
                return out
            self.error("对象里缺少逗号或右花括号")

    def array(self):
        self.at += 1                     # consume "["
        out = []
        self.skip()
        if self.at < len(self.text) and self.text[self.at] == "]":
            self.at += 1
            return out
        while True:
            out.append(self.value())
            self.skip()
            if self.at < len(self.text) and self.text[self.at] == ",":
                self.at += 1
                continue
            if self.at < len(self.text) and self.text[self.at] == "]":
                self.at += 1
                return out
            self.error("数组里缺少逗号或右方括号")

    def string(self, quote):
        self.at += 1                     # consume the opening quote
        chunks = []
        while self.at < len(self.text):
            char = self.text[self.at]
            if char == "\\":
                self.at += 1
                if self.at >= len(self.text):
                    self.error("字符串以反斜杠结束")
                escape = self.text[self.at]
                mapping = {"n": "\n", "r": "\r", "t": "\t", "b": "\b",
                           "f": "\f", "/": "/", "\\": "\\", "'": "'", '"': '"'}
                if escape == "u":
                    digits = self.text[self.at + 1:self.at + 5]
                    if len(digits) != 4:
                        self.error("\\u 转义不完整")
                    chunks.append(chr(int(digits, 16)))
                    self.at += 5
                    continue
                # Kuwo writes escaped HTML entities in some fields.
                chunks.append(mapping.get(escape, escape))
                self.at += 1
                continue
            if char == quote:
                self.at += 1
                return "".join(chunks)
            chunks.append(char)
            self.at += 1
        self.error("字符串没有闭合")

    def word(self):
        start = self.at
        while self.at < len(self.text) and self.text[self.at] not in ",}] \t\r\n":
            self.at += 1
        token = self.text[start:self.at]
        if not token:
            self.error("遇到无法解析的内容")
        if token == "true":
            return True
        if token == "false":
            return False
        if token == "null":
            return None
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            pass
        return token                     # a bare word value, e.g. level:p


def parse_pseudo_json(text):
    """Parse Kuwo's JS-object-literal response into Python data.

    The server emits a JSON-escaped document whose *keys* are unquoted, so the
    text is handed to the scanner as-is rather than being pre-processed by a
    regex. A regex that quotes bare keys also rewrites value contents
    (``level:p,bitrate:192`` is one example).
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("酷我返回了空内容")
    return _Scanner(body).parse()




def _request(url, params, timeout=TIMEOUT):
    """The seam the tests replace. Delegates to the shared transport."""
    return request(url, params=params, referer="http://www.kuwo.cn/", accept="*/*",
                   timeout=timeout, label="酷我")


def _duration_ms(value):
    """Kuwo's DURATION is in seconds."""
    try:
        return int(round(float(str(value).split(".")[0] if "." not in str(value)
                               else float(value)) * 1000))
    except (TypeError, ValueError):
        return 0


def _seconds_text_to_ms(value):
    """``time`` arrives as a seconds string, e.g. ``"1.7"``."""
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _text(value):
    """Decode one Kuwo display field.

    Values arrive HTML-escaped (``&nbsp;``, and ``\\u0026`` for a literal
    ampersand inside artist names); both layers are undone here.
    """
    text = str(value if value is not None else "")
    # A JSON-style \\uXXXX escape that was double-escaped in the payload. A stray
    # backslash can remain in front of a following "&" (Kuwo writes
    # "\u0026五月天"); it is dropped too.
    text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    text = text.replace("\\&", "&")
    return html.unescape(text).replace("\xa0", " ").strip()


def search(keyword, limit=12, cookie=None):
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    count = max(1, min(int(limit), 30))
    text = _request(SEARCH, {
        "all": keyword, "ft": "music", "itemset": "web_2013", "client": "kt",
        "pn": 0, "rn": count, "rformat": "json", "encoding": "utf8",
    })
    try:
        payload = parse_pseudo_json(text)
    except ValueError as error:
        raise ProviderError("酷我搜索结果无法解析：%s" % error) from error
    if not isinstance(payload, dict):
        raise ProviderError("酷我返回了意外的搜索结果结构")
    rows = payload.get("abslist") or []
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_id = str(row.get("MUSICRID") or row.get("DC_TARGETID") or "")
        match = MUSIC_ID.search(raw_id)
        # The lyric endpoint wants the bare number, not the "MUSIC_n" label.
        music_id = match.group(1) if match else (raw_id if raw_id.isdigit() else "")
        results.append({
            "id": music_id,
            "title": _text(row.get("SONGNAME")),
            "artist": _text(row.get("ARTIST")),
            "album": _text(row.get("ALBUM")),
            "duration": _duration_ms(row.get("DURATION")),
        })
    return [r for r in results if r["id"]]


def _clean(lines, drop_credits=True):
    out = []
    for item in lines:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if drop_credits and CREDIT.match(text):
            continue
        out.append(item)
    return out


# The lyric endpoint intermittently answers status 301 for a musicId that
# answers 200 on another call.
#
# Only that status is retried; a 200 carrying an empty lrclist is an answer,
# not a failure.
RETRIES = 4
RETRY_PAUSE = 1.2


def _fetch_rows(music_id):
    """One lyric request; returns ``(rows, status)``."""
    text = _request(LYRIC, {"musicId": music_id})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderError("酷我歌词返回内容无法解析") from error
    rows = ((payload.get("data") or {}).get("lrclist")) or []
    return rows, payload.get("status")


def lyric(song_id, cookie=None, drop_credits=True):
    """Fetch one lyric. ``song_id`` is the numeric id from :func:`search`.

    Retries the intermittent 301 described above. If every attempt comes back
    empty, a readable error is raised.
    """
    import time

    raw_id = str(song_id or "").strip()
    if not raw_id:
        raise ProviderError("缺少酷我歌曲 ID")
    match = MUSIC_ID.search(raw_id)
    music_id = match.group(1) if match else raw_id
    if not music_id.isdigit():
        raise ProviderError("酷我歌曲 ID 必须是数字：%r" % raw_id)

    rows, status = [], None
    for attempt in range(RETRIES):
        rows, status = _fetch_rows(music_id)
        if rows:
            break
        # A 200 with no rows means the track has no lyric; only a non-200
        # status is retried.
        if status == 200:
            break
        if attempt + 1 < RETRIES:
            time.sleep(RETRY_PAUSE)
    if not rows:
        if status not in (200, None):
            raise ProviderError("酷我暂时拒绝提供这首歌的歌词（status=%s，已重试 %d 次），请换一个来源"
                                % (status, RETRIES))
        raise ProviderError("酷我这首没有歌词（可能是纯音乐）")
    main = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        line = _text(row.get("lineLyric")).replace("\\n", " ").strip()
        if not line:
            continue
        at = _seconds_text_to_ms(row.get("time"))
        if at is None:
            continue
        main.append({"time": at, "text": line, "words": []})
    if not main:
        raise ProviderError("酷我返回的歌词没有可用的时间轴")
    main.sort(key=lambda item: item["time"])
    return {
        "provider": "kuwo",
        "song_id": music_id,
        "main": _clean(main, drop_credits),
        "translation": [],
        "word_level": False,
        "word_source": None,
        "raw_main": json.dumps(rows, ensure_ascii=False),
        "raw_translation": "",
        "notes": ["酷我歌词没有翻译轨"],
    }
