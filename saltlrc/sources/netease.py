"""Netease Cloud Music lyric provider.

Two request styles are used on purpose:

* the plain legacy API for search (no encryption, stable, no credentials);
* the encrypted eapi envelope for lyrics, which is the only anonymous route
  that returns the modern lyric payload including the translation track.

Word-level (YRC) data is only served to logged-in sessions, so it is read
opportunistically: when a cookie is supplied and the payload carries per-word
timings we use them, otherwise the caller falls back to local estimation.
"""

import urllib.parse

from ..crypto import eapi, weapi
from ..lrctext import clean_lines, parse_lrc
from ._http import TIMEOUT, USER_AGENT, ProviderError, json_body, request

REFERER = "https://music.163.com/"

def _request(url, data=None, cookie=None, timeout=TIMEOUT):
    """The seam the tests replace. Delegates to the shared transport."""
    return request(url, data=data, cookie=cookie, referer=REFERER,
                   timeout=timeout, label="网易云")


def _json(url, data=None, cookie=None):
    return json_body(_request(url, data=data, cookie=cookie), label="网易云")


def search(keyword, limit=12, cookie=None):
    """Return candidate songs for a free-text keyword."""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    url = "https://music.163.com/api/search/get?s=%s&type=1&offset=0&limit=%d" % (
        urllib.parse.quote(keyword), max(1, min(int(limit), 30)))
    try:
        payload = _json(url, cookie=cookie)
    except ProviderError:
        # Fall back to the encrypted search endpoint.
        payload = _json("https://music.163.com/weapi/cloudsearch/get/web",
                        weapi({"s": keyword, "type": 1, "offset": 0,
                               "limit": max(1, min(int(limit), 30)), "total": "true",
                               "csrf_token": ""}),
                        cookie=cookie)
        songs = ((payload.get("result") or {}).get("songs") or [])
    else:
        songs = ((payload.get("result") or {}).get("songs") or [])
    results = []
    for song in songs:
        artists = song.get("artists") or []
        results.append({
            "id": song.get("id"),
            "title": song.get("name") or "",
            "artist": "/".join(a.get("name") or "" for a in artists if a.get("name")),
            "album": ((song.get("album") or {}).get("name") or ""),
            "duration": song.get("duration") or 0,
        })
    return [r for r in results if r["id"]]


def _payload_from(data):
    """Normalise a raw lyric response into main/translation/word fields."""
    if not isinstance(data, dict):
        raise ProviderError("网易云返回了意外的歌词结构")
    if data.get("code") not in (200, None):
        raise ProviderError("网易云拒绝了该歌词请求（code=%s）" % data.get("code"))
    main_raw = (data.get("lrc") or {}).get("lyric") or ""
    translation_raw = (data.get("tlyric") or {}).get("lyric") or ""
    yrc_raw = (data.get("yrc") or {}).get("lyric") or ""
    body = yrc_raw or main_raw
    parsed = parse_lrc(body)
    translation = parse_lrc(translation_raw)
    source = "yrc" if yrc_raw else ("inline" if parsed["word_level"] else None)
    return {
        "main": parsed["lines"],
        "translation": translation["lines"],
        "word_level": bool(source),
        "word_source": source,
        "raw_main": main_raw,
        "raw_translation": translation_raw,
    }


def lyric(song_id, cookie=None, drop_credits=True):
    """Fetch the original lyric plus its translation for one song.

    The plain legacy endpoint is the primary route: it is anonymous, stable and
    already returns both the original and the translation. The encrypted eapi
    route only adds word-level (YRC) data and only for logged-in sessions, so it
    is attempted first when a cookie is present and its failure is non-fatal.
    """
    payload = None
    failures = []
    if cookie:
        try:
            data = _json(EAPI_LYRIC, eapi("/api/song/lyric/v1", {
                "id": int(song_id), "lv": -1, "kv": -1, "tv": -1, "csrf_token": ""}), cookie=cookie)
            payload = _payload_from(data)
        except ProviderError as error:
            failures.append(str(error))
    if payload is None or not payload["main"]:
        data = _json(LEGACY_LYRIC % int(song_id))
        payload = _payload_from(data)
    if not payload["main"] and failures:
        raise ProviderError(failures[0])
    result = {
        "provider": "netease",
        "song_id": int(song_id),
        "main": clean_lines(payload["main"], drop_credits),
        "translation": clean_lines(payload["translation"], drop_credits),
        "word_level": payload["word_level"],
        "word_source": payload["word_source"],
        "raw_main": payload["raw_main"],
        "raw_translation": payload["raw_translation"],
    }
    if failures:
        result["notes"] = failures
    return result
