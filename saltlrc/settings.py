"""Application settings: AI translation provider, feature switches, key bindings.

Stored next to the credentials file under ``%LOCALAPPDATA%``, so nothing about
the user's setup leaves the machine.

The API key rule is the same one the lyrics credentials follow: **the key is
write-only from the page's point of view.** ``public()`` reports whether a key is
set, never the value — a key that round-trips into the DOM ends up in
screenshots, in the debug journal and in any exported report.
"""

import copy

from . import store

FILE_NAME = "settings.json"
VERSION = 1

# Shape and defaults. `public()` is built from this, so adding a field here is
# all it takes for the UI to receive it.
DEFAULTS = {
    "version": VERSION,
    "translation": {
        # Empty by default rather than pointing at a vendor: a default base_url
        # would quietly send a user's lyrics to whoever that vendor is.
        "base_url": "",
        "model": "",
        "api_key": "",
        "target": "zh",
        "instructions": "",
        "autoTranslateMissing": False,
    },
    "features": {
        "followPlayback": True,
        "tapMode": True,
        "showPerfPanel": False,
        "confirmOverwrite": True,
        # 深色模式；关掉就是浅色主题。
        "darkMode": True,
        # 按播放时从上次停下的地方继续（关掉 = 每次都从这首的开头播放）。
        # 默认开：给逐字打点的人，位置被"从头开始"吃掉是最费时间的一种意外。
        "resumeAfterPause": True,
        # 反复播放：暂停后再按播放，回到**这次开始播放的位置**重播这一段。
        # 它和上面那条是同一个手势（暂停后按播放）的两种答案，**必然冲突**，
        # 所以定了明确的优先级：**repeatSegment 优先**（开着它时 resumeAfterPause
        # 不参与判断）。默认关：先保持"继续往下听"的直觉行为，
        # 要反复抠某一句时再打开。页面侧见 `togglePlay()` 与 `segmentStart`。
        "repeatSegment": False,
        # `.saltproj` 的文件关联：资源管理器里显示自己的图标，双击用本程序打开。
        # 默认开；写的是**当前用户**的注册表（HKCU），
        # 不需要管理员权限，取消勾选保存即可撤销。见 saltlrc/assoc.py。
        "associateProjectFiles": True,
    },
    "keys": {
        # A binding is "Mod+Mod+Key" with the same names a KeyboardEvent uses
        # for `key`, plus Ctrl/Shift/Alt/Meta as modifiers.
        "playPause": "Space",
        "tap": "Enter",
        "undo": "Ctrl+Z",
        "redo": "Ctrl+Y",
        "help": "F1",
        "aiTranslate": "Ctrl+T",
    },
}

# Which sections a client may write, and which keys each section may contain.
# Anything else is dropped: a settings endpoint that accepts arbitrary keys is
# a way to write junk into a file the app later trusts.
WRITABLE = {
    "translation": ("base_url", "model", "api_key", "target", "instructions",
                    "autoTranslateMissing"),
    "features": tuple(DEFAULTS["features"]),
    "keys": tuple(DEFAULTS["keys"]),
}

CLEAR_KEY = "\x00clear"       # sentinel: clear the stored API key


def path():
    return store.app_path(FILE_NAME)


def _merge(base, incoming):
    out = copy.deepcopy(base)
    for section, allowed in WRITABLE.items():
        values = (incoming or {}).get(section)
        if not isinstance(values, dict):
            continue
        for name in allowed:
            if name not in values:
                continue
            value = values[name]
            if name == "autoTranslateMissing" or section == "features":
                out[section][name] = bool(value)
            elif isinstance(value, str):
                out[section][name] = value.strip() if name != "instructions" else value
    return out


def load():
    """Current settings, defaults filled in for anything missing."""
    raw = store.read_json(path(), None)
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS)
    merged = _merge(DEFAULTS, raw)
    merged["version"] = VERSION
    return merged


def save(settings):
    store.atomic_write_json(path(), settings, prefix=".settings-")
    return settings


def update(payload):
    """Apply a partial update and persist it.

    ``api_key`` is special: an empty string means "leave it alone", so saving
    the form after editing some other field cannot wipe the key. The CLEAR
    sentinel is the only value that removes it.
    """
    current = load()
    payload = copy.deepcopy(payload) if isinstance(payload, dict) else {}
    section = payload.get("translation")
    if isinstance(section, dict) and "api_key" in section:
        wanted = section.pop("api_key")          # handled here, never via _merge
        if wanted == CLEAR_KEY:
            current["translation"]["api_key"] = ""
        elif isinstance(wanted, str) and wanted.strip():
            current["translation"]["api_key"] = wanted.strip()
        # an empty value leaves the stored key alone: saving the form after
        # editing some other field must not wipe it
    merged = _merge(current, payload)
    return save(merged)


def public():
    """Settings as the page may see them: the key value is never included."""
    settings = load()
    safe = copy.deepcopy(settings)
    safe["translation"]["api_key"] = ""
    safe["translation"]["has_key"] = bool(settings["translation"]["api_key"])
    return safe


def translate_options():
    """What :mod:`saltlrc.translator` needs, or a readable reason it cannot run."""
    section = load()["translation"]
    if not section["api_key"]:
        return None, "还没有填 API Key：打开「设置 → AI 翻译」填入后即可使用"
    if not section["base_url"]:
        return None, "还没有填接口地址（base_url）：例如 https://api.deepseek.com/v1"
    if not section["model"]:
        return None, "还没有填模型名：例如 deepseek-chat 或 gpt-4o-mini"
    return section, None
