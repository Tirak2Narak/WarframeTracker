"""Setup: what the tool needs before it can read a reward screen, one card per step.

A newcomer used to find this out from `wf doctor` in a terminal, from a README,
or from a friend in a chat: the first start opened on an empty Plan page, the
Rewards page said "Ready" with no data and no usable hotkey, and the only place
that said the `input` group was not active was the command line. Every status
here comes from `wft.readiness` and `wft.gamelog`, the same functions `wf doctor`
prints, so this page and the diagnosis cannot disagree.

Three steps are required (game data, keyboard access, a hotkey) and the page is
what the application opens on while one is open. The game log and the game
window are shown because they explain things, but nothing waits for them.

The first reward screen is a card too, but a hint: it shows what the last
hotkey press read (`wft.relic.last_screen`) and offers "Calibrate with this
screen" only when that reading was bad. So is "Your inventory": what the last
import left (`readiness.inventory_check`, the answer the Import page's status
line gives too) and a button to the Import page. Steps that belong to a later
part of the setup are not drawn as "coming soon" cards: a card that promises
something the program cannot do yet is a false statement in the interface. They
are added as cards here when the feature they lead to exists.

"Open the guide" opens the illustrated guide that ships with the release
archive (`GUIDE.pdf` in the program folder) in the desktop's PDF viewer. A git
checkout has no PDF (it is built by `packaging/build_guide.py` for the archive),
so there the button opens `docs/GUIDE.md`, which is the guide's source, and the line
beside it says why.

Nothing here reaches the network except the one button that says so
("Download game data"), and nothing starts or raises another window of this
program.
"""
from __future__ import annotations

import shlex
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from ui import file_dialogs, settings_io, theme
from ui.views.settings_view import UpdateJob
from ui.widgets.diamond import Diamond
from ui.widgets.elided_label import ElidedLabel
from ui.widgets.key_picker import KeyPicker
from wft import readiness
from wft.relic import last_screen

#: How often the live parts (keyboard, window, log) are looked at while the page
#: is on screen. The cards for things the user is about to fix should notice that
#: it is fixed; nothing is polled while the page is hidden.
TICK_MS = 2000

#: The program folder: where `GUIDE.pdf` sits in an archive (`docs/GUIDE.md`,
#: its source, is only in a checkout).
PROGRAM_DIR = Path(__file__).resolve().parent.parent.parent

#: At most this many log files are offered at once.
MAX_LOG_CHOICES = 3


def age_words(days: int | None) -> str:
    if days is None:
        return ""
    return "today" if days == 0 else "1 day ago" if days == 1 else f"{days} days ago"


class StepCard(QFrame):
    """A card with a number, a title, a status word, a sentence and room for controls."""

    #: kind -> (status word, colour attribute of the theme, judgement)
    KINDS = {"done": ("Done", "GOOD", "good"),
             "todo": ("To do", "WARN", "warn"),
             "note": ("Note", "TEXT_DIM", "quiet")}

    def __init__(self, number: int, title: str, required: bool, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.number = number
        self.required = required
        self.kind = ""
        self._signature = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.card_margins())
        outer.setSpacing(theme.px(0.55))

        head = QHBoxLayout()
        head.setSpacing(theme.px(0.4))
        self.diamond = Diamond(theme.ACCENT)
        head.addWidget(self.diamond)
        self.title = QLabel(f"{number}.  {title}")
        head.addWidget(self.title)
        head.addStretch(1)
        self.badge = QLabel("")
        head.addWidget(self.badge)
        outer.addLayout(head)

        self.text = QLabel("")
        self.text.setWordWrap(True)
        outer.addWidget(self.text)

        #: Controls that belong to the step. Rebuilt only when `signature`
        #: changes, so the timer's refresh does not tear down a button under
        #: the mouse.
        self.extra = QVBoxLayout()
        self.extra.setSpacing(theme.px(0.5))
        outer.addLayout(self.extra)

    def show_state(self, kind: str, text: str, word: str | None = None) -> None:
        default, colour_name, judgement = self.KINDS[kind]
        word = word or default
        colour = getattr(theme, colour_name)
        self.kind = kind
        theme.mark(self, judgement)
        self.title.setStyleSheet(theme.section_text(colour))
        self.badge.setText(word)
        self.badge.setStyleSheet(theme.text("small", colour, 600))
        self.text.setText(text)

    def rebuild(self, signature, build) -> None:
        """Fill `extra` by calling `build(layout)`, unless it is already what it should be."""
        if signature == self._signature:
            return
        self._signature = signature
        while self.extra.count():
            child = self.extra.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout() is not None:
                self._drop_layout(child.layout())
        build(self.extra)

    @staticmethod
    def _drop_layout(layout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif child.layout() is not None:
                StepCard._drop_layout(child.layout())


class SetupView(QWidget):
    #: "Calibrate with this screen". The window that owns the page opens the
    #: dialog; a page does not reach for another window.
    calibrate_requested = Signal()
    #: "Import your inventory": the window that owns the page opens Import.
    import_requested = Signal()

    def __init__(self, cfg, parent=None, update_job=None):
        super().__init__(parent)
        self.cfg = cfg
        self.update_job = update_job or UpdateJob(cfg, self)
        self._update_error = ""
        self._build_ui()

        self.update_job.progress.connect(self.log_view.appendPlainText)
        self.update_job.started.connect(self._on_update_started)
        self.update_job.finished.connect(self._on_update_finished)
        if self.update_job.running:
            self._on_update_started()

        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self.refresh)
        # No refresh here: `MainWindow.select` refreshes a page on the way in.

    # -- layout --------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Setup")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)
        subtitle = QLabel(
            "What the tool needs before it can read a reward screen. The first "
            "three steps are required; the others only help.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        outer.addWidget(self.summary)

        guide_row = QHBoxLayout()
        self.guide_btn = QPushButton("Open the guide")
        self.guide_btn.clicked.connect(self.open_guide)
        guide_row.addWidget(self.guide_btn)
        self.guide_note = QLabel("")
        self.guide_note.setWordWrap(True)
        guide_row.addWidget(self.guide_note, 1)
        outer.addLayout(guide_row)

        body = QWidget()
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, theme.px(0.4), theme.px(0.8), 0)
        self.body.setSpacing(theme.px(1.0))
        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        self.cards = {
            "data": StepCard(1, "Game data", True),
            "input": StepCard(2, "Keyboard access", True),
            "hotkey": StepCard(3, "Hotkey", True),
            "ee_log": StepCard(4, "Game log (EE.log)", False),
            "window": StepCard(5, "Game window", False),
            "reward": StepCard(6, "First reward screen", False),
            "inventory": StepCard(7, "Your inventory", False),
        }
        for card in self.cards.values():
            self.body.addWidget(card)
        self.body.addStretch(1)

        self._build_data_controls(self.cards["data"])
        self._build_hotkey_controls(self.cards["hotkey"])

    def _build_data_controls(self, card: StepCard) -> None:
        row = QHBoxLayout()
        self.download_btn = QPushButton("Download game data")
        self.download_btn.setObjectName("Primary")
        self.download_btn.clicked.connect(self._download)
        row.addWidget(self.download_btn)
        row.addStretch(1)
        card.extra.addLayout(row)
        # The same lines Settings shows for the same job (UpdateJob).
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFixedHeight(theme.px(11))
        self.log_view.setVisible(False)
        card.extra.addWidget(self.log_view)

    def _build_hotkey_controls(self, card: StepCard) -> None:
        self.key_picker = KeyPicker(self.cfg)
        card.extra.addWidget(self.key_picker)

    # -- the guide -------------------------------------------------------------
    #: Where the guide is looked for; a test points it at a made-up folder.
    program_dir = PROGRAM_DIR

    def guide_target(self) -> tuple[Path | None, str]:
        """The file "Open the guide" opens, and a sentence for when it is not the PDF."""
        pdf = self.program_dir / "GUIDE.pdf"
        if pdf.is_file():
            return pdf, ""
        source = self.program_dir / "docs" / "GUIDE.md"
        if source.is_file():
            return source, ("GUIDE.pdf is only in the release archive; this "
                            "opens its source, docs/GUIDE.md.")
        return None, ("There is no guide in this folder. GUIDE.pdf comes with "
                      "the release archive.")

    def open_guide(self) -> None:
        target, note = self.guide_target()
        if target is not None and not QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(target))):
            note = (f"The desktop did not open {target.name}. Open the file "
                    f"yourself: {target}")
        self.guide_note.setText(note)
        self.guide_note.setStyleSheet(theme.text("small", theme.TEXT_DIM))

    # -- the shared download -------------------------------------------------
    def _download(self) -> None:
        self._update_error = ""
        self.update_job.start()

    def _on_update_started(self) -> None:
        self.log_view.setVisible(True)
        self.log_view.clear()
        self.download_btn.setEnabled(False)
        self.refresh()

    def _on_update_finished(self, ok: bool, message: str) -> None:
        self.download_btn.setEnabled(True)
        self.log_view.appendPlainText("Done." if ok else f"Failed: {message}")
        self._update_error = "" if ok else message
        self.refresh()

    # -- state -----------------------------------------------------------------
    def refresh(self) -> None:
        data = readiness.data_check(self.cfg)
        keyboard = readiness.input_check()
        hotkey = readiness.hotkey_check(self.cfg)
        log = readiness.ee_log_check(self.cfg)
        game = readiness.window_check(self.cfg)
        inventory = readiness.inventory_check(self.cfg)

        self._show_data(data)
        self._show_input(keyboard)
        self._show_hotkey(hotkey)
        self._show_log(log)
        self._show_window(game)
        self._show_reward_screen(hotkey)
        self._show_inventory(inventory)
        self._show_summary([data, keyboard, hotkey], hotkey)
        self.key_picker.refresh()

    def _show_summary(self, required: list, hotkey) -> None:
        open_ = [check for check in required if not check.ok]
        if open_:
            self.summary.setText(
                f"{len(required) - len(open_)} of {len(required)} required steps "
                f"done. Next: {open_[0].todo}.")
            self.summary.setStyleSheet(theme.text("body", theme.WARN))
        else:
            self.summary.setText(
                f"All required steps are done. Start a relic mission in Warframe "
                f"and press {hotkey.info['key']} on the reward screen; the result "
                f"appears on the Rewards page.")
            self.summary.setStyleSheet(theme.text("body", theme.GOOD))

    def _show_data(self, check) -> None:
        card = self.cards["data"]
        running = self.update_job.running
        if check.ok:
            text = (f"Game data is loaded (last updated {check.info['stamp']}, "
                    f"{age_words(check.info['days'])}).")
            if check.state == "old":
                text += (f" That is older than {check.info['remind']} days; the "
                         f"button fetches what is new.")
            card.show_state("done", text)
            self.download_btn.setText("Update game data")
        else:
            text = ("The tool needs the names of Warframe's items and relics to "
                    "recognise what the reward screen shows. The button downloads "
                    "them from Digital Extremes, the game's maker. It needs an "
                    "internet connection.")
            if self._update_error:
                text += (f" The last attempt failed: {self._update_error}. Check "
                         f"the connection and press the button again.")
            card.show_state("todo", text)
            self.download_btn.setText("Download game data")
        self.download_btn.setEnabled(not running)

    def _show_input(self, check) -> None:
        card = self.cards["input"]
        state = check.state
        if state == "active":
            card.show_state("done", "The tool can read the keyboard, so it hears "
                            "your hotkey while Warframe is in front.")
            card.rebuild(("input", state), lambda _layout: None)
        elif state == "pending":
            card.show_state("todo", "You are in the \"input\" group, but this login "
                            "session started before that. Log out of the desktop "
                            "and log in again, once. Nothing else is needed.")
            card.rebuild(("input", state), lambda _layout: None)
        elif state == "missing":
            card.show_state(
                "todo", "Linux only lets a program read raw key presses if your "
                "user is in the \"input\" group. The tool needs that to hear the "
                "hotkey while Warframe has the focus. Run this command once in a "
                "terminal, then log out of the desktop and log in again:")
            command = f"sudo usermod -aG input {shlex.quote(check.info['user'])}"
            card.rebuild(("input", state, command),
                         lambda layout: self._command_row(layout, command))
        elif state == "no_evdev":
            card.show_state("todo", "The Python package python-evdev is not "
                            "installed, so the tool cannot read keys. ./install.sh "
                            "installs it; so does the package manager (package "
                            "name: python-evdev).")
            card.rebuild(("input", state), lambda _layout: None)
        else:
            card.show_state("todo", "No keyboard device can be read, although the "
                            "\"input\" group is active. Is a keyboard connected? "
                            "\"wf doctor\" in a terminal lists what the tool sees.")
            card.rebuild(("input", state), lambda _layout: None)

    def _command_row(self, layout, command: str) -> None:
        row = QHBoxLayout()
        label = QLabel(command)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setStyleSheet(f"font-family: {theme.MONO_FAMILY}; "
                            f"color: {theme.TEXT}; padding: {theme.px(0.3)}px;")
        row.addWidget(label)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(command))
        row.addWidget(copy)
        row.addStretch(1)
        layout.addLayout(row)

    def _show_hotkey(self, check) -> None:
        card = self.cards["hotkey"]
        key = check.info["key"]
        if check.ok:
            card.show_state(
                "done", f"The hotkey is {key}. Press it on the reward screen while "
                f"Warframe is in front. You can choose another key at any time.")
        elif check.state == "unset":
            card.show_state(
                "todo", "Choose the key you will press on Warframe's reward "
                "screen, for example F8 or Pause. Pick one the game does not use. "
                "Press the button, then the key.")
        elif check.state == "unknown":
            card.show_state(
                "todo", f"\"{key}\" is not a key name the tool knows. Press the "
                f"button and then the key, instead of typing it.")
        else:
            card.show_state("todo", f"{key} is set, but it cannot be checked "
                            "without python-evdev (see Keyboard access).")

    def _show_log(self, check) -> None:
        card = self.cards["ee_log"]
        path = check.info["path"]
        if check.ok:
            card.show_state(
                "done", f"Found, last written {check.info['when']}. With it the "
                "tool knows which relics your squad opened.")
            card.rebuild(("ee_log", "found", str(path)),
                         lambda layout: self._path_row(layout, path))
            return
        found = list(check.info["found"])[:MAX_LOG_CHOICES]
        if found:
            card.show_state(
                "note", "The game log is not where the settings say, but the tool "
                "found it in your Steam libraries. Use it, and the tool knows "
                "which relics your squad opened.", word="Found elsewhere")
        else:
            card.show_state(
                "note", "The tool did not find the game log at the path in the "
                "settings (below). Warframe writes it when it runs, so start the "
                "game once. If the game is not installed through Steam, or sits "
                "in an unusual place, choose the file yourself. Without it the "
                "tool still works, but it is less sure about the names.",
                word="Not found")
        card.rebuild(("ee_log", "missing", str(path), tuple(str(p) for p in found)),
                     lambda layout: self._log_choices(layout, found, path))

    @staticmethod
    def _path_row(layout, path: Path) -> None:
        # Elided in the middle with the whole path as the tooltip: a path has no
        # spaces to wrap at, and at a large text size it is wider than the page.
        label = ElidedLabel(str(path))
        label.setStyleSheet(f"font-family: {theme.MONO_FAMILY};")
        layout.addWidget(label)

    def _log_choices(self, layout, found: list, configured: Path) -> None:
        self._log_message = QLabel("")
        self._log_message.setWordWrap(True)
        # Shown only with something to say: an empty label still takes a line
        # and left a gap under the buttons of every card that lacked the log.
        self._log_message.setVisible(False)
        if not found:
            self._path_row(layout, configured)
        for candidate in found:
            row = QHBoxLayout()
            name = ElidedLabel(str(candidate))
            name.setStyleSheet(f"font-family: {theme.MONO_FAMILY};")
            row.addWidget(name, 1)
            use = QPushButton("Use this file")
            use.setObjectName("Primary")
            use.clicked.connect(lambda _c=False, p=candidate: self._use_log(p))
            row.addWidget(use)
            layout.addLayout(row)
        row = QHBoxLayout()
        choose = QPushButton("Choose file…")
        choose.clicked.connect(lambda: self._choose_log(configured))
        row.addWidget(choose)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self._log_message)

    def _use_log(self, path: Path) -> None:
        try:
            settings_io.save_value(self.cfg, "paths", "ee_log", str(path))
        except OSError as exc:
            self._log_message.setText(f"Could not save: {exc}")
            self._log_message.setStyleSheet(f"color: {theme.BAD};")
            self._log_message.setVisible(True)
            return
        self.refresh()

    def _choose_log(self, configured: Path) -> None:
        start = configured.parent if configured.parent.is_dir() else Path.home()
        chosen = file_dialogs.open_file(
            self, "Choose Warframe's EE.log", str(start),
            "Log files (*.log);;All files (*)")
        if chosen:
            self._use_log(Path(chosen))

    def _show_window(self, check) -> None:
        card = self.cards["window"]
        if check.ok:
            card.show_state(
                "done", f"Warframe is running and the tool finds its window "
                f"({check.info['width']}x{check.info['height']}).")
        elif check.state == "absent":
            card.show_state("note", "Start Warframe to check that the tool finds "
                            "its window. This is only a check, nothing is needed here.",
                            word="Not running")
        elif check.state == "no_display":
            card.show_state("note", "There is no X display, so the game window "
                            "cannot be looked for. Warframe runs through XWayland "
                            "on a Wayland desktop; if this stays, \"wf doctor\" "
                            "tells more.", word="No display")
        else:
            card.show_state("note", f"The game window could not be looked for: "
                            f"{check.detail}")

    def _show_reward_screen(self, hotkey) -> None:
        """What the last hotkey press read, and what to do if it read badly.

        Calibrating is not a step everyone takes: the default regions fit a
        16:9 window, and the card says so in every state. It is offered only
        when the reading was bad, by the one rule in `wft.relic.last_screen`.
        """
        card = self.cards["reward"]
        screen = last_screen.current()
        if screen is None:
            key = hotkey.info["key"]
            press = f"press {key}" if key else "press your hotkey"
            card.show_state(
                "note", f"Do a relic run and {press} on the reward screen. The "
                "tool reads it and shows here how many names it recognised. "
                "Nothing needs setting up for this: calibrate only if the names "
                "come out wrong, the default regions fit a 16:9 window.",
                word="Waiting")
            card.rebuild(("reward", "none"), lambda _layout: None)
            return
        reading = screen.reading
        age = last_screen.age_words(screen.age_seconds())
        if not reading.bad:
            card.show_state(
                "done", f"{reading.recognised} of {reading.total} names "
                f"recognised ({age}). There is nothing to calibrate unless names "
                "you can read on the reward screen come out wrong.")
            card.rebuild(("reward", "good"), lambda _layout: None)
            return
        what = ("The last reward screen could not be read"
                if reading.failed else
                f"0 of {reading.total} names recognised on the last reward screen")
        card.show_state(
            "todo", f"{what} ({age}). If it was a reward screen and you can read "
            "the names on it, the tool is probably looking in the wrong place; "
            "calibrating on this very screen takes one dragged box. If it was "
            "not a reward screen, press the hotkey on one instead.",
            word="Check")

        def build(layout) -> None:
            row = QHBoxLayout()
            button = QPushButton("Calibrate with this screen")
            button.setObjectName("Primary")
            button.clicked.connect(lambda _checked=False: self.calibrate_requested.emit())
            row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)

        card.rebuild(("reward", "bad"), build)

    def _show_inventory(self, check) -> None:
        """What the tool knows of the inventory, and the way to the Import page.

        A hint like the reward-screen card, no required step: the tool reads a
        reward screen without an inventory, it only ranks the options without
        knowing what you already own. The wording of the counts is
        `readiness.inventory_check`'s, the Import page says the same.
        """
        card = self.cards["inventory"]
        if check.state == "empty":
            card.show_state(
                "note", "Import your inventory. Without it the tool does not know "
                "what you already have, so it cannot say which reward you still "
                "need. The Import page reads a recording or screenshots of your "
                "inventory: Choose recording... or Choose folder...", word="Not yet")
            label = "Import your inventory"
        else:
            card.show_state(
                "done", f"{check.detail}. A new import replaces these numbers.")
            label = "Import again"

        def build(layout) -> None:
            row = QHBoxLayout()
            button = QPushButton(label)
            if check.state == "empty":
                button.setObjectName("Primary")
            button.clicked.connect(lambda _checked=False: self.import_requested.emit())
            row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)

        card.rebuild(("inventory", check.state, label), build)

    # -- lifecycle -------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._tick.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._tick.stop()
