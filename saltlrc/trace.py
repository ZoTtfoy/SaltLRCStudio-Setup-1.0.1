"""Interaction trace — what the user did, what the app answered, where it stalled.

Recorded events
---------------
Every event the page reports is appended with its kind, name and timestamp, so
the sequence of a click — whether it reached a handler, what it answered, and
what happened after — is answerable from a file. The three ways a click can
fail to show an effect look identical from the outside, and the sequence is
what tells them apart.

The useful part is the stall detector
------------------------------------
A gesture is recorded together with the *observations* that follow it: an API
call started or finished, a dialog opened or closed, a state change the page
declared. A gesture followed by none of those within a short window is marked
**no-effect** — and that is the signature of the "取消没反应" report, independent
of what the code believes it does.

Design rules
------------
* **Recording must never break what it observes.** Every entry point swallows its
  own failures; a broken trace is a lost trace, not a broken editor.
* **A ring buffer in memory, mirrored to the diagnostics journal** so a crash does
  not take the evidence with it.
* **The analysis is pure** (`analyse(events)`) so it can be tested without a
  browser, a server or a clock.
"""

import json
import threading
import time

# How many events to keep. Enough for a full editing session's worth of clicks
# without keeping the process growing.
LIMIT = 4000

# A gesture that produced no observation within this window is reported as
# having no effect. Shorter values flag slow handlers; longer ones make the
# trace useless for a "nothing happened" report.
STALL_MS = 900

# Events that count as "something visibly happened".
OBSERVED = ("api-start", "api-end", "dialog-open", "dialog-close", "step", "error")

_STORE = []
_LOCK = threading.Lock()
_SEQ = 0
# Events ever recorded. The ring keeps the last LIMIT; this keeps counting, so a
# report can say "共 N 条（保留最近 M）" instead of quietly implying the session
# only ever produced LIMIT events.
_TOTAL = 0


def record(kind, name, detail=None, page=None, t=None, gesture=None, since=None, **extra):
    """Append one event. Never raises.

    ``t`` is the authoritative timestamp — epoch milliseconds. ``since`` is the
    page's own elapsed-time value, kept only as a cross-check; it is
    NOT comparable between two pages or two reloads.
    """
    global _SEQ, _TOTAL
    try:
        with _LOCK:
            _SEQ += 1
            _TOTAL += 1
            event = {"n": _SEQ, "t": int(t if t is not None else time.time() * 1000),
                     "kind": kind, "name": name}
            if page:
                event["page"] = page
            if detail not in (None, ""):
                event["detail"] = detail
            if gesture is not None:
                event["gesture"] = gesture
            if since is not None:
                event["since"] = int(since)
            if extra:
                event.update({k: v for k, v in extra.items() if v is not None})
            _STORE.append(event)
            if len(_STORE) > LIMIT:
                del _STORE[:len(_STORE) - LIMIT]
        # The journal is the durable half: a crash must not take the evidence.
        try:
            from . import diagnostics
            diagnostics.DIAGNOSTICS.write("trace %s %s %s" % (kind, name, detail or ""))
        except Exception:
            pass
        return event.get("n")
    except Exception:
        return None


# Anything below this is not a plausible epoch-millisecond value (it is 1973), so
# it must be a page-relative number.
EPOCH_FLOOR = 100000000000


def normalise(events, now=None):
    """Give every event an absolute timestamp on the shared machine clock.

    The page stamps ``t`` with its own ``Date.now()``. The page and this process
    run on the same machine, so the epoch value is **directly comparable across
    batches, across reloads and across tabs** — nothing needs shifting.

    Do **not** anchor each batch on its arrival. Entries are flushed both on
    gesture boundaries *and* on a timer, so a gesture and the observation that
    follows it routinely arrive in two different batches, and shifting each
    batch independently collapses the interval between them — which is exactly
    the information the stall verdict depends on. A 954 ms gap becomes 77 ms,
    the verdict flips from "no effect" to "fine", and a dead button stops being
    reported.

    ``since`` (ms since the page loaded) is carried for reference and never used
    for ordering — it restarts at 0 on every reload. A caller that can supply
    only a relative value gets the arrival time plus a ``relative`` marker,
    rather than a fabricated position on the timeline.
    """
    if now is None:
        now = time.time() * 1000
    out = []
    for event in events:
        if not isinstance(event, dict):
            continue
        item = dict(event)
        try:
            stamp = float(item.get("t"))
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None and stamp >= EPOCH_FLOOR:
            item["t"] = int(stamp)
        else:
            if stamp is not None and "since" not in item:
                item["since"] = int(stamp)
            item["t"] = int(now)
            item["relative"] = True
        out.append(item)
    return out


def record_batch(events, now=None):
    """Record a batch posted by a page (see :func:`normalise`). Never raises."""
    try:
        for event in normalise(events, now):
            if not isinstance(event, dict):
                continue
            # `relative` travels with the event so a reader of the report can see
            # that this timestamp is an arrival time, not the page's own.
            extra = {"relative": True} if event.get("relative") else {}
            record(str(event.get("kind") or "?"), str(event.get("name") or "?"),
                   event.get("detail"), event.get("page"), event.get("t"),
                   event.get("gesture"), event.get("since"), **extra)
    except Exception:
        pass


def snapshot(limit=400):
    """The last ``limit`` events, oldest first, plus the stall summary."""
    with _LOCK:
        events = [dict(item) for item in _STORE[-limit:]]
        total = _TOTAL
    return {"events": events, "analysis": analyse(events), "total": total,
            "retained": len(events)}


def clear():
    global _TOTAL
    with _LOCK:
        _STORE.clear()
        _TOTAL = 0


def analyse(events, stall_ms=STALL_MS):
    """Which gestures produced no visible effect? Pure function.

    A gesture's observations are the events between it and the NEXT gesture:
    anything the page reported while that click was in flight belongs to that
    click. An API call counts even if it has not finished yet — a slow request is
    not a dead button.
    """
    out = {"stalled": [], "gestures": 0, "errors": []}
    gestures = [e for e in events if e.get("kind") == "gesture"]
    out["gestures"] = len(gestures)
    for event in events:
        if event.get("kind") == "error":
            out["errors"].append({"t": event.get("t"), "name": event.get("name"),
                                  "detail": event.get("detail")})
    for position, event in enumerate(events):
        if event.get("kind") != "gesture":
            continue
        deadline = event.get("t", 0) + stall_ms
        seen = None
        for later in events[position + 1:]:
            if later.get("kind") == "gesture":
                break
            if later.get("kind") in OBSERVED:
                seen = later
                break
        if seen is None:
            out["stalled"].append({"t": event.get("t"), "name": event.get("name"),
                                   "detail": event.get("detail"), "page": event.get("page")})
        elif seen.get("t", 0) > deadline:
            out["stalled"].append({"t": event.get("t"), "name": event.get("name"),
                                   "detail": event.get("detail"), "page": event.get("page"),
                                   "late_ms": seen.get("t", 0) - event.get("t", 0)})
    return out


def clock(ms):
    """Epoch ms → ``HH:MM:SS.mmm`` in local time, so a report is readable."""
    try:
        moment = time.localtime(ms / 1000.0)
        return "%s.%03d" % (time.strftime("%H:%M:%S", moment), int(ms) % 1000)
    except Exception:
        return str(ms)


def export_text(limit=400):
    """A human-readable dump, for pasting into a report."""
    data = snapshot(limit)
    lines = ["SaltLRC 操作追踪 · 共 %d 条（保留最近 %d 条）"
             % (data["total"], data["retained"]),
             "手势 %d 个，其中 %d 个没有任何可见效果，错误 %d 条"
             % (data["analysis"]["gestures"], len(data["analysis"]["stalled"]),
                len(data["analysis"]["errors"])),
             ""]
    for item in data["events"]:
        lines.append("%-12s %-14s %-20s %s"
                     % (clock(item.get("t", 0)), item.get("kind", ""),
                        item.get("name", ""), item.get("detail", "")))
    if data["analysis"]["errors"]:
        lines.append("")
        lines.append("== 错误 ==")
        for item in data["analysis"]["errors"]:
            lines.append("  %s %s %s" % (clock(item.get("t", 0)), item.get("name", ""),
                                         item.get("detail", "") or ""))
    if data["analysis"]["stalled"]:
        lines.append("")
        lines.append("== 点了却没有任何反应的（最可疑） ==")
        for item in data["analysis"]["stalled"]:
            late = "（%dms 后才出现结果）" % item["late_ms"] if item.get("late_ms") else ""
            lines.append("  %s %s %s %s%s" % (clock(item.get("t", 0)), item.get("page", ""),
                                              item.get("name", ""),
                                              item.get("detail", "") or "", late))
    return "\n".join(lines)


def dump_json(limit=400):
    return json.dumps(snapshot(limit), ensure_ascii=False, indent=1)
