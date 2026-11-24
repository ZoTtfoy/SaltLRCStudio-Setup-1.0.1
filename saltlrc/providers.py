"""Lyric provider registry.

Every provider exposes the same two calls, ``search(keyword, limit, cookie)``
and ``lyric(song_id, cookie, drop_credits)``, so adding a source is a one-line
change to ``REGISTRY`` plus an entry in ``CATALOGUE`` — the server and the UI
read both from here and need no edits of their own.

Adding a source does still need one thing outside this file: a module that
implements the two calls. The contract it must honour is the duration unit
(below) and :class:`~saltlrc.sources._http.ProviderError` for failures.

Three facts about the sources that the rest of the code relies on:

* **Only Netease accepts credentials.** It is also the only one whose
  word-level (YRC) data is real; the others serve line-level LRC only, and the
  per-word times for those are estimated locally and labelled as such.
* **Whichever field carries the duration, it leaves each module in
  milliseconds.** Two sources report seconds and one reports both, in different
  fields of the same flow — see the note in :func:`score`, which compares
  durations against a library track.
* **Failures are per-source and non-fatal.** A provider raises
  :class:`~saltlrc.sources._http.ProviderError` with a readable reason; the caller
  moves on to the next source rather than aborting the lookup.
"""

import re

from .sources import kugou, kuwo, lrclib, netease, qqmusic
from .sources._http import ProviderError

# Order matters twice over: it is the order of the UI dropdown, and the order a
# lookup walks when the user has not picked a source. Netease carries real
# per-word timings and is tried first; LRCLIB needs no account at all, so it
# comes second.
REGISTRY = {
    "netease": netease,
    "lrclib": lrclib,
    "qqmusic": qqmusic,
    "kugou": kugou,
    "kuwo": kuwo,
}
DEFAULT_PROVIDER = "netease"

# What each source is, in the words the UI shows. `needs_login` is true only for
# Netease, and even there a login is an upgrade (real word timings) rather than
# a requirement.
CATALOGUE = {
    "netease": {"label": "网易云音乐", "needs_login": True,
                "login_note": "登录后可用真实逐字（YRC）时间与完整翻译",
                "word_level": True, "translation": True},
    "lrclib": {"label": "LRCLIB（开放歌词库）", "needs_login": False,
               "login_note": "开放数据库，无需账号",
               "word_level": False, "translation": False},
    "qqmusic": {"label": "QQ 音乐", "needs_login": False,
                "login_note": "无需登录；部分歌词带翻译",
                "word_level": False, "translation": True},
    "kugou": {"label": "酷狗音乐", "needs_login": False,
              "login_note": "无需登录；没有翻译轨",
              "word_level": False, "translation": False},
    "kuwo": {"label": "酷我音乐", "needs_login": False,
             "login_note": "无需登录；接口不稳定，失败会提示换来源",
             "word_level": False, "translation": False},
}

NOISE = re.compile(r"[\s\u3000,，.。!！?？、~～\-–—_'\"“”‘’()（）\[\]【】/\\:：;；+*&#]+")


def names():
    return list(REGISTRY)


def catalogue():
    """The dropdown's options: id, label and what a login would buy."""
    out = []
    for key in REGISTRY:
        entry = {"id": key, "label": CATALOGUE[key]["label"],
                 "needs_login": CATALOGUE[key]["needs_login"],
                 "login_note": CATALOGUE[key]["login_note"],
                 "word_level": CATALOGUE[key]["word_level"],
                 "translation": CATALOGUE[key]["translation"]}
        out.append(entry)
    return out


def label(provider):
    return CATALOGUE.get(provider or DEFAULT_PROVIDER, {}).get("label", provider)


def _module(provider):
    try:
        return REGISTRY[provider or DEFAULT_PROVIDER]
    except KeyError:
        raise ProviderError("未注册的歌词来源：%s" % provider) from None


def search(provider, keyword, limit=12, cookie=None):
    return _module(provider).search(keyword, limit=limit, cookie=cookie)


def lyric(provider, song_id, cookie=None, drop_credits=True):
    return _module(provider).lyric(song_id, cookie=cookie, drop_credits=drop_credits)


# Phrases a provider uses for "this particular track is unavailable to me".
# Matched against the message: such a refusal is a domain fact (Kuwo gates
# many karaoke versions), not a transport failure, and the two are
# indistinguishable from the exception type alone.
REFUSAL_MARKERS = ("暂时拒绝", "换一个来源")


def _is_refusal(error):
    message = str(error)
    return any(marker in message for marker in REFUSAL_MARKERS)


def lyric_first_working(provider, candidates, track=None, cookie=None, drop_credits=True,
                        min_score=0):
    """Try candidates in ranked order, returning the first lyric that loads.

    Some sources are reliable per-track rather than per-service: Kuwo
    answers 301 for the *correct* match and 200 for a worse one, sometimes
    minutes apart on the same id. A plausible neighbour's lyric is then better
    than an error — but only a *plausible* one, which is what ``track`` and
    ``min_score`` are for.

    Without a floor, the fallback can return a different song entirely, which is
    the one outcome this project treats as worse than failing. Candidates below
    ``min_score`` are never used, and a substitution that survives the floor is
    reported in ``notes`` so the UI can say the lyric came from a neighbouring
    match rather than implying it is the exact one. The note names the
    neighbouring candidate's title, so the user can tell the two apart at a
    glance.

    ``candidates`` are the dicts returned by :func:`search`; pass ``track`` to
    enable ranking and the floor. Only a per-track refusal advances to the next
    candidate — a transport error or a bad id is reported immediately; more
    candidates would not fix either.

    Returns ``(payload, candidate)``. Raises the first error when none work.
    """
    ordered = []
    seen = set()
    for candidate in candidates or []:
        if isinstance(candidate, dict):
            song_id = str(candidate.get("id") or "").strip()
        else:
            song_id, candidate = str(candidate or "").strip(), {"id": candidate}
        if song_id and song_id not in seen:
            seen.add(song_id)
            ordered.append(candidate)
    if track is not None:
        ordered.sort(key=lambda item: -score(item, track))
        if min_score:
            ordered = [c for c in ordered if score(c, track) >= min_score]
    if not ordered:
        raise ProviderError("没有可尝试的歌词候选")
    first_error = None
    for candidate in ordered:
        try:
            payload = lyric(provider, candidate["id"],
                            cookie=cookie, drop_credits=drop_credits)
        except ProviderError as error:
            if first_error is None:
                first_error = error
            # Anything other than a refusal is fatal: retrying cannot help.
            if not _is_refusal(error):
                raise
            continue
        if candidate["id"] != ordered[0]["id"]:
            # The requested match was refused and a neighbour answered instead;
            # say so rather than letting the swap go unnoticed.
            payload.setdefault("notes", []).append(
                "当前来源拒绝了首选匹配，改用候选「%s」" % (candidate.get("title") or candidate["id"]))
        return payload, candidate
    raise first_error


def normalize(text):
    return NOISE.sub("", (text or "").lower())


def score(candidate, track):
    """Rank a candidate against a library track. Higher is better.

    Every duration reaching this function is in **milliseconds**; each provider
    converts its own units at its own boundary. The thresholds below (1.5 s for
    a confident match, 4 s to stay plausible) are meaningless against seconds
    and would score every candidate as a mismatch.
    """
    value = 0.0
    title = normalize(candidate.get("title"))
    wanted_title = normalize(track.get("title"))
    if title and title == wanted_title:
        value += 60
    elif title and wanted_title and (title in wanted_title or wanted_title in title):
        value += 35
    artist = normalize(candidate.get("artist"))
    wanted_artist = normalize(track.get("artist"))
    if artist and wanted_artist:
        if artist == wanted_artist:
            value += 30
        elif artist in wanted_artist or wanted_artist in artist:
            value += 18
        else:
            value -= 8
    duration = candidate.get("duration") or 0
    wanted_duration = track.get("duration") or 0
    if duration and wanted_duration:
        gap = abs(duration - wanted_duration)
        if gap <= 1500:
            value += 25
        elif gap <= 4000:
            value += 12
        elif gap <= 10000:
            value += 2
        else:
            value -= 15
    return value


def best_match(candidates, track, threshold=45):
    """Return ``(candidate, score, confident)`` for the best candidate."""
    if not candidates:
        return None, 0, False
    ranked = sorted(((score(c, track), c) for c in candidates), key=lambda item: -item[0])
    best_value, best = ranked[0]
    return best, best_value, best_value >= threshold


def credential(provider, cookie=None):
    """The cookie a call should use: the explicit one, else the stored one.

    A cookie passed in the request wins, so a caller can try a different account
    without touching the saved login. When neither exists this is ``None``,
    which every provider treats as anonymous — the app is fully usable without
    ever logging in anywhere.
    """
    if cookie:
        return cookie
    if not CATALOGUE.get(provider or DEFAULT_PROVIDER, {}).get("needs_login"):
        return None
    from . import credentials

    return credentials.cookie(provider or DEFAULT_PROVIDER)
