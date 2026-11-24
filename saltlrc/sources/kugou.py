"""Kugou Music lyric provider.

One lyric takes three calls; the download is addressed by an id + accesskey pair
that the search step returns:

1. search — ``mobilecdn.kugou.com``, giving candidates with a ``hash``;
2. resolve — ``krcs.kugou.com/search`` keyed on that hash, giving the
   ``id`` / ``accesskey`` of the actual lyric file;
3. download — ``lyrics.kugou.com/download``, giving a base64 payload.

**The search host is called over plain HTTP.** Its certificate is not valid for
``mobilecdn.kugou.com``; an HTTPS request fails with
``CERTIFICATE_VERIFY_FAILED: Hostname mismatch``. The resolve and download hosts
are called over plain HTTP as well.

Two unit traps: the search ``duration`` is in seconds while the krcs
``duration`` is in milliseconds, and the downloaded text carries a UTF-8 BOM.
"""

import base64
import urllib.error
import urllib.parse
import urllib.request

from ..lrctext import CREDIT, parse_lrc
from ._http import TIMEOUT, ProviderError, json_body, request

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20

# Called over plain HTTP; see the module docstring.
SEARCH = "http://mobilecdn.kugou.com/api/v3/search/song"
RESOLVE = "http://krcs.kugou.com/search"
DOWNLOAD = "http://lyrics.kugou.com/download"




def _request(url, params, timeout=TIMEOUT):
    """The seam the tests replace. Delegates to the shared transport."""
    return request(url, params=params, referer="http://www.kugou.com/",
                   accept="application/json, text/plain, */*",
                   timeout=timeout, label="酷狗")


def _json(url, params):
    return json_body(_request(url, params), label="酷狗")


def _seconds_to_ms(value):
    """The search endpoint reports duration in seconds."""
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return 0


def search(keyword, limit=12, cookie=None):
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    count = max(1, min(int(limit), 30))
    payload = _json(SEARCH, {"format": "json", "keyword": keyword,
                             "page": 1, "pagesize": count, "showtype": 1})
    if payload.get("status") not in (1, None):
        message = ((payload.get("data") or {}).get("error") if isinstance(payload.get("data"), dict) else "")
        raise ProviderError(message or "酷狗搜索失败（status=%s）" % payload.get("status"))
    info = (payload.get("data") or {}).get("info") or []
    results = []
    for song in info:
        if not isinstance(song, dict):
            continue
        # "hash" addresses the lyric lookup; "album_id" is not a track id.
        song_hash = song.get("hash")
        results.append({
            "id": song_hash,
            "title": song.get("songname") or "",
            "artist": song.get("singername") or "",
            "album": song.get("album_name") or song.get("albumname") or "",
            "duration": _seconds_to_ms(song.get("duration")),
            # Passed through to the resolve step.
            "album_id": song.get("album_id") or "",
        })
    return [r for r in results if r["id"]]


def _candidates(song_hash, duration=None, keyword=""):
    payload = _json(RESOLVE, {
        "ver": 1, "man": "yes", "client": "mobi",
        "keyword": keyword or "", "hash": song_hash,
        # krcs speaks milliseconds.
        "duration": int(duration or 0),
    })
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        raise ProviderError("酷狗返回了意外的歌词候选结构")
    return [c for c in candidates if isinstance(c, dict) and c.get("id") and c.get("accesskey")]


# krcs labels each candidate with the format it can serve: 1 is the
# word-level KRC binary, 2 is plain LRC.
KRC = 1


def _is_krc(candidate):
    """True when the candidate can only be served as the encrypted KRC binary.

    Checked before downloading; a KRC payload decodes into a garbled lyric
    rather than an error.
    """
    return candidate.get("krctype") == KRC


def _download(candidate):
    payload = _json(DOWNLOAD, {
        "ver": 1, "client": "pc", "id": candidate["id"],
        "accesskey": candidate["accesskey"], "fmt": "lrc", "charset": "utf8",
    })
    content = payload.get("content")
    if not content:
        raise ProviderError("酷狗没有返回歌词内容")
    if payload.get("fmt") == "krc":
        # The server overrode the requested format.
        raise ProviderError("酷狗只提供逐字（KRC）格式，本程序暂不支持")
    try:
        raw = base64.b64decode(content).decode("utf-8", "replace")
    except (ValueError, TypeError) as error:
        raise ProviderError("酷狗返回的歌词不是有效的 base64") from error
    # The payload starts with a UTF-8 BOM.
    return raw.lstrip("\ufeff")


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
    """``song_id`` is a Kugou hash, optionally ``"hash|duration_ms"``.

    The duration selects among the candidates krcs returns for one hash (studio,
    live, remix). The caller can pass it as ``"<hash>|<ms>"``; without it the
    first candidate is used.
    """
    text = str(song_id or "").strip()
    if not text:
        raise ProviderError("缺少酷狗歌曲 hash")
    song_hash, _, duration_text = text.partition("|")
    duration = int(duration_text) if duration_text.isdigit() else None
    candidates = _candidates(song_hash, duration)
    if not candidates:
        raise ProviderError("酷狗没有这首歌的歌词")
    failures = []
    for candidate in candidates[:3]:
        # KRC-only candidates are skipped without a request: they are not
        # decoded here.
        if _is_krc(candidate):
            failures.append("酷狗只提供逐字（KRC）格式，本程序暂不支持")
            continue
        try:
            raw = _download(candidate)
        except ProviderError as error:
            failures.append(str(error))
            continue
        main = parse_lrc(raw)["lines"]
        if not main:
            failures.append("酷狗返回的歌词没有时间轴")
            continue
        return {
            "provider": "kugou",
            "song_id": song_hash,
            "main": _clean(main, drop_credits),
            "translation": [],
            "word_level": False,
            "word_source": None,
            "raw_main": raw,
            "raw_translation": "",
            "notes": ["酷狗歌词没有翻译轨"] if not failures else [],
        }
    raise ProviderError(failures[0] if failures else "酷狗没有可用的歌词")
