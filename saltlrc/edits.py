"""Layer 1 — structural edits.

Everything a user can do to the *shape* of a project (add a line, remove a
line, reorder lines) lives here as a pure function: take a
:class:`~saltlrc.document.Document`, return it mutated, and never leave it in a
state :func:`saltlrc.document.check` would reject. The HTTP layer exposes these
as ``/api/edit`` and the browser mirrors them in ``web/document.js``.

Timestamps are never invented. When a new line has nowhere to go — the two
neighbours are too close, or the previous line already sits at the end of the
audio — the edit raises :class:`DocumentError` with a message that says what to
do instead, rather than silently inserting an overlapping line that the player
would render wrong.
"""

from .document import DocumentError, Group, Track, check

DEFAULT_GAP = 1000   # appended line lands one second after the previous one
MIN_SLACK = 1        # two line starts must differ by at least this much


def _clamp(value, low, high):
    return max(low, min(high, value))


def _sung_end(group):
    """这一行已经**写明**唱到哪里：两条轨道里明确写的最大句尾；都没写就是 None。

    口径与校验器一致（"句尾与下一组重叠"是按轨道各判一次），所以这里取两者更晚的那个。
    """
    ends = [track.end for track in (group.main, group.translation) if track.end is not None]
    return max(ends) if ends else None


def insert_group(doc, index, start=None, duration=None, gap=DEFAULT_GAP,
                 text="", translation=""):
    """Insert a new (empty) line at ``index``, pushing that line down.

    The start time is the midpoint of the surrounding gap, and it never lands
    inside the previous line's sung span: when that line states an end, the new
    line starts after it. Appending lands ``gap`` ms after the previous line's
    end (or after its start when it states no end), clamped into the audio.
    """
    groups = doc.groups
    index = _clamp(int(index), 0, len(groups))
    previous = groups[index - 1] if index > 0 else None
    following = groups[index] if index < len(groups) else None
    if start is None:
        if following is not None:
            low = (previous.start + MIN_SLACK) if previous is not None else 0
            previous_end = _sung_end(previous) if previous is not None else None
            if previous_end is not None:
                low = max(low, previous_end + MIN_SLACK)
            high = following.start - MIN_SLACK
            if high < low:
                if previous_end is None:
                    raise DocumentError(
                        f"第 {index} 行与下一行只差 "
                        f"{max(0, following.start - (previous.start if previous else 0))} 毫秒，"
                        "中间插不进新行；请先把两行时间拉开")
                raise DocumentError(
                    f"上一行唱到 {previous_end} 毫秒，下一行 {following.start} 毫秒才开始，"
                    "中间插不进新行；请先把上一行的句尾往前收，或把下一行往后挪")
            guess = ((previous.start + following.start) // 2 if previous is not None
                     else following.start - int(gap))
            start = _clamp(guess, low, high)
        elif previous is not None:
            base = _sung_end(previous)
            if base is None:
                base = previous.start
            start = base + max(MIN_SLACK, int(gap))
            if duration is not None and start > duration:
                start = duration - 1
                if start <= base:
                    raise DocumentError("上一行唱到音频末尾，后面插不进新行；"
                                        "请先把上一行的句尾往前收，或先确认音频时长")
        else:
            start = 0
    else:
        start = int(start)
    if duration is not None and start > duration:
        raise DocumentError("新行的时间超出了音频时长")
    groups.insert(index, Group(start, Track(text), Track(translation)))
    return check(doc, duration)


def delete_group(doc, index):
    """Remove one line, then re-check the document."""
    if not 0 <= index < len(doc.groups):
        raise DocumentError("要删除的行不存在")
    doc.groups.pop(index)
    return check(doc)


def sort_groups(doc, note="句首时间倒序，已按时间重新排序"):
    """Stable sort by line start, keeping the original order inside a tie."""
    if any(b.start < a.start for a, b in zip(doc.groups, doc.groups[1:])):
        doc.groups.sort(key=lambda group: group.start)
        doc.notes.append(note)
    return check(doc)
