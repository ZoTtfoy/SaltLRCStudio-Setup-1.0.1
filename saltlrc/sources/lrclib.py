"""LRCLIB lyric provider (https://lrclib.net).

LRCLIB is a community lyric database with a plain public API: no account, no
signature, no cookie. It is the one source here that works with zero setup, so
it is deliberately cheap to try first when a user has no account anywhere.

Two things about its data shape drive the code below:

* **The duration is in seconds, not milliseconds.** Everything else in this
  project measures in milliseconds (``providers.score()`` reads
  ``candidate["duration"]`` and compares it against a track's ms duration), so
  the conversion happens here, at the boundary, once.
* **Timed and plain lyrics are separate fields.** ``syncedLyrics`` carries the
  ``[mm:ss.xx]`` tags and ``plainLyrics`` is bare text. A record can have one,
  the other, or neither, and the UI needs to know which it got.
"""

import urllib.error
import urllib.parse
import urllib.request

from ..lrctext import CREDIT, parse_lrc
from ._http import TIMEOUT, ProviderError, json_body, raw_text

# LRCLIB asks that clients identify themselves; anonymous default agents get
# throttled and can be refused outright.
#
# Keep this short and free of URLs: a long agent string carrying a link was
# answered with HTTP 503 while a plain one returned 200 for the same request,
# so the server is doing content-based filtering on the header.
USER_AGENT = "SaltLRCStudio/0.11 (local lyric editor)"

# LRCLIB is noticeably slower than the commercial endpoints and times out
# intermittently at the 20s that suffices everywhere else. A longer timeout plus
# one retry turns a flaky failure into a normal result; the retry is bounded so
# a genuinely down service still fails in reasonable time.
TIMEOUT = 35
RETRIES = 2

SEARCH = "https://lrclib.net/api/search"
GET_BY_ID = "https://lrclib.net/api/get/%s"




def _fetch(url, timeout=TIMEOUT):
    """One attempt. Raises the transport exception untouched.

    Kept separate from :func:`_request` so the retry policy can be exercised
    without a network: the tests replace *this* function and let the real
    retry/error-mapping loop run over it.
    """
    return raw_text(url, accept="application/json", referer="https://lrclib.net/",
                    timeout=timeout)


def _request(url, timeout=TIMEOUT):
    """Fetch with the retry policy applied.

    A 404 is a real answer ("no such lyric") and a 4xx is definitive; only 5xx
    and transport failures are retried, so a wrong id fails fast while a flaky
    connection still succeeds.
    """
    last = None
    for attempt in range(RETRIES):
        try:
            return _fetch(url, timeout)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise ProviderError("LRCLIB 没有这首歌的歌词") from error
            # A 5xx is worth retrying; anything else is a definitive answer.
            if error.code < 500:
                raise ProviderError("LRCLIB 返回 HTTP %s" % error.code) from error
            last = error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last = error
    if isinstance(last, urllib.error.HTTPError):
        raise ProviderError("LRCLIB 返回 HTTP %s" % last.code) from last
    raise ProviderError("无法连接 LRCLIB：%s" % last) from last


def _json(url):
    return json_body(_request(url), label="LRCLIB")


def _milliseconds(record):
    """LRCLIB reports duration in seconds; the rest of the app speaks ms."""
    seconds = record.get("duration")
    if seconds is None:
        return 0
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError):
        return 0


def search(keyword, limit=12, cookie=None):
    """Return candidate records for a free-text keyword.

    ``cookie`` is accepted and ignored: the signature is shared with the other
    providers so the registry can call them all the same way.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    url = "%s?%s" % (SEARCH, urllib.parse.urlencode({"q": keyword}))
    payload = _json(url)
    if not isinstance(payload, list):
        raise ProviderError("LRCLIB 返回了意外的搜索结果结构")
    results = []
    for record in payload[:max(1, min(int(limit), 30))]:
        if not isinstance(record, dict):
            continue
        artist = record.get("artistName") or ""
        # Large compilations list every featured artist; the first is the one
        # that matters for matching against a library entry.
        if record.get("artists"):
            names = [a.get("name") for a in record["artists"] if isinstance(a, dict)]
            artist = "/".join(name for name in names if name) or artist
        results.append({
            "id": record.get("id"),
            "title": record.get("trackName") or "",
            "artist": artist,
            "album": record.get("albumName") or "",
            "duration": _milliseconds(record),
            # Surface which lyric kinds exist so the list can hint at it.
            "has_synced": bool(record.get("syncedLyrics")),
            "has_plain": bool(record.get("plainLyrics")),
        })
    return [r for r in results if r["id"] is not None]


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
    """Fetch one record and turn it into the shared lyric payload shape.

    ``song_id`` may be the numeric LRCLIB id or, for convenience, a
    ``"artist - title"`` keyword — a search result always yields an id, but a
    hand-typed id is not always numeric.
    """
    text = str(song_id or "").strip()
    if not text:
        raise ProviderError("缺少 LRCLIB 歌曲 ID")
    if text.isdigit():
        record = _json(GET_BY_ID % text)
        if isinstance(record, list):        # /api/get can answer with a list
            record = record[0] if record else {}
    else:
        found = _json("%s?%s" % (SEARCH, urllib.parse.urlencode({"q": text})))
        record = found[0] if isinstance(found, list) and found else {}
    if not isinstance(record, dict) or not record:
        raise ProviderError("LRCLIB 没有这首歌的歌词")

    synced = record.get("syncedLyrics") or ""
    plain = record.get("plainLyrics") or ""
    if synced:
        parsed = parse_lrc(synced)
        main = parsed["lines"]
        # LRCLIB's synced text sometimes carries no line at all (an empty
        # instrumental) while plain text does; fall back rather than show blank.
        if not main and plain:
            main = _plain_lines(plain)
    else:
        main = _plain_lines(plain)
    if not main:
        raise ProviderError("LRCLIB 这条记录没有歌词内容（可能是纯音乐）")

    return {
        "provider": "lrclib",
        "song_id": record.get("id", song_id),
        "main": _clean(main, drop_credits),
        "translation": [],
        "word_level": False,
        "word_source": None,
        "raw_main": synced or plain,
        "raw_translation": "",
        "instrumental": bool(record.get("instrumental")),
        "notes": [] if synced else ["LRCLIB 只有纯文本歌词，没有时间轴，逐字时间由本地估算"],
    }


def _plain_lines(plain):
    """Bare text into zero-time lines.

    Without a time axis there is nothing honest to show as a timeline, so the
    lines get sequential placeholder times. The caller rebuilds real timing
    from the audio; what matters is that the text itself is not lost.
    """
    lines = []
    for index, row in enumerate((plain or "").splitlines()):
        body = row.strip()
        if body:
            # 100 ms apart keeps ordering strict and is obviously synthetic.
            lines.append({"time": index * 100, "text": body, "words": []})
    return lines
