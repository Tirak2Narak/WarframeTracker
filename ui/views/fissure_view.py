"""What can be opened right now, and which of it is worth opening.

Two halves of one question, and they were on different pages. The shelf knew
that twelve Lith O4 were the best thing to open; the game knew whether any Lith
fissure was running. Only the player held both, and only by alt-tabbing.

The openings table moved here from the plan page rather than being copied. That
page had grown six sections and was answering two different questions — what my
collection still needs, which is planning, and what to do in the next ten
minutes, which is playing. The sidebar already draws that line; this puts the
table on the right side of it.

Era chips filter both tables at once. That is the whole point of the page: pick
Lith and you see the Lith fissures that are open and the Lith relics worth
taking into them, or you see that there are none and pick something else.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ui import theme
from ui.widgets.filter_chips import ALL, FilterChips
from ui.widgets.section import Section
from ui.widgets.tables import (
    Sort, fit_height, order_by, simple_table as _table, sortable,
)
from ui.workers.task import TaskWorker
from wft.data import db as dbq
from wft.world import fissures as fissures_mod

#: Eras in the order the game lists them. Omnia is deliberately absent: it is
#: not an era of relics, it is a fissure that accepts any of them.
ERA_ORDER = ("Lith", "Meso", "Neo", "Axi", "Requiem")

#: Above this many rows the openings table gets its own scrollbar. A shelf of
#: two hundred relics must not push the board off the screen. Rows rather than
#: the 360px this was: 360px is eight rows at 11pt and five at 20pt, and the
#: table is read by its rows.
OPENINGS_TABLE_ROWS = 8

#: How often the "ends in" column is recomputed. Only while the page is on
#: screen - a timer ticking behind a page nobody is looking at is a timer that
#: exists to spend battery.
TICK_MS = 60_000

#: How long an errored or empty board waits before being asked again. Their
#: edge answers from a two-minute cache, so asking faster than this cannot
#: produce a different answer - it can only spend one of their requests on
#: the same bytes.
RETRY_SECONDS = fissures_mod.CACHE_SECONDS


#: Fetches still in flight, held here rather than on the page.
#:
#: A QThread parented to a widget is destroyed with it, and destroying a
#: running QThread aborts the process - Qt says so and then calls abort().
#: Closing the application, or any test that builds this page and throws it
#: away, does exactly that while the request is still open. Held here, the
#: thread outlives the page it was started for; Qt drops the connections to a
#: deleted receiver on its own, so its answer simply goes nowhere.
_in_flight: set = set()


def wait_for_fetches(ms_each: int = 5000) -> None:
    """Let a fetch still in flight finish before the interpreter goes.

    Holding the workers in `_in_flight` saves them from dying with their page,
    but not from dying with the process: a QThread still running when Python
    tears down aborts just the same. The fetch now starts on the page's first
    visit rather than at construction, so an application closed shortly after
    opening Fissures - the self-test does exactly this - can reach exit with
    the request still open. Called from the quit path; five seconds a worker
    against a fifteen-second request timeout is enough for every fetch that
    is going to finish at all.
    """
    for worker in list(_in_flight):
        worker.wait(ms_each)


def _remaining(minutes: int) -> str:
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60:02d}"
    return f"{minutes} min"


class FissureView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        # What counts as quick is read from this config on every row.
        fissures_mod.use_config(cfg)
        self.conn = dbq.connect(cfg.db_path)
        dbq.assert_usable(self.conn)
        self.fissures: list = []
        self.error: str | None = None
        #: Whether the board has answered at all. Without it, an answer of
        #: "nothing is open" was displayed as "still loading" and the page sat
        #: there saying it forever.
        self.answered = False
        self._last_asked = 0.0
        self._worker = None
        self._board_rows: list = []
        #: None until the first refresh renders. The tick reads it, and the
        #: tick starts on show - a beat before `select` gets to refresh.
        self._board_table = None
        self._build_ui()

        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._retime)
        # No refresh here: `MainWindow.select` refreshes the page on the way
        # in, which is also when the board is first worth asking for - a fetch
        # at construction spent a request on a page nobody had opened yet.

    # -- plumbing ---------------------------------------------------------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.55))

        title = QLabel("Fissures")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        subtitle = QLabel(
            "What is open right now, and which relic on your shelf is worth "
            "taking into it.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.body = QVBoxLayout()
        self.body.setSpacing(theme.px(1.2))
        inner = QWidget()
        inner.setLayout(self.body)

        scroll = QScrollArea()
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll, 1)

    def _clear(self):
        """Take the old page apart, and take it apart *now*.

        `deleteLater` alone is not enough here, and this is the one page where
        that matters. Taking a widget out of a layout does not unparent it: it
        stays a child of the scroll area and stays painted where it was until
        the deferred delete runs. On every other page nothing rebuilds while
        somebody is looking, so nobody ever saw it.

        This page rebuilds when the board answers, a second or two after it is
        opened - under the player's hands. A click on "Neo" landed on a chip row
        that had already been replaced: the click reached `_apply_era`, which reads
        the *current* chips, found them saying "every era", and dutifully
        showed everything. The chip that was pressed stayed lit because it belonged
        to a widget nobody had told to go away yet.
        """
        while self.body.count():
            child = self.body.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def showEvent(self, event):  # noqa: N802 - Qt override
        super().showEvent(event)
        self._tick.start()

    def hideEvent(self, event):  # noqa: N802 - Qt override
        super().hideEvent(event)
        self._tick.stop()

    def refresh(self):
        """Rebuild the page, and ask the board for a fresh answer.

        The fetch is off the GUI thread and the page is built without waiting
        for it. Everything except the board is answerable from the database, so
        a slow or unreachable service must never be the reason this page is
        blank.
        """
        self._render()
        self._fetch()

    def _fetch(self, force: bool = False):
        if self._worker is not None and self._worker.isRunning():
            return
        self._last_asked = time.monotonic()
        worker = TaskWorker(lambda: fissures_mod.active(self.cfg, force=force))
        _in_flight.add(worker)
        worker.finished.connect(lambda w=worker: _in_flight.discard(w))
        worker.finished_ok.connect(self._got_board)
        worker.failed.connect(self._board_failed)
        self._worker = worker
        worker.start()

    def _got_board(self, fissures):
        self.fissures, self.error, self.answered = list(fissures), None, True
        self._render()

    def _board_failed(self, message: str):
        self.fissures, self.error, self.answered = [], str(message), True
        self._render()

    # -- rendering --------------------------------------------------------
    def _render(self):
        self._clear()
        self._era_chips = None
        self._board_chips = None
        self._board_rows = []
        self._board_table = None
        self._openings_table = None
        #: The order each table is in, kept on the page: the board is rebuilt
        #: every time the service answers, and an order living on the widget
        #: would be thrown away with it once a minute.
        self._order = {"board": {"column": 0, "descending": False},
                       "openings": {"column": 3, "descending": True}}
        self._openings_rows = []

        from wft.relic import openings as openings_mod

        ranked = [entry for entry in openings_mod.rank(self.conn)
                  if entry.expected > 0]

        # The chips come before either table because they belong to both. One
        # row of controls answering "which era am I playing" is the page.
        on_shelf = {entry.tier for entry in ranked}
        on_board = {f.tier for f in self.fissures}
        eras = [tier for tier in ERA_ORDER if tier in on_shelf or tier in on_board]
        if eras:
            self._era_chips = FilterChips([(tier, tier) for tier in eras],
                                          all_label="Every era")
            self._era_chips.changed.connect(self._apply_era)
            self.body.addWidget(self._era_chips)

        self._board_section()
        self._openings_section(ranked)
        self.body.addStretch(1)
        self._apply_era()

    def _board_section(self):
        section = Section("Open now", "", theme.ACCENT)
        self.body.addWidget(section)
        layout = section.body

        if self.error:
            note = QLabel(
                f"The fissure board could not be read: {self.error}\n\n"
                f"It will be asked again in a couple of minutes. Everything "
                f"else on this page comes from your own database and is "
                f"unaffected.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {theme.WARN};")
            layout.addWidget(note)
            section.toggle.setText("Open now — unavailable")
            self._add_refresh(layout)
            return
        if not self.fissures:
            if not self.answered:
                self._faint(layout, "Reading the board…")
                section.toggle.setText("Open now")
                return
            # An answer that contains nothing. Named rather than left looking
            # like a slow connection, because it is neither: the service has
            # been reached and it has nothing to say.
            self._faint(layout, "No fissure is open right now.")
            section.toggle.setText("Open now — nothing current")
            self._add_refresh(layout)
            return

        # The three lists the game shows separately. Kept apart because adding
        # them gives a number that matches nothing on screen: thirty-two
        # against the ten in front of the player.
        self._board_chips = FilterChips(
            [(kind, fissures_mod.KIND_LABELS[kind])
             for kind in fissures_mod.KINDS],
            all_label="All boards")
        self._board_chips.changed.connect(self._apply_era)
        layout.addWidget(self._board_chips)

        rows = list(self.fissures)
        table = _table(["Era", "Node", "Mission", "Board", "Ends in"],
                       len(rows), stretch=1)
        # Era first, by the game's own order rather than the alphabet: Lith
        # before Axi is what the relic screen says, and A-to-Z would put Axi
        # at the top of a list nobody reads that way.
        columns = {
            0: Sort(lambda f: f.tier_num),
            1: Sort(lambda f: f.node.casefold()),
            2: Sort(lambda f: f.mission.casefold()),
            3: Sort(lambda f: fissures_mod.KIND_LABELS[f.kind].casefold()),
            # Soonest first, because a fissure is a thing that runs out.
            4: Sort(lambda f: f.expiry),
        }
        self._board_table = table
        self._board_rows = rows

        def fill():
            order_by(rows, columns, self._order["board"],
                     tiebreak=lambda f: f.expiry)
            for index, fissure in enumerate(rows):
                era = QTableWidgetItem(fissure.tier)
                if fissure.tier == fissures_mod.ANY_ERA:
                    era.setForeground(QColor(theme.GOOD))
                    era.setToolTip("Takes a relic of any era")
                table.setItem(index, 0, era)
                table.setItem(index, 1, QTableWidgetItem(fissure.node))
                mission = QTableWidgetItem(fissure.mission)
                if fissure.is_quick:
                    mission.setForeground(QColor(theme.GOOD))
                    mission.setToolTip("Short run — ten reactant and out")
                table.setItem(index, 2, mission)
                board = QTableWidgetItem(fissures_mod.KIND_LABELS[fissure.kind])
                if fissure.kind != "normal":
                    board.setForeground(QColor(theme.TEXT_DIM))
                table.setItem(index, 3, board)
                table.setItem(index, 4, QTableWidgetItem(
                    _remaining(fissure.minutes_left())))
            # The chips hide rows by index, so what is hidden has to be worked
            # out again every time the rows move.
            self._apply_era()

        fill()
        sortable(table, columns, self._order["board"], fill)
        layout.addWidget(table)
        self._add_refresh(layout)

    def _add_refresh(self, layout):
        row = QHBoxLayout()
        button = QPushButton("Refresh the board")
        button.setToolTip(
            "The service caches for two minutes, so asking faster than that "
            "returns the same answer.")
        button.clicked.connect(lambda: self._fetch(force=True))
        row.addWidget(button)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        layout.addWidget(holder)

    def _faint(self, layout, text: str):
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        label.setWordWrap(True)
        layout.addWidget(label)

    def _openings_section(self, ranked):
        """Which relic to open next, and whether refining it is worth it.

        Moved here from the plan page unchanged. The one thing added is the
        shared era filter above it: the question "which of my Lith relics is
        worth opening" is only ever asked when a Lith fissure is open.
        """
        from wft.relic import openings as openings_mod

        section = Section(
            f"Open next — {len(ranked)} relic(s) can drop something you need",
            "Chance: how likely one opening gives you a part you still need. "
            "At Radiant: the same chance if the relic were refined to "
            "Radiant. Refining raises the chance of the rare part and lowers "
            "the common ones, so it helps only when what you need is the "
            "rare part.",
            theme.ACCENT)
        self.body.addWidget(section)
        self._openings_section_widget = section
        layout = section.body

        if not ranked:
            self._faint(layout, "No relic on the shelf drops anything missing. "
                                "Import your relics under Import if that looks "
                                "wrong.")
            return

        table = _table(["Relic", "Held", "Refinement", "Chance",
                        "At Radiant", "What you want from it"],
                       len(ranked), stretch=5, wrap=True, scroll_x=True)
        columns = {
            0: Sort(lambda e: e.name.casefold()),
            1: Sort(lambda e: e.held, descending_first=True),
            2: Sort(lambda e: e.refinement.casefold()),
            3: Sort(lambda e: e.expected, descending_first=True),
            4: Sort(lambda e: e.refined, descending_first=True),
            # The column reads as a list of parts, so it sorts as one: how
            # many of the six this relic drops are parts you still want.
            5: Sort(lambda e: len(e.wanted), descending_first=True),
        }
        self._openings_table = table
        self._openings_rows = ranked

        def fill():
            # The page's own answer to "what next": the best opening first,
            # vaulted ahead of farmable and the deeper stack ahead of the
            # single copy where two are worth the same.
            order_by(ranked, columns, self._order["openings"],
                     tiebreak=lambda e: (not e.vaulted, -e.held, e.name))
            for index, entry in enumerate(ranked):
                name = QTableWidgetItem(entry.name)
                if entry.vaulted:
                    name.setForeground(QColor(theme.VAULTED))
                    name.setToolTip("Vaulted — no more of these will drop")
                table.setItem(index, 0, name)
                table.setItem(index, 1, QTableWidgetItem(f"x{entry.held}"))
                table.setItem(index, 2, QTableWidgetItem(entry.refinement.title()))

                per = QTableWidgetItem(openings_mod.percent(entry.expected))
                per.setForeground(QColor(theme.GOOD if entry.expected >= 0.5
                                         else theme.TEXT))
                per.setToolTip("How likely one opening gives a part you "
                               "still need.")
                table.setItem(index, 3, per)

                # The same chance if the relic were Radiant: green when
                # refining raises it, dim when it does not. Refining trades
                # commons for rares, so a relic wanted for its commons gets
                # worse and one wanted for its rare gets better.
                refined = QTableWidgetItem(openings_mod.percent(entry.refined))
                refined.setForeground(QColor(
                    theme.GOOD if entry.refined > entry.expected + 1e-9
                    else theme.TEXT_DIM))
                tip = ("The chance of a part you still need if this relic "
                       "were refined to Radiant.")
                rare = next((w for w in entry.wanted if w[1] == "RARE"), None)
                if rare and entry.refinement.upper() != "RADIANT":
                    after = dbq.DROP_CHANCE["RADIANT"]["RARE"]
                    tip += (f"\n\nThe rare part you want goes from "
                            f"{rare[2]:.0%} to {after:.0%}.")
                refined.setToolTip(tip)
                table.setItem(index, 4, refined)

                wanted = ", ".join(
                    f"{part} ({rarity[:1]}{chance:.0%})"
                    for part, rarity, chance in entry.wanted[:3])
                if len(entry.wanted) > 3:
                    wanted += f"  +{len(entry.wanted) - 3} more"
                table.setItem(index, 5, QTableWidgetItem(wanted))
            self._apply_era()

        fill()
        sortable(table, columns, self._order["openings"], fill)

        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setToolTip("Click a relic to see everything it drops")
        table.cellClicked.connect(
            lambda row, _column: self._open_relic(ranked[row].name))
        layout.addWidget(table)
        # `simple_table` has already sized itself to its rows; this only caps
        # it, and the table keeps the cap when its wrapped rows change height.
        # Asking for `resizeRowsToContents` here instead blew the first row
        # up to the full height of the table - the stretch column has no width
        # yet at this point, so the text it measures is one very tall line.
        fit_height(table, max_rows=OPENINGS_TABLE_ROWS)

    def _open_relic(self, relic_name: str) -> None:
        from ui.widgets.relic_detail import RelicDetail

        RelicDetail(self.conn, relic_name, self).exec()

    # -- filtering --------------------------------------------------------
    def _apply_era(self, *_args):
        """One filter, both tables.

        Omnia rows survive every era filter, because an Omnia fissure takes
        whatever relic you bring. Hiding it under "Lith" would remove the one
        door that is open to a Lith relic when no Lith fissure is running.
        """
        eras = self._era_chips.active() if self._era_chips else {ALL}
        boards = self._board_chips.active() if self._board_chips else {ALL}

        shown_board = 0
        if self._board_table is not None:
            for row, fissure in enumerate(self._board_rows):
                keep = ((eras == {ALL} or fissure.tier in eras
                         or fissure.tier == fissures_mod.ANY_ERA)
                        and (boards == {ALL} or fissure.kind in boards))
                self._board_table.setRowHidden(row, not keep)
                shown_board += keep
            section = self._board_table.parent()
            while section is not None and not isinstance(section, Section):
                section = section.parent()
            if section is not None:
                section.toggle.setText(f"Open now — {shown_board} fissure(s)")

        if self._openings_table is not None:
            shown = 0
            for row, entry in enumerate(self._openings_rows):
                keep = eras == {ALL} or entry.tier in eras
                self._openings_table.setRowHidden(row, not keep)
                shown += keep
            total = len(self._openings_rows)
            self._openings_section_widget.toggle.setText(
                f"Open next — {shown} of {total} relic(s) can drop something "
                f"you need" if shown != total else
                f"Open next — {total} relic(s) can drop something you need")

    def _retime(self):
        """Recompute the countdown without asking the service again.

        The expiry is already known; only the clock moved. Refetching once a
        minute for a number this page can work out itself would be a request
        an hour for nothing.
        """
        if self._board_table is None:
            # No board on screen: the fetch errored, or it answered with
            # nothing. Neither state heals by being looked at - warframestat's
            # poller falling hours behind is a thing that has really happened,
            # and the page used to sit in its error note until somebody
            # pressed the button. Ask again on the service's own cache
            # cadence; `answered` keeps the very first fetch from doubling.
            if (self.answered
                    and time.monotonic() - self._last_asked >= RETRY_SECONDS):
                self._fetch()
            return
        expired = False
        for row, fissure in enumerate(self._board_rows):
            left = fissure.minutes_left()
            expired = expired or left <= 0
            item = self._board_table.item(row, 4)
            if item is not None:
                item.setText(_remaining(left))
        if expired:
            # One has ended, so the board on screen is now wrong about more
            # than the clock. Ask - the cache decides whether that costs a
            # request.
            self._fetch()
