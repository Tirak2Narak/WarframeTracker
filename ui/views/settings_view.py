"""Settings: appearance, data freshness, the update job, hotkey, game window.

Six tabs, the same widget as the Collection page: Appearance, Game data,
Capture, Rewards, Fissures, Reset. The Rewards tab also holds the order
in which the recommendation weighs things.

Proves the GUI is wired to the real library first (D-order in the build
instructions): everything shown here comes from `wft.config` and the database
`meta` table, nothing is invented.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QSlider, QSpinBox,
    QComboBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from ui import settings_io, theme
from ui.widgets.elided_label import ElidedLabel
from ui.widgets.key_picker import KeyPicker
from wft.data import build as build_mod, publicexport as pe, status as data_status
from wft.data import db as dbq, reference as reference_mod
from wft.data import vaulted as vaulted_mod
from wft.profile import fetch as fetch_mod


def _run_update(cfg, *, force: bool = False, report=lambda _msg: None) -> dict:
    """The same sequence as `wf update`, reused rather than re-implemented.

    Runs on a worker thread (see ui/workers/task.py); every step reports a
    line of text so the Settings view can show real progress instead of a
    spinner with no information.

    Returns {"downloaded": manifests fetched, "counts": the build's counts};
    `UpdateJob` writes the first into the run log.
    """
    report(f"config: {cfg.source}")
    index = pe.fetch_index(cfg)
    pending = pe.outdated(cfg, index)
    downloaded = 0
    if not pending and not force:
        report(f"Manifests unchanged ({len(index)} in index) — nothing to download.")
    else:
        targets = ([index[name] for name in cfg.get("sources.manifests", []) if name in index]
                   if force else pending)
        report(f"Downloading {len(targets)} manifest(s)...")
        pe.download(cfg, targets, progress=lambda name, i, n: report(f"  [{i}/{n}] {name}"))
        downloaded = len(targets)

    notes: list[str] = []
    report("Building database...")
    counts = build_mod.build(cfg, notes)
    for key, value in counts.items():
        report(f"  {key}: {value}")

    report("Applying vault status from DE's drop tables...")
    try:
        vault_counts = vaulted_mod.apply(cfg, notes)
    except vaulted_mod.DropTableError as exc:
        # The build above has already gone through; only the vault status is
        # stale, and it says so rather than pretending the update succeeded.
        for note in notes:
            report(f"  note: {note}")
        report(f"VAULT STATUS NOT UPDATED: {exc}")
        raise
    for key, value in vault_counts.items():
        report(f"  {key}: {value}")
    for note in notes:
        report(f"  note: {note}")

    # The timestamp is written by the build itself, so that a `wf update` from
    # the shell and this button leave the same record behind.
    return {"downloaded": downloaded, "counts": counts}


class UpdateJob(QObject):
    """One download-and-build at a time, shared by every page that offers it.

    Settings ("Update now") and Setup ("Download game data") are two buttons for
    the one `_run_update`. Each starting its own worker would let both run at
    once, writing the same database and manifest files, and would leave each page
    blind to what the other started. One job object, owned by the application and
    handed to both: either page can start it, both show its log and both lock
    their buttons while it runs.

    The clock starts it too (`ui.workers.reference_update`), through the
    same `start`. Every run it starts is one `fetch_log` row of the job
    `reference`: written here, on the GUI thread, before the worker begins, and
    filled in when it ends. A log that cannot be written never stops the update.
    """

    progress = Signal(str)
    started = Signal()
    #: (succeeded, message). The message is the error text on failure.
    finished = Signal(bool, str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker = None
        self._row: int | None = None
        self._status: int | None = None

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self, force: bool = False, kind: str | None = None) -> bool:
        """Begin the update; False (and nothing started) when one is running.

        `kind` is what the log says: 'manual' for a button, 'force' for the
        forced one, 'auto' for the clock. The clock passes it by name and never
        as the first argument, which would read as `force`.
        """
        from ui.workers.task import TaskWorker

        if self.running:
            return False
        self._row = self._log_begin(kind or ("force" if force else "manual"))
        self._status = None
        worker = TaskWorker(self._work, force=force)
        worker.progress.connect(self.progress)
        worker.finished_ok.connect(self._done)
        worker.failed.connect(self._failed)
        self._worker = worker
        self.started.emit()
        worker.start()
        return True

    def _work(self, report, force: bool = False):
        """The worker's body: `_run_update`, remembering the HTTP code of a failure."""
        try:
            return _run_update(self.cfg, force=force, report=report)
        except Exception as exc:
            self._status = reference_mod.error_status(exc)
            raise

    def _log_begin(self, kind: str) -> int | None:
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                return reference_mod.begin(connection, fetch_mod.utcnow(), kind)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - no log is no reason not to update
            return None

    def _log_end(self, status: int | None, detail: str) -> None:
        row, self._row = self._row, None
        if row is None:
            return
        try:
            # Opened after the build: it replaces tables, not the log.
            connection = dbq.connect(self.cfg.db_path)
            try:
                reference_mod.end(connection, row, status, detail)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001
            pass

    def _done(self, result) -> None:
        self._worker = None
        self._log_end(None, reference_mod.success_text(result))
        self.finished.emit(True, "")

    def _failed(self, message: str) -> None:
        self._worker = None
        self._log_end(self._status, message)
        self.finished.emit(False, message)

    def wait(self, msec: int = 5000) -> None:
        """For tests and shutdown: let a running worker end."""
        if self._worker is not None:
            self._worker.wait(msec)


class SettingsView(QWidget):
    def __init__(self, cfg, parent=None, relic_panel=None, update_job=None):
        super().__init__(parent)
        self.cfg = cfg
        #: Shared with the Setup page (see UpdateJob); a page built alone makes
        #: its own.
        self.update_job = update_job or UpdateJob(cfg, self)
        # Handed in rather than looked up through the window: this page is
        # built before it is added to one, so asking the window for the panel
        # found nothing and the relic group silently showed its "no panel"
        # state instead of the test button.
        self.relic_panel = relic_panel
        self._build_ui()
        self.update_job.progress.connect(self.log_view.appendPlainText)
        self.update_job.started.connect(self._on_update_started)
        self.update_job.finished.connect(self._on_update_finished)
        if self.update_job.running:
            self._on_update_started()
        self._refresh_data_status()
        self._refresh_pictures()

    def refresh(self):
        self._refresh_data_status()
        self._refresh_pictures()

    # -- layout --------------------------------------------------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.4))

        title = QLabel("Settings")
        title.setObjectName("ViewTitle")
        subtitle = QLabel("How the tracker looks, the game data behind the "
                          "recommendations, how a reward screen is captured "
                          "and what counts as a quick fissure. First-time "
                          "steps are on the Setup page.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)
        outer.addSpacing(theme.px(0.8))

        self._build_footer = None
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # Where the settings live and which build is running, at the bottom in
        # the size such things deserve. It was the first thing on the page,
        # above everything the page is for.
        from wft import version as version_mod

        footer_row = QHBoxLayout()
        footer = ElidedLabel(f"{self.cfg.source}   ·   Version "
                             f"{version_mod.number()}   ·   "
                             f"{version_mod.describe()}"
                             f"   ·   {version_mod.committed_at()}")
        footer.setStyleSheet(theme.text("micro", theme.TEXT_FAINT))
        self.version_footer = footer
        footer_row.addWidget(footer, 1)
        self.whatsnew_button = QPushButton("What's new")
        self.whatsnew_button.clicked.connect(self._open_whatsnew)
        footer_row.addWidget(self.whatsnew_button)
        outer.addLayout(footer_row)

        # One page per tab, each scrolling on its own when it is taller than
        # the window. The Rewards page keeps its layout (`rewards_layout`) so a
        # later group can be appended to it.
        self._tab("Appearance", [self._appearance_group()])
        self._tab("Game data", [self._data_group()])
        self._tab("Capture", [self._capture_group(), self._pictures_group()])
        self.rewards_layout = self._tab(
            "Rewards", [self._relic_status_group(), self._ranking_group()])
        self._tab("Fissures", [self._fissures_group()])
        self._tab("Reset", [self._reset_group()])

    def _tab(self, name: str, groups) -> QVBoxLayout:
        """Add a scrolling tab holding `groups`; returns its layout."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setSpacing(theme.px(1.2))
        layout.setContentsMargins(0, theme.px(0.8), theme.px(0.8), 0)
        for group in groups:
            layout.addWidget(group)
        layout.addStretch(1)
        self.tabs.addTab(scroll, name)
        return layout

    def show_tab(self, name: str) -> None:
        """Bring the tab called `name` to the front; an unknown name is ignored."""
        for index in range(self.tabs.count()):
            if self.tabs.tabText(index) == name:
                self.tabs.setCurrentIndex(index)
                return

    def _open_whatsnew(self) -> None:
        from ui.views import whatsnew_dialog

        whatsnew_dialog.open_from_settings(self)

    def _appearance_group(self) -> QGroupBox:
        """Theme and text size, applied as they are chosen.

        Both earlier attempts had these and both were right to: the palettes
        below are carried over from them. Applying on change rather than behind
        a Save button matters more than usual here - the only way to judge a
        theme is to look at it.
        """
        group = QGroupBox("Appearance")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(theme.px(0.95))
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setColumnStretch(2, 1)

        grid.addWidget(QLabel("Theme"), 0, 0)
        self.theme_box = QComboBox()
        self.theme_box.addItems(theme.names())
        self.theme_box.setCurrentText(theme.active())
        self.theme_box.currentTextChanged.connect(self._apply_appearance)
        grid.addWidget(self._sized(self.theme_box), 0, 1)

        grid.addWidget(QLabel("Text size"), 1, 0)
        size_row = QHBoxLayout()
        self.font_slider = QSlider(Qt.Horizontal)
        self.font_slider.setRange(theme.MIN_FONT_PT, theme.MAX_FONT_PT)
        self.font_slider.setValue(theme.font_size())
        self.font_slider.setPageStep(1)
        # Applied when the knob is let go, not on every step of the drag:
        # each step restyled the whole application, which resized the slider
        # under the pointer, so the knob slid away from the hand dragging it.
        # The number beside it follows the drag.
        self.font_slider.setTracking(False)
        self.font_slider.valueChanged.connect(self._apply_appearance)
        self.font_slider.sliderMoved.connect(
            lambda size: self.font_value.setText(f"{size} pt"))
        # A slider the width of the window moves eight points across a metre of
        # travel. Measured in line heights rather than characters: a slider has
        # no text, so counting glyph widths gave it fifty pixels.
        span = theme.MAX_FONT_PT - theme.MIN_FONT_PT + 1
        self.font_slider.setMinimumWidth(
            self.font_slider.fontMetrics().height() * span // 2)
        self.font_slider.setMaximumWidth(
            self.font_slider.fontMetrics().height() * span)
        size_row.addWidget(self.font_slider)
        size_row.addSpacing(theme.px(0.7))
        self.font_value = QLabel(f"{theme.font_size()} pt")
        self.font_value.setMinimumWidth(theme.px(3.1))
        size_row.addWidget(self.font_value)
        size_row.addStretch(1)
        grid.addLayout(size_row, 1, 1)

        grid.addWidget(QLabel("Dismissed readings"), 2, 0)
        dismissed = QHBoxLayout()
        # The text and the button had no gap: the label takes the slack and
        # its text ran up to the button at 20pt (audit C7).
        dismissed.setSpacing(theme.px(1))
        self.learned_label = QLabel()
        dismissed.addWidget(self.learned_label, 1)
        forget = QPushButton("Forget all")
        forget.setToolTip(
            "Ask about these readings again on the next import.")
        forget.clicked.connect(self._forget_learned)
        dismissed.addWidget(forget)
        grid.addLayout(dismissed, 2, 1)
        self._refresh_learned()

        grid.setColumnStretch(1, 1)
        return group

    def _refresh_learned(self):
        """How many readings the review has been told to stop asking about."""
        from wft.data import db as db_mod

        try:
            connection = db_mod.connect(self.cfg.db_path)
            try:
                count = len(db_mod.learned_ignores(connection))
            finally:
                connection.close()
        except Exception:  # noqa: BLE001
            # A database that cannot be read is not a reason for Settings to be
            # missing. It is the page somebody would go to in order to rebuild
            # it, so it has to open when everything else has failed.
            self.learned_label.setText(
                "The database could not be read — use Update now below to "
                "rebuild it.")
            return
        self.learned_label.setText(
            f"{count} reading(s) dismissed during past imports"
            if count else "none yet")

    def _forget_learned(self):
        """Undo every dismissal, for when one was a mistake.

        All of them rather than one at a time: a wrong dismissal is noticed as
        "something is missing from my inventory" long after the fact, without a
        name attached, so picking the culprit out of a list is not a thing
        anyone can do. Re-answering a review is cheap by comparison.
        """
        from wft.data import db as db_mod

        connection = db_mod.connect(self.cfg.db_path)
        removed = db_mod.forget_ignores(connection)
        connection.close()
        self._refresh_learned()
        QMessageBox.information(
            self, "Dismissals cleared",
            f"Forgot {removed} dismissed reading(s). The next import will ask "
            f"about them again.")

    def _apply_appearance(self):
        name = self.theme_box.currentText()
        size = self.font_slider.value()
        self.font_value.setText(f"{size} pt")

        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(theme.apply(name, size))
            application.setFont(theme.base_font(size))

        # The stylesheet covers everything set through it, but not colours a
        # view chose in Python - a red "need 2 more", a green section heading.
        # Those were read from the old theme when the widget was built, so the
        # pages have to be rebuilt to pick up the new one.
        self._refresh_pages()

        settings_io.set_value("display", "theme", name)
        settings_io.set_value("display", "font_size", str(size))

    def _refresh_pages(self):
        """Rebuild every page from the database and the theme as they are now."""
        window = self.window()
        for page in getattr(window, "_pages", {}).values():
            refresh = getattr(page, "refresh", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: {type(page).__name__} did not refresh: {exc}",
                          file=sys.stderr)

    def _data_group(self) -> QGroupBox:
        group = QGroupBox("Reference data")
        v = QVBoxLayout(group)

        self.status_label = QLabel("Checking...")
        self.status_label.setWordWrap(True)
        self.counts_label = QLabel("")
        self.counts_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.counts_label.setWordWrap(True)

        v.addWidget(self.status_label)
        v.addWidget(self.counts_label)

        # The switch of the daily run, saved like the ones on the Import
        # page: into the user's config file and into the running configuration.
        self.auto_box = QCheckBox("Update the reference data by itself, once a day")
        self.auto_box.setChecked(reference_mod.enabled(self.cfg))
        self.auto_box.toggled.connect(self._set_auto_update)
        v.addWidget(self.auto_box)
        self.auto_label = QLabel("")
        self.auto_label.setWordWrap(True)
        self.auto_label.setStyleSheet(theme.text("small", theme.TEXT_DIM))
        v.addWidget(self.auto_label)
        self.fail_label = QLabel("")
        self.fail_label.setWordWrap(True)
        self.fail_label.setStyleSheet(f"color: {theme.WARN};")
        self.fail_label.setVisible(False)
        v.addWidget(self.fail_label)

        buttons = QHBoxLayout()
        self.update_btn = QPushButton("Update now")
        self.update_btn.setObjectName("Primary")
        self.update_btn.clicked.connect(self._start_update)
        self.force_btn = QPushButton("Force re-download")
        self.force_btn.clicked.connect(lambda: self._start_update(force=True))
        buttons.addWidget(self.update_btn)
        buttons.addWidget(self.force_btn)
        buttons.addStretch(1)
        v.addLayout(buttons)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        # 11em: the 160px it was at 11pt, which is 8 lines there and 4.3 at
        # 20pt. In ems it is the same number of lines at every size.
        self.log_view.setFixedHeight(theme.px(11))
        self.log_view.setVisible(False)
        v.addWidget(self.log_view)

        return group

    def _capture_group(self) -> QGroupBox:
        group = QGroupBox("Capture")
        grid = QGridLayout(group)
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setHorizontalSpacing(theme.px(0.8))
        # The last column takes the slack, so the fields keep their own width
        # instead of being stretched to the window.
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 1)
        row = 0

        grid.addWidget(QLabel("Game window class"), row, 0)
        self.window_class_edit = QLineEdit(str(self.cfg.get("game.window_class", "")))
        grid.addWidget(self._sized(self.window_class_edit), row, 1)
        detect_btn = QPushButton("Detect...")
        detect_btn.clicked.connect(self._detect_window)
        grid.addWidget(detect_btn, row, 2)
        row += 1

        grid.addWidget(QLabel("Hotkey"), row, 0)
        self.hotkey_edit = QLineEdit(str(self.cfg.get("hotkey.key", "")))
        self.hotkey_edit.setPlaceholderText("evdev key name, e.g. F8")
        grid.addWidget(self._sized(self.hotkey_edit), row, 1)
        row += 1

        # The same widget as on the Setup page: press the key instead of typing
        # its evdev name. It saves and applies the key itself, so the field
        # follows it rather than the other way round.
        self.key_picker = KeyPicker(self.cfg, with_check_button=False)
        self.key_picker.picked.connect(self.hotkey_edit.setText)
        grid.addWidget(self.key_picker, row, 1, 1, 3)
        row += 1

        self.detect_list = QListWidget()
        self.detect_list.setObjectName("PickList")
        self.detect_list.setVisible(False)
        self.detect_list.setMaximumHeight(theme.px(9.5))
        self.detect_list.itemDoubleClicked.connect(self._pick_detected_window)
        grid.addWidget(self.detect_list, row, 0, 1, 4)
        row += 1

        # The reward regions were never measured — `wft/relic/regions.py` says
        # so in its own comment, and a live capture once read "SELEC A INVEN"
        # off the sell screen because the estimate pointed there. They cannot
        # be measured from here either: only the person in front of the game
        # can say where the row is. The config file has been telling people to
        # run `wf calibrate` since before this existed.
        calibrate_row = QHBoxLayout()
        calibrate_btn = QPushButton("Calibrate reward regions...")
        calibrate_btn.clicked.connect(self._calibrate_regions)
        calibrate_row.addWidget(calibrate_btn)
        calibrate_note = QLabel(
            "One dragged box on a screenshot of a reward screen, and the "
            "Rewards page knows where to look — with a test that reads the "
            "names back before you trust it.")
        calibrate_note.setWordWrap(True)
        calibrate_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        calibrate_row.addWidget(calibrate_note, 1)
        # Across all four columns, the last of which takes the slack: the note
        # was wrapped at 350px with the right half of the card empty.
        grid.addLayout(calibrate_row, row, 0, 1, 4)
        row += 1

        # Which values the reward row is read with: nothing else shows that an
        # older calibration in config.toml overrides the measured ones.
        self.regions_status = QLabel("")
        self.regions_status.setStyleSheet(f"color: {theme.TEXT_DIM};")
        grid.addWidget(self.regions_status, row, 0, 1, 4)
        row += 1
        self._refresh_regions_status()

        # Saved the moment it is clicked and in force at once: it is a switch,
        # not a field that waits for the button below.
        self.notify_box = QCheckBox("Show the recommendation as a desktop notification")
        self.notify_box.setChecked(bool(self.cfg.get("notifications.enabled", True)))
        self.notify_box.toggled.connect(self._save_notifications)
        grid.addWidget(self.notify_box, row, 0, 1, 4)
        row += 1

        # Where KDE would mute it anyway: on a default Plasma the box above
        # does nothing while the game is fullscreen, and nothing else says so.
        from wft import readiness

        self.notify_note = QLabel(readiness.FULLSCREEN_DND_HINT)
        self.notify_note.setWordWrap(True)
        self.notify_note.setStyleSheet(f"color: {theme.WARN};")
        self.notify_note.setVisible(
            bool(readiness.notifications_held_in_fullscreen()))
        grid.addWidget(self.notify_note, row, 0, 1, 4)
        row += 1

        save_btn = QPushButton("Save capture settings")
        save_btn.setObjectName("Primary")
        save_btn.clicked.connect(self._save_capture_settings)
        grid.addWidget(save_btn, row, 0, 1, 2)

        return group

    def _pictures_group(self) -> QGroupBox:
        """Keeping a picture of every reward screen.

        Every control saves the moment it is changed, like the notification
        switch above; the paths and the count come from
        `wft.relic.kept_pictures`, the code that writes them.
        """
        group = QGroupBox("Reward pictures")
        grid = QGridLayout(group)
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setHorizontalSpacing(theme.px(0.8))
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 1)
        row = 0

        self.keep_box = QCheckBox("Keep a picture of every reward screen")
        self.keep_box.setChecked(bool(self.cfg.get("capture.keep_rewards", False)))
        self.keep_box.toggled.connect(self._save_keep)
        grid.addWidget(self.keep_box, row, 0, 1, 4)
        row += 1

        grid.addWidget(QLabel("Folder"), row, 0)
        self.keep_dir_edit = QLineEdit(
            str(self.cfg.get("capture.keep_rewards_dir", "") or ""))
        self.keep_dir_edit.setReadOnly(True)
        grid.addWidget(self._sized(self.keep_dir_edit), row, 1)
        row += 1

        buttons = QHBoxLayout()
        self.keep_choose_btn = QPushButton("Choose folder...")
        self.keep_choose_btn.clicked.connect(self._choose_keep_dir)
        buttons.addWidget(self.keep_choose_btn)
        self.keep_open_btn = QPushButton("Open folder")
        self.keep_open_btn.clicked.connect(self._open_keep_dir)
        buttons.addWidget(self.keep_open_btn)
        buttons.addStretch(1)
        grid.addLayout(buttons, row, 1, 1, 3)
        row += 1

        grid.addWidget(QLabel("Move pictures older than"), row, 0)
        days_row = QHBoxLayout()
        self.keep_days_spin = QSpinBox()
        self.keep_days_spin.setRange(0, 3650)
        self.keep_days_spin.setSpecialValueText("never")
        self.keep_days_spin.setSuffix(" days")
        self.keep_days_spin.setValue(
            int(self.cfg.get("capture.keep_rewards_days", 30) or 0))
        self.keep_days_spin.valueChanged.connect(self._save_keep_days)
        days_row.addWidget(self.keep_days_spin)
        days_row.addWidget(QLabel("to the trash"))
        days_row.addStretch(1)
        grid.addLayout(days_row, row, 1, 1, 3)
        row += 1

        self.keep_status = QLabel("")
        self.keep_status.setWordWrap(True)
        grid.addWidget(self.keep_status, row, 0, 1, 4)
        row += 1

        note = QLabel(
            "One picture per reading, one folder per day, about 1.3 MB each at "
            "4K. The pictures show the whole game window, so your squad's "
            "names are on them. Use them to look back at what was on the "
            "screen, or to measure the reading regions. Old day folders are "
            "moved to the trash when the program starts.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        grid.addWidget(note, row, 0, 1, 4)

        self._refresh_pictures()
        return group

    def _refresh_pictures(self) -> None:
        from wft.relic import kept_pictures

        folder = kept_pictures.folder_setting(self.cfg)
        self.keep_dir_edit.setText(str(self.cfg.get("capture.keep_rewards_dir", "") or ""))
        self.keep_open_btn.setEnabled(folder is not None)
        if folder is None:
            self.keep_status.setText("No folder chosen")
            return
        self.keep_status.setText(kept_pictures.describe(kept_pictures.summarize(folder)))

    def _save_keep(self, enabled: bool) -> None:
        self._save_capture_value("keep_rewards", bool(enabled))

    def _save_keep_days(self, days: int) -> None:
        self._save_capture_value("keep_rewards_days", int(days))

    def _save_capture_value(self, key: str, value) -> None:
        try:
            settings_io.save_value(self.cfg, "capture", key, value)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    def _choose_keep_dir(self) -> None:
        import os

        start = os.path.expanduser(str(self.cfg.get("capture.keep_rewards_dir", "") or "")) \
            or os.path.expanduser("~")
        chosen = QFileDialog.getExistingDirectory(
            self, "Folder for the reward pictures", start)
        if not chosen:
            return
        self._save_capture_value("keep_rewards_dir", chosen)
        self._refresh_pictures()

    def _open_keep_dir(self) -> None:
        from wft.relic import kept_pictures

        folder = kept_pictures.folder_setting(self.cfg)
        if folder is None:
            return
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Could not open", str(exc))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _sized(self, widget):
        """Give a field the one width every field on this page has.

        A grid column stretches, so a field for "F23" was as wide as the
        window; capped per field by its own number of characters it was 18, 26
        and 28 of them, three widths in two cards. One width, in ems of the
        text: the text size is a setting, and a width in pixels stops matching
        its contents the moment that changes.
        """
        widget.setMinimumWidth(theme.px(12))
        widget.setMaximumWidth(theme.px(22))
        return widget

    # -- data status -----------------------------------------------------
    def _refresh_data_status(self):
        data = data_status.read(self.cfg)
        if not data.readable:
            # There is no data, or what is there cannot be read. Saying so and
            # offering the button that fixes it is the whole job of this
            # panel; failing to build is the one thing it must not do, because
            # this is the page somebody comes to when everything else is
            # broken.
            self.status_label.setText(
                "No reference data yet, or it could not be read. "
                "Update now will fetch and rebuild it.")
            self.status_label.setStyleSheet(theme.text("body", theme.WARN))
            self.counts_label.setText("")
            self._refresh_auto_update(data)
            return

        last_update = data.last_update
        remind_days = int(self.cfg.get("update.remind_after_days", 14) or 0)

        if last_update is None:
            self.status_label.setText(
                "Reference data has never been downloaded. Click \"Update now\" to fetch "
                "the game's item and relic data before using the tool.")
            self.status_label.setStyleSheet(f"color: {theme.WARN};")
        else:
            age_days = data.age_days
            stamp = last_update.strftime("%Y-%m-%d %H:%M UTC")
            # "1 days ago" reads as a typo and makes the rest of the line look
            # careless. Zero has its own word: "0 days ago" is today.
            age = ("today" if age_days == 0 else
                   "1 day ago" if age_days == 1 else f"{age_days} days ago")
            if remind_days and age_days > remind_days:
                self.status_label.setText(
                    f"Last updated {stamp} — {age}. That is older than the "
                    f"configured {remind_days}-day reminder; consider updating.")
                self.status_label.setStyleSheet(f"color: {theme.WARN};")
            else:
                # The threshold is said here, beside the date it is measured
                # against. It used to be a bare "Remind after (days) 14" among
                # the capture settings, which it has nothing to do with.
                reminder = (f" The line turns into a reminder after {remind_days} "
                            f"days." if remind_days else "")
                if self.auto_box.isChecked():
                    # The reminder is for someone who updates by hand; with the
                    # daily run on, the line says that instead.
                    reminder = " The tool updates it by itself once a day."
                self.status_label.setText(f"Last updated {stamp} — {age}.{reminder}")
                self.status_label.setStyleSheet(f"color: {theme.GOOD};")

        if data.has_counts_row:
            if data.counts_text_ok:
                line = "  ·  ".join(f"{k}: {v}" for k, v in data.counts.items())
                # Vault status is the one fact here that rotates on its own.
                # Everything else only changes when the game adds an item; a
                # relic vaulted last month can be back in the drop tables this
                # month, and the ranking leans on it heavily.
                if data.vault_age is not None:
                    when = "today" if data.vault_age == 0 else f"{data.vault_age} days ago"
                    line += f"\nVault status refreshed {when}."
                self.counts_label.setText(line)
            else:
                self.counts_label.setText("")
        else:
            self.counts_label.setText("No database built yet.")
        self._refresh_auto_update(data)

    _HOW = {"auto": "automatic", "manual": "button", "force": "forced re-download"}

    @staticmethod
    def _local(text: str) -> str:
        return fetch_mod.parse_stamp(text).astimezone().strftime("%Y-%m-%d %H:%M")

    def _refresh_auto_update(self, data) -> None:
        """The lines under the switch: when the next run comes, the last run, a failure."""
        try:
            connection = dbq.connect(self.cfg.db_path)
            try:
                last = reference_mod.last_run(connection)
                when = reference_mod.next_due(connection)
            finally:
                connection.close()
        except Exception:  # noqa: BLE001 - a database that cannot say is shown as empty
            last, when = None, None
        on = self.auto_box.isChecked()
        lines = []
        if on:
            if not (data.readable and data.has_counts_row):
                lines.append("Updates itself once a day, but only after the first "
                             "download: that one is the button's.")
            elif when is None or when <= fetch_mod.utcnow():
                lines.append("Updates itself once a day; the next check is due now.")
            else:
                lines.append("Updates itself once a day; the next check comes "
                             f"after {when.astimezone().strftime('%Y-%m-%d %H:%M')}.")
        else:
            lines.append("Updating by itself is off.")
        if last is not None:
            lines.append(f"Last run {self._local(last['at'])} "
                         f"({self._HOW.get(last['kind'], last['kind'])}): "
                         f"{last['detail']}")
        self.auto_label.setText("\n".join(lines))
        failed = (last is not None and last["kind"] == "auto"
                  and not reference_mod.is_ok(last) and last["detail"] != reference_mod.BEGUN)
        if failed:
            retry = fetch_mod.parse_stamp(last["at"]) + fetch_mod.INTERVAL
            text = (f"The automatic update of {self._local(last['at'])} failed: "
                    f"{last['detail']}.")
            if on:
                text += (" It tries again after "
                         f"{retry.astimezone().strftime('%Y-%m-%d %H:%M')}.")
            self.fail_label.setText(text)
        self.fail_label.setVisible(failed)

    def _set_auto_update(self, on: bool) -> None:
        settings_io.save_value(self.cfg, "update", "auto_fetch", bool(on))
        self._refresh_data_status()

    # -- update job --------------------------------------------------------
    def _start_update(self, force: bool = False):
        self.update_job.start(force=force)

    def _on_update_started(self):
        self.update_btn.setEnabled(False)
        self.force_btn.setEnabled(False)
        self.log_view.setVisible(True)
        self.log_view.clear()

    def _on_update_finished(self, ok: bool, message: str):
        self.update_btn.setEnabled(True)
        self.force_btn.setEnabled(True)
        self.log_view.appendPlainText("Done." if ok else f"Failed: {message}")
        # A failure can come after the build went through (the vault step is
        # last), so what the page shows about the data may have changed.
        self._refresh_data_status()

    # -- capture settings --------------------------------------------------
    def _relic_status_group(self) -> QGroupBox:
        """Whether the Rewards page can work, and a way to try it.

        This was its own page in the navigation, which put a status readout and
        a test button where the user looked for a feature — and the feature is
        the Rewards page. It is configuration and diagnosis, so it belongs
        beside the capture settings it depends on.
        """
        group = QGroupBox("Rewards page")
        layout = QVBoxLayout(group)
        try:
            from ui.views.relic_control_view import RelicControlView
        except Exception as exc:  # noqa: BLE001 - one broken view is not fatal
            layout.addWidget(QLabel(f"unavailable: {exc}"))
            return group

        self.relic_control = RelicControlView(
            self.cfg, self.relic_panel, embedded=True)
        layout.addWidget(self.relic_control)

        # Where the log files are, for whoever has to send one.
        from wft import logfile
        self.log_files_note = QLabel(
            f"Log files: {logfile.log_dir(self.cfg)} "
            f"(one per week, the last {logfile.KEEP_WEEKS} are kept)")
        self.log_files_note.setWordWrap(True)
        self.log_files_note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_files_note.setStyleSheet(theme.text("small", theme.TEXT_FAINT))
        layout.addWidget(self.log_files_note)
        folder_row = QHBoxLayout()
        self.log_folder_btn = QPushButton("Open folder")
        self.log_folder_btn.clicked.connect(self._open_log_folder)
        folder_row.addWidget(self.log_folder_btn)
        folder_row.addStretch(1)
        layout.addLayout(folder_row)
        return group

    def _open_log_folder(self):
        from wft import logfile
        folder = logfile.log_dir(self.cfg)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Open folder", f"Cannot create {folder}:\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _calibrate_regions(self):
        """Measure the regions, and show a test where the result will be seen."""
        from ui.views.calibrate_dialog import open_dialog

        open_dialog(self.cfg, self, self.relic_panel)
        self._refresh_regions_status()

    def _refresh_regions_status(self) -> None:
        from wft.relic import regions as regions_mod

        found = regions_mod.configured_overrides(self.cfg)
        self.regions_status.setText(
            "Reward regions: measured values" if not found else
            f"Reward regions: your own calibration in config.toml ({len(found)} values)")

    def _detect_window(self):
        try:
            from wft.capture import window as cap
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Detection failed", str(exc))
            return

        try:
            windows = cap.list_windows()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Detection failed",
                                f"Could not query X11 windows:\n{exc}")
            return

        self.detect_list.clear()
        self.detect_list.setVisible(True)
        for entry in windows:
            item = QListWidgetItem(f"{entry.wm_class!r}  —  {entry.title[:50]!r}  "
                                   f"({entry.width}x{entry.height})")
            item.setData(Qt.UserRole, entry.wm_class)
            self.detect_list.addItem(item)
        if not windows:
            self.detect_list.addItem("No viewable windows found.")

    def _pick_detected_window(self, item: QListWidgetItem):
        wm_class = item.data(Qt.UserRole)
        if wm_class:
            self.window_class_edit.setText(wm_class)

    def _save_notifications(self, enabled: bool):
        try:
            settings_io.save_value(self.cfg, "notifications", "enabled", enabled)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))

    def _save_capture_settings(self):
        key = self.hotkey_edit.text().strip()
        # A name the listener cannot resolve would be saved and then do nothing,
        # which is how a typed "\\" cost a newcomer their first evening. Refused
        # here, with the way out.
        from wft.capture import keys

        if key and keys.evdev is not None and keys.key_code(key) is None:
            QMessageBox.warning(
                self, "Not a key name",
                f"\"{key}\" is not a key name the tool knows (it wants evdev "
                f"names such as F8, PAUSE or BACKSLASH). Use \"Choose a key…\" "
                f"and press the key instead.")
            return
        try:
            path = settings_io.save_value(
                self.cfg, "game", "window_class", self.window_class_edit.text().strip())
            settings_io.save_value(self.cfg, "hotkey", "key", key)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save", str(exc))
            return
        QMessageBox.information(
            self, "Saved",
            f"Written to {path}.\nThe hotkey and the window class are in use "
            "from now on; no restart is needed.")

    # -- the order of the recommendation -------------------------------------
    #: What a line of the list says, for someone who has never read the code:
    #: the name, then what it means. Keyed by `score.CRITERIA`.
    RANKING_TEXT = {
        "collection": ("A piece your collection still needs",
                       "Missing from a set you have not mastered yet, against "
                       "a duplicate or a piece of a set you already mastered. "
                       "Lines above this one weigh every reward, lines below "
                       "it tell the two kinds apart as before."),
        "reach": ("Hard to get any other way",
                  "Few or no relics left that drop it, and you hold few or none."),
        "completes": ("Finishes a set",
                      "Taking it leaves the set with nothing missing."),
        "progress": ("How far the set already is",
                     "More pieces of its set in hand beats fewer."),
        "type": ("Item type",
                 "Warframe, weapon and so on, by the numbers below."),
        "set_size": ("Smaller set",
                     "Fewer pieces to collect for the same progress."),
        "relics": ("Fewer relics drop it",
                   "Harder to come back for later."),
        "rarity": ("Rarer drop", "Rare before uncommon before common."),
        "ducats": ("More ducats",
                   "Below \"A piece your collection still needs\" only for "
                   "rewards that add nothing to a set (duplicates, pieces of a "
                   "mastered set); above it for every reward."),
    }

    def _ranking_group(self) -> QGroupBox:
        """In what order the reward screen's options are weighed.

        Two lists. The criteria are moved with the arrows, a strict order; the
        item types have a number each, equal numbers weigh the same. Both are
        saved as they change and read from the config on every reward screen.
        """
        from wft.relic import score as score_mod

        group = QGroupBox("Order of the recommendation")
        layout = QVBoxLayout(group)
        note = QLabel("When the Rewards page compares two options, the first "
                      "line of this list that tells them apart decides. "
                      "Line 1 matters most. Forma is always last.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(note)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        self.rank_numbers: list[QLabel] = []
        self.rank_names: list[QLabel] = []
        self.rank_hints: list[QLabel] = []
        self.rank_up_btns: list[QPushButton] = []
        self.rank_down_btns: list[QPushButton] = []
        count = len(score_mod.CRITERIA)
        for row in range(count):
            number = QLabel()
            number.setMinimumWidth(theme.px(1.6))
            name = QLabel()
            hint = QLabel()
            hint.setWordWrap(True)
            hint.setStyleSheet(theme.text("small", theme.TEXT_FAINT))
            text = QVBoxLayout()
            text.setSpacing(0)
            text.addWidget(name)
            text.addWidget(hint)
            up = QPushButton("\u2191")
            down = QPushButton("\u2193")
            up.setToolTip("Matters more")
            down.setToolTip("Matters less")
            up.clicked.connect(lambda _=False, at=row: self._move_criterion(at, -1))
            down.clicked.connect(lambda _=False, at=row: self._move_criterion(at, 1))
            grid.addWidget(number, row, 0)
            grid.addLayout(text, row, 1)
            grid.addWidget(up, row, 2)
            grid.addWidget(down, row, 3)
            self.rank_numbers.append(number)
            self.rank_names.append(name)
            self.rank_hints.append(hint)
            self.rank_up_btns.append(up)
            self.rank_down_btns.append(down)
        layout.addLayout(grid)

        types_note = QLabel("Item type: 1 comes first. The same number means the "
                            "same weight. A type that is not listed counts as "
                            "Anything else.")
        types_note.setWordWrap(True)
        types_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(types_note)

        from ui.views.library_view import _CATEGORY_LABELS

        self.type_spins: dict[str, QSpinBox] = {}
        types_grid = QGridLayout()
        columns = 2
        for index, kind in enumerate(score_mod.ITEM_TYPES):
            spin = QSpinBox()
            spin.setRange(1, score_mod.MAX_TYPE_NUMBER)
            spin.valueChanged.connect(
                lambda value, kind=kind: self._save_item_type(kind, value))
            self.type_spins[kind] = spin
            label = QLabel(_CATEGORY_LABELS.get(kind, "Anything else"))
            types_grid.addWidget(label, index // columns, (index % columns) * 2)
            types_grid.addWidget(spin, index // columns, (index % columns) * 2 + 1)
        layout.addLayout(types_grid)

        row = QHBoxLayout()
        self.rank_reset_btn = QPushButton("Reset to default")
        self.rank_reset_btn.clicked.connect(self._reset_ranking)
        row.addWidget(self.rank_reset_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self._sync_ranking()
        return group

    def _sync_ranking(self) -> None:
        """Show the configured order without writing it back."""
        from wft.relic import score as score_mod

        order = score_mod.order_from_config(self.cfg)
        last = len(order.criteria) - 1
        for row, criterion in enumerate(order.criteria):
            title, hint = self.RANKING_TEXT[criterion]
            self.rank_numbers[row].setText(f"{row + 1}.")
            self.rank_names[row].setText(title)
            self.rank_hints[row].setText(hint)
            self.rank_up_btns[row].setEnabled(row > 0)
            self.rank_down_btns[row].setEnabled(row < last)
        numbers = dict(order.types)
        for kind, spin in self.type_spins.items():
            spin.blockSignals(True)
            spin.setValue(numbers[kind])
            spin.blockSignals(False)

    def ranking_criteria(self) -> list[str]:
        """The criteria as listed now, first line first."""
        from wft.relic import score as score_mod

        return list(score_mod.order_from_config(self.cfg).criteria)

    def _move_criterion(self, row: int, step: int) -> None:
        criteria = self.ranking_criteria()
        other = row + step
        if not 0 <= other < len(criteria):
            return
        criteria[row], criteria[other] = criteria[other], criteria[row]
        settings_io.save_value(self.cfg, "ranking", "criteria", criteria)
        self._sync_ranking()

    def _save_item_type(self, kind: str, value: int) -> None:
        settings_io.save_value(self.cfg, "ranking.types", kind, int(value))

    def _reset_ranking(self) -> None:
        """Back to the built-in order: the keys leave the user's file."""
        from wft.relic import score as score_mod

        settings_io.remove_keys(self.cfg, "ranking", ["criteria"])
        settings_io.remove_keys(self.cfg, "ranking.types",
                                list(score_mod.ITEM_TYPES))
        self._sync_ranking()

    # -- fissures ------------------------------------------------------------
    def _fissures_group(self) -> QGroupBox:
        """Which mission types the Fissures page marks as quick.

        One box per mission type the board can show; saved as they are ticked.
        The Fissures page reads the config on every row, so it follows at once.
        """
        from wft.world import fissures as fissures_mod

        group = QGroupBox("Quick missions")
        layout = QVBoxLayout(group)
        note = QLabel("The Fissures page marks these mission types in green: "
                      "short runs, ten reactant and out. Void Storms never "
                      "count as quick.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(note)

        self.quick_boxes: dict[str, QCheckBox] = {}
        grid = QGridLayout()
        columns = 3
        for index, mission in enumerate(fissures_mod.MISSION_TYPES):
            box = QCheckBox(mission)
            box.toggled.connect(self._save_quick_missions)
            self.quick_boxes[mission] = box
            grid.addWidget(box, index // columns, index % columns)
        layout.addLayout(grid)

        row = QHBoxLayout()
        self.quick_reset_btn = QPushButton("Reset to default")
        self.quick_reset_btn.clicked.connect(self._reset_quick_missions)
        row.addWidget(self.quick_reset_btn)
        row.addStretch(1)
        layout.addLayout(row)
        self._sync_quick_boxes()
        return group

    def _sync_quick_boxes(self) -> None:
        """Show the configured choice without writing it back."""
        from wft.world import fissures as fissures_mod

        fissures_mod.use_config(self.cfg)
        chosen = set(fissures_mod.quick_missions())
        for mission, box in self.quick_boxes.items():
            box.blockSignals(True)
            box.setChecked(mission in chosen)
            box.blockSignals(False)

    def _save_quick_missions(self, _checked: bool = False) -> None:
        from wft.world import fissures as fissures_mod

        picked = [mission for mission, box in self.quick_boxes.items()
                  if box.isChecked()]
        settings_io.save_value(self.cfg, "fissures", "quick_missions", picked)
        fissures_mod.use_config(self.cfg)

    def _reset_quick_missions(self) -> None:
        """Back to the shipped choice: the key leaves the user's file."""
        settings_io.remove_keys(self.cfg, "fissures", ["quick_missions"])
        # remove() took it out of memory too; put the shipped value back.
        from wft.world import fissures as fissures_mod

        self.cfg.override("fissures.quick_missions",
                          list(fissures_mod.QUICK_MISSIONS))
        self._sync_quick_boxes()

    # -- reset ---------------------------------------------------------------
    def _reset_group(self) -> QGroupBox:
        """The two ways back to a clean start.

        Last on the page, below everything that is set up once: these are the
        two buttons on it that throw work away.
        """
        group = QGroupBox("Reset")
        grid = QGridLayout(group)
        grid.setVerticalSpacing(theme.px(0.7))
        grid.setHorizontalSpacing(theme.px(0.95))
        grid.setColumnStretch(1, 1)

        rows = (
            ("Delete my inventory...", self._delete_inventory,
             "Your parts, items, relics and mastery, with the import history "
             "and the dismissed readings. Settings, game data, prices and the "
             "riven history stay. A backup goes to the trash first, then the "
             "tracker starts again."),
            ("Delete everything...", self._delete_everything,
             "The inventory as above, the settings, the downloaded game data, "
             "prices and the riven history. The files go to the trash and the "
             "tracker starts again as on its first launch."),
        )
        for row, (label, action, text) in enumerate(rows):
            button = QPushButton(label)
            button.clicked.connect(action)
            grid.addWidget(button, row, 0, Qt.AlignTop)
            note = QLabel(text)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {theme.TEXT_DIM};")
            grid.addWidget(note, row, 1)
        return group

    def _confirm(self, title: str, text: str, action: str) -> bool:
        """Ask once, with Cancel as the default: Enter must not delete."""
        box = QMessageBox(QMessageBox.Warning, title, text, parent=self)
        go = box.addButton(action, QMessageBox.DestructiveRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is go

    def _delete_inventory(self):
        """Empty the holdings, then start the program again.

        Again rather than page by page: not every page re-reads the database on
        `refresh` (the Import page has none, and kept listing the deleted
        imports), and a new start is the one refresh that cannot miss a page.
        """
        from ui import reset
        from wft.data import db as db_mod

        if not self._confirm(
                "Delete your inventory?",
                "This deletes every part, item and relic you own, all mastery, "
                "the import history and the dismissed readings.\n\n"
                "Settings, the downloaded game data, prices and the riven "
                "history stay.\n\nA backup of what is deleted goes to the trash "
                "first; \"Restore from a backup...\" on the Collection page, "
                "tab \"Add and correct\", reads it.\n\nThe tracker then starts "
                "again.",
                "Delete my inventory"):
            return
        try:
            connection = db_mod.connect(self.cfg.db_path)
            try:
                saved = reset.backup_before_deleting(self.cfg, connection)
                removed = db_mod.delete_holdings(connection)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - said, not raised into Qt
            QMessageBox.warning(self, "Nothing deleted",
                                f"The inventory could not be deleted:\n{exc}")
            return
        if saved is None:
            where = "There was nothing to back up."
        elif saved[1]:
            where = (f"The backup is in the trash as {saved[0].name}. To use it, "
                     f"drag it out of the trash into your home folder first.")
        else:
            where = f"The trash was not available; the backup is at\n{saved[0]}"
        QMessageBox.information(
            self, "Inventory deleted",
            f"Deleted {removed['owned_parts']} part(s), {removed['owned_items']} "
            f"item(s) and {removed['owned_relics']} relic(s), and "
            f"{removed['imports']} import(s) from the history.\n\n{where}\n\n"
            f"The tracker starts again now.")
        self._restart()

    def _delete_everything(self):
        """Discard every file the tracker wrote and start it again."""
        from ui import reset

        if self.update_job.running:
            QMessageBox.information(
                self, "Update running",
                "The game data is being updated. Wait until it has finished, "
                "then delete.")
            return
        if not self._confirm(
                "Delete everything?",
                "This deletes your inventory and mastery, the import history, "
                "all settings (hotkey, reward regions, theme), the downloaded "
                "game data, the prices and the riven history. The riven "
                "history cannot be downloaded again: the source only offers "
                "the current week.\n\n"
                "The files go to the trash. Recordings and screenshots in your "
                "own folders stay, and so does the program itself.\n\nThe "
                "tracker then starts again as on its first launch.",
                "Delete everything"):
            return
        window = self.window()
        # Nothing may write a file back once it is gone: the window stores its
        # size when it is moved or closed.
        if hasattr(window, "remember_geometry"):
            window.remember_geometry = False
        try:
            reset.delete_everything(self.cfg)
        except Exception as exc:  # noqa: BLE001 - said, not raised into Qt
            QMessageBox.warning(
                self, "Not everything was deleted",
                f"{exc}\n\nThe tracker starts again now with what is left.")
        self._restart()

    def _restart(self):
        """Replaced in the tests; the real one does not return."""
        from ui import reset

        reset.restart()
