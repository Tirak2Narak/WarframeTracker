"""The reward screens kept on disk when the user asks for it.

One place decides where a picture goes, what the folder holds and which day
folders have expired, so the hotkey that writes, the Settings line that counts
and the Rewards line that names the file cannot disagree. Nothing here imports
Qt: the trash is handed in by the caller.

Layout: `<folder>/YYYY-MM-DD/HH:MM:SS:zzz.jpg`, zzz being milliseconds, the
scheme Spectacle uses for its own screenshots. Two readings in the same
second therefore do not overwrite each other.
"""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Old flat pictures (`reward-*.jpg`) are counted too, but never aged out:
#: only folders named exactly like a day are ever moved.
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

QUALITY = 92


def configured_folder(cfg) -> Path | None:
    """The folder pictures are kept in, or None when keeping is off.

    Off unless `capture.keep_rewards` is true AND a folder is named.
    """
    if not cfg.get("capture.keep_rewards", False):
        return None
    return folder_setting(cfg)


def folder_setting(cfg) -> Path | None:
    """The configured folder whether or not keeping is on; None when empty."""
    text = str(cfg.get("capture.keep_rewards_dir", "") or "").strip()
    return Path(os.path.expanduser(text)) if text else None


def picture_path(folder: Path, when: datetime.datetime) -> Path:
    """`<folder>/YYYY-MM-DD/HH:MM:SS:zzz.jpg` for this moment."""
    return (Path(folder) / when.strftime("%Y-%m-%d")
            / (when.strftime("%H:%M:%S:") + f"{when.microsecond // 1000:03d}.jpg"))


def relative_name(folder: Path, path: Path | str) -> str:
    """The picture's name relative to the folder, as the Rewards page shows it."""
    try:
        return str(Path(path).relative_to(folder))
    except ValueError:
        return str(path)


def save(folder: Path, image, when: datetime.datetime | None = None) -> Path:
    """Write the image to its place in the folder; returns the path."""
    path = picture_path(folder, when or datetime.datetime.now())
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, quality=QUALITY)
    return path


@dataclass(frozen=True)
class Summary:
    count: int
    size: int                 # bytes
    first: str | None         # first and last day folder, YYYY-MM-DD
    last: str | None


def summarize(folder: Path) -> Summary:
    """Count the .jpg files below the folder: day folders and old flat ones."""
    count = size = 0
    days: list[str] = []
    folder = Path(folder)
    try:
        entries = list(folder.iterdir())
    except OSError:
        return Summary(0, 0, None, None)
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink() and _DAY.match(entry.name):
                found = [p for p in entry.iterdir()
                         if p.suffix.lower() == ".jpg" and p.is_file()]
                if found:
                    days.append(entry.name)
                for p in found:
                    count += 1
                    size += p.stat().st_size
            elif (entry.is_file() and entry.suffix.lower() == ".jpg"):
                count += 1
                size += entry.stat().st_size
        except OSError:
            continue
    days.sort()
    return Summary(count, size, days[0] if days else None,
                   days[-1] if days else None)


def describe(summary: Summary) -> str:
    """The status line of the Settings group."""
    if summary.count == 0:
        return "No pictures yet"
    megabytes = summary.size / 1_000_000
    text = f"{summary.count} pictures, {megabytes:.0f} MB" \
        if summary.count != 1 else f"1 picture, {megabytes:.1f} MB"
    if summary.first and summary.last:
        span = (summary.first if summary.first == summary.last
                else f"{summary.first} to {summary.last}")
        text += f", {span}"
    return text


def expired_folders(folder: Path, today: datetime.date, days: int) -> list[Path]:
    """The day folders older than `days` days; nothing when days is 0 or less.

    Only folders named exactly YYYY-MM-DD that are real dates. Files, links and
    every other folder are not the tool's and are never returned.
    """
    if days <= 0:
        return []
    limit = today - datetime.timedelta(days=days)
    expired = []
    try:
        entries = sorted(Path(folder).iterdir())
    except OSError:
        return []
    for entry in entries:
        if not _DAY.match(entry.name):
            continue
        try:
            day = datetime.date.fromisoformat(entry.name)
            if entry.is_symlink() or not entry.is_dir():
                continue
        except (ValueError, OSError):
            continue
        if day < limit:
            expired.append(entry)
    return expired


def tidy(cfg, trash, today: datetime.date | None = None,
         report=lambda _msg: None) -> list[Path]:
    """Move expired day folders to the trash; returns those that went.

    Does nothing unless keeping is on and `capture.keep_rewards_days` is above
    0. Never raises: a failure is reported and the folder stays.
    """
    moved: list[Path] = []
    try:
        folder = configured_folder(cfg)
        days = int(cfg.get("capture.keep_rewards_days", 0) or 0)
        if folder is None or days <= 0 or not folder.is_dir():
            return moved
        for path in expired_folders(folder, today or datetime.date.today(), days):
            try:
                if trash(path):
                    moved.append(path)
                else:
                    report(f"could not move {path} to the trash")
            except Exception as exc:  # noqa: BLE001 - one folder never stops the rest
                report(f"could not move {path} to the trash: {exc}")
    except Exception as exc:  # noqa: BLE001 - tidying never costs the start
        report(f"could not tidy the kept pictures: {exc}")
    return moved
