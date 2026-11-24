"""Local credential store for lyric providers.

Login state is written to a per-user file under ``%LOCALAPPDATA%`` and is never
sent anywhere except back to the provider it belongs to.

Two properties matter and are enforced here:

* **Atomic writes.** A half-written credentials file would silently log the user
  out with no explanation. Every save goes to a temp file and is then replaced.
* **Never log secrets.** ``redact()`` shortens a secret so diagnostics and error
  messages can mention an account without printing its cookie or token.
"""

import json
import os
import tempfile
import time

FILE_NAME = "credentials.json"
VERSION = 1


def config_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SaltLRCStudio")


def path():
    return os.path.join(config_dir(), FILE_NAME)


def _empty():
    return {"version": VERSION, "providers": {}}


def load():
    """Read the store. A missing or corrupt file yields an empty store.

    Corrupt data must never raise: the caller is usually in the middle of an
    unrelated request and a hard failure there would look like a crash. The
    bad file is left in place so the user can inspect it, but it is ignored.
    """
    try:
        with open(path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
        return _empty()
    data.setdefault("version", VERSION)
    return data


def save(data):
    """Atomically replace the store. Returns the path written."""
    directory = config_dir()
    os.makedirs(directory, exist_ok=True)
    target = path()
    # Write next to the target so os.replace stays on one filesystem (a temp
    # file on another drive cannot be renamed over the target atomically).
    handle, temp = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except Exception:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return target


def get(provider):
    """Stored record for one provider, or ``None``."""
    record = load()["providers"].get(provider)
    return record if isinstance(record, dict) else None


def cookie(provider):
    """Just the cookie string a provider call needs, or ``None``."""
    record = get(provider)
    value = (record or {}).get("cookie")
    return value or None


def put(provider, cookie_value, **extra):
    """Persist a provider login. ``cookie_value`` may be empty for providers
    that only need a token (LRCLIB needs nothing at all)."""
    data = load()
    record = dict(extra)
    record["cookie"] = cookie_value or ""
    record["saved_at"] = int(time.time())
    data["providers"][provider] = record
    save(data)
    return record


def clear(provider=None):
    """Forget one provider, or every provider when called with no argument."""
    data = load()
    if provider is None:
        data["providers"] = {}
    else:
        data["providers"].pop(provider, None)
    save(data)


def status():
    """A safe-to-display summary: who is logged in, never the secrets."""
    data = load()
    out = {}
    for name, record in data["providers"].items():
        out[name] = {
            "logged_in": bool(record.get("cookie") or record.get("token")),
            "account": record.get("account") or "",
            "nickname": record.get("nickname") or "",
            "method": record.get("method") or "",
            "saved_at": record.get("saved_at") or 0,
        }
    return out


def redact(value, keep=6):
    """Shorten a secret so it can appear in a log line without being usable."""
    text = str(value or "")
    if len(text) <= keep:
        return "***" if text else ""
    return text[:keep] + "…(" + str(len(text)) + " 字符)"
