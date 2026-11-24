"""Identify an audio file and decide where its lyric should come from.

This module answers the question the UI asks about an audio file: is there
already a lyric, and if not, which candidate is worth loading? It returns the
answer as data, so an audio-only import is not a dead end and nothing is loaded
until the caller chooses a candidate.
"""

import os
import re
from pathlib import Path

from . import library, providers

# "01. Artist - Title", "Artist - Title", "Title" and bracketed tags.
TRACK_NUMBER = re.compile(r"^\s*\d{1,3}\s*[.\-_]\s*")
BRACKET = re.compile(r"\s*[\[(（【][^\]）)】]*[\]）)】]\s*")


def parse_filename(name):
    """Best-effort title/artist from a file name, never from network metadata.

    Bracketed segments are dropped: they are nearly always annotations such as
    "(Piano version)" or "(TV size)", which would only hurt the search and the
    comparison against library metadata.
    """
    stem = Path(os.path.basename(name or "")).stem
    stem = TRACK_NUMBER.sub("", stem).strip()
    cleaned = BRACKET.sub(" ", stem).strip()
    if " - " in cleaned:
        artist, title = cleaned.split(" - ", 1)
        return {"title": title.strip() or cleaned, "artist": artist.strip()}
    return {"title": cleaned or stem, "artist": ""}


def _normalise(text):
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]", "", (text or "").casefold())


def find_track(tracks, name, duration=None):
    """Locate the Salt Player library entry for a file.

    Exact file name wins. A fuzzy title/artist match is accepted only when the
    duration agrees within 3 s, so two different songs never get confused.
    """
    wanted_file = os.path.basename(name or "").casefold()
    for track in tracks:
        if os.path.basename(track["path"]).casefold() == wanted_file:
            return track, "exact"
    parsed = parse_filename(name)
    title = _normalise(parsed["title"])
    artist = _normalise(parsed["artist"])
    if not title:
        return None, None
    best, best_score = None, 0
    for track in tracks:
        score = 0
        track_title = _normalise(track["title"])
        if track_title == title:
            score += 3
        elif track_title and (track_title in title or title in track_title):
            score += 1
        else:
            continue
        # A title can repeat across edits and live versions; a duration gap over
        # 20 s means a different recording, and such a track is skipped.
        if duration and track["duration"] and abs(track["duration"] - duration) > 20000:
            continue
        track_artist = _normalise(track["artist"])
        if artist and track_artist and (track_artist in artist or artist in track_artist):
            score += 2
        if duration and track["duration"] and abs(track["duration"] - duration) <= 3000:
            score += 2
        if score > best_score:
            best, best_score = track, score
    return (best, "fuzzy") if best and best_score >= 3 else (None, None)


def match(name, duration=None, db_path=None, search=True, provider=None, limit=8, cookie=None):
    """Resolve lyric sources for one audio file.

    Local library first (offline, exact), then the online provider. The result
    reports whether an automatic choice is safe; it never loads anything by
    itself.
    """
    parsed = parse_filename(name)
    source = provider or providers.DEFAULT_PROVIDER
    result = {"name": os.path.basename(name or ""), "parsed": parsed,
              "library": None, "lyric": None, "candidates": [], "auto": None,
              "provider": source, "provider_label": providers.label(source), "notes": []}
    try:
        library_data = library.read_library(db_path)
    except (RuntimeError, OSError):
        result["notes"].append("没有读到 Salt Player 曲库，只能联网搜索")
        library_data = {"tracks": []}
    track, how = find_track(library_data["tracks"], name, duration)
    if track:
        result["library"] = {key: track[key] for key in
                             ("title", "artist", "album", "path", "lyric_path", "has_lyric", "duration")}
        result["library"]["matched_by"] = how
        if track["has_lyric"]:
            text = library.read_lyric(track["lyric_path"])
            if text:
                result["lyric"] = text
                result["lyric_path"] = track["lyric_path"]
                result["auto"] = "library"
                result["notes"].append("Salt Player 曲库里已有歌词，直接使用")
                return result
        result["notes"].append("曲库里有这首歌，但没有歌词文件")

    if not search:
        return result
    keyword = " ".join(x for x in (parsed["title"], parsed["artist"]) if x).strip()
    if not keyword:
        result["notes"].append("文件名里没有可用的标题，请手动搜索")
        return result
    try:
        candidates = providers.search(source, keyword, limit=limit, cookie=cookie)
    except Exception as error:
        result["notes"].append("联网搜索失败：%s" % error)
        return result
    target = {"title": parsed["title"], "artist": parsed["artist"], "duration": duration or 0}
    for candidate in candidates:
        candidate["score"] = round(providers.score(candidate, target))
    candidates.sort(key=lambda item: -item["score"])
    result["candidates"] = candidates
    if candidates and candidates[0]["score"] >= 45:
        # The provider that actually answered, not a hardcoded one: both the note
        # and the auto choice name whichever source was asked, so they stay
        # correct once a second source exists.
        result["auto"] = source
        result["auto_song_id"] = candidates[0]["id"]
        result["notes"].append("%s匹配到「%s / %s」，可直接载入" %
                               (providers.label(source), candidates[0]["title"],
                                candidates[0]["artist"]))
    elif candidates:
        result["notes"].append("匹配分数偏低，请人工确认候选")
    else:
        result["notes"].append("没有搜索结果，可手动粘贴歌词")
    return result
