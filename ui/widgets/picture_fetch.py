"""Fetching the game's own item icons: by itself, and on a button.

The pictures make every list in this application recognisable rather than
readable, and they come from DE's content host. That is one of the few places
where the tool reaches out on the user's behalf, and a careless request here
has cost a connection before: probing a Warframe endpoint to see how it behaved
got an address flagged, which stopped the *game* logging in.

So it asks for little: only things the inventory holds, one picture every tenth
of a second, never twice, and at most one run at a time (`PictureFetcher` is
shared with the clock and with the import). It starts by itself after an applied
inventory import and otherwise at most once in 24 hours, and only when pictures
are missing; the switch on this card turns that off. A run can be stopped, and
the reload button starts one now. Every run is a row in `fetch_log`, and this
card says what the last one did and when the next one may come. Nothing happens
as a side effect of opening a page.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QStyle,
    QVBoxLayout,
)

from ui import settings_io, theme
from ui.workers import picture_fetch
from wft.data import db as dbq
from wft.data import pictures
from wft.profile import fetch

_HOW = {"auto": "automatic", "import": "after an import", "manual": "reload"}


class PictureFetch(QFrame):
    """Says what is missing, fetches it, and reports what arrived."""

    def __init__(self, cfg, connection, parent=None, fetcher=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = connection
        self.fetcher = fetcher or picture_fetch.shared(cfg)
        self._result = ""
        self._missing = 0
        self.setObjectName("Card")
        self._build_ui()
        self.fetcher.started.connect(self._on_started)
        self.fetcher.progress.connect(self._on_progress)
        self.fetcher.finished.connect(self._on_done)
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.5))

        heading = QLabel("Item pictures")
        heading.setObjectName("SectionHeader")
        layout.addWidget(heading)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(theme.text("small", theme.TEXT_DIM))
        layout.addWidget(self.note)

        row = QHBoxLayout()
        self.auto_box = QCheckBox("Fetch missing pictures by themselves")
        self.auto_box.setChecked(bool(self.cfg.get("pictures.auto_fetch", True)))
        self.auto_box.toggled.connect(self._set_auto)
        row.addWidget(self.auto_box)
        row.addStretch(1)
        self.reload_btn = QPushButton()
        self.reload_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_btn.setToolTip("Fetch missing pictures now")
        self.reload_btn.setAccessibleName("Fetch missing pictures now")
        self.reload_btn.clicked.connect(self._reload)
        row.addWidget(self.reload_btn)
        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.fetcher.cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.state_label)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        layout.addWidget(self.bar)

    # --- the switch ----------------------------------------------------------

    def _set_auto(self, on: bool):
        settings_io.set_value("pictures", "auto_fetch", bool(on))
        self.cfg.override("pictures.auto_fetch", bool(on))
        self.refresh()

    # --- what the card says --------------------------------------------------

    @staticmethod
    def _local(text: str) -> str:
        return fetch.parse_stamp(text).astimezone().strftime("%Y-%m-%d %H:%M")

    def refresh(self):
        held = dbq.thumbnail_count(self.conn)
        missing = len(dbq.missing_pictures(self.conn))
        if missing:
            self.note.setText(
                f"{held} stored, {missing} still to fetch. They come from "
                f"Warframe's own content host, one every tenth of a second, "
                f"and only for things you hold — about "
                f"{max(1, round(missing * 0.35 / 60))} minute(s).")
        else:
            self.note.setText(
                f"{held} stored, nothing missing. Pictures are fetched only "
                f"for things you did not hold before."
                if held else
                "No pictures yet. Import an inventory first — pictures are "
                "fetched for what you hold, not for the whole game.")

        lines = [self._result] if self._result else []
        try:
            last = fetch.last(self.conn, pictures.JOB)
        except Exception:  # noqa: BLE001 - a database that cannot say is shown as empty
            last = None
        if last is not None and not self._result:
            lines.append(f"Last run {self._local(last['at'])} "
                         f"({_HOW.get(last['kind'], last['kind'])}): "
                         f"{last['detail']}")
        if not self.auto_box.isChecked():
            lines.append("Automatic fetching is off.")
        else:
            when = fetch.next_due(self.conn, pictures.JOB) \
                if last is not None else None
            lines.append(
                "Next automatic run: "
                + ("at the next check, if pictures are missing, and after the "
                   "next import" if when is None or when <= fetch.utcnow()
                   else f"after {when.astimezone().strftime('%Y-%m-%d %H:%M')}, "
                        "and after the next import"))
        self.state_label.setText("\n".join(lines))
        self._missing = missing
        self._sync_busy()

    def _sync_busy(self):
        busy = self.fetcher.busy
        self.reload_btn.setEnabled(not busy and self._missing > 0)
        self.reload_btn.setToolTip("Fetching pictures..." if busy
                                   else "Fetch missing pictures now")
        self.cancel_button.setVisible(busy)
        self.bar.setVisible(busy)

    # --- running -------------------------------------------------------------

    def after_import(self) -> bool:
        """An inventory import was applied: fetch what it made necessary, if allowed."""
        if not pictures.enabled(self.cfg):
            return False
        return self.fetcher.start("import")

    def _reload(self):
        self._result = ""
        self.fetcher.start("manual")

    def _on_started(self):
        self._result = ""
        self.bar.setValue(0)
        self.bar.setMaximum(0)
        self._sync_busy()

    def _on_progress(self, text: str):
        done, _, total = text.partition("/")
        try:
            self.bar.setMaximum(int(total))
            self.bar.setValue(int(done))
        except ValueError:
            pass

    def _on_done(self, outcome):
        counts = outcome["counts"]
        self._result = f"Last run just now: {outcome['message']}."
        if counts.get("fetched"):
            self._result += " Open the Inventory to see them."
        self.refresh()
