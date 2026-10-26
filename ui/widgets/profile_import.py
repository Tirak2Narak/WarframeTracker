"""Mastery from the account's own profile page, as a card of its own.

It lived on the Inventory page next to marking a single item by hand, which
put the one bulk import that is not a scan on a different page from every
other import.

With an account id set, the tool fetches the profile itself, once a day
(`ui.scheduler`, `ui.workers.profile_fetch`, `wft.profile.fetch`), and this
card shows when, what came back and when the next one is due. A reload button
fetches now, without a lock but with a warning once 5 requests lie inside 24
hours. Copying the address and loading a file the user saved stay as the second
way in. Fetching is kept slow and rare because a dozen requests in some
minutes once got an address flagged, and Warframe then refused the game's
own login.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QStyle, QVBoxLayout,
)

from ui import file_dialogs, settings_io, theme
from ui.workers import profile_fetch
from wft.data import db as dbq
from wft.profile import fetch


class ProfileImport(QFrame):
    """Account id, the address to open, and the saved file to read back."""

    imported = Signal()

    def __init__(self, cfg, connection, parent=None, fetcher=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = connection
        self.fetcher = fetcher or profile_fetch.shared(cfg)
        self.setObjectName("Card")
        self._pending = None
        self._build_ui()
        self.fetcher.started.connect(self._sync_busy)
        self.fetcher.finished.connect(self._fetched)
        self.refresh_state()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.7))

        header = QLabel("Mastery from your Warframe profile")
        header.setObjectName("CardHeading")
        layout.addWidget(header)

        # Everything levelled and sold before this tool existed leaves nothing
        # on screen to read, and for an account of any age that is most of the
        # collection. The profile carries the affinity earned on every item the
        # account has ever levelled, sold or not, keyed by the same identifier
        # this database uses.
        what = QLabel("Marks everything this account has ever levelled to its "
                      "rank cap, including items levelled and sold long ago. "
                      "A scan cannot see those — nothing about them is on "
                      "screen. Once your account id is in, the tool fetches "
                      "your profile by itself, once a day.")
        what.setWordWrap(True)
        what.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(what)

        row = QHBoxLayout()
        self.account_edit = QLineEdit(
            str(self.cfg.get("profile.account_id", "") or ""))
        self.account_edit.setPlaceholderText("Account id — 24 hex characters")
        self.account_edit.textChanged.connect(self._update_profile_url)
        self.account_edit.editingFinished.connect(self._save_account_id)
        copy_btn = QPushButton("Copy address")
        copy_btn.clicked.connect(self._copy_profile_url)
        load_btn = QPushButton("Load saved profile...")
        load_btn.clicked.connect(self._load_mastery)
        row.addWidget(self.account_edit, 3)
        row.addWidget(copy_btn, 1)
        row.addWidget(load_btn, 1)
        layout.addLayout(row)

        auto_row = QHBoxLayout()
        self.auto_box = QCheckBox("Fetch my profile once a day")
        self.auto_box.setChecked(bool(self.cfg.get("profile.auto_fetch", True)))
        self.auto_box.toggled.connect(self._set_auto)
        self.reload_btn = QPushButton()
        self.reload_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_BrowserReload))
        self.reload_btn.setToolTip("Fetch my profile now")
        self.reload_btn.setAccessibleName("Fetch my profile now")
        self.reload_btn.clicked.connect(self._reload)
        auto_row.addWidget(self.auto_box)
        auto_row.addStretch(1)
        auto_row.addWidget(self.reload_btn)
        layout.addLayout(auto_row)

        self.state_label = QLabel("")
        self.state_label.setWordWrap(True)
        self.state_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.state_label)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {theme.WARN};")
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

        review_row = QHBoxLayout()
        self.review_btn = QPushButton("Review...")
        self.review_btn.setObjectName("Primary")
        self.review_btn.clicked.connect(self._review)
        self.review_btn.hide()
        review_row.addWidget(self.review_btn)
        review_row.addStretch(1)
        layout.addLayout(review_row)

        self.profile_url_label = QLabel("")
        self.profile_url_label.setWordWrap(True)
        self.profile_url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.profile_url_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: monospace;")
        layout.addWidget(self.profile_url_label)

        # The id has to come from the browser once; everything after it is
        # done here. "Copy address" and "Load saved profile..." are the way
        # in when the server refuses the tool's own request.
        self.profile_feedback = QLabel(
            "Find your id once: log in at warframe.com, press F12, open "
            "Storage (Firefox) or Application (Chrome), then Cookies, "
            "https://www.warframe.com, select the cookie \"user-info\" and "
            "copy \"user_id\" from its parsed value into the field above. "
            "If the server ever refuses the tool, copy the address, open it "
            "in your browser, save the page as a file and load it here.")
        self.profile_feedback.setWordWrap(True)
        self.profile_feedback.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.profile_feedback)
        self._update_profile_url()

    # --- the account id and the switch ---------------------------------------

    def _update_profile_url(self):
        from wft.profile import mastery as profile_mod

        account_id = self.account_edit.text().strip()
        self.profile_url_label.setText(
            profile_mod.profile_url(account_id) if account_id else "")

    def _save_account_id(self) -> bool:
        """Store the id as soon as it is valid, not only after a file was loaded."""
        account_id = self.account_edit.text().strip().lower()
        if not fetch.valid_id(account_id):
            self.refresh_state()
            return False
        if account_id != str(self.cfg.get("profile.account_id", "") or ""):
            settings_io.set_value("profile", "account_id", account_id)
            self.cfg.override("profile.account_id", account_id)
        self.refresh_state()
        return True

    def _set_auto(self, on: bool):
        settings_io.set_value("profile", "auto_fetch", bool(on))
        self.cfg.override("profile.auto_fetch", bool(on))
        self.refresh_state()

    # --- what the card says --------------------------------------------------

    @staticmethod
    def _local(text_or_dt) -> str:
        moment = fetch.parse_stamp(text_or_dt) if isinstance(text_or_dt, str) \
            else text_or_dt
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")

    def refresh_state(self):
        """Read the log and write the card's lines. Everything comes from the database."""
        # The fetcher's clock, not a second one: the 24 hours the warning
        # counts are the same 24 hours the fetcher stamps its requests in.
        now = self.fetcher.clock()
        have_id = fetch.valid_id(self.cfg.get("profile.account_id", ""))
        lines = []
        try:
            last = fetch.last(self.conn)
            recent = fetch.count_recent(self.conn, now)
            pending = fetch.pending_review(self.conn, self.cfg)
        except Exception:  # noqa: BLE001 - a database that cannot say is shown as empty
            last, recent, pending = None, 0, None

        if last is None:
            lines.append("Not fetched yet.")
        else:
            how = "automatic" if last["kind"] == "auto" else "reload"
            code = (f"HTTP {last['status']}" if last["status"] is not None
                    else "no answer")
            lines.append(f"Last fetch {self._local(last['at'])} ({how}): "
                         f"{code} — {last['detail']}")
        path = fetch.profile_path(self.cfg)
        if path.is_file():
            lines.append(f"Kept copy: {path}")
        if not have_id:
            lines.append("Enter your account id to fetch automatically.")
        elif not self.auto_box.isChecked():
            lines.append("Automatic fetching is off.")
        else:
            when = fetch.next_due(self.conn) if last is not None else None
            lines.append("Next automatic fetch: "
                         + ("at the next check" if when is None or when <= now
                            else f"after {self._local(when)}"))
        self.state_label.setText("\n".join(lines))

        if recent >= fetch.WARN_COUNT:
            self.warning_label.setText(
                f"{recent} requests in the last 24 hours. Many requests in a "
                "short time have already made Warframe's server lock an address "
                "out of the game's login. Wait before fetching again.")
            self.warning_label.show()
        else:
            self.warning_label.hide()

        self._pending = pending
        self.review_btn.setVisible(pending is not None)
        self._sync_busy()

    def _sync_busy(self):
        busy = self.fetcher.busy
        self.reload_btn.setEnabled(
            not busy and fetch.valid_id(self.cfg.get("profile.account_id", "")))
        if busy:
            self.reload_btn.setToolTip("Fetching your profile...")
        else:
            self.reload_btn.setToolTip("Fetch my profile now")

    # --- fetching ------------------------------------------------------------

    def _reload(self):
        if not self._save_account_id():
            self.state_label.setText("Enter a valid account id first "
                                     "(24 hex characters).")
            return
        self.fetcher.start("manual")

    def _fetched(self, result):
        self.refresh_state()
        if result["kind"] in ("applied", "nothing"):
            self.imported.emit()

    def _review(self):
        from ui.widgets.mastery_import import MasteryImport

        found = self._pending
        if found is None:
            return
        if MasteryImport(self.conn, found, self).exec():
            dbq.note_mastery_import(self.conn)
            self.imported.emit()
        self.refresh_state()

    # --- the second way in ---------------------------------------------------

    def _copy_profile_url(self):
        text = self.profile_url_label.text()
        if not text:
            self.profile_feedback.setText("Enter your account id first.")
            return
        QApplication.clipboard().setText(text)
        self.profile_feedback.setText(
            "Address copied. Open it in a browser, save what it shows as a "
            "file, then load that file here.")

    def _load_mastery(self):
        from ui.widgets.mastery_import import MasteryImport
        from wft.profile import mastery as profile_mod

        path = file_dialogs.open_file(
            self, "The profile page you saved", "",
            "Profile data (*.json *.txt *.html);;All files (*)")
        if not path:
            return

        try:
            entries = profile_mod.load(path)
        except ValueError as exc:
            self.profile_feedback.setText(str(exc))
            return

        found = profile_mod.resolve(self.conn, entries)
        self._save_account_id()

        if not found["mastered"]:
            dbq.note_mastery_import(self.conn)
            self.profile_feedback.setText(
                f"{len(entries)} item(s) in the profile, nothing new — "
                f"{found['already']} already marked, {found['short']} not yet "
                f"at their rank cap.")
            self.imported.emit()
            return

        if MasteryImport(self.conn, found, self).exec():
            dbq.note_mastery_import(self.conn)
            self.imported.emit()
        done, total = dbq.mastered_count(self.conn)
        self.profile_feedback.setText(
            f"{done} of {total} tracked items are marked as mastered.")
        self.refresh_state()
