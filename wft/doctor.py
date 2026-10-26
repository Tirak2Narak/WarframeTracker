"""`wf doctor`: is this machine set up to run the tool, and if not, what is missing.

Meant to be pasted to whoever handed over the tool, so every line says what
was checked and what was found. It reads and reports only: no network, no
screenshot, no writes (the database is opened read-only and the data
directory is not created). Two things are kept out of the output on purpose:
the account id is reported as set or not set, and EE.log is only stat'ed -
it contains an IP address and an e-mail address.

It must also work when the thing it diagnoses is broken, so this module imports
nothing outside the standard library at load time; every dependency is probed
inside its own check and a check that crashes becomes a FAIL line, not a
traceback.

The questions the Setup page asks too (game data, input group, hotkey, log,
game window) are answered by `wft.readiness`; this module only words them for
the terminal, so the two cannot disagree.
"""
from __future__ import annotations

import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from wft import readiness, version
from wft.readiness import input_group_state  # noqa: F401 - re-exported, install.sh's twin

_ROOT = Path(__file__).resolve().parent.parent
PACKAGES_FILE = _ROOT / "packaging" / "arch-packages.txt"

#: Oldest Python the code runs on: `tomllib` (config loading) arrived in 3.11.
MIN_PYTHON = (3, 11)

Result = tuple[str, str, str]          # (level, name, detail)


def _run(cmd: list[str], timeout: float = 10) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def package_list() -> list[str]:
    """Package names from packaging/arch-packages.txt (`#` starts a comment)."""
    names = []
    for line in PACKAGES_FILE.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


# -- checks ---------------------------------------------------------------

def check_python() -> list[Result]:
    found = platform.python_version()
    if tuple(sys.version_info[:2]) < MIN_PYTHON:
        return [("FAIL", "python", f"{found}, need {'.'.join(map(str, MIN_PYTHON))} or newer")]
    return [("OK", "python", found)]


def check_modules() -> list[Result]:
    results: list[Result] = []
    # (module, package named in the message, level when it is missing)
    wanted = [("PySide6", "pyside6", "FAIL"), ("cv2", "python-opencv", "FAIL"),
              ("numpy", "python-numpy", "FAIL"), ("rapidfuzz", "python-rapidfuzz", "FAIL"),
              ("evdev", "python-evdev", "WARN"), ("PIL", "python-pillow", "FAIL"),
              ("tesserocr", "tesserocr, optional", "INFO")]
    for module, package, level in wanted:
        try:
            loaded = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken install can raise anything
            results.append((level, f"module {module}",
                            f"not importable ({package}): {type(exc).__name__}"))
            continue
        results.append(("OK", f"module {module}", str(getattr(loaded, "__version__", "") or "")))
    return results


def check_tools() -> list[Result]:
    results: list[Result] = []
    tesseract = shutil.which("tesseract")
    if not tesseract:
        results.append(("FAIL", "tesseract", "not found (package tesseract)"))
    else:
        results.append(("OK", "tesseract", tesseract))
        langs = _run([tesseract, "--list-langs"])
        if langs is None:
            results.append(("FAIL", "tesseract eng", "could not run tesseract --list-langs"))
        elif "eng" in langs.stdout.split():
            results.append(("OK", "tesseract eng", "language data present"))
        else:
            results.append(("FAIL", "tesseract eng", "missing (package tesseract-data-eng)"))
    for tool, package in (("xdotool", "xdotool"), ("xprop", "xorg-xprop")):
        path = shutil.which(tool)
        results.append(("OK", tool, path) if path
                       else ("WARN", tool, f"not found (package {package})"))
    path = shutil.which("notify-send")
    results.append(("OK", "notify-send", path) if path
                   else ("INFO", "notify-send",
                         "not found (package libnotify): no desktop notification"))
    return results


def check_packages() -> list[Result]:
    if not shutil.which("pacman"):
        return [("INFO", "packages", "pacman not found, package list not checked")]
    try:
        names = package_list()
    except OSError:
        return [("WARN", "packages", f"{PACKAGES_FILE.name} not found")]
    done = _run(["pacman", "-T", *names])
    if done is None:
        return [("WARN", "packages", "could not run pacman -T")]
    missing = done.stdout.split()
    if missing:
        return [("WARN", "packages", "missing: " + " ".join(missing))]
    return [("OK", "packages", f"all {len(names)} from {PACKAGES_FILE.name} installed")]


def check_session() -> list[Result]:
    results: list[Result] = []
    for variable in ("XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DISPLAY"):
        value = os.environ.get(variable, "")
        results.append(("OK", variable, value) if value
                       else ("WARN", variable, "not set"))
    return results


def check_input() -> list[Result]:
    check = readiness.input_check()
    state = check.info["group"]
    results: list[Result] = [{
        "active": ("OK", "group input", "member, active in this session"),
        "pending": ("WARN", "group input",
                    "member, but not active yet - log out and back in"),
        "missing": ("WARN", "group input",
                    "not a member; the hotkey cannot read input devices "
                    "(run ./install.sh, or: sudo usermod -aG input $USER, then log in again)"),
    }[state]]
    devices = check.info["devices"]
    if devices is None:
        results.append(("WARN", "input devices", "python-evdev missing"))
    else:
        results.append(("OK", "input devices", f"{devices} readable")
                       if devices else
                       ("WARN", "input devices", "no readable key device"))
    return results


def check_config(cfg) -> list[Result]:
    check = readiness.hotkey_check(cfg)
    hotkey: Result = (
        {"unset": "WARN", "unchecked": "INFO", "unknown": "FAIL", "ok": "OK"}[check.state],
        "hotkey", check.detail)
    # Without a file of one's own the source is the shipped example; the bare
    # path made the first reader think that file was theirs to edit.
    if Path(cfg.source).resolve() == (_ROOT / "config.example.toml").resolve():
        source = "no settings of your own yet; the defaults of config.example.toml apply"
    else:
        source = str(cfg.source)
    return [("OK", "config", source),
            ("OK", "data_dir", str(cfg.data_dir_path())), hotkey]


def check_regions(cfg) -> list[Result]:
    from wft.relic import regions
    found = regions.configured_overrides(cfg)
    if not found:
        return [("OK", "reward regions", "measured values")]
    pairs = " ".join(f"{key}={value:g}" for key, value, _builtin in found)
    return [("INFO", "reward regions",
             f"your own calibration in config.toml: {pairs} "
             "(Settings, Calibrate reward regions, Use the measured values)")]


def check_ee_log(cfg) -> list[Result]:
    check = readiness.ee_log_check(cfg)
    if check.ok:
        return [("OK", "EE.log", check.detail)]
    found = check.info["found"]
    # The search already covered the other Steam libraries and Flatpak; what
    # is left is the game never having run, or a place only the person knows.
    hint = ("not in any Steam library either - start the game once, or choose "
            "the file under Setup -> Game log"
            if not found else
            f"but a search of the Steam libraries finds {found[0]} - "
            "Setup -> Game log uses it, or set paths.ee_log")
    return [("WARN", "EE.log", f"{check.detail} (paths.ee_log; {hint})")]


def check_data(cfg) -> list[Result]:
    check = readiness.data_check(cfg)
    if check.state == "ok":
        return [("OK", "database", check.detail)]
    return [("WARN", "database", check.detail)]


def check_game(cfg) -> list[Result]:
    check = readiness.window_check(cfg)
    return [("OK" if check.ok else "WARN", "game window", check.detail)]


def check_inventory(cfg) -> list[Result]:
    check = readiness.inventory_check(cfg)
    # Information, not a verdict: the inventory is not a step that blocks the
    # reading of a reward screen (Setup says "Your inventory" the same way).
    return [("INFO", "inventory", check.detail)]


def check_notifications(cfg) -> list[Result]:
    if not cfg.get("notifications.enabled", True):
        return [("INFO", "notifications", "turned off under Settings, Capture")]
    if readiness.notifications_held_in_fullscreen():
        return [("INFO", "notifications", readiness.FULLSCREEN_DND_HINT)]
    return []


def check_account(cfg) -> list[Result]:
    isset = bool(str(cfg.get("profile.account_id", "") or "").strip())
    results = [("INFO", "account id", "set" if isset else "not set")]
    results.append(("INFO", "profile fetch", _profile_fetch_line(cfg)))
    results.append(("INFO", "picture fetch", _picture_fetch_line(cfg)))
    results.append(("INFO", "reference update", _reference_update_line(cfg)))
    return results


def _picture_fetch_line(cfg) -> str:
    """The automatic picture fetch: on or off, the last run and how it ended."""
    import sqlite3

    from wft.data import pictures
    from wft.profile import fetch

    state = "on" if pictures.enabled(cfg) else "off (switched off)"
    db_path = cfg.db_path
    if not db_path.is_file():
        return f"{state}; no database yet"
    try:
        connection = _read_only(db_path)
    except sqlite3.Error as exc:
        return f"{state}; the run log cannot be read ({exc})"
    try:
        row = (fetch.last(connection, pictures.JOB)
               if _has_table(connection, "fetch_log") else None)
    except sqlite3.Error as exc:
        return f"{state}; the run log cannot be read ({exc})"
    finally:
        connection.close()
    if row is None:
        return f"{state}; never run"
    return f"{state}; last {row['at']} UTC ({row['kind']}): {row['detail']}"


def _reference_update_line(cfg) -> str:
    """The automatic reference data update: on or off, the last run and how it ended."""
    import sqlite3

    from wft.data import reference
    from wft.profile import fetch

    state = "on" if reference.enabled(cfg) else "off (switched off)"
    db_path = cfg.db_path
    if not db_path.is_file():
        return f"{state}; no database yet"
    try:
        connection = _read_only(db_path)
    except sqlite3.Error as exc:
        return f"{state}; the run log cannot be read ({exc})"
    try:
        row = (fetch.last(connection, reference.JOB)
               if _has_table(connection, "fetch_log") else None)
    except sqlite3.Error as exc:
        return f"{state}; the run log cannot be read ({exc})"
    finally:
        connection.close()
    if row is None:
        return f"{state}; never run"
    return f"{state}; last {row['at']} UTC ({row['kind']}): {row['detail']}"


def _read_only(path):
    """The database opened read-only: the doctor reports and never writes.

    `dbq.connect` would add tables an older database lacks, which is a write.
    """
    import sqlite3

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _has_table(connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,)).fetchone() is not None


def _profile_fetch_line(cfg) -> str:
    """The automatic profile fetch: on or off, the last request, how many in 24 h."""
    import sqlite3
    from datetime import datetime, timezone

    from wft.profile import fetch

    if not fetch.valid_id(cfg.get("profile.account_id", "")):
        state = "off (no valid account id)"
    elif not cfg.get("profile.auto_fetch", True):
        state = "off (switched off)"
    else:
        state = "on"
    db_path = cfg.db_path
    if not db_path.is_file():
        return f"{state}; no database yet"
    try:
        connection = _read_only(db_path)
    except sqlite3.Error as exc:
        return f"{state}; the request log cannot be read ({exc})"
    try:
        if _has_table(connection, "fetch_log"):
            row = fetch.last(connection)
            recent = fetch.count_recent(connection, datetime.now(timezone.utc))
        else:
            row, recent = None, 0
    except sqlite3.Error as exc:
        return f"{state}; the request log cannot be read ({exc})"
    finally:
        connection.close()
    if row is None:
        return f"{state}; never fetched; 0 request(s) in 24 h"
    code = f"HTTP {row['status']}" if row["status"] is not None else "no answer"
    return (f"{state}; last {row['at']} UTC ({row['kind']}) {code}; "
            f"{recent} request(s) in 24 h")


def check_logfile(cfg) -> list[Result]:
    from wft import logfile
    return [("INFO", "log file", str(logfile.current_file(cfg)))]


def _guard(name: str, fn: Callable[[], list[Result]]) -> list[Result]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - the doctor must not die on its patient
        return [("FAIL", name, f"check crashed: {type(exc).__name__}: {exc}")]


def run() -> list[Result]:
    results: list[Result] = []
    for name, fn in (("python", check_python), ("modules", check_modules),
                     ("tools", check_tools), ("packages", check_packages),
                     ("session", check_session), ("input", check_input)):
        results += _guard(name, fn)
    try:
        from wft import config
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001
        results.append(("FAIL", "config", f"cannot load: {type(exc).__name__}: {exc}"))
        return results
    for name, fn in (("config", check_config), ("regions", check_regions),
                     ("EE.log", check_ee_log),
                     ("database", check_data), ("inventory", check_inventory),
                     ("game window", check_game),
                     ("notifications", check_notifications),
                     ("account id", check_account),
                     ("log file", check_logfile)):
        results += _guard(name, lambda fn=fn: fn(cfg))
    return results


def main() -> int:
    print(f"warframe-tracker doctor - version {version.number()} "
          f"({version.describe()}), committed {version.committed_at()}")
    results = run()
    width = max(len(name) for _, name, _ in results)
    for level, name, detail in results:
        print(f"{level:<5}{name:<{width}}  {detail}".rstrip())
    print("\nIf something is wrong, send this whole output to whoever gave you the tool,"
          "\nor to the place you downloaded it from.")
    return 1 if any(level == "FAIL" for level, _, _ in results) else 0
