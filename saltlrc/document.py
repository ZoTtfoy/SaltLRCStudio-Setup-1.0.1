"""Layer 0 — the lyric document model.

This is the bottom of the stack: the single definition of what a SaltLRC
project *is*. It has no I/O, no network and no UI, so every other layer can
import it without pulling in anything else.

Rules that live here are the ones an edit may never break (see :func:`check`):
line starts are non-negative integers and strictly increasing; every track's
timeline (start, word starts, optional end) is non-negative, increasing and
inside the audio; the first word always sits exactly on the track start.

The browser keeps a mirror of this module in ``web/document.js`` with the same
function names. Both sides are tested against the same fixtures so the two
copies cannot drift apart.
"""

from dataclasses import dataclass, field, asdict

TRACKS = ("main", "translation")


class DocumentError(ValueError):
    """A document or an edit that would break the timeline invariants.

    Subclasses ``ValueError``: the HTTP layer turns ``ValueError`` into a 400
    with the message, so a rejected edit reaches the user as a readable Chinese
    sentence rather than a stack trace.
    """


@dataclass
class Word:
    start: int
    text: str


@dataclass
class Track:
    text: str = ""
    words: list[Word] = field(default_factory=list)
    end: int | None = None
    start: int | None = None


@dataclass
class Group:
    start: int
    main: Track
    translation: Track = field(default_factory=Track)


@dataclass
class Document:
    groups: list[Group] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    alignment: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        def integer(value):
            if type(value) is not int:
                raise ValueError("工程时间必须是整数毫秒")
            return value

        def string(value):
            if not isinstance(value, str):
                raise ValueError("工程文本字段必须为字符串")
            return value

        def track(t):
            return Track(string(t.get("text", "")),
                         [Word(integer(w["start"]), string(w["text"])) for w in t.get("words", [])],
                         integer(t["end"]) if t.get("end") is not None else None,
                         integer(t["start"]) if t.get("start") is not None else None)
        metadata = {string(k): string(v) for k, v in data.get("metadata", {}).items()}
        return cls([Group(integer(g["start"]), track(g["main"]), track(g.get("translation", {})))
                    for g in data["groups"]], metadata,
                   [string(v) for v in data.get("notes", [])],
                   [string(v) for v in data.get("unresolved", [])], data.get("alignment", {}))


# --------------------------------------------------------------- accessors
def clone(doc):
    """Deep copy through the wire format."""
    return Document.from_dict(doc.to_dict())


def normalize(doc):
    """Fill in what JSON may omit. Idempotent; returns the same document."""
    for group in doc.groups:
        for name in TRACKS:
            track = getattr(group, name)
            track.words = list(track.words or [])
            track.end = track.end if track.end is not None else None
            track.start = track.start if track.start is not None else None
    return doc


def track_start(group, name="main"):
    """A track carries its own start only when it diverges from the line."""
    track = getattr(group, name)
    return group.start if track.start is None else track.start


def ticks(group, name="main"):
    """Every timestamp of one track, in order: start, each word, the end."""
    track = getattr(group, name)
    out = [track_start(group, name)]
    out.extend(word.start for word in track.words[1:])
    if track.end is not None:
        out.append(track.end)
    return out


def line_end(doc, index, fallback=2500):
    """Where a line stops for display: its own end, else the next line start."""
    group = doc.groups[index]
    if group.main.end is not None:
        return group.main.end
    nxt = doc.groups[index + 1] if index + 1 < len(doc.groups) else None
    return (nxt.start if nxt else group.start + fallback)


def span(doc):
    """Largest timestamp in the document, or None when there is nothing."""
    values = [t for g in doc.groups for name in TRACKS for t in ticks(g, name)]
    return max(values) if values else None


# -------------------------------------------------------------- invariants
def check(doc, duration=None):
    """Raise :class:`DocumentError` unless the document is still playable.

    Called after every structural edit. Messages are prefixed with the line
    number so the UI can jump straight to the offending row.
    """
    for i, group in enumerate(doc.groups, 1):
        prefix = f"第 {i} 行："
        if type(group.start) is not int or group.start < 0:
            raise DocumentError(prefix + "句首时间必须是非负整数毫秒")
        if i > 1 and group.start <= doc.groups[i - 2].start:
            raise DocumentError(prefix + "句首时间必须递增且唯一")
        for name in TRACKS:
            track = getattr(group, name)
            label = "主轨" if name == "main" else "翻译轨"
            sequence = ticks(group, name)
            for j, value in enumerate(sequence):
                if type(value) is not int or value < 0:
                    raise DocumentError(prefix + label + "时间必须是非负整数毫秒")
                if duration is not None and value > duration:
                    raise DocumentError(prefix + label + "时间超出音频时长")
                if j and value <= sequence[j - 1]:
                    raise DocumentError(prefix + label + "时间必须递增")
            if track.words and track.words[0].start != track_start(group, name):
                raise DocumentError(prefix + label + "首个逐字时间必须等于轨道开始时间")
    return doc
