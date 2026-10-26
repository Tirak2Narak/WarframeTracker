"""Writing back the handful of settings the Settings view lets the user edit.

`wft.config` is read-only by design (see its module docstring) and there is no
TOML writer in the system packages this project targets (no virtualenv, no
pip extras). Reserialising the whole document would also throw away every
comment in `config.example.toml`, which is the point of that file.

So this does the smallest thing that is still honest: it patches individual
`key = "value"` lines inside a `[section]` block with a regex, leaving
everything else in the file untouched. It only ever handles the keys the
Settings view actually exposes — this is not a general TOML writer.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from wft import config as config_mod


_NEW_FILE_HEADER = """\
# warframe-tracker user configuration.
# Only deviations from the defaults are kept here. Every key and its
# explanation is in config.example.toml in the program folder.

"""


def target_config_path() -> Path:
    """Where a saved setting goes: the first real (non-example) search path."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / config_mod.APP_NAME / "config.toml"


#: The short escapes TOML's basic strings have; every other control character
#: (and DEL) is written as \uXXXX. A pasted newline in a one-line field must
#: not end the line it is stored on.
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
            "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _quote(value: str) -> str:
    escaped = "".join(
        _ESCAPES.get(ch) or (f"\\u{ord(ch):04x}" if ord(ch) < 0x20 or ord(ch) == 0x7F
                            else ch)
        for ch in value)
    return f'"{escaped}"'


def set_value(section: str, key: str,
              value: "str | bool | int | float | list[str]") -> Path:
    """Write one `key = "value"` under `[section]`, creating the file if needed.

    A new file holds only a short header and the key just set. `config.load()`
    lays the user file over `config.example.toml`, so a full copy is not needed
    - and would freeze every default (URLs, the manifest list) at the version
    that happened to be installed on the first save.
    """
    target = target_config_path()
    if target.is_file():
        text = target.read_text()
    else:
        text = _NEW_FILE_HEADER

    section_pat = re.compile(rf"^\[{re.escape(section)}\]\s*$", re.MULTILINE)
    match = section_pat.search(text)
    # A bool is written as a TOML boolean, anything else as a string.
    # An int as a bare TOML integer, a list of strings as a one-line array.
    if isinstance(value, (list, tuple)):
        literal = "[" + ", ".join(_quote(str(item)) for item in value) + "]"
    elif isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    elif isinstance(value, float):
        literal = repr(round(value, 4))
    else:
        literal = _quote(value)
    line = f'{key} = {literal}'

    if match is None:
        # No such section yet: append a fresh one at the end of the file.
        sep = "\n\n" if text and not text.endswith("\n\n") else ""
        text = f"{text}{sep}[{section}]\n{line}\n"
    else:
        # Find the section's body: up to the next "[" at column 0, or EOF.
        body_start = match.end()
        next_section = re.search(r"^\[", text[body_start:], re.MULTILINE)
        body_end = body_start + next_section.start() if next_section else len(text)
        body = text[body_start:body_end]

        key_pat = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
        key_match = key_pat.search(body)
        if key_match:
            body = body[: key_match.start()] + line + body[key_match.end():]
        else:
            body = body.rstrip("\n") + f"\n{line}\n"
        text = text[:body_start] + body + text[body_end:]

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def save_value(cfg, section: str, key: str,
               value: "str | bool | int | float | list[str]") -> Path:
    """`set_value`, and the same value in the running configuration.

    The file is what survives a restart; the in-memory `cfg` is what the rest of
    the program reads. Writing only the file is how a setting came to need a
    restart: the hotkey listener holds the same `cfg` object and looks the key up
    in it on every pass, so a value put there is in force within a second.
    One function does both so a caller cannot do one and forget the other.
    """
    path = set_value(section, key, value)
    cfg.override(f"{section}.{key}", value)
    return path


def _section_body(text: str, section: str) -> "tuple[int, int] | None":
    """Start and end of a `[section]` block's lines in `text`, or None."""
    match = re.search(rf"^\[{re.escape(section)}\]\s*$", text, re.MULTILINE)
    if match is None:
        return None
    start = match.end()
    next_section = re.search(r"^\[", text[start:], re.MULTILINE)
    return start, (start + next_section.start() if next_section else len(text))


def lines_with_keys(section: str, keys) -> list[str]:
    """The `key = ...` lines of `keys` in the user's file under `[section]`.

    What `remove_keys` would remove, for showing it first. Empty when there is
    no file, no section or none of the keys.
    """
    target = target_config_path()
    if not target.is_file():
        return []
    text = target.read_text()
    span = _section_body(text, section)
    if span is None:
        return []
    pattern = _key_pattern(keys)
    return [m.group(0).rstrip("\n") for m in pattern.finditer(text[span[0]:span[1]])]


def _key_pattern(keys) -> "re.Pattern[str]":
    names = "|".join(re.escape(key) for key in keys)
    return re.compile(rf"^(?:{names})[ \t]*=.*(?:\n|\Z)", re.MULTILINE)


def remove_keys(cfg, section: str, keys) -> list[str]:
    """Take `keys` out of `[section]` in the user's file and out of `cfg`.

    The counterpart of `save_value`, and one function for the same reason. Only
    the `key = ...` lines go: comments, other keys and other sections stay as
    they are. A missing file, section or key is not an error. Returns the lines
    that were removed.
    """
    keys = list(keys)
    removed: list[str] = []
    target = target_config_path()
    if target.is_file():
        text = target.read_text()
        span = _section_body(text, section)
        if span is not None:
            body = text[span[0]:span[1]]
            removed = [m.group(0).rstrip("\n") for m in _key_pattern(keys).finditer(body)]
            if removed:
                body = _key_pattern(keys).sub("", body)
                target.write_text(text[:span[0]] + body + text[span[1]:])
    for key in keys:
        cfg.remove(f"{section}.{key}")
    return removed
