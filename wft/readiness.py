"""What is set up on this machine and what is not: one answer for every reader.

`wf doctor` prints it for the terminal, the Setup page draws it as cards and
the Rewards page decides from it whether to say "Ready". Three readers asking
the same question through three pieces of code is how a page ends up saying
"Ready" while the diagnosis beside it says the hotkey cannot work (the first
newcomer was told "Ready - press F23" with no data and no usable key). So each
question is asked here, once, and every reader formats the answer.

Like `wft.doctor` this imports nothing outside the standard library when it
loads: every dependency is probed inside its own check, because the checks
have to work when the thing they ask about is broken.

A `Check` carries a `state` word (what is the case), `ok` (is that good
enough to go on), `detail` (the plain facts, as `wf doctor` prints them) and
`todo` (what to do, short, when it is not ok). Nothing here writes a file or
touches the network.
"""
from __future__ import annotations

import grp
import os
import pwd
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Check:
    name: str            # data, input, hotkey, ee_log, window, inventory
    state: str           # the word for the case, per check
    ok: bool
    detail: str
    todo: str = ""
    info: dict = field(default_factory=dict)


def current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def input_group_state(user: str | None = None) -> str:
    """`missing`, `pending` (listed, not yet active) or `active`, for the 'input' group.

    The middle state is the common trap: `usermod -aG` changes /etc/group at
    once, but the running session keeps its old groups until the next login.
    install.sh asks the same question in shell; the three words are the
    contract between the two.
    """
    user = user or current_user()
    try:
        entry = grp.getgrnam("input")
    except KeyError:
        return "missing"
    listed = user in entry.gr_mem or pwd.getpwnam(user).pw_gid == entry.gr_gid
    if not listed:
        return "missing"
    return "active" if entry.gr_gid in os.getgroups() else "pending"


# -- the checks -------------------------------------------------------------

def data_check(cfg) -> Check:
    """Is the game data (items, relics, drop tables) built, and how old."""
    from wft.data import status

    data = status.read(cfg, read_only=True)
    todo = "download the game data"
    if not data.readable:
        return Check("data", "missing", False,
                     "not built yet - Setup -> Download game data", todo)
    if data.last_update is None:
        # The database file exists (the program creates it on start) but no
        # update ever ran: there is nothing in it to match names against.
        return Check("data", "unrecorded", False,
                     "built, but no update time recorded", todo)
    stamp = data.last_update.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    days = data.age_days
    remind = int(cfg.get("update.remind_after_days", 14) or 0)
    detail = f"last update {stamp} ({days} days ago)"
    info = {"stamp": stamp, "days": days, "remind": remind}
    if remind and days > remind:
        return Check("data", "old", True,
                     f"{detail}, older than the {remind}-day reminder", "", info)
    return Check("data", "ok", True, detail, "", info)


def readable_key_devices() -> int | None:
    """How many input devices this account can read, None without python-evdev."""
    from wft.capture import keys

    if keys.evdev is None:
        return None
    devices = list(keys.key_devices())
    for device in devices:
        device.close()
    return len(devices)


def input_check(user: str | None = None) -> Check:
    """May the tool read the keyboard: group `input`, or any readable device.

    Reading a device is what counts, the group is how one normally gets there.
    So a machine where a device is readable anyway is ready, and one where the
    group is listed but the session is older than the change is not.
    """
    state = input_group_state(user)
    devices = readable_key_devices()
    info = {"group": state, "devices": devices, "user": user or current_user()}
    if devices is None:
        return Check("input", "no_evdev", False, "python-evdev missing",
                     "install python-evdev", info)
    if devices > 0:
        return Check("input", "active", True,
                     f"{devices} readable input device(s)", "", info)
    if state == "pending":
        return Check("input", "pending", False,
                     "member of 'input', but not active yet - log out and back in",
                     "log out and back in so keyboard access starts", info)
    if state == "missing":
        return Check("input", "missing", False,
                     "not a member of 'input'; the hotkey cannot read input devices",
                     "give yourself keyboard access", info)
    return Check("input", "no_device", False, "no readable key device",
                 "no keyboard device could be read", info)


def hotkey_check(cfg) -> Check:
    """Is a hotkey configured, and is it a name the listener understands."""
    from wft.capture import keys

    key = str(cfg.get("hotkey.key", "") or "").strip()
    info = {"key": key}
    if not key:
        return Check("hotkey", "unset", False, "none configured",
                     "choose a hotkey", info)
    if keys.evdev is None:
        return Check("hotkey", "unchecked", False,
                     f"{key} (not validated, python-evdev missing)",
                     "install python-evdev", info)
    if keys.key_code(key) is None:
        return Check("hotkey", "unknown", False,
                     f"{key!r} is not a known evdev key name",
                     "choose the hotkey again", info)
    return Check("hotkey", "ok", True, key, "", info)


def ee_log_check(cfg) -> Check:
    """Does paths.ee_log exist; if not, does a search of the Steam libraries find one."""
    from wft import gamelog

    path = cfg.path("paths.ee_log")
    try:
        modified = path.stat().st_mtime
    except OSError:
        found = gamelog.find_for(cfg)
        return Check("ee_log", "missing", False,
                     f"not found at {path}", "", {"path": path, "found": found})
    age = (time.time() - modified) / 86400
    when = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
    return Check("ee_log", "found", True,
                 f"found, last written {when} ({age:.0f} days ago)", "",
                 {"path": path, "when": when, "days": age})


def window_check(cfg) -> Check:
    """Is the game window there right now (the tool finds it by name)."""
    if not os.environ.get("DISPLAY"):
        return Check("window", "no_display", False, "no DISPLAY, cannot look")
    try:
        from wft.capture import window

        found = window.find_game(str(cfg.get("game.window_class", "")))
    except Exception as exc:  # noqa: BLE001 - CaptureError, or X11 not loadable
        return Check("window", "error", False, str(exc))
    if found is None:
        return Check("window", "absent", False, "not found - game not running?")
    return Check("window", "found", True, f"{found.width}x{found.height} {found.wm_class!r}",
                 "", {"width": found.width, "height": found.height})


def _local_day(stamp: str) -> str | None:
    """The journal writes UTC (`datetime('now')`); the card names the user's day."""
    try:
        moment = datetime.strptime(str(stamp)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")


def inventory_check(cfg, connection=None) -> Check:
    """What the last recording or screenshot import left in the inventory.

    The Setup card "Your inventory" and the status line of the Import page both
    say it from this answer, so they cannot say two things. `state` is `empty`
    (nothing from an import: the inventory is empty or hand-entered) or `held`;
    a hint, never a required step, so `ok` only says there is something.

    `info`: `parts` (pieces held), `kinds` (how many different ones), `items`
    (built items held) and `when` (the local day of the newest import that was
    not taken back, "YYYY-MM-DD", or None when the journal has none - a database
    older than the journal). Pass an open `connection` to reuse it; without
    one the database is opened read-only and not created.
    """
    import sqlite3

    from wft.config import DB_NAME
    from wft.data import db as dbq

    info = {"parts": 0, "kinds": 0, "items": 0, "when": None}
    own = None
    try:
        if connection is None:
            path = cfg.data_dir_path() / DB_NAME
            if not path.is_file():
                return Check("inventory", "empty", False,
                             "nothing imported yet", "import your inventory", info)
            own = connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        summary = dbq.inventory_summary(connection)
        parts = summary["parts"].get("video")
        items = summary["items"].get("video")
        info.update(parts=parts[1] if parts else 0, kinds=parts[0] if parts else 0,
                    items=items[1] if items else 0)
        try:
            row = connection.execute(
                "SELECT MAX(written_at) FROM imports "
                "WHERE kind = 'scan' AND undone_at IS NULL").fetchone()
            info["when"] = _local_day(row[0]) if row and row[0] else None
        except sqlite3.Error:
            pass        # a database from before the journal: no date, not no inventory
    except sqlite3.Error:
        return Check("inventory", "empty", False,
                     "nothing imported yet", "import your inventory", info)
    finally:
        if own is not None:
            own.close()
    if not parts and not items:
        return Check("inventory", "empty", False,
                     "nothing imported yet", "import your inventory", info)
    when = info["when"] or "date unknown"
    return Check("inventory", "held", True,
                 f"{info['parts']} parts, {info['items']} built items, "
                 f"last import {when}", "", info)


# -- what is required -------------------------------------------------------

def required(cfg) -> list[Check]:
    """The steps without which a reward screen cannot be read, in the order to do them.

    Not the log (the tool works without it, a little less certain) and not the
    game window (it only exists while the game runs).
    """
    return [data_check(cfg), input_check(), hotkey_check(cfg)]


def open_required(cfg) -> list[Check]:
    return [check for check in required(cfg) if not check.ok]


def ready(cfg) -> bool:
    return not open_required(cfg)


#: Where KDE's own switch is, in the words of its settings page (Plasma 6.7.5,
#: kcm_notifications). Said by `wf doctor` and on the Settings page.
FULLSCREEN_DND_HINT = (
    "KDE holds notifications back while a fullscreen window has focus, so the "
    "recommendation does not appear while the game is in front. To change "
    "that: System Settings > Notifications, Do Not Disturb mode, Enable "
    "automatically: untick \"While a fullscreen application is focused\".")


def notifications_held_in_fullscreen() -> bool | None:
    """Whether KDE Plasma mutes notifications while a fullscreen window has focus.

    Plasma 6 switches Do Not Disturb on by itself while a fullscreen
    application is focused, and the game is one, so on a default desktop the
    notification of a reading is never shown while it could help. Measured on
    Plasma 6.7.5: the default (`defaultWhenFullscreenValue_helper` in
    libnotificationmanager) returns 1, and plasmanotifyrc holds the key only
    once somebody changed it - hence `--default true`.

    None where this cannot be told: not a KDE session, or no kreadconfig6.
    Nothing is claimed about other desktops.
    """
    import shutil
    import subprocess

    if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
        return None
    tool = shutil.which("kreadconfig6")
    if tool is None:
        return None
    try:
        done = subprocess.run(
            [tool, "--file", "plasmanotifyrc", "--group", "DoNotDisturb",
             "--key", "WhenFullscreen", "--default", "true"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip().lower()
    return value == "true" if value in ("true", "false") else None
