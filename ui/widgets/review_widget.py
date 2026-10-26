"""One review decision, ported from an earlier attempt's correction widget:
scored candidates as radio buttons, a manual override underneath, and an
ignore option.

The original also showed the source crop on the left. That evidence is not
available here: `wft.inventory.scan` tracks tiles as spatial objects across a
whole recording and does not currently retain a per-cell crop image (see its
module docstring). Carrying that through would mean extending the scanner,
which is out of scope for this pass — the reading text and the candidate
scores are what is shown instead, honestly, rather than faking a thumbnail.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from rapidfuzz import fuzz, process
from PySide6.QtGui import QImage, QPixmap
from ui import theme
from ui.widgets.name_suggest import suggest_on_typing
from ui.widgets.tile_view import TilePicture
from PySide6.QtWidgets import (
    QButtonGroup, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QRadioButton, QSizePolicy, QSpinBox, QWidget,
    QVBoxLayout,
)


class ClickableLineEdit(QLineEdit):
    """A line edit that reports being clicked.

    Qt gives a disabled widget no mouse events at all, so the usual "enable it
    when the radio is picked" arrangement makes the field unreachable by mouse.
    The field stays enabled and selects its own radio instead.
    """

    clicked = Signal()

    def mousePressEvent(self, event):  # noqa: N802 - Qt override
        self.clicked.emit()
        super().mousePressEvent(event)


def review_chord(event) -> str | None:
    """What a one-handed key on the review means, or None for any other key.

    The right hand sits on the arrows, Ctrl/Shift and the number pad.
    One place decides it for the list and for the open card, so the two cannot
    drift apart. Qt marks number-pad keys with `KeypadModifier`; it is ignored
    when comparing modifiers, and `+`, `-` and `0` are only taken from the pad,
    never from the main row.
    """
    key = event.key()
    mods = event.modifiers()
    pad = bool(mods & Qt.KeypadModifier)
    mods &= ~Qt.KeypadModifier
    if mods == Qt.NoModifier and pad:
        return {Qt.Key_Plus: "count_up", Qt.Key_Minus: "count_down",
                Qt.Key_0: "ignore"}.get(key)
    if mods == Qt.ControlModifier:
        return {Qt.Key_Up: "count_up", Qt.Key_Down: "count_down"}.get(key)
    if mods == Qt.ShiftModifier:
        return {Qt.Key_Down: "next_question", Qt.Key_Up: "previous_question",
                Qt.Key_Return: "previous_question",
                Qt.Key_Enter: "previous_question"}.get(key)
    return None


class ReviewWidget(QGroupBox):
    """`options` is [(display_name, score, part_unique, item_unique), ...]."""

    decided = Signal()  # a radio, manual text or ignore changed
    #: A navigation key (Up, Down, PageUp, PageDown, Home, End) pressed inside
    #: the card. The list owns movement: left to the radios and the count
    #: field these keys changed the answer or the number instead.
    navigate = Signal(int)
    #: Esc in the name field: hand the keyboard back to the list.
    released = Signal()
    #: A mouse wheel turn over the count field, for the list to scroll by.
    scrolled = Signal(object)
    #: A one-handed key (see `review_chord`) pressed inside the card; the view
    #: acts on it, so it means the same here as with the focus on the list.
    chord = Signal(str)

    def __init__(self, reading: str, occurrences: int,
                 options: list[tuple[str, float, str | None, str | None]],
                 evidence: bytes | None = None, known: list[str] | None = None,
                 reason: str = "", is_repeat: bool = False,
                 frame: bytes | None = None, bbox=None, count: int = 1,
                 resolve=None, parent=None):
        super().__init__(f'"{reading}"  —  {occurrences} tile(s)', parent)
        # Its own name, so the roomy padding a settings panel wants does not
        # reach a card that appears two hundred times in a list. Sharing the
        # generic rule took these from 223 pixels to 430 and halved how many
        # questions fit on a screen.
        self.setObjectName("ReviewCard")
        self.options = options
        self.reading = reading
        self.counted_at_scan = max(0, count)
        self.evidence = evidence
        self.frame = frame
        self.bbox = bbox
        #: Every name the database knows, for completion and for telling the
        #: user whether what they typed exists. Typing a name by hand and
        #: having it silently dropped is worse than not offering the field.
        self.known = known or []
        #: What a typed name would actually be written as - the same call the
        #: apply step makes, handed in because it needs a database connection
        #: this widget has no business holding. Without it the field cannot be
        #: judged at all, and saying nothing is honest; saying "matches" from a
        #: second, looser rule once showed a green tick and a refusal on the
        #: same text.
        self.resolve = resolve
        self.reason = reason
        self.is_repeat = is_repeat
        self._build_ui()
        # The spin box's own line edit takes keys before the box sees them,
        # so it is filtered as well: "+" and "-" would otherwise be typed.
        for control in (*self.radios, self.ignore_radio, self.manual_radio,
                        self.count, self.count.lineEdit(), self.manual_edit):
            control.installEventFilter(self)

    _LIST_KEYS = (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        if event.type() == QEvent.Wheel and obj in (self.count, self.count.lineEdit()):
            # Scrolling down the list past the field must not turn the
            # number (0 drops the part); the list scrolls instead.
            self.scrolled.emit(event)
            return True
        if event.type() != QEvent.KeyPress:
            return super().eventFilter(obj, event)
        key = event.key()
        action = review_chord(event)
        if action == "ignore" and obj in (self.count, self.count.lineEdit()):
            # A count of 10 typed on the number pad must not ignore the
            # reading at its second key.
            action = None
        if action is not None and obj is not self.manual_edit:
            self.chord.emit(action)
            return True
        if (key in (Qt.Key_Left, Qt.Key_Right)
                and obj not in (self.count, self.count.lineEdit(),
                                self.manual_edit)):
            # An exclusive group steps to the next radio on Left/Right and
            # ticks it: the answer changed under the reader's hand.
            return True
        if obj is self.manual_edit:
            if key == Qt.Key_Escape:
                self.released.emit()
                return True
            # The suggestion popup takes its own keys; when it is shown the
            # field gets them first, so leave those to it.
            completer = self.manual_edit.completer()
            if completer is not None and completer.popup().isVisible():
                return False
            if action == "previous_question" and key in (
                    Qt.Key_Return, Qt.Key_Enter):
                # Shift+Enter is not text; the other chords are digits, signs
                # and text selection in this field.
                self.chord.emit(action)
                return True
            keys = self._LIST_KEYS  # Home/End move the cursor in a text field
        elif obj is self.count.lineEdit():
            keys = self._LIST_KEYS
        else:
            keys = self._LIST_KEYS + (Qt.Key_Home, Qt.Key_End)
        if key in keys and not event.modifiers() & (
                Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            self.navigate.emit(int(key))
            return True
        return super().eventFilter(obj, event)

    def _build_ui(self):
        # Picture on the left, choices on the right. This is the layout the
        # previous attempt got right and the reason its review was usable:
        # deciding from a bare string means squinting at a name and hoping,
        # deciding with the tile in view takes a second.
        outer = QHBoxLayout(self)
        if self.evidence:
            # Clickable, like every other tile in the application. It was a
            # plain label here - on the one screen where opening the frame
            # actually answers something, because a reading that went wrong
            # usually went wrong because of what is next to the tile.
            picture = TilePicture(self.evidence, self.frame, self.bbox,
                                  caption=f'read as "{self.reading}"')
            image = QImage.fromData(self.evidence)
            if not image.isNull():
                pixmap = QPixmap.fromImage(image)
                # Sized against the text, not in fixed pixels. At 260px it was
                # wide enough to read at 11pt and wide enough to push the
                # option names off the panel at 14pt, where they truncated to
                # "Aklex Prime Blu…" - which is exactly the information the
                # picture is there to help decide between.
                # Sized against the text. Wide enough that the tile can be
                # judged without opening it, narrow enough that the option
                # names still fit beside it at the largest text size - and the
                # card is as tall as the options anyway, so the picture was
                # leaving that height unused at a third of it.
                em = self.fontMetrics().height()
                picture.setPixmap(pixmap.scaledToWidth(
                    max(170, min(260, em * 10)), Qt.SmoothTransformation))
                picture.setAlignment(Qt.AlignTop)
                picture.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
                outer.addWidget(picture)

        choices = QWidget()
        layout = QVBoxLayout(choices)
        layout.setContentsMargins(0, 0, 0, 0)
        # The choices get whatever is left and never less than they need: an
        # option the reader cannot finish reading is not an option.
        choices.setSizePolicy(QSizePolicy.MinimumExpanding, QSizePolicy.Preferred)
        outer.addWidget(choices, 1)
        if self.reason:
            why = QLabel(self.reason)
            why.setWordWrap(True)
            why.setStyleSheet(f"color: {theme.TEXT_FAINT}; padding-bottom: {theme.px(0.3)}px;")
            layout.addWidget(why)

        self.group = QButtonGroup(self)
        self.radios: list[QRadioButton] = []

        # Five fields, not four: relics carry their own identifier alongside
        # part and item. Unpacking four here raised ValueError while building
        # the review, which killed the handover from the scan - the window kept
        # showing the finished progress page and looked hung.
        # Two columns once there are more than three. Six stacked rows made
        # the card taller than the tile beside it and left the right half of
        # the panel empty, which in a list of two hundred rows is the
        # difference between seeing two questions and seeing five.
        shown = self.options[:6]
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.px(1.2))
        grid.setVerticalSpacing(theme.px(0.15))
        columns = 2 if len(shown) > 3 else 1
        for index, (name, score, *_ids) in enumerate(shown):
            label = f"{index + 1}.  {name}"
            if score:
                label += f"   ({score:.0f})"
            radio = QRadioButton(label)
            radio.toggled.connect(lambda _c: self.decided.emit())
            grid.addWidget(radio, index % ((len(shown) + columns - 1) // columns),
                           index // ((len(shown) + columns - 1) // columns))
            self.group.addButton(radio, index)
            self.radios.append(radio)
        layout.addLayout(grid)


        self.ignore_radio = QRadioButton(
            "Ignore — this tile is not a second one"
            if self.is_repeat else "Ignore — not a tracked item")
        self.ignore_radio.toggled.connect(lambda _c: self.decided.emit())
        self.group.addButton(self.ignore_radio, -2)

        # Two rows. The count, the manual field and Ignore were one row, and
        # at 20pt the field - the only one of the three with text typed into
        # it - was left 327px for a hint 939px long.
        answer_row = QHBoxLayout()
        manual_row = QHBoxLayout()
        self.manual_radio = QRadioButton("Manual:")
        self.manual_radio.toggled.connect(self._on_manual_toggled)
        self.manual_edit = ClickableLineEdit()
        # Said here because the reading is where it would otherwise have been
        # learned, and the reading is exactly what lost it: "Meso B9 Relic
        # [Exceptional]" comes back as "Meso B94ReI c" often enough that the
        # bracket has to be typeable.
        self.manual_edit.setPlaceholderText(
            "a name — a relic may carry its refinement, e.g. "
            "Meso B9 Relic [Exceptional]")
        # Clicking the field selects its radio. Leaving it disabled until the
        # radio is picked meant the field could not be clicked at all - the
        # only way in was the M shortcut, which is not discoverable and not
        # what anyone tries first.
        self.manual_edit.clicked.connect(lambda: self.manual_radio.setChecked(True))
        self.manual_edit.textChanged.connect(self._on_manual_text)
        if self.known:
            # Names that contain the text, then the closest ones for a typo
            # (the same list logic as Add and correct). Only the user's typing
            # refills it, so the text-changed handler above is not disturbed.
            self._manual_model = suggest_on_typing(
                self.manual_edit, lambda: self.known)
        self.group.addButton(self.manual_radio, -1)
        # How many of it. The card had no count field at all, which left the
        # one question that is *about* a count - "the stack read 13 in one
        # frame and 15 in another" - with no way to answer the part being
        # asked about. It sits beside the manual field because both are
        # "the tool got this wrong, here is the right answer".
        count_label = QLabel("How many:")
        self.count = QSpinBox()
        self.count.setRange(0, 99999)
        self.count.setValue(self.counted_at_scan)
        self.count.setToolTip("How many of this you actually have")
        # What five figures and the buttons need at this text size (130px was
        # 230 short at 20pt), the way AcceptedRow's field is sized.
        self.count.setFixedWidth(self.count.minimumSizeHint().width())
        self.count.valueChanged.connect(lambda _v: self.decided.emit())
        answer_row.addWidget(count_label)
        answer_row.addWidget(self.count)
        answer_row.addSpacing(theme.px(0.8))
        # Ignore beside the count rather than under the options: it is the
        # answer that is not one of the offered names, and needs no row of its
        # own.
        answer_row.addWidget(self.ignore_radio)
        answer_row.addStretch(1)
        layout.addLayout(answer_row)

        manual_row.addWidget(self.manual_radio)
        manual_row.addWidget(self.manual_edit, 1)
        layout.addLayout(manual_row)

        # Says whether what is typed will actually be written. Without it a
        # typo produced a row that resolved to nothing and was dropped without
        # a word - the work had been done and the item was still missing.
        self.manual_hint = QLabel("")
        self.manual_hint.setWordWrap(True)
        self.manual_hint.setStyleSheet(f"color: {theme.TEXT_FAINT}; padding-left: {theme.label_indent()}px;")
        self.manual_hint.setVisible(False)
        layout.addWidget(self.manual_hint)

        # Default chosen last, once every button exists. A repeat defaults to
        # Ignore rather than to the top match: the top match is usually perfect
        # - the tile really does read "Ash" - and confirming it records a second
        # Ash the user does not own, because the extra tile is a skin named
        # after the frame.
        if self.is_repeat:
            self.ignore_radio.setChecked(True)
        elif self.radios:
            self.radios[0].setChecked(True)


    def counted(self) -> int:
        """How many the reviewer says there are."""
        return self.count.value()

    def count_edited(self) -> bool:
        """Whether the number was changed from what the scan counted."""
        return self.count.value() != self.counted_at_scan

    def _on_manual_toggled(self, checked: bool):
        self.manual_hint.setVisible(checked)
        if checked:
            self.manual_edit.setFocus()
        self._on_manual_text(self.manual_edit.text())

    def _on_manual_text(self, text: str):
        """Say now whether this name exists, and what was probably meant."""
        self.decided.emit()
        text = text.strip()
        if not self.manual_radio.isChecked() or not text:
            self.manual_hint.setText("")
            return
        if not self.known:
            return

        bare, _refinement = self._without_refinement(text)
        found = self.resolve(text) if self.resolve is not None else None
        if found is not None:
            # Say what will be written, not merely that something will. The
            # user types "neo c9 radi" and the answer needed is the whole
            # name it became, refinement included - that is the part that
            # cannot be checked once the review is applied.
            self.manual_hint.setText(
                "matches a known item" if found.exact and found.name == text
                else f"will be written as \u201c{found.name}\u201d")
            self.manual_hint.setStyleSheet(
                f"color: {theme.GOOD}; padding-left: {theme.label_indent()}px;")
            return

        # Suggest against the name without its refinement. Offering
        # "did you mean Meso B9 Relic?" for "Meso B9 Relic [Radiant]" reads as
        # a rejection of the refinement, which is the one part that was right.
        near = process.extractOne(bare, self.known, scorer=fuzz.ratio,
                                  processor=str.casefold)
        if near and near[1] >= 70:
            self.manual_hint.setText(
                f"no such name — did you mean \u201c{near[0]}\u201d?")
        else:
            self.manual_hint.setText(
                "no such name — this will not be written")
        self.manual_hint.setStyleSheet(f"color: {theme.BAD}; padding-left: {theme.label_indent()}px;")

    def manual_is_resolvable(self) -> bool:
        """True when the typed name exists, or nothing was typed."""
        if not self.manual_radio.isChecked():
            return True
        text = self.manual_edit.text().strip()
        if not text:
            return True
        if self.resolve is None:
            # Nothing to ask. The apply step still collects what it could not
            # resolve and says so, so an unjudged field loses nothing.
            return True
        return self.resolve(text) is not None

    @staticmethod
    def _without_refinement(text: str) -> tuple[str, str | None]:
        """Split a relic's refinement off, the way the writer does.

        The two used to disagree. The field's own placeholder asks for
        "Meso B9 Relic [Exceptional]", and applying the review resolves it -
        strips the bracket, looks the relic up, records that refinement's own
        stack. Only this check did not, so typing exactly what was asked for
        answered "no such name - this will not be written", which was both a
        refusal and untrue.
        """
        from wft.inventory import quantity as quantity_mod

        return quantity_mod.strip_refinement(text)

    def select_index(self, index: int) -> None:
        """Digit-key selection: 1-based, radios only (not manual/ignore)."""
        if 0 <= index < len(self.radios):
            self.radios[index].setChecked(True)

    def focus_manual(self) -> None:
        self.manual_radio.setChecked(True)

    def set_ignored(self) -> None:
        self.ignore_radio.setChecked(True)

    def was_ignored(self) -> bool:
        """Dismissed on purpose, as opposed to left unanswered.

        Both return None from `decision`, and they mean opposite things: one is
        "this is not a tracked item", the other is "I did not look". Only the
        first is worth remembering.
        """
        return self.ignore_radio.isChecked()

    def decision(self):
        """(part_unique, item_unique, relic_unique, display_name), or None."""
        if self.ignore_radio.isChecked():
            return None
        if self.manual_radio.isChecked():
            text = self.manual_edit.text().strip()
            return (None, None, None, text) if text else None
        for index, radio in enumerate(self.radios):
            if radio.isChecked():
                name, _score, *ids = self.options[index]
                part_unique, item_unique, relic_unique = (list(ids) + [None] * 3)[:3]
                return (part_unique, item_unique, relic_unique, name)
        return None
