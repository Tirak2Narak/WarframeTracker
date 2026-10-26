"""Measuring the reward regions on the user's own screen.

The config file tells people to run this when the names come out wrong. It
takes a picture of the reward screen - captured live or loaded from a file -
and asks for one card: the first (leftmost) one is dragged open on the picture
and then fitted a pixel at a time, together with the gap between two cards and
the name band of that card. The rest of the row follows: it is centred,
so the other cards come from the first, the gap and the number of rewards.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
)

from ui import file_dialogs, settings_io, theme
from ui.widgets import dialog_size
from ui.widgets.region_picker import BAND, CARD, RegionPicker
from wft.capture import window as cap
from wft.relic import last_screen, shape

#: Where the name sits inside a reward card, as fractions of the card's height,
#: for the green band a newly drawn card starts with. Measured on a real reward
#: screen: a single line spanned 0.887 to 0.984, and a wrapped name puts its
#: first line one line higher - so two lines start at 0.79. The band starts
#: higher than 0.79 on purpose: a live capture caught the row a few pixels from
#: where a screenshot of the same screen showed it (the row animates in), and
#: the only reading that suffered was the two-line one, its first line clipped.
#: Fitted afterwards with the keys, so this is only where it starts.
NAME_TOP_IN_TILE = 0.750
NAME_BOTTOM_IN_TILE = 0.985

#: What the keys do, said in the dialog.
KEYS_HINT = (
    "Click the picture, then fit with the keyboard: arrow keys move by 1 pixel "
    "(with Ctrl by 10), Shift + arrows change the size, Alt + Left/Right change "
    "the gap between the cards, Tab switches between the red card and the green "
    "name band (Ctrl + Tab leaves the picture). The three small windows below "
    "the picture show the card's corner, the gap and the band's corner at 4x.")

#: How often the "last reward screen" button looks at the store while the dialog
#: is open: a hotkey press in the game while the dialog sits open should make
#: the button appear, and its age should not stand still.
TICK_MS = 2000


def open_dialog(cfg, parent, relic_panel=None) -> None:
    """Open the calibration, with its test shown on the Rewards page.

    The one way in, for Settings, the Rewards page and Setup. The Rewards page
    rather than a line of text in the dialog: the question being asked is "will
    the thing I use during a run work", and the answer is more convincing in the
    thing itself.
    """
    dialog = CalibrateDialog(cfg, parent)
    if relic_panel is not None:
        dialog.tested.connect(relic_panel.show_result)
    dialog.exec()
    dialog.deleteLater()


class CalibrateDialog(QDialog):
    """Mark the reward row once, on a real reward screen."""

    #: A finished test, for whoever wants to show it where it will be seen.
    tested = Signal(object)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Calibrate the reward regions")
        # 75em by 52em: the 1100x760 it was at 11pt, and in proportion to the
        # text at every other size - at 20pt the picture had 300 of 760px.
        dialog_size.open_at(self, 75, 52)

        self._frame_data = None
        #: How many rewards the picture shows, when its reading said so.
        self._read_count: int | None = None
        #: A sentence when the loaded picture does not have the shape of the
        #: game window; Save asks before going on while it is set.
        self.shape_warning = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.px(1.1), theme.px(0.95),
                                  theme.px(1.1), theme.px(0.95))
        layout.setSpacing(theme.space("close"))

        self.heading = QLabel("")
        self.heading.setStyleSheet(theme.text("section", weight=600))
        layout.addWidget(self.heading)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.note)

        # Shown only while the config sets reward-row keys: a calibration from
        # before the measured values silently overrides them, and nothing else
        # in the program said so.
        self.overrides_note = QLabel("")
        self.overrides_note.setWordWrap(True)
        self.overrides_note.setTextFormat(Qt.PlainText)
        self.overrides_note.setStyleSheet(theme.text("body", theme.WARN, 600))
        layout.addWidget(self.overrides_note)
        self.measured_btn = QPushButton("Use the measured values")
        self.measured_btn.setToolTip(
            "Remove your own reward-row values from config.toml, so the values "
            "measured for the program apply again.")
        self.measured_btn.clicked.connect(self._use_measured)
        measured_row = QHBoxLayout()
        measured_row.addWidget(self.measured_btn)
        measured_row.addStretch(1)
        layout.addLayout(measured_row)
        self._refresh_overrides()

        # Where the picture below comes from, in a sentence: a loaded file, the
        # game window just captured or the last reward screen the hotkey read
        # all look alike, and which one it is decides whether it can be trusted.
        self.source_note = QLabel("")
        self.source_note.setWordWrap(True)
        self.source_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.source_note)

        self.shape_note = QLabel("")
        self.shape_note.setWordWrap(True)
        self.shape_note.setStyleSheet(theme.text("body", theme.WARN, 600))
        self.shape_note.setVisible(False)
        layout.addWidget(self.shape_note)

        self.picker = RegionPicker(QPixmap())
        self.picker.placeholder = self._empty_text()
        self.picker.picked.connect(self._on_picked)
        self.picker.changed.connect(self._marks_changed)
        layout.addWidget(self.picker, 1)

        # What the keys adjust, the gap, and how many cards the row has.
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Adjusting:"))
        self.adjust_card = QRadioButton("the red card")
        self.adjust_band = QRadioButton("the green name band")
        self.adjust_card.setChecked(True)
        self.adjust_card.toggled.connect(self._adjust_chosen)
        controls.addWidget(self.adjust_card)
        controls.addWidget(self.adjust_band)
        controls.addSpacing(theme.px(1.0))
        controls.addWidget(QLabel("Gap between cards:"))
        self.gap_spin = QSpinBox()
        self.gap_spin.setRange(-1, 400)
        self.gap_spin.setSuffix(" px")
        # Before a card is drawn there is no gap in pixels to show: a "0 px"
        # there read as the gap in force, so it shows a dash.
        self.gap_spin.setSpecialValueText("–")
        self.gap_spin.setValue(-1)
        self.gap_spin.setEnabled(False)
        self.gap_spin.valueChanged.connect(self._gap_chosen)
        controls.addWidget(self.gap_spin)
        controls.addSpacing(theme.px(1.0))
        controls.addWidget(QLabel("Cards on this screen:"))
        self.count_box = QComboBox()
        for count in (2, 3, 4):
            self.count_box.addItem(str(count), count)
        self.count_box.setCurrentIndex(2)
        self.count_box.currentIndexChanged.connect(self._count_chosen)
        controls.addWidget(self.count_box)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.keys_note = QLabel(KEYS_HINT)
        self.keys_note.setWordWrap(True)
        self.keys_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.keys_note)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.result)

        buttons = QHBoxLayout()
        # First, because it is the way that works: a real reward screen the
        # tool itself captured, with no second program and no race against the
        # fifteen seconds. Shown only while there is one.
        self.last_btn = QPushButton("")
        self.last_btn.clicked.connect(lambda _checked=False: self._use_last())
        buttons.addWidget(self.last_btn)
        grab_btn = QPushButton("Capture the game window")
        grab_btn.clicked.connect(self._capture)
        buttons.addWidget(grab_btn)
        load_btn = QPushButton("Load a screenshot...")
        load_btn.clicked.connect(self._load)
        buttons.addWidget(load_btn)
        self.current_btn = QPushButton("Start from the current regions")
        self.current_btn.setToolTip(
            "Put the red card, the gap and the green band where the regions in "
            "effect now lie, to fit them from there.")
        self.current_btn.setEnabled(False)
        self.current_btn.clicked.connect(self._start_from_current)
        buttons.addWidget(self.current_btn)
        self.test_btn = QPushButton("Read the names")
        self.test_btn.setToolTip(
            "Crop the name regions out of this picture and read them, "
            "so the regions can be checked here rather than mid-mission.")
        self.test_btn.setEnabled(False)
        self.test_btn.clicked.connect(self._test_read)
        buttons.addWidget(self.test_btn)
        self.back_btn = QPushButton("Mark again")
        self.back_btn.clicked.connect(self._restart_marking)
        self.back_btn.setEnabled(False)
        buttons.addWidget(self.back_btn)
        buttons.addStretch(1)
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("Primary")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(self.save_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._show_step()

        # The last reward screen is the preferred picture, so it is loaded
        # without being asked for; the buttons below stay for everything else.
        self._refresh_last_button()
        if last_screen.current() is not None:
            self._use_last()
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._refresh_last_button)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._refresh_last_button()
        self._tick.start()
        self.picker.setFocus()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._tick.stop()

    def done(self, result: int) -> None:  # noqa: D102 - Qt override
        # Let go of the picture while the application still exists: a QPixmap
        # that Python frees after the QApplication is gone ends the process in
        # a segmentation fault, and a held frame is up to 25 MB.
        self.picker._source = QPixmap()
        self._frame_data = None
        super().done(result)

    def _empty_text(self) -> str:
        key = str(self.cfg.get("hotkey.key", "") or "").strip()
        press = f"press {key}" if key else "press your hotkey (see Setup)"
        return (
            "No picture yet.\n\n"
            f"A picture comes from a relic run: do one, {press} on the reward "
            "screen, and come back here. This dialog then offers that screen as "
            "\"Use the last reward screen\".\n\n"
            "Or, with Warframe open on a reward screen, \"Capture the game "
            "window\"; or \"Load a screenshot...\" of the game window alone.\n\n"
            "Calibrating is only needed when the names come out wrong. The "
            "default regions are measured for a 16:9 window.")

    # -- getting a picture ---------------------------------------------------
    def _refresh_last_button(self) -> None:
        screen = last_screen.current()
        self.last_btn.setVisible(screen is not None)
        if screen is not None:
            self.last_btn.setText(
                f"Use the last reward screen ({last_screen.age_words(screen.age_seconds())})")

    def _use_last(self) -> None:
        screen = last_screen.current()
        if screen is None:
            return
        reading = screen.reading
        how = ("it could not be read at all" if reading.failed else
               f"{reading.recognised} of {reading.total} names were recognised")
        self._set_image(self._pixmap_of(screen.image), screen.image,
                        f"The last reward screen the hotkey read "
                        f"({last_screen.age_words(screen.age_seconds())}; {how}). "
                        f"It is the whole game window, kept in memory only.",
                        count=None if reading.failed else reading.total)

    @staticmethod
    def _pixmap_of(frame) -> QPixmap:
        """A BGR array as a pixmap. Copied, so the array may go away or stay."""
        return QPixmap.fromImage(QImage(
            frame.data, frame.shape[1], frame.shape[0],
            frame.strides[0], QImage.Format_BGR888).copy())

    def _capture(self):
        """Grab the game window as it is right now.

        Live rather than from a file wherever possible: a screenshot saved by
        another tool can be cropped, scaled or include the desktop, and every
        one of those makes the fractions wrong in a way nothing later can
        detect.
        """
        window_class = str(self.cfg.get("game.window_class", "") or "")
        try:
            game = cap.find_game(window_class)
            if game is None:
                QMessageBox.warning(
                    self, "Game not found",
                    f"No window of class {window_class!r} is open.\n\n"
                    f"Open Warframe on a reward screen, or load a screenshot "
                    f"instead.")
                return
            frame = cap.grab(game)
        except cap.CaptureError as exc:
            QMessageBox.warning(self, "Capture failed", str(exc))
            return
        image = cap.to_bgr(frame)
        self._set_image(self._pixmap_of(image), image,
                        "The game window, captured just now.")

    def _load(self):
        path = file_dialogs.open_file(
            self, "A screenshot of a reward screen", "",
            "Images (*.png *.jpg *.jpeg *.bmp);;All files (*)")
        if not path:
            return
        import cv2

        pixmap = QPixmap(path)
        frame = cv2.imread(path)
        if pixmap.isNull() or frame is None:
            QMessageBox.warning(self, "Not an image", f"Could not read {path}.")
            return
        self._set_image(pixmap, frame, f"Loaded from {path}.",
                        check_shape=True)

    def _game_size(self) -> tuple[int, int] | None:
        """The running game window's size, or None when there is none to ask."""
        window_class = str(self.cfg.get("game.window_class", "") or "")
        if not window_class:
            return None
        try:
            game = cap.find_game(window_class)
        except Exception:  # noqa: BLE001 - no display, no X: nothing to compare
            return None
        return (game.width, game.height) if game is not None else None

    def _shape_warning(self, frame) -> str:
        """Why a loaded picture cannot be a screenshot of the game window, or "".

        The regions are fractions of the game window. A picture of the whole
        desktop, of two monitors side by side or with a window frame marks them
        on the wrong pixels, and the numbers it gives look just as plausible.
        """
        picture = (int(frame.shape[1]), int(frame.shape[0]))
        game = self._game_size()
        mismatch = shape.check(picture, game)
        if mismatch is None:
            return ""
        if game is not None:
            against = (f"The game window is {shape.describe(game)} and this "
                       f"picture is {shape.describe(picture)}.")
        else:
            against = (f"This picture is {shape.describe(picture)}, which is not "
                       f"the shape of a game window (the game is not running, "
                       f"so it cannot be compared with it).")
        return (f"{against} The regions are measured as fractions of the game "
                f"window, so the picture must show the game and nothing else — "
                f"not the whole desktop, not a second monitor, not a window "
                f"frame. Take the screenshot of the game window alone, or use "
                f"the last reward screen instead.")

    def _set_image(self, pixmap: QPixmap, frame=None, source: str = "",
                   check_shape: bool = False, count: int | None = None):
        """Show a picture. `count` is how many rewards its reading found, when
        it was read: the row then starts with that many cards."""
        self._frame_data = frame
        self._read_count = count if count in (2, 3, 4) else None
        self.source_note.setText(source)
        self.shape_warning = (self._shape_warning(frame)
                              if check_shape and frame is not None else "")
        self.shape_note.setText(self.shape_warning)
        self.shape_note.setVisible(bool(self.shape_warning))
        self.picker._source = pixmap
        self.picker.clear_selection()
        self.picker.update()
        self._restart_marking()

    # -- marking -------------------------------------------------------------
    def _restart_marking(self):
        self.result.setText("")
        self.save_btn.setEnabled(False)
        self.test_btn.setEnabled(False)
        self.back_btn.setEnabled(False)
        self.picker.clear_selection()
        self.current_btn.setEnabled(not self.picker._source.isNull())
        self._set_count(self._read_count or self._configured_count())
        self._show_step()
        self.picker.setFocus()

    def _configured_count(self) -> int:
        count = int(self.cfg.get("game.reward_count", 4) or 4)
        return max(2, min(4, count))

    def _set_count(self, count: int) -> None:
        index = self.count_box.findData(count)
        self.count_box.blockSignals(True)
        self.count_box.setCurrentIndex(index if index >= 0 else 2)
        self.count_box.blockSignals(False)
        self.picker.marks.count = int(self.count_box.currentData())

    def _show_step(self):
        if self.picker.marks.card is None:
            self.heading.setText("Mark the leftmost reward card")
            self.note.setText(
                "Drag a box around the whole first reward on the left - artwork "
                "and name, without the coloured bar below it. The "
                "name band (green) and the other cards (orange) appear at once; "
                "then fit them with the keys.")
        else:
            self.heading.setText("Fit it, then save")
            self.note.setText(self._sanity_note())

    def _refresh_overrides(self) -> None:
        """Show the note and the button while the config overrides the row."""
        from wft.relic import regions as regions_mod

        found = regions_mod.configured_overrides(self.cfg)
        self.overrides_note.setVisible(bool(found))
        self.measured_btn.setVisible(bool(found))
        if not found:
            self.overrides_note.setText("")
            return
        lines = "\n".join(f"{key}: yours {value:.4f}, measured {builtin:.4f}"
                          for key, value, builtin in found)
        self.overrides_note.setText(
            "The tool is using your own calibration from config.toml, not the "
            f"measured values:\n{lines}")

    def _use_measured(self) -> None:
        """Take the reward-row keys out of config.toml, after asking."""
        from wft.relic import regions as regions_mod

        keys = [key for key, _v, _b in regions_mod.configured_overrides(self.cfg)]
        if not keys:
            self._refresh_overrides()
            return
        lines = settings_io.lines_with_keys("regions.reward", keys)
        answer = QMessageBox.question(
            self, "Use the measured values?",
            f"These lines are removed from [regions.reward] in\n"
            f"{settings_io.target_config_path()}:\n\n" + "\n".join(lines),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        settings_io.remove_keys(self.cfg, "regions.reward", keys)
        regions_mod.load_overrides(self.cfg)
        self._refresh_overrides()
        # The regions drawn on the picture follow the values now in force.
        if self.picker.marks.card is not None:
            self._start_from_current()

    def _on_picked(self, x: float, y: float, width: float, height: float):
        """The first card was drawn (fractions of the picture)."""
        self.picker.set_card_fractions(
            x, y, width, height, (NAME_TOP_IN_TILE, NAME_BOTTOM_IN_TILE))
        marks = self.picker.marks
        from wft.relic import regions as regions_mod

        marks.gap = round(regions_mod.TILE_GAP * max(1, marks.width))
        self._set_count(self._read_count or self._configured_count())
        self.back_btn.setEnabled(True)
        self._marks_changed()

    def _start_from_current(self):
        """Put the marks where the regions in effect lie, to fit them from there."""
        from wft.relic import regions as regions_mod

        marks = self.picker.marks
        self.picker.sync_size()
        if not marks.width:
            return
        count = int(self.count_box.currentData())
        tile = regions_mod.tile(0, count)
        marks.from_fractions({
            "tile_top": tile.y, "tile_height": tile.height,
            "tile_width": tile.width, "tile_gap": regions_mod.TILE_GAP,
            "row_centre_x": regions_mod.ROW_CENTRE_X,
            "name_top": regions_mod.NAME_TOP,
            "name_height": regions_mod.NAME_HEIGHT}, count)
        self.back_btn.setEnabled(True)
        self.picker.update()
        self._marks_changed()
        self.picker.setFocus()

    def _marks_changed(self):
        """Whatever moved: bring the controls and the numbers along."""
        marks = self.picker.marks
        self.adjust_card.blockSignals(True)
        self.adjust_card.setChecked(marks.mode == CARD)
        self.adjust_band.setChecked(marks.mode == BAND)
        self.adjust_card.blockSignals(False)
        marked = marks.card is not None
        self.gap_spin.blockSignals(True)
        self.gap_spin.setMinimum(0 if marked else -1)
        self.gap_spin.setValue(marks.gap if marked else -1)
        self.gap_spin.setEnabled(marked)
        self.gap_spin.blockSignals(False)
        self.save_btn.setEnabled(marked)
        self.test_btn.setEnabled(marked)
        if marked:
            self._summarise()
        self._show_step()

    def _adjust_chosen(self, card_checked: bool):
        marks = self.picker.marks
        if marks.card is None:
            return
        marks.mode = CARD if card_checked else BAND
        self.picker.update()
        self.picker.setFocus()

    def _gap_chosen(self, value: int):
        marks = self.picker.marks
        if marks.card is None:
            # Remembered for the card that is drawn next, which starts from the
            # configured gap again: the spin box shows what is in use.
            return
        marks.gap = int(value)
        self.picker.update()
        self._marks_changed()

    def _count_chosen(self, _index: int):
        self.picker.marks.count = int(self.count_box.currentData())
        self.picker.update()
        if self.picker.marks.card is not None:
            self._marks_changed()

    def _sanity_note(self) -> str:
        values = self._values()
        if values is None:
            return ""
        notes = []
        if not 0.05 < values["tile_width"] < 0.25:
            notes.append(
                f"A tile width of {values['tile_width']:.3f} of the window is "
                f"unusual. Four tiles should span roughly half the screen.")
        if not 0.3 < values["row_centre_x"] < 0.7:
            notes.append(
                f"The row would be centred at {values['row_centre_x']:.3f}, "
                f"which is a long way off centre. Did the first box land on "
                f"the leftmost tile?")
        if notes:
            return "  ".join(notes)
        return ("The red frame is the first card, the orange ones the rest of "
                "the row, the green band is where the names are read (both "
                "lines of a wrapped name). Check the numbers below, then "
                "\"Read the names\" and save.")

    def _values(self) -> dict[str, float] | None:
        """The region constants, as fractions of the picture, from the marks."""
        return self.picker.marks.fractions()

    def _summarise(self):
        marks = self.picker.marks
        x, y, w, h = marks.card
        pixels = (f"card x {x}, y {y}, {w} x {h} px  ·  gap {marks.gap} px  ·  "
                  f"name band y {y + marks.band_dy}, {marks.band_height} px high  "
                  f"(picture {marks.width} x {marks.height})")
        values = self._values()
        self.result.setText(
            pixels + "\n" + "  ·  ".join(f"{key} {value:.5f}" for key, value in values.items()))

    def _frame(self):
        """The loaded picture as a BGR array, or None.

        Kept from where the picture came in rather than read back out of the
        QPixmap. Going through `QImage.constBits` hands out a pointer into an
        image whose lifetime Python does not control, and reading through it
        killed the process outright — no traceback, because there is no Python
        error in a use-after-free. The capture path already has an array and
        the file path can be read as one, so neither ever needed to.
        """
        return self._frame_data

    def _test_read(self):
        """Run the real reading and ranking against this picture.

        Not a private reimplementation of it: the panel's own path, with the
        marked regions applied and the picture standing in for the game
        window. Calibrating is only worth anything if the reading then works,
        and until now the only way to find that out was pressing the hotkey
        during a mission — fifteen seconds, once per run, no second try.

        The result is handed to the relic panel, so what is checked here is
        exactly what will be seen there.
        """
        from ui.workers.relic_trigger import run_on_image
        from wft.relic import regions as regions_mod

        frame = self._frame()
        if frame is None or self._values() is None:
            return
        self.result.setText("Reading...")
        QApplication.processEvents()

        keys = ("tile_top", "tile_height", "tile_width", "tile_gap",
                "row_centre_x", "name_top", "name_height")
        saved = {key: getattr(regions_mod, key.upper()) for key in keys}
        try:
            # Passed in rather than set beforehand: the reader loads the config
            # over the module constants as its first act, so anything set here
            # would be gone before the first crop.
            # Against every prime part, not the relics the log currently
            # names: this picture was taken before whatever the log now
            # describes, and matching it against the wrong twenty names reads
            # the wrong parts and blames the regions for it.
            result = run_on_image(self.cfg, frame, regions=self._values(),
                                  use_log=False)
        except Exception as exc:  # noqa: BLE001 - a failed test is a result
            self.result.setText(f"Could not read: {exc}")
            return
        finally:
            for key, value in saved.items():
                setattr(regions_mod, key.upper(), value)

        if result.error:
            self.result.setText(result.error)
            return
        verdict = result.verdict
        seen = "   ·   ".join(
            f"{r.position}: {r.name or r.raw_text.strip() or '(nothing)'}"
            for r in verdict.rewards) if verdict else "(nothing)"
        self.result.setText(seen)
        self.tested.emit(result)

    def _save(self):
        if self.shape_warning:
            # Ask, don't forbid: the shape test cannot see everything and a
            # 32:9 monitor is a legitimate window. But the default answer is no.
            answer = QMessageBox.question(
                self, "This picture may not be the game window",
                f"{self.shape_warning}\n\nSave these regions anyway?",
                QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Save:
                return
        values = self._values()
        if values is None:
            return
        for key, value in values.items():
            settings_io.set_value("regions.reward", key, f"{value:.5f}")
        from wft.relic import regions as regions_mod

        regions_mod.load_overrides(self.cfg)
        self._refresh_overrides()
        QMessageBox.information(
            self, "Saved",
            "The reward regions are written to config.toml and are in effect "
            "now. Press the hotkey on a reward screen to check them.")
        self.accept()
