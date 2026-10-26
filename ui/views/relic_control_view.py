"""Status and a manual trigger for the Rewards page, inside Settings.

Not the Rewards page itself (`ui/views/relic_panel.py`). This only ever calls
`relic_panel.trigger()`, the same entry point the hotkey uses, so a manual test
and a real capture behave identically.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ui import theme

#: How often to re-check the game window and the log. Fast enough that the page
#: reflects reality by the time you have finished reading it, slow enough to be
#: invisible: both checks are a window list and a tail read.
POLL_MS = 3000


class RelicControlView(QWidget):
    def __init__(self, cfg, relic_panel, parent=None, embedded: bool = False):
        super().__init__(parent)
        self.cfg = cfg
        self.relic_panel = relic_panel
        #: Inside the Settings page rather than a page of its own: no title of
        #: its own, no outer padding, since the group box already provides both.
        self.embedded = embedded
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        if self.embedded:
            outer.setContentsMargins(0, 0, 0, 0)
        else:
            outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.7))

        if not self.embedded:
            title = QLabel("Relic")
            title.setObjectName("ViewTitle")
            outer.addWidget(title)

        subtitle = QLabel(
            "The Rewards page fills when the hotkey reads a reward screen. "
            "This shows what it is doing and lets you test it without waiting "
            "for a reward screen.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        status_frame = QFrame()
        if not self.embedded:
            status_frame.setObjectName("Card")
        status_layout = QVBoxLayout(status_frame)
        status_layout.setContentsMargins(
            0 if self.embedded else theme.px(1.4),
            theme.px(0.3) if self.embedded else theme.px(1.2),
            0 if self.embedded else theme.px(1.4),
            theme.px(0.3) if self.embedded else theme.px(1.2))
        status_layout.setSpacing(theme.px(0.55))

        if self.relic_panel is None:
            warning = QLabel(
                "The panel could not be created — check the log printed at startup. "
                "Nothing on this page will work until that is fixed.")
            warning.setStyleSheet(f"color: {theme.BAD};")
            warning.setWordWrap(True)
            status_layout.addWidget(warning)
        else:
            window_class = str(self.cfg.get("game.window_class", "") or "(not configured)")
            hotkey = str(self.cfg.get("hotkey.key", "") or "(not set)")
            # Live state first, configuration second. The question this page is
            # opened with is "will it work when I press the key", and three
            # things answer it: is the game there, is the log readable, and what
            # is in play right now. The configured values only explain a "no".
            self.checks = QGridLayout()
            self.checks.setHorizontalSpacing(theme.px(0.7))
            self.checks.setVerticalSpacing(theme.px(0.4))
            self.check_labels = {}
            for row, name in enumerate(("Game window", "Relics in play", "Hotkey")):
                caption = QLabel(name)
                caption.setStyleSheet(f"color: {theme.TEXT_DIM};")
                value = QLabel("checking…")
                value.setWordWrap(True)
                self.checks.addWidget(caption, row, 0)
                self.checks.addWidget(value, row, 1)
                self.check_labels[name] = value
            self.checks.setColumnStretch(1, 1)
            status_layout.addLayout(self.checks)

            info = QLabel(
                f"window class {window_class}   ·   hotkey {hotkey}")
            info.setStyleSheet(
                theme.text("small", theme.TEXT_FAINT)
                + f" font-family: {theme.MONO_FAMILY};"
                f" padding-top: {theme.space('close')}px;")
            status_layout.addWidget(info)

            row = QHBoxLayout()
            self.trigger_btn = QPushButton("Trigger test capture")
            self.trigger_btn.setObjectName("Primary")
            self.trigger_btn.clicked.connect(self._trigger)
            row.addWidget(self.trigger_btn)
            row.addStretch(1)
            status_layout.addLayout(row)

            self.relic_panel.log_line.connect(self._append_log)
            self.relic_panel.result_ready.connect(self._on_result)

            self._poll = QTimer(self)
            self._poll.timeout.connect(self._refresh_checks)
            self._poll.start(POLL_MS)
            QTimer.singleShot(0, self._refresh_checks)

        outer.addWidget(status_frame)

        log_header = QLabel("Log")
        log_header.setObjectName("SectionHeader")
        outer.addWidget(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        outer.addWidget(self.log_view, 1)

    def _set_check(self, name: str, text: str, colour: str):
        label = self.check_labels.get(name)
        if label is not None:
            label.setText(text)
            label.setStyleSheet(f"color: {colour};")

    def _refresh_checks(self):
        """Ask the system rather than reporting what was configured.

        Everything here is best-effort: a failure means the answer is unknown,
        not that the page should break. The hotkey is named here and checked by
        `readiness` for being a key the listener knows; that it actually arrives
        is what the Setup page tests, by asking for a press.
        """
        from wft.capture import window as cap
        from wft.data import db as dbq
        from wft.relic import eelog

        window_class = str(self.cfg.get("game.window_class", "") or "")
        try:
            game = cap.find_game(window_class) if window_class else None
        except Exception:  # noqa: BLE001 - a missing X display is a "no", not a crash
            game = None
        if game is not None:
            self._set_check("Game window", f"found, {game.width}x{game.height}", theme.GOOD)
        else:
            self._set_check(
                "Game window",
                f"not found — nothing matches {window_class!r}. "
                f"List what is open with: wf windows", theme.BAD)

        try:
            opened = eelog.read(str(self.cfg.get("paths.ee_log", "")))
            if opened:
                connection = dbq.connect(self.cfg.db_path)
                marks = ",".join("?" * len(opened.unique_names))
                names = [row["name"] for row in connection.execute(
                    f"SELECT name FROM relics WHERE unique_name IN ({marks})",
                    opened.unique_names)]
                if names:
                    self._set_check(
                        "Relics in play", ", ".join(sorted(names)), theme.GOOD)
                else:
                    self._set_check(
                        "Relics in play",
                        "the log names relics this database does not know — "
                        "run an update", theme.WARN)
            else:
                self._set_check(
                    "Relics in play",
                    "none in the log yet — matching falls back to every prime "
                    "part, which still works but is less certain", theme.TEXT_DIM)
        except Exception as exc:  # noqa: BLE001
            self._set_check("Relics in play", f"log unreadable: {exc}", theme.WARN)

        from wft import readiness

        hotkey = readiness.hotkey_check(self.cfg)
        if hotkey.ok:
            self._set_check(
                "Hotkey",
                f"{hotkey.detail} — Setup tests that the key arrives. If it "
                f"does nothing in the game, the button below bypasses it.",
                theme.TEXT_DIM)
        else:
            self._set_check("Hotkey", f"{hotkey.detail} — choose it on the "
                            f"Setup page", theme.WARN)

    def _trigger(self):
        if self.relic_panel is None:
            return
        self.trigger_btn.setEnabled(False)
        self.log_view.appendPlainText("--- triggering capture ---")
        self.relic_panel.trigger()

    def _append_log(self, message: str):
        self.log_view.appendPlainText(message)

    def _on_result(self, _result):
        self.trigger_btn.setEnabled(True)
