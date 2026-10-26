"""Everything the account holds, in one place.

Inventory and Library answered the same question about the same rows. Library
listed every item with what was held of it and opened a detail showing the
pieces and the relics that drop them; Inventory listed the same items with the
same held-of-total and opened the same detail in a dialog. Two pages, one
question, and no way to tell from the names which one to open.

What each had that the other did not has gone where it belongs rather than
being carried along:

* Inventory's totals strip was already in the status bar, on every page.
* Its "ready to build" box is the Plan page's first answer.
* Its Duplicates tab is the Plan page's "Done with" section, which reads the
  same spares against the same recipes.
* Its Add / Manage tab is the only part with nowhere else to go, and it is a
  tab here.

Composed rather than rewritten. The two views are kept as hosts and their pages
are borrowed: the alternative was moving nine hundred lines to prove a point
about tidiness, and a merge that reimplements both halves is a merge that
breaks both halves.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget

from ui import theme
from ui.views.inventory_view import InventoryView
from ui.views.library_view import LibraryView


class CollectionView(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._library = LibraryView(cfg)
        self._inventory = InventoryView(cfg)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*theme.page_margins())
        outer.setSpacing(theme.px(0.4))

        title = QLabel("Collection")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)
        subtitle = QLabel("Everything recorded: what is held, what each set "
                          "needs, and what drops it.")
        subtitle.setObjectName("ViewSubtitle")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # Borrowed from their hosts. Reparenting is what addTab does; the host
        # keeps working because everything it does to these pages it does
        # through its own attributes, which still point at them.
        self.tabs.addTab(self._library.items_page, "Items")
        self.tabs.addTab(self._library.relics_page, "Relics")
        self.tabs.addTab(self._inventory.manage_page, "Add and correct")

    def refresh(self):
        self._library.refresh()
        self._inventory.refresh()
