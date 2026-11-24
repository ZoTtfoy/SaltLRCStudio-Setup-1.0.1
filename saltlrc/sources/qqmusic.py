"""QQ Music lyric provider.

Two endpoints, both under the ``c.y.qq.com`` "client" API:

* search — ``client_search_cp``, which answers with ``data.song.list``;
* lyrics — ``fcg_query_lyric_new.fcg``, which answers with ``lyric`` plus a
  separate ``trans`` field for the translation.

**Both endpoints are called with GET.** A POST carrying the same parameters
gets a 200 whose body is an opaque encrypted blob; the request is built as a
query string.

Both require a matching ``Referer`` — the lyric endpoint in particular the
player page, not the site root. The referer is a per-call argument rather than
one module constant.

No login is needed, and a cookie is optional: supplying one only raises the
quality of the metadata, never the availability of lyrics.
"""

import base64
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from ..lrctext import CREDIT, parse_lrc
from ._http import TIMEOUT, ProviderError, json_body, request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

REFERER_SEARCH = "https://y.qq.com/"
REFERER_LYRIC = "https://y.qq.com/portal/player.html"

SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
LYRIC = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"

# A JSON-P wrapper shows up when the endpoint ignores format=json.
CALLBACK = re.compile(r"^[A-Za-z_$][\w$]*\s*\((.*)\)\s*;?\s*$", re.S)




def _get(url, params, referer, cookie=None, timeout=TIMEOUT):
    """The seam the tests replace. Delegates to the shared transport."""
    return request(url, params=params, referer=referer, cookie=cookie,
                   accept="application/json, text/plain, */*",
                   timeout=timeout, label="QQ 音乐")


def _strip_callback(body):
    match = CALLBACK.match(body)
    return match[1].strip() if match else body


def _json(text):
    return json_body(text, label="QQ 音乐", unwrap=_strip_callback,
                     not_json="QQ 音乐返回了加密内容（请检查请求方式是否为 GET）")


def _seconds_to_ms(value):
    """QQ Music reports the interval in seconds; this app speaks milliseconds."""
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return 0


def search(keyword, limit=12, cookie=None):
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    count = max(1, min(int(limit), 30))
    payload = _json(_get(SEARCH, {
        "w": keyword, "p": 1, "n": count, "format": "json",
        # cr=1 asks for the singer list; without it the response has no artist.
        "cr": 1, "g_tk": 5381, "inCharset": "utf-8", "outCharset": "utf-8",
    }, REFERER_SEARCH, cookie))
    if payload.get("code") not in (0, None):
        raise ProviderError("QQ 音乐搜索失败（code=%s）" % payload.get("code"))
    songs = (((payload.get("data") or {}).get("song") or {}).get("list")) or []
    results = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        singers = song.get("singer") or []
        # songmid is the stable id; songid is numeric and is not accepted by the
        # lyric endpoint. The mid goes into the candidate.
        mid = song.get("songmid")
        results.append({
            "id": mid,
            "title": song.get("songname") or song.get("title") or "",
            "artist": "/".join(s.get("name") or "" for s in singers if s.get("name")),
            "album": ((song.get("albumname") if isinstance(song.get("albumname"), str)
                       else (song.get("album") or {}).get("name")) or ""),
            "duration": _seconds_to_ms(song.get("interval")),
        })
    return [r for r in results if r["id"]]


def _decode_lyric(raw):
    """The lyric field is base64 unless ``nobase64=1`` was honoured.

    Both forms occur. The decoder checks rather than assumes: a base64 blob of
    LRC tags decodes into a string starting with ``[``, and anything already
    starting with ``[`` is passed straight through.

    HTML entities are undone here, on the raw body: the bodies arrive as
    HTML-escaped text (``&amp;`` for a literal ampersand).
    """
    text = raw or ""
    if not text.lstrip().startswith("["):
        try:
            decoded = base64.b64decode(text).decode("utf-8", "replace")
        except (ValueError, TypeError):
            decoded = ""
        if decoded.lstrip().startswith("["):
            text = decoded
    return html.unescape(text)


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


def lyric(song_id, cookie=None, drop_credits=True):
    mid = str(song_id or "").strip()
    if not mid:
        raise ProviderError("缺少 QQ 音乐歌曲 ID（songmid）")
    payload = _json(_get(LYRIC, {
        "songmid": mid, "format": "json", "nobase64": 1,
        "g_tk": 5381, "inCharset": "utf-8", "outCharset": "utf-8",
    }, REFERER_LYRIC, cookie))
    if payload.get("code") not in (0, None):
        raise ProviderError("QQ 音乐拒绝该歌词请求（code=%s）" % payload.get("code"))
    main_raw = _decode_lyric(payload.get("lyric"))
    trans_raw = _decode_lyric(payload.get("trans"))
    main = parse_lrc(main_raw)["lines"]
    if not main:
        raise ProviderError("QQ 音乐这首没有歌词（可能是纯音乐）")
    translation = parse_lrc(trans_raw)["lines"]
    return {
        "provider": "qqmusic",
        "song_id": mid,
        "main": _clean(main, drop_credits),
        "translation": _clean(translation, drop_credits),
        "word_level": False,
        "word_source": None,
        # Already HTML-unescaped by _decode_lyric.
        "raw_main": main_raw,
        "raw_translation": trans_raw,
    }
