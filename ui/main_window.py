"""The main window: sidebar navigation over the stacked views.

The Rewards page (the relic panel, `ui/views/relic_panel.py`) is one of the
stacked pages; it used to be a window of its own on a second monitor.
What still holds from that is that nothing here shows, raises or activates a
page by itself: only a keypress or a click switches it.
"""
from __future__ import annotations

import sys

from PySide6.QtGui import QIcon
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)


from ui import geometry as _geometry
from ui.widgets.ticker import TickerLabel


def _build_catalogue(cfg):
    """Build the reference tables, for a worker thread.

    A wrapper rather than `build` itself: `build`'s second parameter is called
    `report` and is a list it appends notes to, and the worker hands a
    `report` argument to anything that names one - which would be a signal
    where a list is expected.
    """
    from wft.data import build as build_mod

    notes: list[str] = []
    counts = build_mod.build(cfg, notes)
    return counts, notes


#: How narrow and how wide the navigation may get. The minimum is what the
#: longest page name needs when its label is allowed to shorten; the maximum is
#: what the application's own name needs at the largest text size.
SIDEBAR_MINIMUM = 140
SIDEBAR_MAXIMUM = 420


class NavList(QListWidget):
    """A navigation list that gaining focus does not turn into a choice.

    Both lists use it. The page on display can live in the other list (the
    main list and the footer with Settings), and then this one has no current
    row. A list with no current row picks its first when it gains focus - by
    Shift+Tab out of the footer, or by the window being activated again - and
    that row change would switch the page though nobody chose anything
    (Rewards from the main list, Settings from the footer). The page is only
    ever changed by a selection the user makes; focus alone leaves the rows
    as they were.
    """

    def focusInEvent(self, event) -> None:
        had_current = self.currentRow() >= 0
        self.blockSignals(True)
        try:
            super().focusInEvent(event)
            if not had_current:
                self.setCurrentRow(-1)
        finally:
            self.blockSignals(False)


class MainWindow(QMainWindow):
    # "Relic" was a page of status and a test button, which put diagnosis
    # where the user looked for a feature — the feature is the Rewards page.
    # The status now sits in Settings beside the capture settings it depends
    # on, and what a newcomer still has to do is on Setup.
    #: The navigation, grouped by when a page is used rather than by what kind
    #: of data it holds. Six flat entries said Settings was as important as the
    #: reason the application exists; these say what the tool is for first,
    #: what feeds it second, and leave the configuration where configuration
    #: belongs.
    NAV_GROUPS = [
        ("Playing", ["Rewards", "Fissures", "Plan", "Overlays"]),
        ("Collection", ["Collection", "Ducats", "Rivens", "Import"]),
        ("Trading", ["I want", "I offer"]),
    ]
    #: Pinned to the bottom, out of the groups. Setup is what a newcomer needs
    #: first and an old hand never again, Settings is the rest of the
    #: configuration; neither is something the application is for.
    NAV_FOOTER = ["Setup", "Settings"]
    NAV_ITEMS = [name for _group, names in NAV_GROUPS for name in names] + NAV_FOOTER

    def __init__(self, cfg, relic_panel=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.relic_panel = relic_panel
        # The panel writes to the same database from its own window, so the
        # page on display here goes stale the moment a reward is taken.
        if relic_panel is not None and hasattr(relic_panel, "recorded"):
            relic_panel.recorded.connect(self._panel_recorded)
        # "Not ready" on the Rewards page points at Setup with a button.
        if relic_panel is not None and hasattr(relic_panel, "setup_requested"):
            relic_panel.setup_requested.connect(lambda: self.select("Setup"))
        self.setWindowTitle("warframe-tracker")
        #: Whether moving and resizing are stored (`ui.geometry`). Off for the
        #: self-test, whose offscreen window would overwrite the user's.
        self.remember_geometry = True
        # Stored after the last move or resize, not only on close: the program
        # is often ended without a close event (logout, kill), and a close-only
        # save would lose the size exactly then. Until now it was never stored
        # at all - only read back - so every start was 1400x900.
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(500)
        self._geometry_timer.timeout.connect(self._save_geometry)
        # A stored geometry wins over the default: the user arranges the window
        # once and should not have to do it again on every start.
        if not _geometry.restore(self, "main"):
            self.resize(1400, 900)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.setCentralWidget(splitter)

        splitter.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        splitter.addWidget(self.stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 1180])

        self._pages: dict[str, QWidget] = {}
        # One permanent widget in place of `showMessage`: a line wider than the
        # window runs past as a ticker instead of being cut.
        self.ticker = TickerLabel()
        self.ticker.setContentsMargins(4, 0, 4, 0)
        self.statusBar().addWidget(self.ticker, 1)
        self._refresh_status()
        self._build_catalogue_if_missing()

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        # Width follows the text. It was pinned to 200-280px, which fitted the
        # title at the default font and cut it at every larger one - and the
        # text size is a setting that players change on a 4K monitor precisely
        # so things would be legible. Measured at 11pt: the title needs 197px
        # plus 32 of padding against a 200px sidebar.
        sidebar.setMinimumWidth(SIDEBAR_MINIMUM)
        sidebar.setMaximumWidth(SIDEBAR_MAXIMUM)
        self._sidebar = sidebar
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title = QLabel("warframe-tracker")
        title.setObjectName("SidebarTitle")
        subtitle = QLabel("relic & inventory helper")
        subtitle.setObjectName("SidebarSubtitle")
        # Wraps rather than widening the whole sidebar: it is the longest
        # string in there and the least important.
        subtitle.setWordWrap(True)
        self._sidebar_title = title
        layout.addWidget(title)
        layout.addWidget(subtitle)

        self.nav_list = NavList()
        self.nav_list.setObjectName("NavList")
        #: Which row holds which page. Group headings are rows too - they have
        #: to be, a QListWidget has no other kind - so the mapping cannot be
        #: the row number any more.
        self._nav_rows: dict[int, str] = {}
        for group, names in self.NAV_GROUPS:
            heading = QListWidgetItem(group.upper(), self.nav_list)
            heading.setFlags(Qt.NoItemFlags)
            heading.setData(Qt.UserRole, "heading")
            for name in names:
                QListWidgetItem(name, self.nav_list)
                self._nav_rows[self.nav_list.count() - 1] = name
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        self.nav_list.setIconSize(QSize(16, 16))
        self.nav_list.setTextElideMode(Qt.ElideRight)
        self.nav_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.nav_list, 1)

        # Setup and Settings, pinned under the stretch. A list of their own
        # rather than last entries: they are not among the things the
        # application is for, and putting them in the same run said they were.
        self.nav_footer = NavList()
        self.nav_footer.setObjectName("NavList")
        for name in self.NAV_FOOTER:
            QListWidgetItem(name, self.nav_footer)
        self.nav_footer.setIconSize(QSize(16, 16))
        self.nav_footer.setTextElideMode(Qt.ElideRight)
        self.nav_footer.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav_footer.setFixedHeight(
            self.nav_footer.sizeHintForRow(0) * len(self.NAV_FOOTER)
            + 2 * self.nav_footer.frameWidth() + 4)
        self.nav_footer.currentRowChanged.connect(self._on_footer_changed)
        layout.addWidget(self.nav_footer)

        return sidebar

    def resizeEvent(self, event):  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_sidebar()
        self._geometry_changed()

    def showEvent(self, event):  # noqa: N802 - Qt override
        """Fit the sidebar to its title once the stylesheet is in force.

        Font metrics taken in the constructor are the default font's: the
        theme is applied to the application after the window is built, so
        anything measured earlier is measured against the wrong type.
        """
        super().showEvent(event)
        self._fit_sidebar()

    def _fit_sidebar(self) -> None:
        title = getattr(self, "_sidebar_title", None)
        sidebar = getattr(self, "_sidebar", None)
        if title is None or sidebar is None:
            return
        # The padding in the stylesheet is 1.07em either side, plus the
        # splitter handle. Measured rather than derived: at 20pt a factor of
        # 2.2 left the last letter of the title clipped by four pixels.
        padding = int(title.fontMetrics().height() * 3.0)
        wanted = title.fontMetrics().horizontalAdvance(title.text()) + padding
        wanted = max(200, min(SIDEBAR_MAXIMUM, wanted))
        # Never more than a quarter of the window. Sized from the title alone
        # it took a third of an 800-pixel window - a navigation strip wider
        # than some of the pages it navigates to.
        if self.width() > 0:
            wanted = min(wanted, max(SIDEBAR_MINIMUM, self.width() // 4))
        if wanted == sidebar.width():
            return
        # Said once, both bounds the same. Negotiating with the splitter over a
        # minimum left the cap doing nothing on the way down and undershooting
        # on the way up; a width that is stated is a width that holds. The
        # lists are allowed to shorten their labels so a narrow window gets a
        # narrow navigation rather than a clipped one.
        sidebar.setMinimumWidth(wanted)
        sidebar.setMaximumWidth(wanted)
        splitter = self.centralWidget()
        if hasattr(splitter, "setSizes"):
            splitter.setSizes([wanted, max(1, self.width() - wanted)])

    def register_page(self, name: str, widget: QWidget) -> None:
        self._pages[name] = widget
        self.stack.addWidget(widget)
        # "Calibrate with this screen" on the Rewards page and on Setup: the
        # page asks, the window opens the dialog (a page never reaches for
        # another window itself).
        request = getattr(widget, "calibrate_requested", None)
        if request is not None:
            request.connect(self.open_calibration)
        # An import on the Import page: the status line says how old it is.
        imported = getattr(widget, "imports_changed", None)
        if imported is not None:
            imported.connect(self._refresh_status)

    def open_calibration(self) -> None:
        """The calibration dialog, with its test shown on the Rewards page."""
        from ui.views.calibrate_dialog import open_dialog

        open_dialog(self.cfg, self, self.relic_panel)

    def select(self, name: str) -> None:
        if name in self._pages:
            widget = self._pages[name]
            self.stack.setCurrentWidget(widget)
            # The one place pages are filled. Constructors build their layout
            # and leave it empty - filling there too meant the startup page
            # was surveyed twice - so this refresh is not just about staleness
            # after an import: it is the first population of every page, and
            # any future way of showing a page that bypasses select() must do
            # this itself.
            refresh = getattr(widget, "refresh", None)
            if callable(refresh):
                refresh()
            self._refresh_status()
            self._sync_navigation(name)

    def _panel_recorded(self) -> None:
        """Re-read the page on display after the relic panel wrote something."""
        widget = self.stack.currentWidget()
        # Not the panel itself: it is showing the answer it just recorded, and
        # rebuilding it would throw that away mid-fissure.
        if widget is not self.relic_panel:
            refresh = getattr(widget, "refresh", None)
            if callable(refresh):
                refresh()
        self._refresh_status()

    def _sync_navigation(self, name: str) -> None:
        """Show the current page as current, in whichever list holds it."""
        for widget in (self.nav_list, self.nav_footer):
            widget.blockSignals(True)
        try:
            in_main = name not in self.NAV_FOOTER
            if in_main:
                self.nav_footer.setCurrentRow(-1)
                for row, page in self._nav_rows.items():
                    if page == name:
                        self.nav_list.setCurrentRow(row)
                        break
            else:
                self.nav_list.setCurrentRow(-1)
                self.nav_footer.setCurrentRow(self.NAV_FOOTER.index(name))
        finally:
            for widget in (self.nav_list, self.nav_footer):
                widget.blockSignals(False)
        self._mark_current(name)

    def _on_nav_changed(self, row: int) -> None:
        name = self._nav_rows.get(row)
        if name:
            self.select(name)

    def _on_footer_changed(self, row: int) -> None:
        if 0 <= row < len(self.NAV_FOOTER):
            self.select(self.NAV_FOOTER[row])

    def _mark_current(self, name: str) -> None:
        """Put the diamond against the current page.

        The same mark heads every section on the pages themselves, so a reader
        who has learned it here already knows what it means there.
        """
        from ui import theme
        from ui.widgets.diamond import pixmap

        blank = QIcon()
        mark = QIcon(pixmap(theme.ACCENT))
        for row in range(self.nav_list.count()):
            page = self._nav_rows.get(row)
            self.nav_list.item(row).setIcon(
                mark if page and page == name else blank)
        for row in range(self.nav_footer.count()):
            self.nav_footer.item(row).setIcon(
                mark if self.NAV_FOOTER[row] == name else blank)

    def set_status(self, message: str) -> None:
        self.ticker.setText(message)

    def _build_catalogue_if_missing(self) -> None:
        """Rebuild the reference tables when the database has none.

        The catalogue is derived data - it is the manifests on disk turned
        into tables - so a missing one is a thing to fix, not a thing to ask
        about. Losing the database (a wipe, a fresh install, a copy that was
        never built) left every page empty and every scan matching nothing,
        and the only clue was "No database built yet" on the Settings page
        beside a green line saying the data was updated today. The question that
        followed was the right one: the tool was restarted, so why is anyone
        being asked to press a button.

        No network. This is the manifests already downloaded, read from disk.
        """
        from wft.data import db as dbq

        try:
            connection = dbq.connect(self.cfg.db_path)
        except Exception:  # noqa: BLE001 - startup must survive a bad file
            return
        try:
            empty = connection.execute(
                "SELECT (SELECT COUNT(*) FROM items) + "
                "(SELECT COUNT(*) FROM parts)").fetchone()[0] == 0
        except Exception:  # noqa: BLE001
            return
        finally:
            connection.close()
        if not empty:
            return

        manifests = sorted(self.cfg.manifest_dir.glob("Export*_en.json"))
        if not manifests:
            self.set_status("No catalogue and no manifests — open Setup "
                            "and press Download game data.")
            return

        from ui.workers.task import TaskWorker

        self.set_status(f"Building the catalogue from {len(manifests)} "
                        f"manifest(s) already on disk…")
        worker = TaskWorker(_build_catalogue, self.cfg)
        worker.finished_ok.connect(self._catalogue_built)
        worker.failed.connect(
            lambda message: self.set_status(
                f"Could not build the catalogue: {message} — open Setup "
                f"and press Download game data."))
        self._catalogue_worker = worker
        worker.start()

    def _catalogue_built(self, result) -> None:
        """Show the new data on the pages that are already open.

        Pages read the database when they are built, and some of them keep
        what they read, so a page built against an empty catalogue stays empty
        until it is told. `catalogue_changed` is that telling; `refresh` alone
        is not enough for a page holding a cached list of names.
        """
        counts, _notes = result
        for widget in self._pages.values():
            for hook in ("catalogue_changed", "refresh"):
                method = getattr(widget, hook, None)
                if callable(method):
                    try:
                        method()
                    except Exception as exc:  # noqa: BLE001
                        print(f"warning: {hook} failed: {exc}", file=sys.stderr)
                    break
        self._refresh_status()
        total = counts.get("items", 0) + counts.get("parts", 0)
        self.set_status(f"Catalogue built — {total} name(s) to match against. "
                        f"Vault status is not in it; Setup fetches that.")

    def _refresh_status(self) -> None:
        """Say what the database holds and how old its facts are.

        The bar said "Ready" from launch to quit, which is a decoration rather
        than a status: it was true before any data existed and stayed true
        after. What is worth a permanent line is what every page is built on -
        how much has been recorded, and whether the vault data behind the
        recommendations is still current.
        """
        try:
            from wft.data import db as dbq

            connection = dbq.connect(self.cfg.db_path)
        except Exception:  # noqa: BLE001 - a status line must not be fatal
            return
        try:
            summary = dbq.inventory_summary(connection)
            mastered, trackable = dbq.mastered_count(connection)
            age = dbq.vault_data_age(connection)
            # The same function the Trading pages print their dates from.
            from wft import trading

            stamps = trading.data_age(connection)
            stock_age = dbq.days_since(connection, stamps.stock_changed)
            mastery_age = dbq.days_since(connection, stamps.mastery_imported)
            relics = connection.execute(
                "SELECT COALESCE(SUM(count), 0) FROM owned_relics").fetchone()[0]
        except Exception:  # noqa: BLE001
            return
        finally:
            connection.close()

        # `inventory_summary` reports by source; the bar wants the total.
        parts = summary.get("part_total", 0)
        built = summary.get("item_total", 0)
        def phrase(days, noun: str, missing: str) -> str:
            if days is None:
                return missing
            return f"{noun} from today" if days <= 0 else f"{noun} {days} day(s) old"

        self.set_status(
            f"{parts} part(s) · {built} built · {relics} relic(s) · "
            f"{mastered} of {trackable} mastered · "
            f"{phrase(stock_age, 'inventory import', 'inventory never imported')} · "
            f"{phrase(mastery_age, 'mastery import', 'mastery import not recorded yet')} · "
            f"{phrase(age, 'vault status', 'vault status never fetched')}")

    def show_rewards(self) -> None:
        """Bring the rewards page forward, for a hotkey the user just pressed.

        Only ever called from the hotkey. The page does not decide it has
        something to say and jump in front of whatever is being read — that is
        the part of the old rule that still holds, and it holds because the
        switch is a response to a keypress rather than to a result arriving.
        """
        if "Rewards" in self.NAV_ITEMS:
            self.select("Rewards")

    def _save_geometry(self) -> None:
        if self.remember_geometry:
            _geometry.save(self, "main")

    def _geometry_changed(self) -> None:
        # Only a shown window: restoring and laying out before the first show
        # resize it too, and that is not the user arranging anything.
        timer = getattr(self, "_geometry_timer", None)
        if timer is not None and self.isVisible():
            timer.start()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().moveEvent(event)
        self._geometry_changed()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._geometry_timer.stop()
        self._save_geometry()
        super().closeEvent(event)
