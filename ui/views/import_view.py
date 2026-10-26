"""Import: recordings and screenshots to inventory. Select, scan, review, apply.

Review by exception was the original design and it was wrong: confident
is not the same as correct, and the errors that hurt are the ones nothing asks
about. The review is one list with every reading in it now — questions first, then everything the scan was
sure of, each with the tile it came from, the text actually read, and a count
that can be corrected. Clustering stays: identical readings are one decision.

The queue is still keyboard-first — digits pick a candidate, Enter goes to the
next unanswered question, I ignores, M opens manual entry, Space keeps or drops
an accepted row.

Every tile carries a picture and the frame it was cut from, so clicking one
shows the whole frame with the tile ringed. Nothing reaches the database
until Apply, and what Apply writes is journalled and reversible.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import shiboken6 as _shiboken
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QFrame,
    QHBoxLayout, QLabel, QScrollArea, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from ui import file_dialogs, theme
from ui.widgets import tables
from ui.widgets.accepted_row import AcceptedRow
from ui.widgets.review_widget import ReviewWidget, review_chord
from ui.workers.task import TaskWorker
from wft import readiness
from wft.data import db as dbq
from wft.inventory import frames as frames_mod, quantity as quantity_mod
from wft.relic import match as match_mod

PAGE_SELECT, PAGE_PROCESSING, PAGE_REVIEW, PAGE_APPLY = range(4)

#: Rows of the apply table that stay in view however much text is above it.
APPLY_MIN_ROWS = 10


#: The four Requiem relics differ by one roman numeral and are mixed up often.
#: Whenever one of them is offered they stand on 1-4 in this order.
_REQUIEM = tuple(f"Requiem {numeral} Relic"
                 for numeral in ("I", "II", "III", "IV"))


def requiem_in_order(options: list, lookup: dict) -> list:
    """Put Requiem I, II, III, IV on keys 1-4 when any of them is offered.

    The rest follow in the order they had. A Requiem that was not among the
    suggestions comes in without a score (0 shows none), not with an invented
    one. Only names are moved: the refinement is put on the labels afterwards,
    by `Cluster.with_refined_options`.
    """
    present = {option[0]: option for option in options}
    if not any(name in present for name in _REQUIEM):
        return options
    fixed = [present[name] if name in present else (name, 0.0, *lookup[name])
             for name in _REQUIEM if name in present or name in lookup]
    return fixed + [option for option in options if option[0] not in _REQUIEM]


def doubted_word_last(options: list, members: list) -> list:
    """Offer the short twin of a doubted composite name first.

    Only one case is handled. The scan doubts that the reading carries the word
    W of the matched name, and an offered option is that name with W replaced by
    a shorter word W' that W ends with ("Aklex" -> "Lex"), and W' stands as a
    whole word in the reading. Then the noise letters of a badge icon, not a
    missing "Ak", explain the reading, and that twin becomes option 1; the rest
    keep their order. In every other case nothing moves: when letters were cut
    off the front ("ntis Prime Handle") the doubted name is the right one.
    """
    pairs = {(m.name, m.doubt_word, m.reading) for m in members
             if getattr(m, "doubt_word", None)}
    if len(pairs) != 1:
        return options
    name, word, reading = pairs.pop()
    reading_words = {w.casefold() for w in reading.split()}
    parts = name.split()
    if not parts or parts[0] != word:
        return options
    for option in options:
        other = option[0].split()
        if (len(other) == len(parts) and other[1:] == parts[1:]
                and 0 < len(other[0]) < len(word)
                and word.casefold().endswith(other[0].casefold())
                and other[0].casefold() in reading_words):
            return [option] + [o for o in options if o is not option]
    return options


def _with_prime_variants(names: list[str], lookup: dict) -> list[str]:
    """Offer the Prime counterpart of every suggestion, and the plain one.

    OCR loses the middle word of a wrapped name more often than either end:
    "Inaros Prime / Chassis" came back as "Ina hassis", which then matched
    Inaros Chassis, Ivara Chassis, Mag Chassis and Voruna Chassis - four
    suggestions, not one of them Prime, for a tile that plainly said Prime. The
    correct answer was not on the list at all and had to be typed.

    Adding the counterpart costs one lookup and puts it there. It is not a
    guess: both names exist, and which one the tile shows is exactly what the
    picture beside them settles.
    """
    offered: list[str] = []
    for name in names:
        if name not in offered:
            offered.append(name)
        if " Prime" in name:
            plain = name.replace(" Prime", "", 1)
            if plain in lookup and plain not in offered:
                offered.append(plain)
        else:
            words = name.split()
            # "Inaros Chassis" -> "Inaros Prime Chassis": Prime sits after the
            # item name, not at the end.
            primed = f"{words[0]} Prime {' '.join(words[1:])}".strip()
            if primed in lookup and primed not in offered:
                offered.append(primed)
    return offered[:MAX_OPTIONS]


#: Radio buttons offered per reading. Four was the width of the digit-key
#: shortcut; six still fits on one hand and leaves room for the Prime
#: counterparts to appear beside their plain forms.
MAX_OPTIONS = 6


def _cluster_refinement(cluster) -> str | None:
    """The refinement the scan saw on these tiles, or None.

    Asked of the candidates, not of the reading. The scan gathers a relic's
    refinement across every hypothesis a cell produced - `decide_cell` keeps
    the first one any of them showed - and stores it on the candidate. The
    reading is one of those hypotheses, so reading the refinement back out of
    it throws away everything the other passes saw: "Axi A21 Relic [Radiant]"
    came out right while its displayed reading was only "AXi A21 Relic", and
    the same tile answered as a question would have been recorded Intact.

    Unanimity is required. A cluster is grouped by reading text and answered
    once, so tiles that disagree about their refinement cannot both be served;
    the reading decides then, which is what this did before and is no worse.
    """
    seen = {candidate.refinement for candidate in cluster.candidates
            if getattr(candidate, "refinement", None)}
    if len(seen) == 1:
        return seen.pop()
    _bare, level = quantity_mod.strip_refinement(cluster.reading)
    return level


@dataclass
class Cluster:
    reading: str
    candidates: list
    options: list
    #: Whether the scan already accepted this. Such a cluster is shown only
    #: when the user asked to see everything, and arrives already answered.
    accepted: bool = False

    @property
    def refinement(self) -> str | None:
        """The refinement of the relic this question is about, or None."""
        return _cluster_refinement(self)

    def with_refined_options(self) -> "Cluster":
        """Name the refinement on every relic option, once the options are final.

        The tile shows "Meso V16 Relic [Radiant]" and the scan knows it, so the
        card must offer that and nothing else: offering the bare name wrote the
        Intact stack and listed the Radiant one as gone. Done last, after
        `requiem_in_order` and the alternatives, which work on bare names. The
        label is what `ReviewWidget.decision` returns and what the apply step
        resolves, through the same call as a typed name.
        """
        level = self.refinement
        if not level or level == "INTACT":
            return self
        self.options = [
            (dbq.relic_label(name, level), score, *ids)
            if len(ids) > 2 and ids[2] else (name, score, *ids)
            for name, score, *ids in self.options]
        return self

    @property
    def is_repeat(self) -> bool:
        """Another tile already matched this item more convincingly."""
        return any(getattr(c, "repeat_of", None) for c in self.candidates)

    @property
    def reason(self) -> str:
        """Why this is being asked about, in the user's terms.

        A reading that scored 100 and still needs review looks like a bug
        unless the reason is on screen. Read from the candidate's own field
        rather than inferred from its numbers: inferring it missed every repeat
        that scored under 100, which then defaulted to accepting a duplicate.
        """
        if self.accepted:
            score = max((c.score for c in self.candidates), default=0.0)
            return (f"accepted by the scan at {score:.0f} — Enter confirms, "
                    f"pick another only if the reading says otherwise")
        # Named doubt first: it says something specific, where the fallbacks
        # below can only guess from the numbers.
        stated = next((c.doubt for c in self.candidates
                       if getattr(c, "doubt", None)), None)
        if stated:
            return stated
        if self.is_repeat:
            other = next(c.repeat_of for c in self.candidates if c.repeat_of)
            return (f"{other} is already recognised on another tile — a second "
                    f"one is usually a skin named after it")
        lead = max((c.lead for c in self.candidates), default=0.0)
        if lead <= 2:
            return "several names fit this reading equally well"
        return "the reading is too damaged to be sure"

    @property
    def evidence(self) -> bytes | None:
        """A picture of one of the tiles in this cluster.

        Any of them will do — they were clustered because they read the same,
        so the reviewer is deciding what that reading means, not which tile it
        was.
        """
        for candidate in self.candidates:
            if getattr(candidate, "evidence", None):
                return candidate.evidence
        return None

    @property
    def source(self):
        """The tile whose picture this cluster shows, for opening the frame.

        The crop answers "is this the right tile" and the frame answers "why
        did it read that", which on a question is the more useful of the two -
        and the card had no way to open it, on the one screen where the answer
        is usually next door.
        """
        for candidate in self.candidates:
            if getattr(candidate, "evidence", None):
                return candidate
        return None


def _run_scan(video_path, db_path, movement_step,
              report=lambda _m: None, value=lambda _p: None, cancel=None):
    """Run a scan, reporting progress and honouring a cancel request.

    Takes a database *path* and opens its own connection. An sqlite3 connection
    belongs to the thread that created it and refuses to be used from another,
    and this runs on a worker thread while the view's connection was made on
    the GUI thread - so passing the connection in failed the moment the scan
    touched the database. It only reads, so a second connection costs nothing.

    Progress carries an estimate because a two-minute job with no visible state
    is indistinguishable from a hung one, and that is the difference between a
    tool someone uses and one they stop trusting.
    """
    from wft.data import db as db_mod
    from wft.inventory import scan as scan_mod

    connection = db_mod.connect(db_path)

    last = [0.0]

    def show(progress):
        # Report on a timer rather than every nth frame: frames arrive in
        # bursts, so a frame-count trigger produces silence during slow
        # stretches, which is exactly when reassurance is wanted.
        import time as _time

        now = _time.perf_counter()
        if now - last[0] >= 0.5:
            last[0] = now
            report(progress.describe())
            value(progress)

    try:
        return scan_mod.scan(video_path, connection, movement_step=movement_step,
                             progress=show, cancel=cancel)
    finally:
        connection.close()


class ImportView(QWidget):
    #: The database's imports changed: a recording applied, a mastery import,
    #: an import taken back. The window's status line says how old they are.
    imports_changed = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.conn = dbq.connect(cfg.db_path)

        self.video_path: str | None = None
        self.scan_result = None
        self.clusters: list[Cluster] = []
        self.review_widgets: list[ReviewWidget] = []
        self.visited: set[int] = set()
        self._worker: TaskWorker | None = None
        #: Every style made from the theme's colours, by widget, with the
        #: function that makes it; `refresh` makes them again after a theme
        #: change. See `_paint`.
        self._painted: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.7))

        title = QLabel("Import")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_select_page())
        self.stack.addWidget(self._build_processing_page())
        self.stack.addWidget(self._build_review_page())
        self.stack.addWidget(self._build_apply_page())

        self._install_shortcuts()

    # -- select page -----------------------------------------------------
    def _build_select_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(theme.px(0.95))
        layout.addSpacing(theme.px(0.55))

        # What happens, before it happens. The scan takes minutes and writes at
        # the end, and both facts are worth knowing before starting rather than
        # discovering. The old text mentioned "", an internal decision
        # reference that means nothing outside this repository.
        steps = QFrame()
        steps.setObjectName("Card")
        steps_layout = QVBoxLayout(steps)
        steps_layout.setContentsMargins(*theme.card_margins())
        steps_layout.setSpacing(theme.px(0.7))

        for number, (heading, detail) in enumerate((
            ("Record", "Record each inventory tab you want tracked, scrolling "
                       "slowly from top to bottom, or take a screenshot of each "
                       "screen (on KDE Spectacle does both; the guide has the "
                       "keys). Put the files of one import in a folder of their "
                       "own. A tile seen several times is read several times, "
                       "and the repetition corrects the reading."),
            ("Scan", "Reading a 4K recording takes a few minutes. Progress and "
                     "a time estimate are shown throughout, and it can be "
                     "cancelled without losing what was already read."),
            ("Review", "Uncertain readings are shown beside a picture of the "
                       "tile they came from, grouped so each distinct reading "
                       "is one decision rather than one per tile."),
            ("Apply", "Nothing reaches the database until this step. The final "
                      "table lists every change against what is currently "
                      "recorded, and each row can be unticked."),
        ), start=1):
            row = QHBoxLayout()
            row.setSpacing(theme.px(0.8))
            marker = QLabel(str(number))
            marker.setObjectName("StepNumber")
            marker.setFixedWidth(theme.px(1.8))
            marker.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            row.addWidget(marker)

            column = QVBoxLayout()
            column.setSpacing(2)
            name = QLabel(heading)
            name.setObjectName("StepHeading")
            body = QLabel(detail)
            body.setWordWrap(True)
            body.setObjectName("ViewSubtitle")
            column.addWidget(name)
            column.addWidget(body)
            row.addLayout(column, 1)
            steps_layout.addLayout(row)

        layout.addWidget(steps)

        row = QHBoxLayout()
        choose_btn = QPushButton("Choose recording...")
        choose_btn.clicked.connect(self._choose_video)
        row.addWidget(choose_btn)
        folder_btn = QPushButton("Choose folder...")
        folder_btn.setToolTip(
            "Every recording and screenshot in the folder, read as one import. "
            "One capture per category keeps cosmetics out of the scan, and "
            "cosmetics read as the Warframe they belong to are most of what "
            "ends up in the review queue.")
        folder_btn.clicked.connect(self._choose_folder)
        row.addWidget(folder_btn)
        row.addStretch(1)
        layout.addLayout(row)
        # On a line of its own: a path is as long as it likes.
        self.path_label = QLabel("No file selected.")
        self._paint(self.path_label, lambda: f"color: {theme.TEXT_DIM};")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        self.start_btn = QPushButton("Start scan")
        self.start_btn.setObjectName("Primary")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_scan)
        layout.addWidget(self.start_btn)

        self.catalogue_warning = QLabel(
            "No catalogue yet. Every tile would match nothing and the review "
            "would come back empty — open Setup and press Download game "
            "data first. It reuses what is already downloaded.")
        self.catalogue_warning.setWordWrap(True)
        self.catalogue_warning.setVisible(False)
        self._paint(self.catalogue_warning, lambda: theme.text("body", theme.WARN))
        layout.addWidget(self.catalogue_warning)

        self.last_import = QLabel()
        self.last_import.setObjectName("ViewSubtitle")
        self.last_import.setWordWrap(True)
        layout.addWidget(self.last_import)
        self._show_last_import()
        self._check_catalogue()

        # The other import. Two ways in exist because they see different
        # things: a scan reads what is on screen now, the profile knows what
        # the account has ever levelled — including everything sold. They
        # belong on the same page.
        from ui.widgets.import_history import ImportHistory
        from ui.widgets.profile_import import ProfileImport

        self.profile_import = ProfileImport(self.cfg, self.conn)
        self.profile_import.imported.connect(self._show_last_import)
        self.profile_import.imported.connect(self.imports_changed.emit)
        layout.addWidget(self.profile_import)

        # The pictures, next to the import that decides which ones are needed.
        # Fetched for what the inventory holds, so the button belongs on the
        # page that fills the inventory.
        from ui.widgets.picture_fetch import PictureFetch

        self.pictures = PictureFetch(self.cfg, self.conn)
        layout.addWidget(self.pictures)

        # The journal, on the page that writes to it. An import that cannot be
        # inspected afterwards is an import that has to be trusted, and this
        # one has been wrong.
        self.history = ImportHistory(self.conn)
        self.history.changed.connect(self._show_last_import)
        self.history.changed.connect(self.imports_changed.emit)
        layout.addWidget(self.history, 1)

        layout.addStretch(0)

        # The page is taller than the window at a large text size, and with
        # nowhere to go Qt squeezed every row below its text: at 14 and 17pt in
        # a 2000x1150 window the step descriptions, the profile row and its help
        # text were cut off and the history showed only its header (11 and 13
        # widgets, measured; none in a tall window). It scrolls instead, as the
        # Plan page does.
        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        return scroll

    def catalogue_changed(self) -> None:
        """The reference tables were rebuilt under us.

        The list of names a manual answer can resolve to is read once and kept
        - it is three thousand rows and the completer is handed the same list
        for every card - so it has to be dropped when the tables behind it
        change, or a freshly built catalogue would still offer nothing.
        """
        self._known_cache = None
        self._lookup = None
        self._show_last_import()
        self._check_catalogue()

    def _catalogue_size(self) -> int:
        """How many names a scan has to match against.

        Zero is a real state and it used to be a silent one: after the
        database was wiped the catalogue is empty until it is rebuilt, every
        tile matches nothing, and the scan finishes with an empty review and
        no explanation. A scan is minutes of work, so this is worth saying
        before it starts rather than after.
        """
        try:
            return self.conn.execute(
                "SELECT (SELECT COUNT(*) FROM items) + (SELECT COUNT(*) FROM "
                "parts) + (SELECT COUNT(*) FROM relics)").fetchone()[0]
        except Exception:  # noqa: BLE001 - a broken database is its own message
            return 0

    def _check_catalogue(self) -> bool:
        """Say so on the page when there is nothing to match against."""
        empty = self._catalogue_size() == 0
        self.catalogue_warning.setVisible(empty)
        if empty:
            self.start_btn.setEnabled(False)
            self.start_btn.setToolTip(
                "There is nothing to match tiles against yet — build the "
                "catalogue first.")
        else:
            self.start_btn.setToolTip("")
        return not empty

    def _show_last_import(self):
        """What the database already holds from a recording.

        A page that looks identical before and after a successful import gives
        no way to tell whether the last one landed.
        """
        # The counts come from `readiness.inventory_check`, the answer the
        # Setup card "Your inventory" gives too, so the two cannot differ.
        check = readiness.inventory_check(self.cfg, self.conn)
        if check.state == "empty":
            self.last_import.setText(
                "No recording has been applied yet — the inventory is empty or "
                "hand-entered.")
            return
        held = check.info
        text = (f"Currently recorded from a recording: {held['parts']} part(s) "
                f"across {held['kinds']} kind(s), {held['items']} "
                f"built item(s). Applying a new scan replaces these — a "
                f"recording is a photo of the current state, not an addition "
                f"to it.")

        # Say when the stored numbers predate the reader now installed. A
        # correction to the reader does not reach back into the database, so
        # someone has to be told to scan again - and until now nobody was, which
        # is how counts read before the badge fix went on being trusted.
        from wft.inventory import scan as scan_mod

        stale, stored = dbq.stale_scan(self.conn, scan_mod.SCAN_REVISION)
        if stale:
            text += ("\n\nThese were read by an older version of the reader"
                     + (f" (revision {stored}, now {scan_mod.SCAN_REVISION})"
                        if stored is not None else "")
                     + ". Stack counts in particular were wrong about as often "
                       "as they were right before the current one. Scanning "
                       "again replaces them.")
            self._paint(self.last_import, lambda: f"color: {theme.WARN};")
        else:
            self._paint(self.last_import, lambda: "")
        self.last_import.setText(text)

    def _choose_video(self):
        path = file_dialogs.open_file(
            self, "Choose inventory recording", "",
            "Video files (*.mp4 *.mkv *.webm *.mov *.avi);;All files (*)")
        if path:
            self._accept_source(path)

    def _choose_folder(self):
        """Take a whole folder - recordings, screenshots, or a mix of both.

        One capture per category is the point, and there is no reason they
        would all be the same kind: a tab that fits on one screen is a
        screenshot, a long one is a recording. Both go in together as one
        import, each file its own page.

        Most of the review queue is cosmetics read as the Warframe they belong
        to, and a capture of the Warframes tab does not contain the Arcanes
        tab. That is worth more than the speed.
        """
        path = file_dialogs.open_directory(
            self, "Choose a folder of captures")
        if not path:
            return
        self._accept_folder(path)

    def _accept_folder(self, path: str) -> bool:
        """Take a folder as the source of the import."""
        found = frames_mod.media_files(path)
        if not found:
            QMessageBox.warning(
                self, "Nothing to read",
                f"That folder holds no recordings or screenshots.\n\n"
                f"Looked for: "
                f"{', '.join(frames_mod.IMAGE_SUFFIXES + frames_mod.VIDEO_SUFFIXES)}")
            return False
        videos = sum(1 for p in found if frames_mod.is_video(p))
        pictures = len(found) - videos
        parts = []
        if videos:
            parts.append(f"{videos} recording{'s' if videos != 1 else ''}")
        if pictures:
            parts.append(f"{pictures} screenshot{'s' if pictures != 1 else ''}")
        self._accept_source(path, f"{path}  ({', '.join(parts)})")
        return True

    def _accept_source(self, path: str, label: str | None = None):
        self.video_path = path
        self.path_label.setText(label or path)
        self._paint(self.path_label, lambda: f"color: {theme.TEXT};")
        self.start_btn.setEnabled(self._check_catalogue())

    # -- processing page -----------------------------------------------------
    def _build_processing_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addSpacing(theme.px(2.0))

        self.processing_label = QLabel("Scanning...")
        self._paint(self.processing_label, lambda: theme.text("section"))
        layout.addWidget(self.processing_label)

        # A determinate bar with a time left, not a barber's pole. The whole
        # point of showing progress on a job this long is telling "working" from
        # "hung", and an indeterminate bar says neither. It starts
        # indeterminate only because the first estimate needs a few seconds of
        # measured throughput to exist.
        bar = QProgressBar()
        bar.setRange(0, 0)
        bar.setTextVisible(True)
        self.processing_bar = bar
        layout.addWidget(bar)

        row = QHBoxLayout()
        self.processing_eta = QLabel("estimating…")
        self.processing_eta.setObjectName("ViewSubtitle")
        row.addWidget(self.processing_eta, 1)
        self.cancel_btn = QPushButton("Cancel scan")
        self.cancel_btn.clicked.connect(self._cancel_scan)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        from PySide6.QtWidgets import QPlainTextEdit
        self.processing_log = QPlainTextEdit()
        self.processing_log.setReadOnly(True)
        self.processing_log.setMaximumBlockCount(500)
        layout.addWidget(self.processing_log, 1)
        return page

    def _start_scan(self):
        from wft.inventory import frames as frames_mod, quantity as quantity_mod

        if not self._check_catalogue():
            QMessageBox.warning(
                self, "No catalogue yet",
                "There are no item names to match tiles against, so this scan "
                "would read every tile and recognise none of them.\n\n"
                "Open Setup, press Download game data, then scan.")
            return

        self.stack.setCurrentIndex(PAGE_PROCESSING)
        self.processing_label.setText(f"Scanning {self.video_path}")
        self.processing_log.clear()
        self.processing_bar.setRange(0, 0)
        self.processing_eta.setText("estimating…")
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel scan")

        worker = TaskWorker(_run_scan, self.video_path, self.cfg.db_path,
                            frames_mod.MOVEMENT_STEP)
        worker.progress.connect(self.processing_log.appendPlainText)
        worker.progress_value.connect(self._on_progress)
        worker.finished_ok.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        self._worker = worker
        worker.start()

    def _on_progress(self, progress):
        """Move the bar and say how long is left."""
        # Switch to a real bar as soon as there is a total to measure against.
        # Waiting for a non-zero percentage kept the marching block on screen
        # through the first minute, because a few percent of the sampling pass
        # rounds to zero percent of the job.
        if progress.frames_total:
            self.processing_bar.setRange(0, 100)
            self.processing_bar.setValue(int(progress.overall * 100))

        remaining = progress.overall_remaining
        stage = progress.stage
        if remaining is None:
            self.processing_eta.setText(f"{stage} — estimating…")
        elif remaining > 90:
            self.processing_eta.setText(
                f"{stage} — about {remaining / 60:.0f} min left")
        else:
            self.processing_eta.setText(
                f"{stage} — about {remaining:.0f} s left")

    def _cancel_scan(self):
        """Stop, and keep what was read.

        Cancelling returns the partial result rather than nothing: eighteen
        minutes of work should not evaporate because someone changed their mind
        at minute seventeen, and what was read so far is still reviewable.
        """
        worker = getattr(self, "_worker", None)
        if worker is not None:
            worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self.processing_eta.setText(
                "stopping — the frame being read has to finish first")

    def _on_scan_failed(self, message: str):
        QMessageBox.warning(self, "Scan failed", message)
        self.stack.setCurrentIndex(PAGE_SELECT)

    def _on_scan_finished(self, result):
        """Hand the scan over to the review, and say so if that fails.

        An exception raised in a Qt slot does not propagate anywhere a user can
        see: Qt logs it and carries on, so the window sat on the finished
        progress page indefinitely. That is indistinguishable from a hang, and
        it is what a five-element tuple unpacked into four names produced for
        several hours.
        """
        try:
            self._hand_over(result)
        except Exception as exc:  # noqa: BLE001 - never leave the page stuck
            import traceback

            traceback.print_exc()
            QMessageBox.critical(
                self, "Could not open the review",
                f"The scan finished but preparing the review failed:\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                f"The scan itself is not lost — nothing has been written, and "
                f"running it again after the fault is fixed will not cost "
                f"anything else.")
            self.stack.setCurrentIndex(PAGE_SELECT)

    def _hand_over(self, result):
        self.scan_result = result
        if result.grid is None:
            QMessageBox.warning(
                self, "No grid found",
                "Could not find a six-column inventory grid in this recording. "
                "Notes:\n" + "\n".join(result.notes or ["(none)"]))
            self.stack.setCurrentIndex(PAGE_SELECT)
            return
        self._prepare_review()
        self.stack.setCurrentIndex(PAGE_REVIEW)

    # -- review page -----------------------------------------------------
    def _build_review_page(self) -> QWidget:
        """One list, every reading in it.

        The previous arrangement was two views sharing a page: a table of what
        the scan accepted above, a question-at-a-time queue below. Neither had
        room - the table showed two rows of two hundred and eight - and the two
        had separate ways of saying the same thing, so a reading corrected in
        one was still wrong in the other.

        One list, questions first, everything else after it in the order the
        scan read them. This is the shape the earlier tool had,
        and the reason its work could be checked.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(theme.px(0.7))

        self.review_summary = QLabel("")
        self._paint(self.review_summary, lambda: f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.review_summary)

        head_row = QHBoxLayout()
        self.filter_group = QButtonGroup(self)
        self.filter_buttons = {}
        for key, label in (("all", "Everything"), ("open", "Needs review"),
                           ("accepted", "Accepted")):
            button = QToolButton()
            button.setObjectName("FilterChip")  # the chips every other page has
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(key == "all")
            button.clicked.connect(lambda _checked, k=key: self._filter_review(k))
            self.filter_group.addButton(button)
            self.filter_buttons[key] = button
            head_row.addWidget(button)
        head_row.addStretch(1)
        layout.addLayout(head_row)
        # The search is a row of its own. Beside the three filters it needed
        # 1400px at 20pt, and in a 1600x900 window the page has 1178: the
        # filters were squeezed under their text and the field ran out of the
        # page.
        search_row = QHBoxLayout()
        self.review_search = QLineEdit()
        self.review_search.setPlaceholderText("Search a reading or a name")
        self.review_search.setClearButtonEnabled(True)
        self.review_search.setToolTip(
            "Narrow the list to rows whose reading, answer or reason contains "
            "this text. The filter above still applies.")
        # A floor as well as a ceiling: the field was squeezed to 81px at 20pt
        # (its hint is 348px) by a hint line that did not wrap. 17em holds the
        # hint, the clear button and the padding at every text size.
        self.review_search.setMinimumWidth(theme.px(17))
        self.review_search.setMaximumWidth(theme.px(24))
        self.review_search.textChanged.connect(self._search_review)
        search_row.addWidget(self.review_search)
        search_row.addStretch(1)
        layout.addLayout(search_row)
        # Its own line, wrapped: beside the search field it was one label 897px
        # wide on a page 1482px wide at 20pt and cut after "M type a n".
        hint = QLabel("↑↓ move  ·  Shift+↑↓ next/previous question  ·  "
                      "Num 1-6 pick  ·  Num 0 ignore  ·  Num +/- or Ctrl+↑↓ "
                      "count  ·  Enter next  ·  Esc back  ·  M type a name  "
                      "·  Space keep/drop  ·  click a tile to see where it "
                      "came from")
        hint.setWordWrap(True)
        self._paint(hint, lambda: theme.text("small", theme.TEXT_FAINT))
        layout.addWidget(hint)

        self.review_list = QListWidget()
        self.review_list.setSelectionMode(QListWidget.SingleSelection)
        self.review_list.setUniformItemSizes(False)
        self.review_list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.review_list.currentRowChanged.connect(self._on_row_selected)
        self.review_list.installEventFilter(self)
        layout.addWidget(self.review_list, 1)

        footer = QHBoxLayout()
        self.review_position_label = QLabel("")
        self._paint(self.review_position_label, lambda: f"color: {theme.TEXT_DIM};")
        # Wraps: "11 question(s) left · 220 row(s) shown" was 482px and got 313
        # beside three buttons at 20pt in a 1600x900 window.
        self.review_position_label.setWordWrap(True)
        footer.addWidget(self.review_position_label, 1)
        discard = QPushButton("Discard import")
        discard.setToolTip("Throw this scan away. Nothing has been written yet.")
        discard.clicked.connect(self._abort_import)
        footer.addWidget(discard)
        next_btn = QPushButton("Next question  (Enter)")
        next_btn.clicked.connect(self._advance)
        footer.addWidget(next_btn)
        finish_btn = QPushButton("Finish review")
        finish_btn.setObjectName("Primary")
        finish_btn.clicked.connect(self._finish_review)
        footer.addWidget(finish_btn)
        layout.addLayout(footer)

        return page

    def _filter_review(self, which: str) -> None:
        """Show one kind of row, or all of them.

        A filter rather than a second page: the rows are the same rows, and
        moving between them must not lose an answer.
        """
        self._filter = which
        self._apply_filter()

    def _search_review(self, text: str) -> None:
        self._search = text.strip().casefold()
        self._apply_filter()

    def _apply_filter(self) -> None:
        which = getattr(self, "_filter", "all")
        needle = getattr(self, "_search", "")
        for entry in self.rows:
            wanted = (which == "all"
                      or (which == "open" and entry["kind"] == "question")
                      or (which == "accepted" and entry["kind"] == "accepted"))
            if wanted and needle:
                wanted = dbq.name_matches(needle, self._row_texts(entry))
            entry["item"].setHidden(not wanted)
        self._update_position()

    def _row_texts(self, entry: dict) -> list[str]:
        """Everything about a row that is worth searching for, one string each.

        Matched one by one with `dbq.name_matches`, so a typo is forgiven
        against a name or a reading and a word found anywhere on the row still
        counts.

        The answer as well as the reading: a player may ask "why does
        Thornbak say ignored, and is it ignored anywhere else" - which is a
        search for a word that is on the row but is not what the tile read.
        """
        if entry["kind"] == "accepted":
            parts = [entry["name"]]
            parts += sorted({m.reading for m in entry["members"]})
        else:
            cluster = self.clusters[entry["cluster"]]
            parts = [cluster.reading, cluster.reason]
            parts += [name for name, *_rest in cluster.options]
            widget = self.review_widgets[entry["cluster"]]
            if widget is not None:
                decision = widget.decision()
                parts.append(decision[-1] if decision else "ignored")
        return [p for p in parts if p]

    def _known_names(self) -> list[str]:
        """Every name a manual entry can resolve to, cached for the review.

        Built once: the completer is handed the same list for every widget, and
        rebuilding it per cluster would query the database forty times for an
        answer that does not change.
        """
        if getattr(self, "_known_cache", None) is None:
            # Relics too. They live in their own table, and leaving them out
            # meant a relic could not be typed at all: "Lith" came back as "no
            # such name — did you mean Laith?", which is a weapon.
            names = [row["name"] for row in self.conn.execute(
                "SELECT name FROM parts UNION SELECT name FROM items "
                "UNION SELECT name FROM relics")]
            # Forma and its kind resolve to nothing, so they are not offered
            # either.
            uncounted = dbq.uncounted_names(self.conn)
            self._known_cache = sorted(n for n in names if n not in uncounted)
        return self._known_cache

    def _resolver_for(self, cluster: "Cluster"):
        """`_resolve_typed` for one question: its refinement is the default.

        The card's tick and the written row both come from this, with the
        same text, so a name typed without a refinement on a card whose tiles
        showed [Radiant] is ticked and written as the Radiant stack.
        """
        refinement = cluster.refinement
        return lambda text: self._resolve_typed(text, refinement)

    def _resolve_typed(self, text: str, default_refinement: str | None = None):
        """What a hand-typed name would be written as, or None.

        Handed to every review card and called again when the review is
        applied, so the tick beside the field and the row that reaches the
        database are the same answer to the same question. They were two
        answers until a typed "Neo c9 relic [radiant]" was shown a
        green "matches" and then got "do not match anything in the database" at
        the end of the review.
        """
        return dbq.resolve_typed_name(self.conn, text, self._known_names(),
                                      default_refinement)

    def _prepare_review(self):
        """Build the one list: questions first, then everything else."""
        from wft.inventory import scan as scan_mod

        result = self.scan_result
        lookup = scan_mod._tracked_names(self.conn)
        self._lookup = lookup
        self._names_list = list(lookup)

        # Questions. Identical readings are one decision: forty tiles reading
        # "Neuroptics Blueprint" are one wrapped-name problem, not forty.
        #
        # Except where the scan says two tiles are contesting one relic. Those
        # are the opposite case - the readings agree and the tiles do not -
        # and merging them would answer both at once and add their counts
        # together, which is exactly the loss the scan flagged them to avoid:
        # nine of one relic and twenty of another would be written as
        # twenty-nine of whichever was answered. Keyed by position so each
        # keeps its own picture, its own count and its own answer.
        groups: dict = defaultdict(list)
        for candidate in result.needs_review:
            key = ((candidate.column, candidate.row)
                   if getattr(candidate, "contested", False)
                   else candidate.reading.strip().lower())
            groups[key].append(candidate)

        scored: list[tuple[float, Cluster]] = []
        for members in groups.values():
            representative = members[0].reading
            options = self._options_for(representative)
            options = requiem_in_order(
                doubted_word_last(self._with_alternatives(options, members),
                                  members), lookup)
            scored.append((min(m.score for m in members),
                           Cluster(representative, members, options)
                           .with_refined_options()))
        scored.sort(key=lambda pair: pair[0])
        self.clusters = [cluster for _, cluster in scored]

        # Accepted readings, grouped by the name they matched, in the order the
        # scan met them. Capture order rather than alphabetical: the eye
        # remembers a shelf, and a list in the order it was photographed can be
        # followed against the recording.
        accepted: dict[str, list] = defaultdict(list)
        for candidate in result.confident:
            accepted[candidate.display_name].append(candidate)
        ordered = sorted(accepted.items(),
                         key=lambda pair: min((m.row, m.column)
                                              for m in pair[1]))

        self.review_list.clear()
        self.review_widgets = []
        self.rows = []
        self.auto_boxes = {}
        self.accepted_rows = {}
        self.visited = set()
        self._filter = "all"
        self._search = ""
        self._open_row = None
        self._typed_counts = {}

        for index, cluster in enumerate(self.clusters):
            self.review_widgets.append(None)
            self._add_row({"kind": "question", "cluster": index,
                           "widget": self._closed_question(index)})

        for name, members in ordered:
            self._add_accepted_row(name, members)

        if not self.rows:
            # A scan that recognised nothing lands here, and a blank page is
            # the least useful thing to show somebody who just waited minutes
            # for it. The commonest cause by far is an empty catalogue.
            reason = ("The catalogue is empty, so no tile could match anything "
                      "— open Setup, press Download game data, and scan again."
                      if self._catalogue_size() == 0 else
                      "No tile in the recording could be read. Check that the "
                      "capture shows the inventory screen and that the names "
                      "are legible in it.")
            self.review_summary.setText(f"Nothing was recognised. {reason}")
            theme.mark(self.review_summary, "warn")
            self._paint(self.review_summary, lambda: theme.text("body", theme.WARN))
        else:
            self._paint(self.review_summary, lambda: f"color: {theme.TEXT_DIM};")
            self.review_summary.setText(
                f"{len(result.confident)} tile(s) read without a question, "
                f"{len(result.needs_review)} across {len(self.clusters)} "
                f"reading(s) to answer. Every tile is in this list.")
        # Tiles, not rows. The summary above counts tiles and these counted
        # rows, so "227 tile(s) read without a question" sat next to
        # "Accepted (225)" - and the two names carrying two tiles each, marked
        # x2 in their own rows, looked like two tiles gone missing. Same
        # quantity in both places or the pair reads as an error report.
        for key, label, count in (
                ("all", "Everything",
                 len(result.confident) + len(result.needs_review)),
                ("open", "Needs review", len(result.needs_review)),
                ("accepted", "Accepted", len(result.confident))):
            self.filter_buttons[key].setText(f"{label}  ({count})")

        if self.rows:
            self.review_list.setCurrentRow(0)
        self._update_position()

    def _frame_for(self, candidate):
        """The frame a candidate was cut from, if the scan still holds it."""
        if candidate is None or getattr(candidate, "frame_index", None) is None:
            return None
        frames = getattr(self.scan_result, "frames", None) or {}
        return frames.get(candidate.frame_index)

    def _closed_question(self, index: int):
        """The compact form of a question, for a list you scan down."""
        from ui.widgets.question_row import QuestionRow

        cluster = self.clusters[index]
        widget = self.review_widgets[index]
        guess = ""
        if widget is not None:
            decision = widget.decision()
            guess = decision[-1] if decision else "ignored"
        elif cluster.options:
            guess = cluster.options[0][0]
        source = cluster.source
        row = QuestionRow(
            cluster.reading, len(cluster.candidates), cluster.reason, guess,
            answered=index in self.visited, count=self._cluster_count(index),
            evidence=cluster.evidence, frame=self._frame_for(source),
            bbox=getattr(source, "bbox", None))
        row.chosen.connect(lambda i=index: self._focus_question(i))
        return row

    def _open_question(self, index: int):
        """The full card, built the first time this question is answered.

        Built on demand rather than up front: two hundred rows each carrying
        six radio buttons and a completer is a page that takes a second to
        appear, and almost none of them are ever opened.
        """
        widget = self.review_widgets[index]
        if widget is not None:
            return widget
        cluster = self.clusters[index]
        source = cluster.source
        widget = ReviewWidget(cluster.reading, len(cluster.candidates),
                              cluster.options, evidence=cluster.evidence,
                              known=self._known_names(),
                              resolve=self._resolver_for(cluster),
                              reason=cluster.reason,
                              is_repeat=cluster.is_repeat,
                              frame=self._frame_for(source),
                              bbox=getattr(source, "bbox", None),
                              count=self._cluster_count(index))
        self.review_widgets[index] = widget
        self._wire_card(widget)
        return widget

    def _wire_card(self, widget: ReviewWidget) -> None:
        """Movement and Esc inside an open card belong to the list."""
        widget.navigate.connect(self._navigate_from_card)
        widget.released.connect(self.review_list.setFocus)
        widget.chord.connect(self._on_chord)
        widget.scrolled.connect(
            lambda event: QApplication.sendEvent(
                self.review_list.viewport(), event))

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        # The one-handed keys with the focus on the list; the open card has
        # its own filter that reports the same chords (`ReviewWidget.chord`).
        if (obj is getattr(self, "review_list", None)
                and event.type() == QEvent.KeyPress):
            action = review_chord(event)
            if action is not None:
                self._on_chord(action)
                return True
        if (obj is getattr(self, "apply_table", None)
                and event.type() == QEvent.KeyPress
                and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & ~Qt.KeypadModifier
                and self.apply_table.state() != QAbstractItemView.EditingState):
            # With a count open for typing the editor has the key and
            # confirms the number; this is the table's own Enter.
            self._toggle_apply_row()
            return True
        return super().eventFilter(obj, event)

    def _on_chord(self, action: str) -> None:
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        if action == "ignore":
            self._on_ignore()
        elif action in ("next_question", "previous_question"):
            self.review_list.setFocus()
            self._step_question(1 if action == "next_question" else -1)
        elif action in ("count_up", "count_down"):
            widget = self._current_review_widget()
            if widget is not None:
                # The field's own value: typing in it changes the same
                # number, and `_remember_count` reads it from there.
                widget.count.setValue(max(0, widget.count.value()
                                          + (1 if action == "count_up" else -1)))

    def _step_question(self, direction: int) -> None:
        """Go to the next (1) or previous (-1) question still to be opened.

        Sure readings and hidden rows are skipped, as `_advance` does. When
        every question has been opened, the neighbouring question anyway, so
        the key still moves.
        """
        count = len(self.rows)
        current = self.review_list.currentRow()
        for only_open in (True, False):
            for offset in range(1, count):
                index = (current + direction * offset) % count
                entry = self.rows[index]
                if entry["item"].isHidden() or entry["kind"] != "question":
                    continue
                if only_open and entry["cluster"] in self.visited:
                    continue
                self.review_list.setCurrentRow(index)
                return
        self._update_position()

    def _navigate_from_card(self, key: int) -> None:
        # The same key the list would have got, so it moves exactly as it does
        # when it has the focus.
        self.review_list.setFocus()
        QApplication.sendEvent(self.review_list, QKeyEvent(
            QEvent.KeyPress, key, Qt.NoModifier))

    def _cluster_count(self, index: int) -> int:
        """How many this reading stands for, as it will be recorded.

        The reviewer's own number if they gave one, otherwise the sum of what
        the badges said. Kept on the view rather than on the card because the
        card is closed and rebuilt as the list is walked, and a number typed
        into it has to outlive that.
        """
        typed = getattr(self, "_typed_counts", {}).get(index)
        if typed is not None:
            return typed
        cluster = self.clusters[index]
        return sum(m.quantity if m.quantity is not None else 1
                   for m in cluster.candidates)

    def _remember_count(self, index: int) -> None:
        widget = self.review_widgets[index]
        if widget is not None and widget.count_edited():
            self._typed_counts[index] = widget.counted()

    def _remember_all_counts(self) -> None:
        for index, widget in enumerate(self.review_widgets):
            if widget is not None:
                self._remember_count(index)

    def _focus_question(self, index: int) -> None:
        for position, entry in enumerate(self.rows):
            if entry["kind"] == "question" and entry["cluster"] == index:
                self._select(position)
                return

    def _select(self, position: int) -> None:
        """Put the cursor on a row and open it, even if it is already there.

        `setCurrentRow` on the current row is silent, so clicking a question
        you had already visited did nothing at all - the row you were looking
        at was the one row you could not open. Answering moves the cursor on,
        which makes going back to check an answer the ordinary case, not an
        odd one.
        """
        if self.review_list.currentRow() == position:
            self._on_row_selected(position)
        else:
            self.review_list.setCurrentRow(position)

    def _set_row_widget(self, entry: dict, widget) -> None:
        """Swap what a row shows, without the list taking the old one with it.

        Two things went wrong here and both were invisible.

        A list owns the widget you hand it, and handing it a second one
        destroys the first. The answered cards were kept in `review_widgets`
        to be reopened later, so every one of them became a Python name for a
        destroyed object the moment the reader moved on - reading an answer
        back raised "Internal C++ object already deleted". So the list is
        given a plain holder per row, once, and the cards are swapped inside
        it: the list owns the holder, this view owns the cards.

        And a widget's size hint before it has been laid out is not its size.
        Asked straight after construction a review card reported about half
        what it needs, and the row clipped it - the picture cut off at the top,
        the options crushed against the buttons. `adjustSize` lays it out
        first, and the pass after the event loop catches whatever settled
        later, such as a reason line that wrapped.
        """
        holder = entry["holder"]
        box = holder.layout()
        previous = entry.get("widget")
        if previous is not None and previous is not widget:
            # A focused control leaving the window drops focus to whatever
            # comes next in the tab order - a filter chip - and arrow keys
            # then went nowhere. The list keeps it.
            focus = QApplication.focusWidget()
            if focus is not None and (focus is previous
                                      or previous.isAncestorOf(focus)):
                self.review_list.setFocus()
            box.removeWidget(previous)
            previous.setParent(None)
            # Closed rows are cheap and disposable; cards hold an answer and
            # are kept by `review_widgets` for when the reader comes back.
            if not isinstance(previous, ReviewWidget):
                previous.deleteLater()
        entry["widget"] = widget
        if widget.parent() is not holder:
            box.addWidget(widget)
        widget.show()
        holder.adjustSize()
        entry["item"].setSizeHint(holder.sizeHint())
        QTimer.singleShot(0, lambda e=entry: self._refit_row(e))

    def _refit_row(self, entry: dict) -> None:
        # Deferred by a turn of the event loop, by which time the list may have
        # been rebuilt: its items are deleted with it while a holder can live on.
        holder, item = entry.get("holder"), entry.get("item")
        if holder is None or not _shiboken.Shiboken.isValid(holder) \
                or item is None or not _shiboken.Shiboken.isValid(item):
            return
        wanted = holder.sizeHint()
        if wanted.height() > item.sizeHint().height():
            item.setSizeHint(wanted)

    def _expand(self, row: int) -> None:
        """Open the question under the cursor and close the one before it.

        One card at a time: the list is for scanning and the card is for
        answering, and a page of open cards is the tall-cards arrangement that was
        dropped.
        """
        if getattr(self, "_open_row", None) is not None and self._open_row != row:
            previous = self.rows[self._open_row] if 0 <= self._open_row < len(self.rows) else None
            if previous is not None and previous["kind"] == "question":
                self._remember_count(previous["cluster"])
                self._set_row_widget(previous,
                                     self._closed_question(previous["cluster"]))
        self._open_row = None
        if not (0 <= row < len(self.rows)):
            return
        entry = self.rows[row]
        if entry["kind"] != "question":
            return
        self._set_row_widget(entry, self._open_question(entry["cluster"]))
        self._open_row = row
        self.review_list.scrollToItem(entry["item"])

    def _add_row(self, entry: dict) -> None:
        item = QListWidgetItem()
        holder = QWidget()
        # A hairline under every row. Two hundred cards of the same height and
        # colour run together when they are read down a column - the eye needs
        # to see where one relic's count stops and the next one's starts.
        #
        # A rule in the theme rather than a stylesheet on each holder, so it
        # follows a theme change; and a border rather than a QFrame in the
        # layout, because `_set_row_widget` appends to that layout and a line
        # living in it would end up above the widget it belongs under.
        holder.setObjectName("ReviewRow")
        # Without this a bare QWidget ignores the rule: it paints itself, not
        # its stylesheet.
        holder.setAttribute(Qt.WA_StyledBackground, True)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        entry["item"] = item
        entry["holder"] = holder
        widget = entry.pop("widget")
        self.review_list.addItem(item)
        self.review_list.setItemWidget(item, holder)
        self._set_row_widget(entry, widget)
        self.rows.append(entry)

    def _add_accepted_row(self, name: str, members: list) -> None:
        best = next((m for m in members if m.evidence), members[0])
        frame = (self.scan_result.frames.get(best.frame_index)
                 if getattr(self.scan_result, "frames", None) else None)
        counted = sum(m.quantity if m.quantity is not None else 1
                      for m in members)
        row = AcceptedRow(
            name=name,
            reading=sorted({m.reading for m in members})[0],
            tiles=len(members),
            score=sum(m.score for m in members) / len(members),
            count=counted,
            evidence=best.evidence,
            frame=frame,
            bbox=getattr(best, "bbox", None))
        row.reopened.connect(lambda n=name: self._reopen(n))
        self.auto_boxes[name] = row.keep
        self.accepted_rows[name] = row
        self._add_row({"kind": "accepted", "name": name, "members": members,
                       "widget": row})

    def _with_alternatives(self, options: list, members: list) -> list:
        """Put the name the tile's own band gave at the top of the choices.

        The suggestions are worked out from the reading, so when the reading
        itself came from the wrong tile every suggestion is wrong with it - the
        answer the picture plainly shows would not be on the card at all.
        """
        lookup = self._lookup
        offered = [name for name, *_rest in options]
        extra = []
        for member in members:
            name = getattr(member, "alternative", None)
            if name and name in lookup and name not in offered:
                offered.append(name)
                extra.append((name, 100.0, *lookup[name]))
        return extra + options

    def _options_for(self, reading: str) -> list:
        """The names a reading might mean, best first.

        The scan's own tie-break is repeated here. Without it the suggestions
        for "Lith 04 Relic" were A4, B4, C4 and D4 - the alphabetical first
        four of twenty-three names that all score 92.3 - while the scan had
        already worked out that O4 is the one a misread zero points at.
        """
        from rapidfuzz import fuzz, process

        lookup = self._lookup
        names_list = self._names_list
        extracted = process.extract(reading, names_list, scorer=fuzz.ratio,
                                    processor=str.casefold, limit=4)
        if len(extracted) > 1 and extracted[0][1] == extracted[1][1]:
            key = match_mod.ocr_key(match_mod.normalise(reading))
            closest = process.extractOne(key, names_list, scorer=fuzz.ratio,
                                         processor=match_mod.ocr_key)
            if closest and closest[0] not in [n for n, _s, _i in extracted]:
                extracted = ([(closest[0], extracted[0][1], 0)]
                             + list(extracted)[:3])
        offered = _with_prime_variants([n for n, _s, _i in extracted], lookup)
        scores = {n: s for n, s, _ in extracted}
        return [(name, scores.get(name, 0.0), *lookup[name]) for name in offered]

    def _reopen(self, name: str) -> None:
        """Turn an accepted row into a question, in place.

        The alternatives are worked out now rather than for all two hundred
        rows up front: computing them for every accepted reading cost a visible
        pause on a page where almost none of them are ever asked for.
        """
        entry = next((e for e in self.rows
                      if e["kind"] == "accepted" and e["name"] == name), None)
        if entry is None:
            return
        members = entry["members"]
        cluster = Cluster(members[0].reading, members,
                          requiem_in_order(self._options_for(members[0].reading),
                                           self._lookup),
                          accepted=True).with_refined_options()
        self.clusters.append(cluster)
        source = cluster.source
        widget = ReviewWidget(cluster.reading, len(members), cluster.options,
                              evidence=cluster.evidence,
                              known=self._known_names(),
                              resolve=self._resolver_for(cluster),
                              reason=cluster.reason,
                              is_repeat=cluster.is_repeat,
                              frame=self._frame_for(source),
                              bbox=getattr(source, "bbox", None))
        self.review_widgets.append(widget)
        self._wire_card(widget)

        # The accepted row is gone, so its checkbox must not still be answering
        # for these tiles: the cluster answers for them now, and counting both
        # doubled every tile that was looked at twice.
        self.auto_boxes.pop(name, None)
        self.accepted_rows.pop(name, None)
        entry["kind"] = "question"
        entry["cluster"] = len(self.clusters) - 1
        self._set_row_widget(entry, widget)
        self.review_list.setCurrentItem(entry["item"])

    def _on_row_selected(self, row: int):
        if 0 <= row < len(self.rows):
            entry = self.rows[row]
            if entry["kind"] == "question":
                self.visited.add(entry["cluster"])
        self._expand(row)
        self._update_position()

    def _update_position(self) -> None:
        open_left = sum(1 for entry in self.rows
                        if entry["kind"] == "question"
                        and entry["cluster"] not in self.visited)
        shown = sum(1 for entry in self.rows if not entry["item"].isHidden())
        if open_left:
            self.review_position_label.setText(
                f"{open_left} question(s) left  ·  {shown} row(s) shown")
        else:
            self.review_position_label.setText(
                f"Every question answered  ·  {shown} row(s) shown")

    def _current_entry(self) -> dict | None:
        row = self.review_list.currentRow()
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None

    def _current_review_widget(self) -> ReviewWidget | None:
        entry = self._current_entry()
        if entry and entry["kind"] == "question":
            return self._open_question(entry["cluster"])
        return None

    def _advance(self):
        """Move to the next unanswered question, wrapping to the end of the list.

        Not simply the next row: with every tile in one list, "next" past the
        questions is two hundred rows nobody is being asked about.
        """
        current = self.review_list.currentRow()
        for offset in range(1, len(self.rows) + 1):
            index = (current + offset) % len(self.rows)
            entry = self.rows[index]
            if entry["item"].isHidden():
                continue
            if (entry["kind"] == "question"
                    and entry["cluster"] not in self.visited):
                self.review_list.setCurrentRow(index)
                return
        self._update_position()

    def _install_shortcuts(self):
        for digit in range(1, 7):
            shortcut = QShortcut(str(digit), self)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda d=digit: self._on_digit(d))

        # Enter is the review's alone. On the summary a shortcut would take it
        # from a count being typed, so it is off there and the table handles
        # the key itself (`eventFilter`).
        self._enter_shortcuts = []
        for key in (Qt.Key_Return, Qt.Key_Enter):
            enter = QShortcut(key, self)
            enter.setContext(Qt.WidgetWithChildrenShortcut)
            enter.activated.connect(self._on_enter)
            self._enter_shortcuts.append(enter)
        self.stack.currentChanged.connect(
            lambda page: [s.setEnabled(page == PAGE_REVIEW)
                          for s in self._enter_shortcuts])

        ignore = QShortcut(Qt.Key_I, self)
        ignore.setContext(Qt.WidgetWithChildrenShortcut)
        ignore.activated.connect(self._on_ignore)

        manual = QShortcut(Qt.Key_M, self)
        manual.setContext(Qt.WidgetWithChildrenShortcut)
        manual.activated.connect(self._on_manual)

        keep = QShortcut(Qt.Key_Space, self)
        keep.setContext(Qt.WidgetWithChildrenShortcut)
        keep.activated.connect(self._on_space)

    def _on_digit(self, digit: int):
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        widget = self._current_review_widget()
        if widget:
            widget.select_index(digit - 1)

    def _on_enter(self):
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        self._advance()

    def _toggle_apply_row(self) -> None:
        """Enter on the summary ticks or unticks the current row."""
        row = self.apply_table.currentRow()
        if 0 <= row < len(self.apply_rows):
            box = self.apply_rows[row]["checkbox"]
            if box.isEnabled():
                box.setChecked(not box.isChecked())

    def _on_ignore(self):
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        widget = self._current_review_widget()
        if widget:
            widget.set_ignored()
            self._advance()

    def _on_manual(self):
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        widget = self._current_review_widget()
        if widget:
            widget.focus_manual()

    def _on_space(self):
        """Keep or drop the accepted reading under the cursor."""
        if self.stack.currentIndex() != PAGE_REVIEW:
            return
        entry = self._current_entry()
        if entry and entry["kind"] == "accepted":
            entry["widget"].toggle()

    def _abort_import(self):
        """Throw the scan away and go back to the start.

        Reachable from the review and the apply table, because both are places
        someone realises they recorded the wrong thing - and until the write
        happens there is nothing to undo, so leaving is free. Confirmed anyway:
        the scan took three minutes and the click is next to the one that
        keeps it.
        """
        if self.scan_result is None:
            self._reset()
            return
        reply = QMessageBox.question(
            self, "Discard this import?",
            "The scan and every decision made in the review will be thrown "
            "away. Nothing has been written to the database.")
        if reply == QMessageBox.Yes:
            self._reset()

    def _finish_review(self):
        self._remember_all_counts()
        unresolved = [i for i in range(len(self.clusters)) if i not in self.visited]
        # Only the questions are worth warning about. An accepted reading that
        # was never opened is not skipped - the scan's answer stands - so
        # counting it here would tell the user that two hundred rows are about
        # to be dropped when none are.
        unanswered = [i for i in unresolved if not self.clusters[i].accepted]
        if unanswered:
            reply = QMessageBox.question(
                self, "Unreviewed readings",
                f"{len(unanswered)} reading(s) were never opened and will be skipped "
                "rather than guessed at. Continue?")
            if reply != QMessageBox.Yes:
                return
        self._build_apply_table(unresolved)
        self.stack.setCurrentIndex(PAGE_APPLY)
        self.apply_table.setFocus()

    # -- apply page -----------------------------------------------------
    def _build_apply_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(theme.px(0.7))

        # The two notes and the table scroll together; the buttons below do
        # not, so "Write to database" never leaves the window. With the notes
        # above the table and nowhere to scroll, they were given their height
        # first and the table got what was left: 11 rows at 14pt, 4 at 17pt
        # and 1 at 20pt, of 216. The table keeps ten rows now and the
        # page scrolls when the notes and those ten rows are more than fit.
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.px(0.7))

        header = QLabel(
            "This capture showed the following counts. Applying replaces the "
            "recorded count for each checked row with what was seen.")
        header.setWordWrap(True)
        self._paint(header, lambda: f"color: {theme.TEXT_DIM};")
        layout.addWidget(header)

        self.apply_note = QLabel(self._APPLY_NOTE)
        self.apply_note.setWordWrap(True)
        self._paint(self.apply_note, lambda: f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.apply_note)

        # The shared table, so it hovers, selects and heads its columns
        # like every other. Not sorted: `apply_rows` is read by row index, and
        # a table that reorders itself would hand the wrong row to the write.
        # Not fitted: it is the one table that is longer than a page.
        self.apply_table = tables.simple_table(
            ["Apply", "Kind", "Name", "Currently recorded", "Write this"], 0,
            stretch=2, fit=False)
        # The last column is typed into, not just read. A scan that says two
        # where the user knows there is one left them with a number they could see
        # was wrong and no way to say so — the choice was take it or discard
        # the whole import. Everything else here stays read-only.
        # One click on a count opens it for typing; _build_apply_table makes
        # every other cell read-only, so that click cannot open a name.
        self.apply_table.setEditTriggers(
            QTableWidget.CurrentChanged | QTableWidget.DoubleClicked
            | QTableWidget.SelectedClicked | QTableWidget.EditKeyPressed
            | QTableWidget.AnyKeyPressed)
        # A click anywhere on a row flips it, except on the count, which is
        # typed into.
        tables.check_rows(
            self.apply_table,
            lambda row: (self.apply_rows[row]["checkbox"]
                         if row < len(self.apply_rows) else None),
            typed_columns=(4,))
        tables.keep_rows(self.apply_table, APPLY_MIN_ROWS)
        self.apply_table.installEventFilter(self)
        layout.addWidget(self.apply_table, 1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        abort = QPushButton("Discard import")
        abort.setToolTip("Throw this scan away. Nothing has been written yet.")
        abort.clicked.connect(self._abort_import)
        footer.addWidget(abort)
        footer.addStretch(1)
        write_btn = QPushButton("Write to database")
        write_btn.setObjectName("Primary")
        write_btn.clicked.connect(self._write_apply_table)
        footer.addWidget(write_btn)
        outer.addLayout(footer)

        self.apply_rows: list[dict] = []
        return page

    def _build_apply_table(self, unresolved: list[int]):
        # Rebuilt from scratch: this runs again whenever the review changes,
        # and appending to the note would stack the same warning up each time.
        self.apply_note.setText(self._APPLY_NOTE)

        combined: dict[tuple[str, str], dict] = {}
        unresolved_manual: list[str] = []

        self._thumbnail_sources = {}

        def settled(kind: str, unique_name: str) -> str:
            """The kind a reading is written under.

            A tile that reads as a weapon which is also a component of another
            item (Lex Prime for Aklex Prime) is a built copy: it goes to
            `owned_items`, not `owned_parts`, however the scan matched it.
            """
            if kind == "part" and dbq.is_built_component(self.conn, unique_name):
                return "item"
            return kind

        def bump(kind: str, unique_name: str, name: str, amount: int = 1,
                 source_candidate=None):
            kind = settled(kind, unique_name)
            key = (kind, unique_name)
            entry = combined.setdefault(key, {"name": name, "held": 0,
                                              "reading": "", "score": None})
            entry["held"] += amount
            # The text OCR actually read, kept for the journal. Without it the
            # history says a number changed and cannot say what the tool
            # thought it was looking at - which is exactly the question asked
            # about a number that turns out wrong.
            if source_candidate is not None and not entry["reading"]:
                entry["reading"] = getattr(source_candidate, "reading", "") or ""
                entry["score"] = getattr(source_candidate, "score", None)
            # The tile a picture can be cut from later. The first one with a
            # crop wins; they are all the same artwork.
            if (source_candidate is not None
                    and getattr(source_candidate, "evidence", None)
                    and key not in self._thumbnail_sources):
                self._thumbnail_sources[key] = source_candidate

        def held(candidate) -> int:
            """How many this tile represents.

            A tile is one of the item unless its badge says otherwise, and only
            stackable items carry a badge - so 25 relics are one tile reading
            25, while 25 Warframes would be 25 tiles with no badge at all.
            """
            return candidate.quantity if candidate.quantity is not None else 1

        # Mastery, from the rank on the tile. Only from confident matches: the
        # flag latches and a scan never clears it, so a wrong one would mark the
        # wrong item for good. A rank on a tile the reviewer is still unsure
        # about is not evidence enough for something permanent.
        mastered_now: dict[str, str] = {}
        for candidate in self.scan_result.confident:
            rank = getattr(candidate, "rank", None)
            if (candidate.item_unique and rank is not None
                    and rank >= dbq.MASTERY_RANK
                    and not dbq.is_mastered(self.conn, candidate.item_unique)):
                mastered_now[candidate.item_unique] = candidate.name

        # A confident candidate is counted here *unless* the user asked to step
        # through the accepted ones too, in which case its cluster answers for
        # it below. Counting it in both places doubled every tile the user
        # confirmed, so the more carefully they checked, the more wrong the
        # numbers became — and a correction added the right item without ever
        # removing the wrong one.
        reviewed = {id(member)
                    for cluster in self.clusters if cluster.accepted
                    for member in cluster.candidates}

        unticked = {name for name, box in getattr(self, "auto_boxes", {}).items()
                    if not box.isChecked()}
        for candidate in self.scan_result.confident:
            if id(candidate) in reviewed:
                continue
            # Unticked in the accepted list: the reading was looked at and
            # rejected. Dropped rather than asked about again.
            if candidate.display_name in unticked or candidate.name in unticked:
                continue
            if candidate.part_unique:
                bump("part", candidate.part_unique, candidate.name,
                     held(candidate), candidate)
            elif candidate.item_unique:
                bump("item", candidate.item_unique, candidate.name,
                     held(candidate), candidate)
            elif getattr(candidate, "relic_unique", None):
                bump("relic", candidate.relic_unique, candidate.display_name,
                     held(candidate), candidate)

        for index, cluster in enumerate(self.clusters):
            if index in unresolved:
                # Unvisited. For a question that means "no answer yet" and the
                # row is left out; for an accepted reading it means "not
                # looked at", and what the scan decided still stands.
                if cluster.accepted:
                    for member in cluster.candidates:
                        if member.part_unique:
                            bump("part", member.part_unique, member.name,
                                 held(member), member)
                        elif member.item_unique:
                            bump("item", member.item_unique, member.name,
                                 held(member), member)
                        elif getattr(member, "relic_unique", None):
                            bump("relic", member.relic_unique,
                                 member.display_name, held(member), member)
                continue
            decision = self._open_question(index).decision()
            if decision is None:
                # Dismissed. Remember the reading so the next scan does not ask
                # again - the same tile produces much the same mangled text
                # every time, and answering it once should be enough.
                # Never learn a repeat. Ignoring one means "this position is a
                # duplicate", not "this text is untracked" - and the text is a
                # real item name. Learning "Ash" would quietly discard a
                # damaged but genuine Ash reading on a later scan.
                if (self.review_widgets[index].was_ignored()
                        and not cluster.is_repeat and not cluster.accepted):
                    dbq.learn_ignore(self.conn, cluster.reading)
                continue
            part_unique, item_unique = decision[0], decision[1]
            name = decision[-1]
            if part_unique is None and item_unique is None:
                # Typed text, or a relic picked off the card: a relic option
                # carries no part or item, and its label already names the
                # refinement. Both are resolved by the call the card used to
                # show its tick, with the question's refinement as default,
                # so what was approved is what is written. A relic may be
                # named with its refinement, in full or short: "Meso B9 Relic
                # [Exceptional]", "neo c9 radi". That is not cosmetic,
                # refinement decides the drop odds and each one is its own
                # stack.
                found = self._resolver_for(cluster)(name)
                if found is not None:
                    bump(found.kind, found.unique_name, found.name,
                         self._cluster_count(index), cluster.candidates[0])
                    continue
                # Typed, but matching nothing. Collected rather than dropped:
                # typing it was work that should not be lost, or the item would
                # be missing later with no idea why.
                unresolved_manual.append(name)
                continue
            if part_unique:
                kind, unique = "part", part_unique
            else:
                kind, unique = "item", item_unique
            bump(kind, unique, name, self._cluster_count(index),
                 cluster.candidates[0])

        if unresolved_manual:
            QMessageBox.warning(
                self, "Some typed names matched nothing",
                "These were typed by hand but do not match anything in the "
                "database, so they cannot be written:\n\n  "
                + "\n  ".join(unresolved_manual)
                + "\n\nGo back and correct them, or leave them — everything "
                  "else on this page is unaffected.")

        # What the reviewer typed into a row wins over what the scan counted.
        # The count field sits beside the tile on the review page because that
        # is where someone is looking at the picture; carrying the answer this
        # far is the whole point of putting it there.
        for name, row_widget in getattr(self, "accepted_rows", {}).items():
            entry = next((e for e in self.rows
                          if e["kind"] == "accepted" and e["name"] == name), None)
            if entry is None or not row_widget.keeping():
                continue
            member = entry["members"][0]
            if member.part_unique:
                key = (settled("part", member.part_unique), member.part_unique)
            elif getattr(member, "relic_unique", None):
                key = ("relic", member.relic_unique)
            elif member.item_unique:
                key = ("item", member.item_unique)
            else:
                continue
            if key not in combined or not row_widget.edited():
                # Untouched means agreement, not an instruction. Writing the
                # spinner's value regardless overwrote whatever else had
                # resolved to the same thing - a question cluster answering
                # onto the same part had its tiles silently dropped by a row
                # the user never touched.
                continue
            # Applied as a difference, so other contributions to the same thing
            # survive. The correction is about the tiles in this row.
            adjusted = (combined[key]["held"] - row_widget.scanned()
                        + row_widget.counted())
            combined[key]["held"] = max(0, adjusted)

        self.apply_rows = []
        # Rows as tall as the other tables' rows. Left at Qt's default of 30px,
        # the theme's cell padding grew with the text and squeezed what was
        # left: at 20pt the check box had 6 pixels of height and the count's
        # editor about as much.
        tables.fit_check_rows(self.apply_table)
        tables.keep_rows(self.apply_table, APPLY_MIN_ROWS)
        # The rows that write 0 or less than is recorded come first:
        # the user checks those and nothing else, and they used to sit under
        # up to two hundred rows. The order is the order of `apply_rows`,
        # which the write reads by row index, so each block below appends to
        # both in the same sequence.
        consumed = self._built_rows(combined)
        if consumed:
            self.apply_note.setText(
                self.apply_note.text() +
                f"\n{len(consumed)} piece(s) went into the foundry: this "
                f"capture shows finished items the last one did not, and a "
                f"build consumes its recipe. Uncheck any you had a spare of.")
        absent = self._absent_rows(combined)
        self.apply_table.setRowCount(len(absent) + len(consumed)
                                     + len(mastered_now) + len(combined))
        row = 0
        # Rows the previous capture recorded and this one never saw. A capture
        # is a photo of the whole inventory, so something absent from it has
        # been sold, traded or built - but the tool used to leave those numbers
        # standing forever, which made the page's own promise ("a photo of the
        # current state, not an addition to it") untrue.
        #
        # Off by default all the same. Absence is weaker evidence than
        # presence: a category not captured this time, a scan cut short, a
        # scroll that missed a shelf all look exactly like a sale.
        if absent:
            for kind, unique_name, name, current, source in absent:
                checkbox = QCheckBox()
                checkbox.setChecked(False)
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(theme.px(0.5), 0, 0, 0)
                box.addWidget(checkbox)
                box.addStretch(1)
                self.apply_table.setCellWidget(row, 0, holder)
                self.apply_table.setItem(row, 1, QTableWidgetItem(
                    f"{kind}, gone?{self._GONE_SOURCE.get(source, '')}"))
                self.apply_table.setItem(row, 2, QTableWidgetItem(name))
                self.apply_table.setItem(row, 3, QTableWidgetItem(str(current)))
                zero = QTableWidgetItem("0")
                zero.setForeground(QColor(theme.WARN))
                self.apply_table.setItem(row, 4, zero)
                self.apply_rows.append({
                    "kind": kind, "unique_name": unique_name,
                    "checkbox": checkbox, "held": 0,
                })
                row += 1

        # Pieces the foundry ate. A finished item the arsenal did not have
        # before is an event the capture witnessed, not a silence it left
        # behind, so these are checked and they skip the coverage rules
        # below: whether the components tab was photographed has no bearing
        # on what a build consumed.
        if consumed:
            for kind, unique_name, name, current, after in consumed:
                checkbox = QCheckBox()
                checkbox.setChecked(True)
                holder = QWidget()
                box = QHBoxLayout(holder)
                box.setContentsMargins(theme.px(0.5), 0, 0, 0)
                box.addWidget(checkbox)
                box.addStretch(1)
                self.apply_table.setCellWidget(row, 0, holder)
                self.apply_table.setItem(
                    row, 1, QTableWidgetItem(f"{kind}, built"))
                self.apply_table.setItem(row, 2, QTableWidgetItem(name))
                self.apply_table.setItem(row, 3, QTableWidgetItem(str(current)))
                taken = QTableWidgetItem(str(after))
                taken.setForeground(QColor(theme.WARN))
                self.apply_table.setItem(row, 4, taken)
                self.apply_rows.append({
                    "kind": kind, "unique_name": unique_name,
                    "checkbox": checkbox, "held": after,
                })
                row += 1

        # Mastery rows, as their own kind. Shown rather than applied
        # quietly because the flag cannot be un-set by a later scan: if a
        # misread put one here, this is the only place to catch it.
        for unique_name, name in sorted(
                mastered_now.items(), key=lambda kv: kv[1]):
            checkbox = QCheckBox()
            checkbox.setChecked(True)
            holder = QWidget()
            box = QHBoxLayout(holder)
            box.setContentsMargins(theme.px(0.5), 0, 0, 0)
            box.addWidget(checkbox)
            box.addStretch(1)
            self.apply_table.setCellWidget(row, 0, holder)
            self.apply_table.setItem(row, 1, QTableWidgetItem("mastery"))
            self.apply_table.setItem(row, 2, QTableWidgetItem(name))
            self.apply_table.setItem(row, 3, QTableWidgetItem("not mastered"))
            reached = QTableWidgetItem("rank 30 seen")
            reached.setForeground(QColor(theme.GOOD))
            self.apply_table.setItem(row, 4, reached)
            self.apply_rows.append({
                "kind": "mastery", "unique_name": unique_name,
                "checkbox": checkbox, "held": 1,
            })
            row += 1

        for (kind, unique_name), data in sorted(
                combined.items(), key=lambda kv: kv[1]["name"]):
            if kind == "relic":
                current = dbq.relic_count(self.conn, unique_name)
            elif kind == "part":
                current = dbq.owned_count(self.conn, unique_name)
            else:
                current = dbq.item_built(self.conn, unique_name)

            checkbox = QCheckBox()
            checkbox.setChecked(True)
            self.apply_table.setCellWidget(row, 0, checkbox)
            self.apply_table.setItem(row, 1, QTableWidgetItem(kind))
            self.apply_table.setItem(row, 2, QTableWidgetItem(data["name"]))
            recorded = QTableWidgetItem(str(current))
            recorded.setFlags(recorded.flags() & ~Qt.ItemIsEditable)
            self.apply_table.setItem(row, 3, recorded)
            seen = QTableWidgetItem(str(data["held"]))
            seen.setToolTip("Type a different number if the scan miscounted.")
            self.apply_table.setItem(row, 4, seen)
            self.apply_rows.append({
                "kind": kind, "unique_name": unique_name,
                "checkbox": checkbox, "held": data["held"],
                "reading": data.get("reading", ""), "score": data.get("score"),
            })
            row += 1
        # Only the count is typed into. Everything else was editable by
        # accident - typed-over names went nowhere - and with a single click
        # opening an editor it would have opened on every name clicked.
        for row in range(self.apply_table.rowCount()):
            for column in range(1, self.apply_table.columnCount()):
                cell = self.apply_table.item(row, column)
                if cell is None:
                    continue
                editable = column == 4 and cell.text().strip().isdigit()
                flags = cell.flags()
                cell.setFlags(flags | Qt.ItemIsEditable if editable
                              else flags & ~Qt.ItemIsEditable)

        lowered = len(absent) + len(consumed)
        if lowered:
            self.apply_note.setText(
                f"{lowered} row(s) at the top would write 0 or less than is "
                f"recorded ({len(absent)} gone?, {len(consumed)} built). "
                f"Look at those first.\n" + self.apply_note.text())
        if self.apply_table.rowCount():
            # Up/Down and Enter work from the first row without a click. The
            # name column: the count column opens an editor when it becomes
            # the current cell.
            self.apply_table.setCurrentCell(0, 2)

    #: Shown under the apply table, and the base the absent-row warnings are
    #: appended to.
    _APPLY_NOTE = (
        "Rows marked \"gone?\" were recorded before and are absent from this "
        "capture. They are proposed as 0 and left unchecked: absence is weaker "
        "evidence than presence, since a category you did not capture looks "
        "exactly like one you sold. A part being built in the foundry is "
        "missing from every capture too: if you know it is there, leave the "
        "row unchecked. Rows from your own hand entries or relic picks are "
        "marked.")

    #: What the kind column adds to "gone?" when the number did not come from a
    #: capture.
    _GONE_SOURCE = {"manual": " (by hand)", "pick": " (picked)",
                    "test": " (test)", "profile": " (profile)"}

    #: Above this share of built items (or relics) missing from a capture, the
    #: absent rows are not listed. Parts have no limit. Half a category gone is not half a category sold - it is
    #: a capture that did not cover it, and a hundred unchecked rows saying
    #: "gone?" is a page nobody reads rather than a question anybody answers.
    ABSENT_SHARE_LIMIT = 0.30

    #: How many tiles of a category a capture must show before its silence
    #: about the rest of that category counts as evidence. One melee weapon
    #: caught at the edge of a Warframes recording does not mean the melee tab
    #: was covered.
    COVERAGE_MINIMUM = 3

    @staticmethod
    def _group_label(kind, category):
        """What one inventory tab shows, in words: parts, built items, relics."""
        if kind == "relic":
            return "relics"
        if kind == "part":
            return f"{category} parts"
        return f"built {category} items"

    def _absent_rows(self, combined):
        """What the previous capture held and this one did not see.

        By kind AND category, and parts without a share limit. Category
        alone mixed the built items of a category with its parts: a Prime Parts
        recording can never show a built Warframe, so those always counted as
        absent and pushed the share over the limit, and no part was ever
        offered. Kind alone asked whether a capture had seen "a part" and then
        treated its silence about every other part as evidence: a Warframes
        recording proposed zeroing weapon components. A group is what one
        inventory tab can show: the parts of one category, the built items of
        one category, the relics.
        """
        previous = dbq.recorded_with_source(self.conn)
        held_entries = [(kind, unique)
                        for kind, held in previous.items() for unique in held]
        categories = dbq.categories_of(
            self.conn, list(combined.keys()) + held_entries)

        def group_of(kind, unique):
            return (kind, categories.get((kind, unique), "unknown"))

        seen_counts: dict[tuple, int] = defaultdict(int)
        for kind, unique in combined:
            seen_counts[group_of(kind, unique)] += 1
        covered = {group for group, count in seen_counts.items()
                   if count >= self.COVERAGE_MINIMUM}

        skipped = sorted(self._group_label(*group)
                         for group, count in seen_counts.items()
                         if group not in covered and group[1] != "unknown")
        if skipped:
            self.apply_note.setText(
                self.apply_note.text() +
                f"\nToo few tiles to judge: {', '.join(skipped)}. Nothing "
                f"there is proposed as gone.")

        by_group: dict[tuple, list] = defaultdict(list)
        for kind, held in previous.items():
            for unique, (count, source) in held.items():
                by_group[group_of(kind, unique)].append(
                    (kind, unique, count, source))

        rows = []
        for group in sorted(covered):
            # A recording never shows a beast, so its silence says
            # nothing about one.
            if group[1] in dbq.NOT_IN_RECORDINGS:
                continue
            entries = by_group.get(group, [])
            if not entries:
                continue
            missing = [(kind, unique, count, source)
                       for kind, unique, count, source in entries
                       if (kind, unique) not in combined]
            if not missing:
                continue
            share = len(missing) / len(entries)
            if group[0] == "part":
                # No limit for parts: after a big sale the share of
                # absent parts is highest exactly when the rows matter most.
                # They stay unticked; the note covers a recording that
                # stopped early.
                if share > self.ABSENT_SHARE_LIMIT:
                    self.apply_note.setText(
                        self.apply_note.text() +
                        f"\n{len(missing)} of {len(entries)} recorded "
                        f"{self._group_label(*group)} are absent from this "
                        f"capture. If the recording did not reach the end of "
                        f"the tab, leave them unticked.")
            elif share > self.ABSENT_SHARE_LIMIT:
                self.apply_note.setText(
                    self.apply_note.text() +
                    f"\n{len(missing)} of {len(entries)} recorded "
                    f"{self._group_label(*group)} ({share:.0%}) are absent "
                    f"from this capture — too many to be sales, so they are "
                    f"left alone. Capture that tab again if they really are "
                    f"gone.")
                continue
            for kind, unique, count, source in missing:
                rows.append((kind, unique,
                             dbq.name_for(self.conn, kind, unique), count, source))
        rows.sort(key=lambda r: (r[0], r[2]))
        return rows

    def _built_rows(self, combined):
        """Pieces consumed by items this capture found finished.

        A player built Vadarya Prime, levelled it and imported. The scan read
        the weapon off the arsenal and recorded it; its four pieces stayed on
        the shelf where the import before had put them, and the plan went on
        offering a complete set that no longer existed.

        `_absent_rows` cannot reach that case and should not have to. Two of
        those four pieces carried a source it does not look at, it proposes
        its rows unchecked, and its whole subject is silence. This one's
        subject is a copy that appeared, which is the game telling us what it
        took: a build is a trade, and the tool only ever wrote down the half
        of it that arrived.

        A piece the capture photographed for itself is left alone. What is on
        the shelf beats what a recipe says should have come off it - that is
        how a second set survives a first one being built.
        """
        proposals: dict[tuple[str, str], tuple] = {}
        for (kind, unique_name), data in combined.items():
            if kind == "item":
                made = int(data["held"]) - dbq.item_built(self.conn, unique_name)
            else:
                continue
            if made <= 0:
                continue
            for piece in dbq.pieces_of_one_set(
                    self.conn, unique_name, made):
                key = (piece[0], piece[1])
                if key in combined:
                    continue
                # Two builds can want the same piece. The deeper cut wins:
                # both took from the one shelf.
                standing = proposals.get(key)
                if standing is None or piece[4] < standing[4]:
                    proposals[key] = piece
        rows = list(proposals.values())
        rows.sort(key=lambda row: (row[0], row[2]))
        return rows

    def _thumbnail_for(self, kind: str, unique_name: str) -> bytes | None:
        """A small picture of this thing, cut from the tile that named it.

        The scan already holds a crop of every tile - it is what the review
        shows - and the import was letting it go. Kept, it turns every list in
        the application from text into something recognisable at a glance,
        with nothing fetched from anywhere: the artwork is the artwork the
        player's own game drew.
        """
        import cv2
        import numpy as np

        candidate = self._thumbnail_sources.get((kind, unique_name))
        if candidate is None or not candidate.evidence:
            return None
        image = cv2.imdecode(np.frombuffer(candidate.evidence, np.uint8),
                             cv2.IMREAD_COLOR)
        if image is None:
            return None
        # The artwork alone, as a square. Not the name band under it - the name
        # is already the row beside the picture - and not the stack badge above
        # it, which belongs to the count and not to the thing. Square because a
        # list of pictures at four different shapes is a list that looks
        # broken; the artwork sits centred in its tile, so a centred square is
        # the artwork.
        height, width = image.shape[:2]
        # Cut above the name, found rather than assumed. A fixed fraction
        # worked for one-line names and left "Acceltra Prim" in the picture
        # for names that wrap to two - and the anchor that finds the name is
        # the one the scanner already uses to place the tile.
        from wft.inventory import quantity as quantity_mod

        name_at = quantity_mod.name_bottom(image)
        bottom = int(height * 0.70)
        if name_at is not None:
            # The name's own height is about a tenth of the tile; a wrapped one
            # is two of those. Back off from the bottom of the text by both.
            bottom = min(bottom, max(int(height * 0.35),
                                     name_at - int(height * 0.22)))
        top = int(height * 0.13)
        artwork = image[top:bottom, :]
        if artwork.size == 0:
            artwork = image
        side = min(artwork.shape[0], artwork.shape[1])
        y0 = (artwork.shape[0] - side) // 2
        x0 = (artwork.shape[1] - side) // 2
        artwork = artwork[y0:y0 + side, x0:x0 + side]
        scale = dbq.THUMBNAIL_WIDTH / max(artwork.shape[1], 1)
        if scale < 1.0:
            artwork = cv2.resize(artwork, None, fx=scale, fy=scale,
                                 interpolation=cv2.INTER_AREA)
        ok, buffer = cv2.imencode(".jpg", artwork,
                                  [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return buffer.tobytes() if ok else None

    def _write_apply_table(self):
        from wft.inventory import scan as scan_mod

        result = self.scan_result
        journal = dbq.start_import(
            self.conn, source=self.video_path or "", kind="scan",
            scan_revision=scan_mod.SCAN_REVISION,
            tiles=len(result.candidates) if result else None,
            questions=len(self.clusters))

        written = 0
        for index, row in enumerate(self.apply_rows):
            if not row["checkbox"].isChecked():
                continue
            # What is in the cell wins over what the scan counted: the user
            # looked at the tile and the tool did not.
            cell = self.apply_table.item(index, 4)
            if cell is not None:
                try:
                    row = {**row, "held": max(0, int(cell.text().strip()))}
                except ValueError:
                    pass

            kind, unique_name = row["kind"], row["unique_name"]
            # Read what stands there before writing over it. This is the whole
            # journal: without the previous number, a wrong one is invisible
            # the moment it lands and there is nothing to put back.
            # The source the row carries now, read before it is overwritten:
            # taking this import back has to restore the stamp as well as the
            # number, because the next import reads the stamp to work out what
            # a previous recording covered.
            before_source = dbq.source_of(self.conn, kind, unique_name)
            if kind == "mastery":
                before = 1 if dbq.is_mastered(self.conn, unique_name) else 0
                dbq.set_item_mastered(self.conn, unique_name)
                after = 1
            elif kind == "relic":
                before = dbq.relic_count(self.conn, unique_name)
                dbq.set_relic_count(self.conn, unique_name, row["held"], "video")
                after = row["held"]
            elif kind == "part":
                before = dbq.owned_count(self.conn, unique_name)
                dbq.set_part_count(self.conn, unique_name, row["held"], "video")
                after = row["held"]
            else:
                before = dbq.item_built(self.conn, unique_name)
                dbq.set_item_count(self.conn, unique_name, row["held"], "video")
                after = row["held"]
            dbq.record_observation(self.conn, journal, kind, unique_name,
                                   before, after,
                                   reading=row.get("reading", ""),
                                   score=row.get("score"),
                                   before_source=before_source)
            picture = self._thumbnail_for(kind, unique_name)
            if picture:
                dbq.set_thumbnail(self.conn, kind, unique_name, picture)
            written += 1
        self.conn.commit()

        # Record which version of the reader produced these numbers, so a later
        # correction can say that what is stored predates it.
        dbq.set_scan_revision(self.conn, scan_mod.SCAN_REVISION)
        moved = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE import_id = ? "
            "AND COALESCE(before, 0) <> COALESCE(after, 0)",
            (journal,)).fetchone()[0]
        QMessageBox.information(
            self, "Import applied",
            f"Wrote {written} record(s), {moved} of them a change.\n\n"
            f"Recorded as import #{journal}. The Import page lists it with "
            f"what it moved, and can take it back.")
        if hasattr(self, "history"):
            self.history.refresh()
        if hasattr(self, "pictures"):
            self.pictures.refresh()
            # Only here: an applied recording is what makes pictures necessary.
            # Not the profile import, not taking an import back.
            self.pictures.after_import()
        self.imports_changed.emit()
        self._reset()

    def _paint(self, widget, style) -> None:
        """Style `widget` with `style()` and remember how, for `refresh`.

        A colour taken from `theme` is the colour of the theme active at that
        moment. The other pages rebuild themselves in `refresh`; this one holds
        a scan, a review and an unapplied import that a rebuild would throw
        away, so it keeps the recipe and only paints again.
        """
        self._painted[widget] = style
        widget.setStyleSheet(style())

    def refresh(self):
        """The theme's current colours, and the history and pictures as stored.

        Called on every visit, after a theme change and after the relic panel
        wrote something. It had none, so a theme change left this page in the
        old colours and the history went on listing deleted imports.
        """
        for widget, style in list(self._painted.items()):
            try:
                widget.setStyleSheet(style())
            except RuntimeError:
                # The widget is gone (a page part built anew); so is the need.
                del self._painted[widget]
        if hasattr(self, "history"):
            self.history.refresh()
        if hasattr(self, "pictures"):
            self.pictures.refresh()

    def _reset(self):
        """Go back to the start and let go of what the scan was holding.

        Not only tidiness. A finished import leaves a list of two hundred
        widgets, each holding a tile crop, and every one of them holds a
        reference into the frames the scan kept - about twenty megabytes of
        JPEG that exist to draw a red box on a picture nobody is looking at any
        more. Measured across four reviews in one session it plateaus rather
        than growing without bound, but plateauing at the cost of a whole
        import is not the same as releasing it.
        """
        self.video_path = None
        self.path_label.setText("No file selected.")
        self._paint(self.path_label, lambda: f"color: {theme.TEXT_DIM};")
        self.start_btn.setEnabled(False)
        self.scan_result = None
        self.review_list.clear()
        self.rows = []
        self.review_widgets = []
        self.clusters = []
        self.auto_boxes = {}
        self.accepted_rows = {}
        self.visited = set()
        self.stack.setCurrentIndex(PAGE_SELECT)
        self._show_last_import()
        self._check_catalogue()
