"""Overlays: grids the tool draws over the game window.

Two cards. The reward overlay draws the recommendation over the reward
screen: a switch, a test button that shows the last reading again, a status
line. The palette grid: a labelled 5 x 18 grid over the colour palette in
the Arsenal, so a cell can be named ("B7") instead of counted. The grid is
drawn by a helper process (`ui.overlay`); this page starts and stops it, shows
what it reports and stores the position the user locked it at.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout,
    QWidget,
)

from ui import geometry, settings_io, theme
from ui.overlay import drawing, looks

ALIGN_HELP = (
    "While aligning: drag the grid with the left mouse button. Drag the yellow "
    "square at the bottom right, or hold Shift while dragging, to change its "
    "size. The arrow keys move it by 1 pixel, with Shift they change the size; "
    "hold Ctrl for 10 pixels. Enter or a double click locks it where it is, "
    "Esc takes back what you changed.")


def _swatch(colours: dict) -> QIcon:
    """A small icon of a preset: the frame colour with the text colour inside."""
    pixmap = QPixmap(28, 16)
    pixmap.fill(QColor(colours["frame"]))
    painter = QPainter(pixmap)
    painter.fillRect(5, 4, 18, 8, QColor(colours["text"]))
    painter.end()
    return QIcon(pixmap)


def describe(state: dict | None, running: bool) -> str:
    """The status line for the last `state` message of the helper."""
    if not running or not state or not state.get("shown"):
        return "Not running" if not running else "Not shown"
    position = state.get("position") or {}
    where = (f"x {position.get('x')}, y {position.get('y')}, "
             f"{position.get('w')} × {position.get('h')}")
    if state.get("mode") == "aligning":
        return f"Aligning at {where}"
    return f"Shown, locked at {where}"


class OverlaysView(QWidget):
    def __init__(self, cfg, client, parent=None, rewards=None):
        super().__init__(parent)
        self.cfg = cfg
        self.client = client
        #: The reward overlay (`ui.overlay.rewards.RewardOverlay`); without one
        #: the page shows the palette grid only.
        self.rewards = rewards
        self._state: dict | None = None
        self._build_ui()
        if rewards is not None:
            rewards.shown.connect(lambda _token: self._reward_status("Shown over the game."))
            rewards.ended.connect(self._reward_ended)
            rewards.unavailable.connect(self._reward_unavailable)
        client.state.connect(self._on_state)
        client.failed.connect(self._on_failed)
        client.stopped.connect(self._on_stopped)
        self._update_controls()

    def refresh(self) -> None:
        self._update_controls()
        if self.rewards is not None:
            self._update_test_button()

    # -- layout --------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.4))

        title = QLabel("Overlays")
        title.setObjectName("ViewTitle")
        subtitle = QLabel("Drawings over the game window. They only draw: "
                          "mouse clicks go through to the game, and nothing is "
                          "read from the game or sent to it.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)
        outer.addSpacing(theme.px(0.8))

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setSpacing(theme.px(1.2))
        layout.setContentsMargins(0, 0, theme.px(0.8), 0)
        if self.rewards is not None:
            layout.addWidget(self._reward_group())
        layout.addWidget(self._palette_group())
        layout.addStretch(1)

    def _reward_group(self) -> QGroupBox:
        group = QGroupBox("Reward overlay")
        grid = QGridLayout(group)
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setHorizontalSpacing(theme.px(0.8))
        row = 0

        what = QLabel("After you press the hotkey on the reward screen, the "
                      "recommendation is drawn over the game itself: a frame "
                      "and TAKE THIS on the card to take, and a few lines "
                      "under the player names of every card (ducats, mastery, "
                      "what is missing) on a dark background. Click the card you take in the game and a tick "
                      "shows on it. It goes away with the reward screen, after 15 "
                      "seconds at the latest. While it is shown, the desktop "
                      "notification is not sent. The Rewards page stays as it "
                      "is.")
        what.setWordWrap(True)
        grid.addWidget(what, row, 0, 1, 4)
        row += 1

        self.rewards_box = QCheckBox("Show the recommendation over the game")
        self.rewards_box.setChecked(bool(self.cfg.get("overlay.rewards", True)))
        self.rewards_box.toggled.connect(self._rewards_toggled)
        grid.addWidget(self.rewards_box, row, 0, 1, 4)
        row += 1

        self.click_box = QCheckBox("Count the card I click as my pick")
        self.click_box.setToolTip(
            "While the overlay is up, left clicks in the game are read to see "
            "which card you click. A tick shows on it, the last card you click "
            "counts, and it is recorded when the overlay goes away (undo on the "
            "Rewards page). Nothing is sent to the game and nothing is stored "
            "but the card.")
        self.click_box.setChecked(bool(self.cfg.get("overlay.click_to_pick", True)))
        self.click_box.setEnabled(self.rewards_box.isChecked())
        self.click_box.toggled.connect(self._click_toggled)
        grid.addWidget(self.click_box, row, 0, 1, 4)
        row += 1

        grid.addWidget(QLabel("Colours"), row, 0)
        self.colours_box = QComboBox()
        self.colours_box.addItem(_swatch(looks.FIXED["red-green"]), "Red frame, green text", "red-green")
        self.colours_box.addItem(_swatch(looks.FIXED["blue-orange"]),
                                 "Orange frame, blue text (colour-blind safe)", "blue-orange")
        for name, colours in looks.theme_presets().items():
            self.colours_box.addItem(_swatch(colours), f"Theme {name}", name)
        self.colours_box.addItem("Custom colours", looks.CUSTOM)
        self.colours_box.setToolTip("The colours of the frame and the text. Applies "
                                    "the next time the overlay is drawn.")
        wanted = self.cfg.get("overlay.colours")
        self.colours_box.setCurrentIndex(max(0, self.colours_box.findData(wanted)))
        self.colours_box.currentIndexChanged.connect(self._colours_changed)
        grid.addWidget(self.colours_box, row, 1, 1, 3)
        row += 1

        self.custom_edits: dict[str, QLineEdit] = {}
        self.custom_swatches: dict[str, QPushButton] = {}
        self.custom_labels: list[QLabel] = []
        defaults = looks.custom_from_config(self.cfg)
        for key, title in (("frame", "Frame"), ("text", "Text")):
            label = QLabel(title)
            grid.addWidget(label, row, 0)
            self.custom_labels.append(label)
            edit = QLineEdit(defaults[key])
            edit.setMaxLength(7)
            edit.setMaximumWidth(theme.px(8))
            edit.editingFinished.connect(lambda k=key: self._custom_edited(k))
            grid.addWidget(edit, row, 1)
            button = QPushButton("")
            button.setFixedWidth(theme.px(3))
            button.setToolTip("Pick the colour in a dialog")
            button.clicked.connect(lambda _checked=False, k=key: self._custom_pick(k))
            grid.addWidget(button, row, 2)
            self.custom_edits[key], self.custom_swatches[key] = edit, button
            row += 1
        self.custom_hint = QLabel("")
        self.custom_hint.setWordWrap(True)
        grid.addWidget(self.custom_hint, row, 0, 1, 4)
        row += 1
        self._contrast_note()
        self._sync_custom()

        self.reward_status = QLabel("")
        self.reward_status.setWordWrap(True)
        self.reward_status.setStyleSheet(theme.text("body", theme.TEXT))
        grid.addWidget(self.reward_status, row, 0, 1, 4)
        row += 1

        self.reward_error = QLabel("")
        self.reward_error.setWordWrap(True)
        self.reward_error.setStyleSheet(f"color: {theme.BAD};")
        self.reward_error.setVisible(False)
        grid.addWidget(self.reward_error, row, 0, 1, 4)
        row += 1

        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Show over the last reward screen")
        self.test_btn.setToolTip("Draws the last reading over the game window for "
                                 "5 seconds, without checking that the reward "
                                 "screen is still up")
        self.test_btn.clicked.connect(self._test_overlay)
        buttons.addWidget(self.test_btn)
        buttons.addStretch(1)
        grid.addLayout(buttons, row, 0, 1, 4)
        row += 1

        self.test_reason = QLabel("")
        self.test_reason.setWordWrap(True)
        self.test_reason.setStyleSheet(f"color: {theme.TEXT_DIM};")
        grid.addWidget(self.test_reason, row, 0, 1, 4)
        grid.setColumnStretch(3, 1)
        self._update_test_button()
        return group

    def _palette_group(self) -> QGroupBox:
        group = QGroupBox("Palette grid")
        grid = QGridLayout(group)
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setHorizontalSpacing(theme.px(0.8))
        row = 0

        what = QLabel("Lays a grid with the letters A to E and the numbers 1 to "
                      "18 over the colour palette in the Arsenal, so you can "
                      "name a cell (for example B7) instead of counting. Put "
                      "the palette on screen, press Start, then align the grid "
                      "once; it stays where you lock it.")
        what.setWordWrap(True)
        grid.addWidget(what, row, 0, 1, 4)
        row += 1

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(theme.text("body", theme.TEXT))
        grid.addWidget(self.status, row, 0, 1, 4)
        row += 1

        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.BAD};")
        self.error.setVisible(False)
        grid.addWidget(self.error, row, 0, 1, 4)
        row += 1

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setToolTip("Align the grid again")
        self.unlock_btn.clicked.connect(lambda: self.client.send({"cmd": "palette_unlock"}))
        self.lock_btn = QPushButton("Lock")
        self.lock_btn.setToolTip("Keep the grid where it is; clicks go through again")
        self.lock_btn.clicked.connect(lambda: self.client.send({"cmd": "palette_lock"}))
        self.reset_btn = QPushButton("Reset position")
        self.reset_btn.setToolTip("Forget the stored position and align the grid anew")
        self.reset_btn.clicked.connect(self._reset)
        for button in (self.start_btn, self.stop_btn, self.unlock_btn,
                       self.lock_btn, self.reset_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        grid.addLayout(buttons, row, 0, 1, 4)
        row += 1

        help_label = QLabel(ALIGN_HELP)
        help_label.setWordWrap(True)
        help_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        grid.addWidget(help_label, row, 0, 1, 4)
        row += 1

        style = drawing.style_from_config(self.cfg)
        self.names_box = QCheckBox("Cell names")
        self.names_box.setToolTip("Write each cell's name into the cell")
        self.names_box.setChecked(style["cell_names"])
        self.names_box.toggled.connect(self._names_changed)
        grid.addWidget(self.names_box, row, 0, 1, 4)
        row += 1

        grid.addWidget(QLabel("Font size"), row, 0)
        self.font_spin = QSpinBox()
        self.font_spin.setRange(6, 60)
        self.font_spin.setSuffix(" pt")
        self.font_spin.setValue(style["font_pt"])
        self.font_spin.valueChanged.connect(self._font_changed)
        grid.addWidget(self.font_spin, row, 1)
        row += 1

        grid.addWidget(QLabel("Line opacity"), row, 0)
        self.opacity_spin = QSpinBox()
        self.opacity_spin.setRange(5, 100)
        self.opacity_spin.setSuffix(" %")
        self.opacity_spin.setValue(int(round(style["line_opacity"] * 100)))
        self.opacity_spin.valueChanged.connect(self._opacity_changed)
        grid.addWidget(self.opacity_spin, row, 1)
        grid.setColumnStretch(3, 1)
        return group

    # -- buttons ----------------------------------------------------------------
    def _style(self) -> dict:
        return drawing.style_from_config(self.cfg)

    def _show_command(self, position: dict | None) -> dict:
        return {"cmd": "palette_show", "position": position, "style": self._style()}

    def _start(self) -> None:
        self._clear_error()
        self.client.send(self._show_command(geometry.palette_position()))
        self._update_controls()

    def _stop(self) -> None:
        self.client.stop()

    def _reset(self) -> None:
        self._clear_error()
        geometry.clear_palette()
        self.client.send(self._show_command(None))
        self._update_controls()

    # -- the reward overlay ------------------------------------------------------
    def _rewards_toggled(self, checked: bool) -> None:
        try:
            settings_io.save_value(self.cfg, "overlay", "rewards", bool(checked))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
        self.click_box.setEnabled(checked)
        self._sync_custom()
        if not checked and self.rewards is not None:
            self.rewards.hide()

    def _colours_changed(self, _index: int) -> None:
        try:
            settings_io.save_value(self.cfg, "overlay", "colours",
                                   str(self.colours_box.currentData()))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
        self._sync_custom()

    def _sync_custom(self) -> None:
        """Show the two custom rows only for "Custom colours"; all colour controls
        follow the switch for the overlay."""
        custom = self.colours_box.currentData() == looks.CUSTOM
        on = self.rewards_box.isChecked()
        self.colours_box.setEnabled(on)
        for widget in (*self.custom_labels, *self.custom_edits.values(),
                       *self.custom_swatches.values()):
            widget.setVisible(custom)
            widget.setEnabled(on)
        self.custom_hint.setVisible(custom and bool(self.custom_hint.text()))
        for key, edit in self.custom_edits.items():
            colour = looks.valid_hex(edit.text())
            self.custom_swatches[key].setStyleSheet(
                f"background-color: {colour or theme.BG_CARD};")

    def _custom_edited(self, key: str) -> None:
        """Save a custom colour typed or picked; an invalid one is not saved."""
        edit = self.custom_edits[key]
        colour = looks.valid_hex(edit.text())
        if colour is None:
            self.custom_hint.setStyleSheet(f"color: {theme.BAD};")
            self.custom_hint.setText(f"Use a colour like {drawing.DEFAULT_LOOK[key]}.")
        else:
            edit.setText(colour)
            try:
                settings_io.save_value(self.cfg, "overlay", f"custom_{key}", colour)
            except OSError as exc:
                QMessageBox.warning(self, "Could not save", str(exc))
            self._contrast_note()
        self._sync_custom()

    def _contrast_note(self) -> None:
        """Warn about a custom text colour that is hard to read on a light
        picture (it is saved all the same), or clear the hint."""
        text = looks.valid_hex(self.custom_edits["text"].text())
        if text is not None and looks.contrast(text) < looks.MIN_CONTRAST:
            self.custom_hint.setStyleSheet(f"color: {theme.WARN};")
            self.custom_hint.setText("Hard to read on a light picture.")
        else:
            self.custom_hint.setText("")

    def _custom_pick(self, key: str) -> None:
        start = QColor(looks.valid_hex(self.custom_edits[key].text())
                       or drawing.DEFAULT_LOOK[key])
        picked = QColorDialog.getColor(start, self, "Frame colour" if key == "frame"
                                       else "Text colour")
        if picked.isValid():
            self.custom_edits[key].setText(picked.name())
            self._custom_edited(key)

    def _click_toggled(self, checked: bool) -> None:
        try:
            settings_io.save_value(self.cfg, "overlay", "click_to_pick", bool(checked))
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
        if self.rewards is not None:
            self.rewards.refresh_arming()

    def _reward_status(self, text: str) -> None:
        self.reward_status.setText(text)
        self.reward_error.clear()
        self.reward_error.setVisible(False)

    def _reward_ended(self, reason: str) -> None:
        words = {"header": "The reward screen is gone.", "time": "Time is up.",
                 "hidden": "Hidden.", "replaced": "Replaced by a new reading.",
                 "failed": "Stopped."}
        if reason != "replaced":
            self.reward_status.setText("Not shown. " + words.get(reason, ""))

    def _reward_unavailable(self, _token: int) -> None:
        self.reward_status.setText("Not shown.")
        self.reward_error.setText(self.rewards.last_error or "The overlay could not be shown.")
        self.reward_error.setVisible(True)

    def _last_reading_args(self):
        """(arguments of the overlay, reason it cannot be had) for the last reading."""
        from ui.views.relic_panel import overlay_args
        from wft.relic import last_screen

        screen = last_screen.current()
        if screen is None or screen.result is None:
            return None, "No reward screen has been read yet. Press the hotkey on one first."
        args = overlay_args(screen.result)
        if args is None:
            return None, "The last reading found nothing to show."
        return args, ""

    def _update_test_button(self) -> None:
        args, reason = self._last_reading_args()
        if args is not None and self.rewards.current_placement() is None:
            reason = "The game window was not found."
        self.test_btn.setEnabled(not reason)
        self.test_reason.setText(reason)
        self.test_reason.setVisible(bool(reason))

    def _test_overlay(self) -> None:
        from ui.overlay.rewards import TEST_SECONDS

        args, reason = self._last_reading_args()
        placement = self.rewards.current_placement() if args is not None else None
        if args is None or placement is None:
            self._update_test_button()
            return
        self.reward_status.setText("Showing for 5 seconds.")
        self.reward_error.setVisible(False)
        self.rewards.show(args, placement, None, None, seconds=TEST_SECONDS, test=True)

    # -- the look -------------------------------------------------------------------
    def _save_style(self, key: str, value) -> None:
        try:
            settings_io.save_value(self.cfg, "palette_grid", key, value)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
        if self.client.running:
            self.client.send({"cmd": "palette_style", "style": self._style()})

    def _names_changed(self, checked: bool) -> None:
        self._save_style("cell_names", bool(checked))

    def _font_changed(self, value: int) -> None:
        self._save_style("font_pt", int(value))

    def _opacity_changed(self, percent: int) -> None:
        self._save_style("line_opacity", round(percent / 100, 2))

    # -- what the helper says ---------------------------------------------------------
    def _on_state(self, state: dict) -> None:
        self._state = state
        # The position is stored when the user locked it, and only then: a
        # state that merely repeats the stored one, or a cancelled alignment,
        # leaves the stored position alone.
        if (state.get("reason") == "lock" and state.get("mode") == "locked"
                and state.get("position")):
            try:
                geometry.save_palette(state["position"])
            except (KeyError, TypeError, ValueError) as exc:
                self._show_error(f"The position could not be stored: {exc}")
        self._update_controls()

    def _on_failed(self, message: str) -> None:
        self._show_error(message)
        self._update_controls()

    def _on_stopped(self) -> None:
        self._state = None
        self._update_controls()

    def _show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(True)

    def _clear_error(self) -> None:
        self.error.clear()
        self.error.setVisible(False)

    def _update_controls(self) -> None:
        running = bool(self.client.running)
        state = self._state if running else None
        mode = state.get("mode") if state and state.get("shown") else None
        self.status.setText(describe(state, running))
        self.start_btn.setEnabled(mode is None)
        self.stop_btn.setEnabled(running)
        self.unlock_btn.setEnabled(mode == "locked")
        self.lock_btn.setEnabled(mode == "aligning")
        self.reset_btn.setEnabled(mode is not None or geometry.stored_palette() is not None)
