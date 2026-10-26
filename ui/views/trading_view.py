"""The two trading lists: Prime parts I want, and Prime parts I offer.

One class, two pages (`kind` is "want" or "offer"). What the lists contain is
decided in `wft/trading.py`; this page only filters, shows, books and copies.
The table, the data-age line and the message all read the same `Row` list, so a
number in the message cannot differ from the number on the screen.

Two things on the page write: "Got one" adds a piece through the function the
Collection page's "Add and correct" uses, and "It's gone" takes a spare piece
off the shelf through `trade_spare`, which refuses to go below what a set
still needs. Nothing here sells or sends anything; the user pastes the message.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import file_dialogs, settings_io, theme
from ui.views.ducat_view import GONE, _action, _clickable
from ui.widgets.filter_chips import ALL, FilterChips
from ui.widgets.market_lookup import MarketLookup
from ui.widgets.tables import Sort, order_by, simple_table, sortable
from wft import trading
from wft.data import db as dbq

#: Page name in the navigation -> kind.
PAGES = {"I want": "want", "I offer": "offer"}

GOT_ONE = "Got one"
#: Highest value of the "I want" limit; 0 is no limit.
MAX_MISSING_LIMIT = 20

_NOTHING_TO_COPY = "Nothing to copy: no part matches the filters."
#: How long "Copy message" reads "Copied" after a click.
COPIED_MS = 2000


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _today() -> dt.date:
    return dt.date.today()


def age_words(stamp: str | None, now: dt.datetime | None = None) -> str:
    """"today", "1 d ago", "3 d ago" for a stored UTC time; "" without one."""
    return trading.age_words(stamp, now or _now())


def _parse(stamp: str | None) -> dt.datetime | None:
    return trading._stamp(stamp)


def _when(stamp: str | None, missing: str) -> str:
    when = _parse(stamp)
    if when is None:
        return missing
    return f"{when:%Y-%m-%d} ({age_words(stamp)})"


class TradingView(MarketLookup, QWidget):
    _hidden_sets = 0          # sets the "I want" limit hides, set by `rows`

    def __init__(self, cfg, kind: str, parent=None):
        super().__init__(parent)
        if kind not in ("want", "offer"):
            raise ValueError(f"kind is 'want' or 'offer', not {kind!r}")
        self.cfg = cfg
        self.kind = kind
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        self.init_market_lookup()
        #: The header order of the table, kept here because the table is built
        #: again on every refresh.
        self._order = {"column": 0, "descending": False}
        #: Whether the user has set "Include parts without a price" themselves;
        #: until then it follows the limit, as the core's default does.
        self._unpriced_touched = False
        self._rows: list = []
        self._building = True
        self._build_ui()
        self._building = False
        # Built empty; `MainWindow.select` refreshes the page on the way in.

    # -- plumbing ---------------------------------------------------------
    def _build_ui(self) -> None:
        want = self.kind == "want"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("I want" if want else "I offer")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)
        subtitle = QLabel(
            "Prime sets you are missing parts of, as a message for the "
            "trading chat." if want else
            "Prime parts no set of yours needs, as a message for the "
            "trading chat.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        body = QVBoxLayout()
        body.setSpacing(theme.px(0.9))
        inner = QWidget()
        inner.setLayout(body)
        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        self.age_label = QLabel("")
        self.age_label.setWordWrap(True)
        self.age_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        body.addWidget(self.age_label)

        body.addLayout(self._build_filters())

        self.price_holder = QVBoxLayout()
        self.price_holder.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self.price_holder)

        self.table_holder = QVBoxLayout()
        self.table_holder.setContentsMargins(0, 0, 0, 0)
        body.addLayout(self.table_holder)

        body.addWidget(self._build_message())
        body.addStretch(1)

    def _build_filters(self) -> QVBoxLayout:
        want = self.kind == "want"
        layout = QVBoxLayout()
        layout.setSpacing(theme.px(0.45))

        row = QHBoxLayout()
        if want:
            row.addWidget(QLabel("Sets missing at most"))
            self.limit_spin = QSpinBox()
            self.limit_spin.setRange(0, MAX_MISSING_LIMIT)
            self.limit_spin.setValue(
                self._stored_int("max_missing", 2, 0, MAX_MISSING_LIMIT))
            row.addWidget(self.limit_spin)
            row.addWidget(QLabel("parts (0 = no limit)"))
        else:
            row.addWidget(QLabel("Cheapest seller asks at most"))
            self.limit_spin = QSpinBox()
            self.limit_spin.setRange(0, 9999)
            self.limit_spin.setValue(self._stored_int("max_platinum", 0, 0, 9999))
            row.addWidget(self.limit_spin)
            row.addWidget(QLabel("platinum (0 = no limit)"))
        # Typing a number commits on Enter or leaving the field; the arrows
        # commit at once. So a value being typed is never written half-way.
        self.limit_spin.setKeyboardTracking(False)
        self.limit_spin.valueChanged.connect(self._limit_changed)
        row.addStretch(1)
        layout.addLayout(row)

        self.hidden_label = QLabel("")
        self.hidden_label.setWordWrap(True)
        self.hidden_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.hidden_label.setVisible(False)
        layout.addWidget(self.hidden_label)

        if want:
            self.unmastered_box = QCheckBox("Only sets I have not mastered")
            self.unmastered_box.setChecked(True)
            self.vaulted_box = QCheckBox("Vaulted only")
            for box in (self.unmastered_box, self.vaulted_box):
                box.toggled.connect(self._filters_changed)
                layout.addWidget(box)
        else:
            self.unpriced_box = QCheckBox("Include parts without a price")
            self.unpriced_box.setChecked(not self.limit_spin.value())
            self.unpriced_box.clicked.connect(self._unpriced_clicked)
            self.unpriced_box.toggled.connect(self._filters_changed)
            layout.addWidget(self.unpriced_box)

        self.chips = FilterChips(list(trading.CATEGORIES.items()), all_label="All")
        self.chips.changed.connect(self._filters_changed)
        layout.addWidget(self.chips)

        row = QHBoxLayout()
        row.addWidget(QLabel("Trade for"))
        self.trade_combo = QComboBox()
        for value, label in trading.TRADE_FOR.items():
            self.trade_combo.addItem(label, value)
        stored = trading.trade_for_or_default(
            self.cfg.get(f"trading.{self.kind}_trade_for", trading.DEFAULT_TRADE_FOR))
        self.trade_combo.setCurrentIndex(self.trade_combo.findData(stored))
        self.trade_combo.currentIndexChanged.connect(self._trade_for_changed)
        row.addWidget(self.trade_combo)
        row.addStretch(1)
        layout.addLayout(row)
        # Which parts have no price yet, when platinum is in the message.
        self.price_hint = QLabel("")
        self.price_hint.setWordWrap(True)
        self.price_hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.price_hint)
        return layout

    def _build_message(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.6))
        heading = QLabel("Message")
        heading.setObjectName("CardHeading")
        layout.addWidget(heading)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.preview.setMinimumHeight(theme.px(11))
        layout.addWidget(self.preview)

        row = QHBoxLayout()
        self.copy_button = QPushButton("Copy message")
        self.copy_button.setObjectName("Primary")
        self.copy_button.clicked.connect(self._copy)
        self.csv_button = QPushButton("Save as CSV...")
        self.csv_button.clicked.connect(self._save_csv)
        self.html_button = QPushButton("Save as HTML...")
        self.html_button.clicked.connect(self._save_html)
        self.browser_button = QPushButton("Open in browser")
        self.browser_button.clicked.connect(self._open_in_browser)
        self.copy_note = QLabel("")
        self.copy_note.setWordWrap(True)
        self.copy_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        # Right beside the button and in the colour of a success: in grey under
        # the second row of buttons the "Copied" went unseen.
        self.copied_note = QLabel("")
        self.copied_note.setWordWrap(True)
        self.copied_note.setStyleSheet(theme.text("body", theme.GOOD, 600))
        row.addWidget(self.copy_button)
        row.addWidget(self.copied_note, 1)
        layout.addLayout(row)
        # A second row: four buttons in one do not fit at the largest text size.
        row = QHBoxLayout()
        for button in (self.csv_button, self.html_button, self.browser_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self.copy_note)
        hint = QLabel(
            "Save as CSV opens in a spreadsheet; Save as HTML makes a page to "
            "look at, print or send. Open in browser writes that page to "
            f"{self._browser_path()}, replacing it each time.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(hint)
        return card

    # -- the settings ------------------------------------------------------
    def _stored_int(self, key: str, default: int, low: int, high: int) -> int:
        value = self.cfg.get(f"trading.{key}", default)
        return max(low, min(high, value)) if isinstance(value, int) else default

    def _save(self, key: str, value) -> None:
        try:
            settings_io.save_value(self.cfg, "trading", key, value)
        except OSError as exc:
            self.copy_note.setText(f"Could not save the setting: {exc}")

    def trade_for(self) -> str:
        """The setting the message follows: "ducats", "platinum" or "both"."""
        return trading.trade_for_or_default(self.trade_combo.currentData())

    def _trade_for_changed(self, _index: int) -> None:
        if self._building:
            return
        name = f"{self.kind}_trade_for"
        stored = trading.trade_for_or_default(
            self.cfg.get(f"trading.{name}", trading.DEFAULT_TRADE_FOR))
        if self.trade_for() != stored:
            self._save(name, self.trade_for())
        self._update_preview()

    def _limit_changed(self, value: int) -> None:
        if self._building:
            return
        self._save("max_missing" if self.kind == "want" else "max_platinum", value)
        if self.kind == "offer" and not self._unpriced_touched:
            # The core's default: parts without a price stay when there is no
            # limit, go when there is one.
            self.unpriced_box.blockSignals(True)
            self.unpriced_box.setChecked(not value)
            self.unpriced_box.blockSignals(False)
        self._filters_changed()

    def _unpriced_clicked(self, _checked: bool) -> None:
        self._unpriced_touched = True

    # -- the rows -----------------------------------------------------------
    def _categories(self):
        active = self.chips.active()
        return None if ALL in active else active

    def rows(self) -> list:
        """The list as the filters leave it, in the order of the message."""
        if self.kind == "want":
            rows, self._hidden_sets = trading.want_list(
                self.conn, max_missing=self.limit_spin.value(),
                only_unmastered=self.unmastered_box.isChecked(),
                categories=self._categories(),
                vaulted_only=self.vaulted_box.isChecked())
            return rows
        return trading.offer_rows(
            self.conn, max_platinum=self.limit_spin.value(),
            include_unpriced=self.unpriced_box.isChecked(),
            categories=self._categories())

    def rows_before_price_filter(self) -> list:
        """The list a price lookup counts: the price filter can only see
        prices that exist, so it must not decide which parts get asked."""
        if self.kind == "want":
            return self.rows()
        return trading.offer_rows(self.conn, max_platinum=0,
                                  include_unpriced=True,
                                  categories=self._categories())

    def csv_text(self, rows: list | None = None) -> str:
        return trading.to_csv(self._rows if rows is None else rows, self.kind)

    def html_text(self, rows: list | None = None) -> str:
        """The web page for the rows on screen, in the active theme's colours."""
        return trading.to_html(
            self._rows if rows is None else rows, self.kind,
            trade_for=self.trade_for(), max_missing=self._max_missing(),
            as_of=_today(), now=_now(),
            colours={"bg": theme.BG, "card": theme.BG_CARD, "border": theme.BORDER,
                     "text": theme.TEXT, "dim": theme.TEXT_DIM,
                     "accent": theme.ACCENT})

    def message(self, rows: list | None = None) -> str:
        return trading.to_markdown(
            self.rows() if rows is None else rows, self.kind,
            trade_for=self.trade_for(), max_missing=self._max_missing(),
            as_of=_today())

    def _max_missing(self) -> int:
        return self.limit_spin.value() if self.kind == "want" else 2

    # -- showing it ----------------------------------------------------------
    def _filters_changed(self, *_args) -> None:
        if not self._building:
            self.refresh()

    def refresh(self) -> None:
        self._market_widgets = {}
        _empty(self.price_holder)
        _empty(self.table_holder)

        age = trading.data_age(self.conn)
        self.age_label.setText(
            f"Stock last changed: {_when(age.stock_changed, 'never recorded')}. "
            f"Mastery last imported: "
            f"{_when(age.mastery_imported, 'not recorded yet')}. "
            "Import first if either is old.")

        rows = self.rows()
        self._show_hidden_sets()
        self.price_holder.addWidget(self._market_row(
            [r.part_unique for r in self.rows_before_price_filter()], "list"))
        self._table(rows)

        self._rows = rows
        self._update_preview()
        self.copy_note.setText("" if rows else _NOTHING_TO_COPY)
        self.copied_note.setText("")

    def _show_hidden_sets(self) -> None:
        """"I want": how many sets the limit hides, from the same call as the rows."""
        hidden = self._hidden_sets if self.kind == "want" else 0
        self.hidden_label.setText(
            f"{hidden} more set(s) miss more than {self.limit_spin.value()} "
            "parts; 0 lists them all." if hidden else "")
        self.hidden_label.setVisible(bool(hidden))

    def _update_preview(self, *_args) -> None:
        """The message for the rows on screen; the setting does not refilter."""
        self.preview.setPlainText(self.message(self._rows))
        missing = trading.missing_prices(self._rows)
        self.price_hint.setText(
            f"{missing} of {len(self._rows)} parts have no price yet; "
            "Look up platinum prices asks for them."
            if missing and self.trade_for() != "ducats" else "")
        for button in (self.copy_button, self.csv_button, self.html_button,
                       self.browser_button):
            button.setEnabled(bool(self._rows))

    def _table(self, rows: list) -> None:
        want = self.kind == "want"
        if not rows:
            note = QLabel("No part matches the filters.")
            note.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            self.table_holder.addWidget(note)
            return

        headers = ["Category", "Item", "Part", "Count", "Ducats", "Platinum",
                   "Set"] + (["Needs"] if want else []) + [
                   "Got it?" if want else "Gone?"]
        shown = list(rows)
        table = simple_table(headers, len(shown), stretch=1, scroll_x=True)
        rank = {key: i for i, key in enumerate(trading.CATEGORIES)}
        needs = trading.TEXTS["needs"]

        def need_text(row) -> str:
            return ", ".join(needs.format(n=n, item=item) for n, item in row.needs)

        columns = {
            0: Sort(lambda r: rank[r.category]),
            1: Sort(lambda r: r.item.lower()),
            2: Sort(lambda r: r.part.lower()),
            3: Sort(lambda r: r.count, descending_first=True),
            4: Sort(lambda r: r.ducats or 0, descending_first=True),
            5: Sort(lambda r: -1 if r.sell_min is None else r.sell_min,
                    descending_first=True),
            6: Sort(lambda r: r.held / r.total if r.total else 0,
                    descending_first=True),
        }
        if want:
            columns[7] = Sort(need_text)
        owners = [None] * len(shown)

        def fill() -> None:
            order_by(shown, columns, self._order,
                     tiebreak=lambda r: (rank[r.category], r.item.lower()))
            owners[:] = [{"unique": r.item_unique, "name": r.item} for r in shown]
            for index, row in enumerate(shown):
                cells = [
                    trading.CATEGORIES[row.category], row.item, row.part,
                    f"x{row.count}",
                    str(row.ducats) if row.ducats else "—",
                    self._platinum_text(row), f"{row.held}/{row.total}"]
                if want:
                    cells.append(need_text(row))
                for column, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    if column == 5 and row.sell_min is not None:
                        cell.setForeground(QColor(theme.ACCENT))
                    elif text == "—":
                        cell.setForeground(QColor(theme.TEXT_FAINT))
                    table.setItem(index, column, cell)
                table.setItem(index, len(headers) - 1,
                              _action(GOT_ONE if want else GONE))

        fill()
        _clickable(self, table, owners)
        table.cellClicked.connect(
            lambda row, column: (self._act(shown[row])
                                 if column == len(headers) - 1 else None))
        table.setToolTip("Click an item to see its set, a heading to reorder, "
                         "or the last column once it is booked")
        sortable(table, columns, self._order, fill)
        self.table_holder.addWidget(table)

    @staticmethod
    def _platinum_text(row) -> str:
        if row.sell_min is None:
            return "—"
        return f"{row.sell_min}p · {age_words(row.price_at)}"

    # -- the actions ----------------------------------------------------------
    def _act(self, row) -> None:
        if self.kind == "want":
            self._got_one(row)
        else:
            self._gone(row)

    def _got_one(self, row) -> None:
        """One more of this part, written the way "Add and correct" writes it."""
        held = dbq.owned_count(self.conn, row.part_unique)
        dbq.set_part_count(self.conn, row.part_unique, held + 1, "manual")
        self.refresh()

    def _gone(self, row) -> None:
        """One spare piece left the shelf; the floor is `trade_spare`'s."""
        held = dbq.owned_count(self.conn, row.part_unique)
        name = f"{row.item} {row.part}"
        answer = QMessageBox.question(
            self, f"{name} — gone?",
            f"{name}: {held} → {held - 1}\n\n"
            f"1 of the {row.count} spare. What its set still needs stays on "
            f"the shelf.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            return
        dbq.trade_spare(self.conn, row.part_unique, 1, "manual")
        self.refresh()

    def _suggested(self, extension: str) -> str:
        name = "i-want" if self.kind == "want" else "i-offer"
        return f"{name}-{_today():%Y-%m-%d}.{extension}"

    def _browser_path(self) -> Path:
        name = "i-want" if self.kind == "want" else "i-offer"
        return self.cfg.data_dir_path() / "trading" / f"{name}.html"

    def _write(self, path: Path, text: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # newline="": the text is written as made, no line-end conversion.
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            self.copy_note.setText(f"Could not write {path}: {exc}")
            return False
        return True

    def _save_csv(self) -> None:
        path = file_dialogs.save_file(
            self, "Save the list as CSV", self._suggested("csv"),
            "CSV (*.csv);;All files (*)")
        if path and self._write(Path(path), self.csv_text()):
            self.copy_note.setText(f"Saved {len(self._rows)} row(s) to {path}.")

    def _save_html(self) -> None:
        path = file_dialogs.save_file(
            self, "Save the list as a web page", self._suggested("html"),
            "Web page (*.html);;All files (*)")
        if path and self._write(Path(path), self.html_text()):
            self.copy_note.setText(f"Saved the page to {path}.")

    def _open_in_browser(self) -> None:
        path = self._browser_path()
        if not self._write(path, self.html_text()):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.copy_note.setText(f"Opened {path}")

    def _copy(self) -> None:
        text = self.preview.toPlainText()
        # Only text/plain: the chat renders Markdown itself, and a rich-text
        # flavour would paste as formatting instead. `setText` makes exactly
        # that; a QMimeData of our own made the process end in a segmentation
        # fault under the offscreen platform.
        QApplication.clipboard().setText(text)
        self.copied_note.setText(f"Copied ({len(text)} characters). "
                                 "Paste it into the chat.")
        # The button answers too, for a moment, where the eye already is.
        self.copy_button.setText("Copied")
        QTimer.singleShot(COPIED_MS, lambda: self.copy_button.setText("Copy message"))


def _empty(layout) -> None:
    while layout.count():
        child = layout.takeAt(0)
        widget = child.widget()
        if widget is not None:
            widget.deleteLater()
