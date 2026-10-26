"""The price lookup that the Ducats page and the Trading pages share.

One path to the market: the count on the button, the two-requests-a-second
throttle behind it, the Stop button and the note under it. A page uses it by
inheriting `MarketLookup` and providing `conn`, `refresh()` and the two
attributes set in `init_market_lookup`.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui import theme


class MarketLookup:
    """Methods for a QWidget page that offers "Look up platinum prices (n)"."""

    def init_market_lookup(self) -> None:
        #: The lookup button and its note, per section. Two sections ask the
        #: market on the Ducats page, and one pair of attributes for both meant
        #: the parts button drove the sets label.
        self._market_widgets: dict[str, tuple] = {}
        self._stop_fetch = False

    def _market_age_note(self) -> str:
        row = self.conn.execute(
            "SELECT MAX(fetched_at) AS newest FROM market_prices").fetchone()
        if row is None or not row["newest"]:
            return "warframe.market has not been asked yet."
        return f"Prices from warframe.market, last checked {row['newest']} UTC."

    def _unpriced(self, uniques: list[str]) -> list[str]:
        """The rows a lookup would actually spend a request on.

        The rule is `market.unpriced`, shared with the Trading lists so the
        two buttons count the same way.
        """
        from wft.market import client as market

        return market.unpriced(self.conn, uniques)

    def _market_row(self, uniques: list[str], key: str) -> QWidget:
        """The lookup button and what it will cost, for one section.

        Never fetches on opening the page: prices are somebody else's server,
        and a page that asks on sight spends a request every time it is built.
        The count is on the button because the two sections are not the same
        size - a handful of sets against four dozen parts - and a button that
        does not say it is about to take half a minute is a button that gets
        pressed once.
        """
        from wft.market import client as market

        pending = self._unpriced(uniques)
        row = QHBoxLayout()
        button = QPushButton(
            f"Look up platinum prices ({len(pending)})" if pending
            else "Platinum prices are current")
        button.setEnabled(bool(pending))
        note = QLabel(self._market_age_note())
        note.setWordWrap(True)  # 870px unwrapped: the page ran sideways at 20pt
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        if pending:
            button.setToolTip(
                f"{len(pending)} request(s) at two a second — about "
                f"{max(1, round(len(pending) * 0.5))} second(s). Prices "
                f"fetched in the last {market.FRESH_HOURS} hours are not asked "
                f"again.")
        self._market_widgets[key] = (button, note)
        button.clicked.connect(lambda: self._fetch_prices(pending, key))
        row.addWidget(button)
        row.addWidget(note)
        row.addStretch(1)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _fetch_prices(self, uniques: list[str], key: str) -> None:
        """Ask the market for these rows, one at a time, and stop when told.

        Synchronous on purpose: the throttle in the client is the whole safety
        argument, and a thread pool around it would be a second place where
        the interval could be got wrong. An earlier attempt to measure a rate
        limit on a Warframe service locked this user out of the game, so this
        path stays the only one and stays slow.

        What the parts list adds is length. Four dozen requests at two a
        second is half a minute, which is too long to be unstoppable - so the
        button becomes the way out, and the rows fetched before it was pressed
        are kept. `processEvents` is already in the loop for the progress
        line; the stop flag rides along with it.
        """
        from PySide6.QtWidgets import QApplication
        from wft.market import client as market

        button, note = self._market_widgets[key]
        self._stop_fetch = False
        button.setText("Stop")
        button.clicked.disconnect()
        button.clicked.connect(self._stop_fetching)
        try:
            if not self.conn.execute(
                    "SELECT 1 FROM market_items LIMIT 1").fetchone():
                note.setText("Fetching the market's item list…")
                QApplication.processEvents()
                market.refresh_catalogue(self.conn)
            for done, unique in enumerate(uniques, start=1):
                if self._stop_fetch:
                    break
                note.setText(
                    f"Asking warframe.market… {done} of {len(uniques)}")
                QApplication.processEvents()
                market.price_for(self.conn, unique, fetch=True)
        except market.MarketError as exc:
            # Whatever was fetched before the refusal is already stored, so
            # the page is rebuilt either way and the message survives it.
            self.refresh()
            self._market_widgets[key][1].setText(str(exc))
            return
        self.refresh()

    def _stop_fetching(self) -> None:
        """Leave the rest of the queue unasked. Nothing already stored is lost."""
        self._stop_fetch = True
