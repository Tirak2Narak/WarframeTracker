#!/usr/bin/env python3
"""Render the pictures of the guide (docs/guide/*.png) from the real interface.

    packaging/guide_shots.py [--source-db FILE] [--out DIR]

Release-build tool, not part of the release archive. It builds the program's own
windows offscreen and saves what they look like, so the pictures in docs/GUIDE.md
are the interface as it is and can be made again at every release.

What keeps private things out of the pictures:

* The reference data (items, relics, drop tables) is a copy of the database of
  the machine that builds the release; everything personal in it (inventory, import journal, remembered
  grid) is deleted, and a small fixed sample inventory takes its place.
* The pictures are rendered inside a user and mount namespace (`unshare -rmn`)
  with an empty tmpfs on /home and HOME=/home/player, so no path in a picture
  carries a real user name, and with no network. The program's folder is
  bound to another place first, because /home is hidden afterwards.
* Standard theme, standard text size, the system font, a 1600x900 window.
* The time is frozen where it reaches a picture (the age of the game data) and
  the commit shown on the Settings page is a placeholder.

The pictures that need something from the running game are drawn: a reward
screen painted with Qt (`draw_reward_screen`), read by the same code that reads
the real ones.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The window every page picture is taken of.
WINDOW = (1600, 900)

#: The user name inside the namespace. Shown in paths, so it says what it is.
PLAYER = "player"

#: The state of the sample game data, and the day the pictures pretend it is.
DATA_STAMP = "2026-10-14T12:00:00+00:00"
FROZEN_NOW = "2026-10-14T18:00:00+00:00"
LAST_IMPORT = "2026-10-12 18:40:00"
MASTERY_IMPORT = "2026-10-14 08:30:00"
VAULT_REFRESH = "2026-10-14 12:00:00"

# -- the sample inventory ----------------------------------------------------
#
# Names only; the unique names are looked up in the copied database, so the
# sample follows the game's data. Chosen so that every page has something to
# say: a set one piece short, a complete set, spare parts, built and mastered
# items, and relics that drop what is missing.

#: set name -> {part name: count}. A part count of the set's own parts.
PARTS = {
    "Ember Prime": {"Ember Prime Blueprint": 1, "Ember Prime Neuroptics": 1,
                    "Ember Prime Chassis": 1},
    "Frost Prime": {"Frost Prime Blueprint": 1, "Frost Prime Neuroptics": 2,
                    "Frost Prime Chassis": 1, "Frost Prime Systems": 1},
    "Loki Prime": {"Loki Prime Blueprint": 1, "Loki Prime Chassis": 1},
    "Mesa Prime": {"Mesa Prime Neuroptics": 1},
    "Nikana Prime": {"Nikana Prime Blueprint": 1, "Nikana Prime Blade": 1},
    "Soma Prime": {"Soma Prime Barrel": 2},
    "Braton Prime": {"Braton Prime Stock": 1, "Braton Prime Receiver": 2},
}
#: item name -> (built copies, mastered)
ITEMS = {
    "Rhino Prime": (1, True),
    "Soma Prime": (1, True),
    "Braton Prime": (1, False),
    "Ash Prime": (0, True),
}
#: part name -> copies of the first relic that drops it.
RELICS = {
    "Ember Prime Systems": 3, "Volt Prime Blueprint": 2,
    "Mesa Prime Chassis": 5, "Loki Prime Neuroptics": 1,
    "Nikana Prime Hilt": 2, "Mesa Prime Systems": 4,
}


def prepare_reference(source: Path, target: Path) -> None:
    """A copy of the reference data with nothing personal in it, plus the sample."""
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        live.backup(copy)
        copy.close()
    finally:
        live.close()
    db = sqlite3.connect(target)
    db.row_factory = sqlite3.Row
    try:
        for table in ("owned_parts", "owned_items", "owned_relics", "imports",
                      "observations", "learned_ignores", "kept_items"):
            db.execute(f"DELETE FROM {table}")
        db.execute("DELETE FROM sqlite_sequence")
        # Item pictures are fetched for what the build machine's inventory
        # holds, so they say what that is; a newcomer has none until the first
        # import.
        db.execute("DELETE FROM thumbnails")
        # Prices looked up for that inventory say what it is.
        db.execute("DELETE FROM market_prices")
        # The remembered grid and the scan revision belong to the build machine's
        # screen; a newcomer starts without them.
        db.execute("DELETE FROM meta WHERE key IN ('tile_grid', 'scan_revision')")
        # The sample import was read by the reader that is installed, so the
        # Import page does not call it out of date.
        sys.path.insert(0, str(ROOT))
        from wft.inventory.scan import SCAN_REVISION

        db.execute("INSERT INTO meta(key, value) VALUES('scan_revision', ?)",
                   (str(SCAN_REVISION),))
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', ?)",
                   (DATA_STAMP,))

        def part(name):
            row = db.execute("SELECT unique_name FROM parts WHERE name = ? "
                             "AND alias_of IS NULL", (name,)).fetchone()
            if row is None:
                raise SystemExit(f"sample inventory: no part named {name!r} in "
                                 f"the reference data")
            return row[0]

        def item(name):
            row = db.execute("SELECT unique_name FROM items WHERE name = ?",
                             (name,)).fetchone()
            if row is None:
                raise SystemExit(f"sample inventory: no item named {name!r}")
            return row[0]

        for held in PARTS.values():
            for name, count in held.items():
                db.execute("INSERT INTO owned_parts(part_unique, count, source, "
                           "updated_at) VALUES(?,?,'video',?)",
                           (part(name), count, DATA_STAMP))
        for name, (count, mastered) in ITEMS.items():
            db.execute("INSERT INTO owned_items(item_unique, count, mastered, "
                       "source, updated_at) VALUES(?,?,?,'video',?)",
                       (item(name), count, int(mastered), DATA_STAMP))
        for name, count in RELICS.items():
            rows = db.execute(
                "SELECT rl.unique_name FROM relic_rewards rr JOIN relics rl "
                "ON rl.unique_name = rr.relic_unique WHERE rr.part_unique IN "
                "(SELECT unique_name FROM parts WHERE unique_name = ? OR alias_of = ?) "
                "ORDER BY rl.vaulted, rl.name, rl.unique_name LIMIT 1",
                (part(name), part(name))).fetchall()
            if not rows:
                raise SystemExit(f"sample inventory: no relic drops {name!r}")
            db.execute("INSERT INTO owned_relics(relic_unique, count, source, "
                       "updated_at) VALUES(?,?,'video',?)",
                       (rows[0][0], count, DATA_STAMP))
        db.execute("INSERT INTO imports(written_at, source, kind, scan_revision, "
                   "tiles, questions) VALUES(?, 'sample', 'scan', ?, 40, 2)",
                   (LAST_IMPORT, SCAN_REVISION))
        # The build machine's own request and import times would date the
        # pictures by the day they are made; the sample has its own.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'fetch_log'").fetchone():
            db.execute("DELETE FROM fetch_log")
        db.execute("DELETE FROM meta WHERE key = 'profile_snapshot_at'")
        for key, stamp in (("mastery_imported_at", MASTERY_IMPORT),
                           ("vaulted_updated_at", VAULT_REFRESH)):
            db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, stamp))
        db.commit()
        db.execute("VACUUM")
    finally:
        db.close()


#: What the painted reward screen shows. The game lists the parts of a Warframe
#: as blueprints ("Ember Prime Systems Blueprint"), and so does the sample.
REWARD_NAMES = ["Ember Prime Systems Blueprint", "Frost Prime Neuroptics Blueprint",
                "Mesa Prime Chassis Blueprint", "Volt Prime Blueprint"]


# -- outer stage: prepare, then render inside the namespace ---------------------

#: Run by `unshare`: bind the program folder out of the way of /home, hide
#: /home, and start the renderer as the sample user with a clean environment.
_NAMESPACE_SCRIPT = r'''
set -e
work=$1; src=$2
mkdir -p "$work/src"
mount --bind "$src" "$work/src"
mount -t tmpfs none /home
mkdir -p /home/%(player)s
exec env -i PATH="$PATH" HOME=/home/%(player)s USER=%(player)s LOGNAME=%(player)s \
    LC_ALL=C.UTF-8 TZ=UTC QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1 \
    python3 "$work/src/packaging/guide_shots.py" --inner --work "$work" --only "$3"
''' % {"player": PLAYER}


def render(source_db: Path, out: Path, only: str = "") -> list[Path]:
    """Prepare the data, render in the namespace, optimise and return the files."""
    work = Path(tempfile.mkdtemp(prefix="wft-guide-"))
    try:
        prepare_reference(source_db, work / "reference.sqlite")
        (work / "out").mkdir()
        # A namespace check first, with a message that says what to do: some
        # systems forbid unprivileged user namespaces.
        probe = subprocess.run(["unshare", "-rmn", "true"], capture_output=True)
        if probe.returncode != 0:
            raise SystemExit("`unshare -rmn` does not work here (unprivileged "
                             "user namespaces are needed): "
                             + probe.stderr.decode(errors="replace").strip())
        done = subprocess.run(
            ["unshare", "-rmn", "sh", "-c", _NAMESPACE_SCRIPT, "sh",
             str(work), str(ROOT), only])
        if done.returncode != 0:
            raise SystemExit(f"rendering failed (exit {done.returncode})")
        out.mkdir(parents=True, exist_ok=True)
        made = sorted((work / "out").glob("*.png"))
        if not made:
            raise SystemExit("rendering made no pictures")
        # Lossless and byte-stable: optipng never changes a pixel.
        optimiser = shutil.which("optipng")
        files = []
        for picture in made:
            if optimiser:
                subprocess.run([optimiser, "-quiet", "-o5", "-strip", "all",
                                str(picture)], check=True)
            target = out / picture.name
            shutil.copyfile(picture, target)
            files.append(target)
        if not optimiser:
            print("note: optipng not found; the pictures are not optimised",
                  file=sys.stderr)
        return files
    finally:
        # The scratch folder is ours; find -delete cannot reach further than it.
        subprocess.run(["find", str(work), "-delete"], check=False,
                       stderr=subprocess.DEVNULL)


# -- inner stage: the pictures -----------------------------------------------------

def draw_reward_screen(names, width=1920, height=1080):
    """A BGR picture of a reward screen: four cards, the names under the art.

    Painted with Qt, in the places the program reads (`wft.relic.regions`); the
    card art is a plain shape. Not the game's own artwork.
    """
    import numpy
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
    from PySide6.QtCore import QPointF
    from wft.relic import regions

    image = QImage(width, height, QImage.Format_RGB888)
    image.fill(QColor("#070b12"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    top, bottom = regions.TILE_TOP * height, (regions.TILE_TOP + regions.TILE_HEIGHT) * height
    font = QFont("Noto Sans")
    font.setPixelSize(round(0.0139 * height))
    painter.setFont(font)
    for index, name in enumerate(names):
        tile = regions.tile(index, len(names))
        card = QRectF((tile.x + 0.004) * width, top,
                      (tile.width - 0.008) * width, bottom - top)
        painter.setPen(QPen(QColor("#33415a"), 2))
        painter.setBrush(QColor("#0d1522"))
        painter.drawRoundedRect(card, 6, 6)
        # The "art": a diamond, so the card is not an empty box.
        cx, cy, r = card.center().x(), card.top() + card.height() * 0.36, card.width() * 0.2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#1d2c44"))
        painter.drawPolygon(QPolygonF([QPointF(cx, cy - r), QPointF(cx + r, cy),
                                       QPointF(cx, cy + r), QPointF(cx - r, cy)]))
        painter.setPen(QColor("#d8dde6"))
        # The names sit where the program reads them (second line of the band).
        painter.drawText(
            QRectF(tile.x * width, 0.401 * height, tile.width * width,
                  0.024 * height),
            Qt.AlignCenter, name)
    painter.end()
    rows = numpy.frombuffer(image.constBits(), numpy.uint8).reshape(
        height, image.bytesPerLine())[:, :width * 3].reshape(height, width, 3)
    return numpy.ascontiguousarray(rows[:, :, ::-1])


def _png_bytes(image) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def draw_tile(name: str, size: int = 190) -> bytes:
    """PNG of one inventory tile with its name, for the review's pictures."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPolygonF
    from PySide6.QtCore import QPointF

    image = QImage(size, size, QImage.Format_RGB888)
    image.fill(QColor("#0d1522"))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#1d2c44"))
    cx, cy, r = size / 2, size * 0.36, size * 0.2
    painter.drawPolygon(QPolygonF([QPointF(cx, cy - r), QPointF(cx + r, cy),
                                   QPointF(cx, cy + r), QPointF(cx - r, cy)]))
    font = QFont("Noto Sans")
    font.setPixelSize(round(size * 0.1))
    painter.setFont(font)
    painter.setPen(QColor("#d8dde6"))
    painter.drawText(QRectF(4, size * 0.68, size - 8, size * 0.3),
                     Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, name)
    painter.setPen(QPen(QColor("#33415a"), 2))
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(1, 1, size - 2, size - 2)
    painter.end()
    return _png_bytes(image)


class Shots:
    """The inner stage: one process, one QApplication, pictures one after another."""

    def __init__(self, work: Path):
        self.work = work
        self.out = work / "out"
        self.home = Path.home()
        if str(self.home) != f"/home/{PLAYER}":
            raise SystemExit(f"refusing to render: HOME is {self.home}, not "
                             f"/home/{PLAYER}; run through guide_shots.py")
        for variable in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            os.environ.pop(variable, None)
        sys.path.insert(0, str(ROOT))
        sys.path.insert(0, str(ROOT / "tests"))
        from PySide6.QtWidgets import QApplication

        import test_ui_smoke as T

        self.app = QApplication.instance() or QApplication([])
        # A dialog nobody answers would wait for ever in a script.
        T._quiet()
        self._freeze_time()
        self._offline()
        self.made: list[str] = []
        self.patches = None

    # -- the world around the program ---------------------------------------
    def _freeze_time(self) -> None:
        """The age of the game data reaches a picture; pin it to the sample day."""
        from datetime import datetime

        from wft import version
        from wft.data import status

        frozen = datetime.fromisoformat(FROZEN_NOW)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz is not None else frozen.replace(tzinfo=None)

        status.datetime = FrozenDatetime
        # The Trading pages say how old the stock is and date the message.
        from ui.views import trading_view

        trading_view._now = lambda: frozen
        trading_view._today = lambda: frozen.date()
        # The status line counts the age of imports and vault status in SQL,
        # from SQLite's own clock.
        from wft.data import db as dbq

        clock = frozen.strftime("%Y-%m-%d %H:%M:%S")

        def days_since(connection, stamp):
            if not stamp:
                return None
            row = connection.execute("SELECT CAST(julianday(?) - julianday(?) AS INTEGER)",
                                     (clock, stamp)).fetchone()
            return int(row[0]) if row and row[0] is not None else None

        dbq.days_since = days_since
        version.describe = lambda: "a1b2c3d"
        version.committed_at = lambda: "2026-10-14 12:00"

    def _offline(self) -> None:
        """Nothing in the pictures reaches anybody's server (there is no network anyway)."""
        from wft.riven import prices

        prices.refresh_if_stale = lambda *a, **k: None

    def reset_patches(self):
        import test_ui_smoke as T

        if self.patches is not None:
            self.patches.restore()
        self.patches = T._Patches()
        return self.patches

    # -- the configurations ----------------------------------------------------
    def config(self, *, data: bool, key: str = "", log: bool = False):
        """A configuration over its own data folder, in the sample home."""
        from wft import config as config_mod

        data_dir = Path(tempfile.mkdtemp(prefix="data-", dir=self.home))
        if data:
            shutil.copyfile(self.work / "reference.sqlite", data_dir / "warframe.sqlite")
        cfg = config_mod.load()
        cfg.override("paths.data_dir", str(data_dir))
        cfg.override("hotkey.key", key)
        if log:
            path = cfg.path("paths.ee_log")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
            stamp = datetime_stamp("2026-10-13 20:15:00")
            os.utime(path, (stamp, stamp))
        return cfg

    def machine(self, group: str, devices: int | None, user: str = PLAYER) -> None:
        """What the keyboard side looks like, as `readiness` is told."""
        from wft import readiness

        patches = self.reset_patches()
        patches.set(readiness, "input_group_state", lambda user=None: group)
        patches.set(readiness, "readable_key_devices", lambda: devices)
        patches.set(readiness, "current_user", lambda: user)
        # A display name that leads nowhere: `window_check` wants some DISPLAY to
        # look for the game, and the program must never open a connection to
        # the real desktop of whoever runs this. The game is answered by
        # `no_game` / `game_screen` instead.
        os.environ["DISPLAY"] = ":99"
        self.no_game()

    def no_game(self) -> None:
        from wft.capture import window as cap

        self.patches.set(cap, "find_game", lambda _class, display=None: None)

    def game_screen(self, names, width=1920, height=1080):
        import test_ui_smoke as T

        screen = draw_reward_screen(names, width, height)
        T._fake_game(self.patches, screen)
        return screen

    # -- windows and pictures -----------------------------------------------------
    def window(self, cfg, page: str):
        from ui import app as app_mod

        _app, window = app_mod.build_application(cfg, page)
        window.resize(*WINDOW)
        window.show()
        self.settle()
        return window

    def close(self, window) -> None:
        import test_ui_smoke as T

        window.close()
        window.deleteLater()
        T._settle(self.app)

    def wait_for_fades(self, panel) -> None:
        """The reward cards fade in one behind the next; wait until they are all in."""
        import test_ui_smoke as T
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        def done() -> bool:
            for card in panel.cards:
                effect = card.graphicsEffect()
                if (card.isVisible() and isinstance(effect, QGraphicsOpacityEffect)
                        and effect.opacity() < 1.0):
                    return False
            return True

        T._wait(self.app, done, seconds=5.0)
        # The first frame at full opacity is not painted until the loop turns.
        self.settle(20)

    def settle(self, rounds: int = 12) -> None:
        for _ in range(rounds):
            self.app.processEvents()

    def save(self, widget, name: str) -> None:
        from PySide6.QtGui import QImage

        self.settle()
        image = widget.grab().toImage().convertToFormat(QImage.Format_RGB888)
        if not image.save(str(self.out / name)):
            raise SystemExit(f"could not save {name}")
        self.made.append(name)
        print(f"  {name}  {image.width()}x{image.height()}")

    # -- the scenes --------------------------------------------------------------------
    def setup_pages(self) -> None:
        """Setup: first start, part way, done; the cards that need a picture."""
        from wft.relic import last_screen

        last_screen.forget()

        # 1. The first start: nothing downloaded, no group, no key.
        self.machine("missing", 0)
        self.no_game()
        cfg = self.config(data=False)
        window = self.window(cfg, "Setup")
        self.save(window, "setup-first-start.png")
        setup = window._pages["Setup"]
        self.save(setup.cards["data"], "card-data.png")
        self.save(setup.cards["input"], "card-input-missing.png")
        self.close(window)

        # 2. Part way: data there, in the group but the session is older, key chosen.
        self.machine("pending", 0)
        self.no_game()
        cfg = self.config(data=True, key="F8")
        window = self.window(cfg, "Setup")
        setup = window._pages["Setup"]
        self.save(window, "setup-part-way.png")
        self.save(setup.cards["input"], "card-input-pending.png")
        self.close(window)

        # 3. The hotkey, being chosen and then heard.
        self.hotkey_cards()

        # 4. Done: everything required, the game running, a reward screen read.
        self.machine("active", 4)
        cfg = self.config(data=True, key="F8", log=True)
        window = self.window(cfg, "Setup")
        setup = window._pages["Setup"]
        self.no_game()
        setup.refresh()
        self.save(setup.cards["ee_log"], "card-log.png")
        self.save(setup.cards["window"], "card-window-absent.png")
        self.save(setup.cards["reward"], "card-reward-waiting.png")
        self.save(setup.cards["inventory"], "card-inventory-held.png")
        self.save(window, "setup-done.png")
        self.patches.restore()
        self.machine("active", 4)
        self.game_screen(REWARD_NAMES)
        setup.refresh()
        self.save(setup.cards["window"], "card-window-found.png")
        self.close(window)

    def hotkey_cards(self) -> None:
        import test_ui_smoke as T
        from wft.capture import keys

        self.machine("active", 4)
        self.no_game()
        keyboard = T._FakeKeyboard()
        T._fake_devices(self.patches, keyboard)
        cfg = self.config(data=True, key="")
        window = self.window(cfg, "Setup")
        setup = window._pages["Setup"]
        picker = setup.key_picker
        picker.start_pick()
        self.settle()
        self.save(setup.cards["hotkey"], "card-hotkey-waiting.png")
        keyboard.press(keys.ecodes.KEY_F8)
        T._wait(self.app, lambda: cfg.get("hotkey.key") == "F8")
        keyboard.press(keys.ecodes.KEY_F8)
        T._wait(self.app, lambda: "Received" in picker.message.text())
        self.save(setup.cards["hotkey"], "card-hotkey-received.png")
        picker.cancel("")
        self.close(window)
        keyboard.really_close()

    def rewards_pages(self) -> None:
        """Rewards: not ready, ready, with a result; the calibration dialog."""
        from ui.workers import relic_trigger as RT
        from wft.relic import last_screen

        last_screen.forget()
        # Not ready: no game data, no hotkey.
        self.machine("missing", 0)
        self.no_game()
        window = self.window(self.config(data=False), "Rewards")
        self.save(window, "rewards-not-ready.png")
        self.close(window)

        # Ready, waiting for a run.
        self.machine("active", 4)
        self.no_game()
        cfg = self.config(data=True, key="F8", log=True)
        window = self.window(cfg, "Rewards")
        self.save(window, "rewards-ready.png")

        # A read: the game window is answered from a painted reward screen.
        self.patches.restore()
        self.machine("active", 4)
        screen = self.game_screen(REWARD_NAMES)
        panel = window.relic_panel
        result = RT.run_trigger(cfg)
        if result.error:
            raise SystemExit(f"the painted reward screen was not read: {result.error}")
        panel.show_result(result)
        self.wait_for_fades(panel)
        recognised = last_screen.current().reading
        print(f"  reading: {recognised.recognised} of {recognised.total}: "
              + ", ".join(f"{r.raw_text!r}->{r.name!r}"
                          for r in result.verdict.rewards))
        self.save(window, "rewards-result.png")

        # The calibration dialog with the last screen in it.
        from ui.views import calibrate_dialog as dialog_mod

        dialog = dialog_mod.CalibrateDialog(cfg)
        dialog.resize(1100, 760)
        dialog.show()
        self.settle(30)
        self.save(dialog, "calibrate-dialog.png")
        import test_ui_smoke as T
        T._drop(self.app, dialog)
        self.close(window)
        del screen

    def import_pages(self) -> None:
        """Import: the page, the review, the Apply table."""
        import test_ui_smoke as T
        from wft.data import db as dbq
        from wft.inventory import scan as scan_mod
        from wft.inventory import pagemodel as pm

        self.machine("active", 4)
        cfg = self.config(data=True, key="F8", log=True)
        window = self.window(cfg, "Import")
        page = window._pages["Import"]
        self.save(window, "import-start.png")

        # The review of a made-up scan.
        result = self.fake_scan(cfg, dbq, scan_mod, pm)
        page._hand_over(result)
        self.settle(20)
        self.save(window, "import-review.png")

        # Answer the open question with the first candidate, then the Apply table.
        page._on_digit(1)
        self.settle()
        page.visited.update(range(len(page.clusters)))
        page._finish_review()
        self.settle(20)
        self.save(window, "import-apply.png")
        T._settle(self.app)
        self.close(window)

    def fake_scan(self, cfg, dbq, scan_mod, pm):
        """A scan result for the review: tiles that read well and two that need an answer."""
        connection = dbq.connect(cfg.db_path)

        def part(name):
            return connection.execute(
                "SELECT unique_name FROM parts WHERE name = ? AND alias_of IS NULL",
                (name,)).fetchone()[0]

        def item(name):
            return connection.execute("SELECT unique_name FROM items WHERE name = ?",
                                      (name,)).fetchone()[0]

        def relic(name):
            return connection.execute(
                "SELECT unique_name FROM relics WHERE name = ? ORDER BY unique_name",
                (name,)).fetchone()[0]

        candidates = []
        position = 0

        def add(name, reading=None, score=100.0, lead=30.0, **kwargs):
            nonlocal position
            row, column = divmod(position, 6)
            position += 1
            candidates.append(scan_mod.Candidate(
                column=column, row=row, name=name, score=score,
                reading=reading or name, sightings=3, lead=lead,
                sources=frozenset({1}), evidence=draw_tile(reading or name),
                **kwargs))

        # Tiles that need an answer: a damaged reading, and a tie between two names.
        add("Ember Prime Systems", "Ember Prime Syst3ms", score=88.0, lead=2.0,
            part_unique=part("Ember Prime Systems"), quantity=1,
            doubt="the reading is too damaged to be sure")
        add("Mesa Prime Chassis", "Mesa Prime Chass", score=90.0, lead=1.5,
            part_unique=part("Mesa Prime Chassis"), quantity=1)
        for name in ("Ember Prime Chassis", "Frost Prime Systems", "Loki Prime Blueprint",
                     "Nikana Prime Blade", "Soma Prime Barrel", "Braton Prime Stock",
                     "Mesa Prime Neuroptics"):
            add(name, part_unique=part(name), quantity=1)
        for name in ("Rhino Prime", "Ash Prime"):
            add(name, item_unique=item(name))
        for name in ("Axi E1 Relic", "Neo N12 Relic"):
            add(name, relic_unique=relic(name), quantity=3, refinement="INTACT")
        connection.close()
        grid = pm.Grid(column_pitch=208.0, column_phase=380.0, row_pitch=221.0,
                       row_phase=300.0, screen_row_pitch=221.0, confidence=0.9)
        return scan_mod.ScanResult(
            candidates=candidates, frames_read=3, words_total=60,
            words_after_ui_filter=50, cells_found=len(candidates), grid=grid)

    def pick_item(self, collection, name: str) -> None:
        """Open one item in the Collection page, as a click on its name would."""
        listing = collection._library.item_list
        for row in range(listing.count()):
            if name in listing.item(row).text().split("   ")[0]:
                listing.setCurrentRow(row)
                return
        raise SystemExit(f"the Collection page lists no item named {name!r}")

    def other_pages(self) -> None:
        """Plan, Fissures, Collection, Ducats, Trading, Rivens, Settings, Overlays, over the sample."""
        from datetime import datetime, timedelta, timezone

        from wft.world import fissures as fissures_mod

        self.machine("active", 4)
        self.no_game()
        cfg = self.config(data=True, key="F8", log=True)
        # A board that cannot be fetched is not what the guide shows; a made-up one.
        base = datetime.now(timezone.utc) + timedelta(seconds=30)

        def at(minutes):
            return base + timedelta(minutes=minutes)

        board = [
            fissures_mod.Fissure("Lith", 1, "Everest (Earth)", "Excavation", "Grineer", at(41), False, False),
            fissures_mod.Fissure("Meso", 2, "Carpo (Jupiter)", "Extermination", "Corpus", at(23), False, False),
            fissures_mod.Fissure("Neo", 3, "Hepit (Void)", "Capture", "Orokin", at(52), False, False),
            fissures_mod.Fissure("Axi", 4, "Stribog (Void)", "Sabotage", "Orokin", at(17), False, False),
            fissures_mod.Fissure("Lith", 1, "Ur (Uranus)", "Disruption", "Grineer", at(33), True, False),
            fissures_mod.Fissure("Meso", 2, "Ukko (Void)", "Capture", "Orokin", at(44), True, False),
            fissures_mod.Fissure("Axi", 4, "Sechura (Venus)", "Mobile Defense", "Corpus", at(28), True, False),
            fissures_mod.Fissure("Omnia", 5, "Circulus (Lua)", "Extermination", "Orokin", at(36), False, True),
        ]
        self.patches.set(fissures_mod, "active", lambda *a, **k: board)
        window = self.window(cfg, "Plan")
        self.save(window, "plan.png")
        for name, picture in (("Fissures", "fissures.png"), ("Collection", "collection.png"),
                              ("Ducats", "ducats.png"),
                              ("I want", "trading-want.png"),
                              ("I offer", "trading-offer.png"),
                              ("Rivens", "rivens.png"),
                              ("Settings", "settings.png"),
                              ("Overlays", "overlays.png")):
            window.select(name)
            if name == "Collection":
                self.pick_item(window._pages["Collection"], "Ember Prime")
            if name == "Settings":
                # The guide's picture is the tab it talks about most: Capture.
                window._pages["Settings"].show_tab("Capture")
            if name == "Fissures":
                import test_ui_smoke as T
                T._wait(self.app, lambda: window._pages["Fissures"].answered)
            self.settle(20)
            self.save(window, picture)
        self.close(window)

    def run(self, only: str = "") -> None:
        self.out.mkdir(exist_ok=True)
        scenes = [("setup", self.setup_pages), ("rewards", self.rewards_pages),
                  ("import", self.import_pages), ("other", self.other_pages)]
        wanted = {word for word in only.split(",") if word}
        for name, scene in scenes:
            if wanted and name not in wanted:
                continue
            print(f"{name} pages")
            scene()
        if self.patches is not None:
            self.patches.restore()
        import gc
        from PySide6.QtWidgets import QApplication
        import test_ui_smoke as T

        for widget in QApplication.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        T._settle(self.app)
        gc.collect()
        T._settle(self.app)
        print(f"{len(self.made)} pictures")


def datetime_stamp(text: str) -> float:
    from datetime import datetime, timezone

    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-db", type=Path,
                        help="the reference database to copy (default: the one "
                             "the configuration names)")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "guide",
                        help="where the pictures go (default: docs/guide)")
    parser.add_argument("--only", default="",
                        help="render only these scenes (setup, rewards, import, "
                             "other; comma separated), for working on one")
    parser.add_argument("--inner", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.inner:
        Shots(args.work).run(args.only)
        return 0

    source = args.source_db
    if source is None:
        sys.path.insert(0, str(ROOT))
        from wft import config as config_mod

        source = config_mod.load().db_path
    if not Path(source).is_file():
        raise SystemExit(f"no reference database at {source}; run the program's "
                         f"'Download game data' once, or pass --source-db")
    files = render(Path(source), args.out, args.only)
    total = sum(path.stat().st_size for path in files)
    print(f"{len(files)} pictures in {args.out}, {total / 1024:.0f} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
