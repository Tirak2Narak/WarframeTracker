"""The Rewards page (the relic panel): the reason this project exists.

It is a page in the main window now, not a window of its own. The separate
window was justified by a rule — never steal focus during a mission — and
that argument was over-applied: the rule is about a window *appearing* at the
critical moment, and updating a page in a window that is already open steals
focus no more than updating a floating panel does. A second window bought
nothing and cost a geometry, a theme exception and a place for results to hide behind.

What is genuinely given up: the page cannot sit over gameplay when the tracker
shares a screen with the game, and a minimised tracker shows nothing. Both were
traded knowingly.

The one rule that survives unchanged: nothing here may call `show()`,
`raise_()` or `activateWindow()`. The page is switched to by the window that
owns it, on a hotkey the user pressed — never by this widget deciding it has
something to say.
"""
from __future__ import annotations

import time

from ui import theme
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QLineEdit,
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui import notify
from ui.widgets.name_suggest import suggest_on_typing
from ui.widgets.reward_card import RewardCard, badges_for, overlay_lines
from wft.data.db import UNCOUNTED_CATEGORIES
from wft.relic.score import is_forma, looks_like_forma
from ui.workers.relic_trigger import TriggerResult, run_trigger
from ui.workers.task import TaskWorker


def banner_text(verdict) -> str | None:
    """The banner's words for a verdict, or None when there is nothing to say.

    The one place the text is made: the banner shows it and the desktop
    notification repeats it.
    """
    if verdict is not None and verdict.best is not None:
        return f"TAKE {verdict.best.position} — {verdict.best.name}"
    if verdict is not None and verdict.rewards:
        return "NOTHING WORTH TAKING"
    return None


#: How long after one reading another reading of the same rewards still counts
#: as the same screen. The reward screen is up for 15 seconds at most.
SAME_SCREEN_SECONDS = 20.0

#: What Forma is called where the tool says what it recorded.
FORMA_NAME = "Forma Blueprint"


def booking_kind(reward) -> str | None:
    """What taking this reward books: "part" (a tracked part), "forma" (a named
    reward that is no tracked part: only the relic is spent) or None (not
    recognised). The one place the click and the button ask. Forma is
    Forma also when the reading resolved to the catalogue's Forma Blueprint,
    and Kuva and the rest of its kind book as it does."""
    if is_forma(reward) or reward.category in UNCOUNTED_CATEGORIES:
        return "forma"
    if reward.part_unique is not None:
        return "part"
    return None


def _screen_key(result):
    """What makes a reading "this screen": the reward of each position and the
    relics the log named; None when the reading says too little to compare."""
    verdict = getattr(result, "verdict", None)
    if result is None or result.error or verdict is None or not verdict.rewards:
        return None
    cards = tuple(reward.part_unique or ("forma" if booking_kind(reward) == "forma"
                                         else None)
                  for reward in verdict.rewards)
    if all(card is None for card in cards):
        return None
    return cards, tuple(sorted(result.opened_relics or ()))


def same_screen(previous, new, window: float = SAME_SCREEN_SECONDS) -> bool:
    """Whether `new` is the reward screen `previous` already read.

    Same rewards at every position (the part, or Forma), same number of cards,
    the same relics from the log, and no more than `window` seconds between the
    two readings (their own `read_at`, no clock here). One function for the
    click and the button: a second reading of one screen is not a second
    reward.
    """
    first, second = _screen_key(previous), _screen_key(new)
    if first is None or first != second:
        return False
    return 0 <= new.read_at - previous.read_at <= window


def overlay_args(result: TriggerResult) -> dict | None:
    """What the reward overlay shows for one reading (the arguments of
    `ui.overlay.rewards.show_command`), or None when there is nothing to show.

    Nothing for a failed reading and for one that recognised no tile at all:
    the overlay must not state what the tool does not know (the page and the
    notification say "could not read"). With a recommendation: the card to take
    and the lines under every card. Without one ("nothing worth taking"): the
    banner's own words instead, and no frame. The words come from `banner_text`,
    the lines from `overlay_lines`, the same functions as everywhere.
    """
    from wft.relic import last_screen, regions

    verdict = result.verdict
    if (result.error or verdict is None or not verdict.rewards
            or last_screen.reading_of(result).bad):
        return None
    count = len(verdict.rewards)
    best = None
    if verdict.best is not None:
        for index, reward in enumerate(verdict.rewards):
            if reward.position == verdict.best.position:
                best = index
    return {
        "tiles": [regions.tile(index, count).as_tuple() for index in range(count)],
        "best": best,
        "headline": None if best is not None else banner_text(verdict),
        "lines": [overlay_lines(reward) for reward in verdict.rewards],
    }


def notification_for(result: TriggerResult) -> tuple[str, str]:
    """(summary, body) of the notification for one finished reading."""
    from wft.relic import last_screen

    verdict = result.verdict
    if last_screen.reading_of(result).bad:
        return ("Could not read the reward screen",
                "Open Warframe Tracker to see what happened.")
    text = banner_text(verdict)
    if verdict.best is not None:
        # The badges the card of the recommended reward shows.
        return text, " · ".join(t for t, _key in badges_for(verdict.best))
    return text, verdict.headline


class RelicPanel(QWidget):
    log_line = Signal(str)
    result_ready = Signal(object)  # TriggerResult
    #: Emitted after this panel has written a holding. The main window is a
    #: separate window on a separate monitor and had no way of knowing: taking
    #: a reward here left the Plan page on the screen next to it showing "0
    #: ready to build" for a set the click had just completed.
    recorded = Signal()
    #: The "Open Setup" button of the not-ready state. The window that owns this
    #: page switches; the page does not reach for another one.
    setup_requested = Signal()
    #: "Calibrate with this screen": the window that owns the page opens the
    #: calibration, which loads the last reward screen by itself.
    calibrate_requested = Signal()

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self._worker: TaskWorker | None = None
        self._last_result: TriggerResult | None = None
        #: When the last hotkey press started, so that the screen offered for
        #: calibration is the one this press kept and not an older one.
        self._triggered_at = 0.0
        #: Whether the result on screen has been answered. A pick nobody
        #: recorded is a reward the inventory never learns about, and the next
        #: run quietly recommends from a number that is one too low.
        self._answered = True
        #: Whether the page shows its waiting state rather than a result.
        self._idle = True
        #: What was recorded for the result on screen, as (unique_name, count
        #: before). A reward screen is one choice: clicking a second card is
        #: changing your mind, not taking two rewards, and adding to both is
        #: how an inventory drifts by four in an evening.
        self._recorded: tuple[str, int, str | None] | None = None
        #: The relic this screen has already been charged, so a change
        #: of mind can put it back.
        self._spent: tuple[str, int, str | None] | None = None
        #: How the screen on display was booked: "button" or "click", and the
        #: card (0-based) a booking by the overlay's click took. A reading of
        #: the same screen carries both on.
        self._booked_via: str | None = None
        self._booked_index: int | None = None

        #: The reward overlay (`ui.overlay.rewards.RewardOverlay`), given by the
        #: application; None in tests and wherever no helper can run.
        self.overlay = None
        self._overlay_token: int | None = None
        self._overlay_result: TriggerResult | None = None

        self._build_ui()
        self.set_idle()

    def set_overlay(self, overlay) -> None:
        """Let the recommendation also be drawn over the game."""
        self.overlay = overlay
        overlay.unavailable.connect(self._overlay_unavailable)
        overlay.settling.connect(self._overlay_settled)
        overlay.note.connect(self.log_line)

    # -- layout ------------------------------------------------------------
    def _build_ui(self):
        # The page scrolls, as every other page does: it needs 951px at 20pt
        # (the briefing alone lists every relic worth opening) and a 1600x900
        # window has 858, which cut the briefing's own text. The cards stay
        # together, so what scrolls is the briefing above them.
        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        frame.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        outer = QVBoxLayout(body)
        outer.setContentsMargins(*theme.page_margins())

        title = QLabel("Rewards")
        title.setObjectName("ViewTitle")
        outer.addWidget(title)

        # Shown while there is nothing else to say and taken away the moment
        # there is. It is an instruction for the first day and furniture after
        # it, and it sat above the answer for every run thereafter.
        self.subtitle = QLabel(
            "Press the hotkey on a reward screen and this page comes forward "
            "with the four options ranked.")
        self.subtitle.setObjectName("ViewSubtitle")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)
        # 0.45em: this page does not scroll, and at 20pt in a 1150px window the
        # 0.7em that matches the other pages (19px against the 10 it was) left
        # the briefing a line short of its own text.
        outer.setSpacing(theme.px(0.45))

        self.idle_label = QLabel("")
        self.idle_label.setAlignment(Qt.AlignCenter)
        self.idle_label.setWordWrap(True)
        self.idle_label.setStyleSheet(theme.text("title", theme.TEXT_FAINT))
        outer.addWidget(self.idle_label)

        # Only shown while something required is missing (see set_idle).
        self.setup_row = QWidget()
        setup_layout = QHBoxLayout(self.setup_row)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.addStretch(1)
        self.setup_btn = QPushButton("Open Setup")
        self.setup_btn.setObjectName("Primary")
        self.setup_btn.clicked.connect(self.setup_requested)
        setup_layout.addWidget(self.setup_btn)
        setup_layout.addStretch(1)
        self.setup_row.setVisible(False)
        outer.addWidget(self.setup_row)

        # What to open, on the page you are sitting on before you open it.
        # Idle, this page was a title and a sentence over an empty screen -
        # three quarters of it was blank grey - while the one thing worth
        # knowing at that moment was already computed elsewhere.
        # Two columns: what to open, and what it would finish. Together they
        # are the whole answer to "what am I doing this run", which is what
        # this page is for in the minutes either side of the fifteen seconds.
        self.briefing = QWidget()
        briefing_row = QHBoxLayout(self.briefing)
        briefing_row.setContentsMargins(0, 0, 0, 0)
        briefing_row.setSpacing(theme.px(0.95))
        self.briefing_body = self._briefing_card(
            briefing_row, "Best on your shelf")
        self.briefing_needs = self._briefing_card(
            briefing_row, "Closest to finishing")
        outer.addWidget(self.briefing)

        # One place the answer appears, whatever the answer is. Reading four
        # cards to find the one with the green border is four readings; from
        # three metres away, in fifteen seconds, there is time for one. The
        # cards stay - they are the reason, for the second look - but the
        # verdict gets a line of its own above them.
        #
        # The failure state lands in the same slot rather than in a label of
        # its own: "RESCAN" is also an answer, and an answer belongs where the
        # eye is already going.
        self.verdict_banner = QLabel("")
        self.verdict_banner.setAlignment(Qt.AlignCenter)
        self.verdict_banner.setWordWrap(True)
        self.verdict_banner.setVisible(False)
        outer.addWidget(self.verdict_banner)
        self.failure_label = self.verdict_banner

        # Under the verdict, not floating in the middle of the page. The banner
        # is the answer and the cards are why; a gap between them makes the eye
        # travel twice for one decision.
        self.cards_row = QHBoxLayout()
        self.cards_row.setSpacing(theme.px(0.7))
        outer.addLayout(self.cards_row)
        outer.addStretch(1)

        self.cards = [RewardCard(i + 1) for i in range(4)]
        for card in self.cards:
            card.taken.connect(lambda position: self._confirm_pick(position - 1))
            self.cards_row.addWidget(card, 1)

        self.pick_row = QHBoxLayout()
        self._last_shown_best: str | None = None
        self.pick_prompt = QLabel("Which did you take?  ·  press \"I took this\" on its card")
        self.pick_prompt.setStyleSheet(theme.text("body", theme.TEXT_DIM))
        self.pick_row.addWidget(self.pick_prompt)
        self.pick_buttons: list[QPushButton] = []
        for i in range(4):
            button = QPushButton(str(i + 1))
            button.setVisible(False)
            button.clicked.connect(lambda _checked, idx=i: self._confirm_pick(idx))
            self.pick_row.addWidget(button)
            self.pick_buttons.append(button)
        # A reward the reading could not name is still a reward the user took,
        # and until now taking it recorded nothing — the database went quietly
        # wrong, which is exactly what spoils the next recommendation. Typing
        # it is a few seconds after the fifteen that mattered have passed.
        self.pick_manual = QLineEdit()
        self.pick_manual.setPlaceholderText("or type what you took")
        self.pick_manual.setClearButtonEnabled(True)
        # 22em: the 320px it was at 11pt. At 20pt the hint alone is 277px and
        # the field was cut at 242.
        self.pick_manual.setMinimumWidth(theme.px(13))
        self.pick_manual.setMaximumWidth(theme.px(22))
        self.pick_manual.returnPressed.connect(self._confirm_typed)
        # Offers part names while typing, typos included. Only a hint: what
        # gets recorded is still decided by `_confirm_typed`.
        self._typed_part_names: list[str] | None = None
        self._pick_model = suggest_on_typing(
            self.pick_manual, self._part_names)
        self.pick_row.addWidget(self.pick_manual)

        self.pick_row.addStretch(1)
        self.pick_feedback = QLabel("")
        # Wraps: "None of them read ..." is two sentences long, and in one line
        # it made the page 1278px wider than its window (measured at 14pt).
        self.pick_feedback.setWordWrap(True)
        self.pick_feedback.setStyleSheet(theme.text("small", theme.GOOD))
        self.pick_row.addWidget(self.pick_feedback)
        outer.addLayout(self.pick_row)

        # Only shown when the shelf cannot answer for itself. The log says
        # which four relics the squad opened, never which one was the user's,
        # so the answer comes from what the player holds - and when two of
        # them are held, one question is cheaper than a wrong guess.
        self.relic_row = QHBoxLayout()
        self.relic_prompt = QLabel("Which relic was yours?")
        self.relic_prompt.setStyleSheet(
            theme.text("small", theme.TEXT_DIM))
        self.relic_row.addWidget(self.relic_prompt)
        self.relic_buttons: list[QPushButton] = []
        self.relic_row.addStretch(1)
        outer.addLayout(self.relic_row)

        # Only shown when the last reading was bad (see `_offer_calibration`).
        self.calibrate_row = QWidget()
        calibrate_layout = QHBoxLayout(self.calibrate_row)
        calibrate_layout.setContentsMargins(0, 0, 0, 0)
        self.calibrate_btn = QPushButton("Calibrate with this screen")
        self.calibrate_btn.setObjectName("Primary")
        self.calibrate_btn.clicked.connect(self.calibrate_requested)
        calibrate_layout.addWidget(self.calibrate_btn)
        self.calibrate_note = QLabel("")
        self.calibrate_note.setWordWrap(True)
        self.calibrate_note.setStyleSheet(theme.text("small", theme.TEXT_DIM))
        calibrate_layout.addWidget(self.calibrate_note, 1)
        self.calibrate_row.setVisible(False)
        outer.addWidget(self.calibrate_row)

        # "Picture kept: ..." after a reading whose screen was written to disk
        # (Settings, Reward pictures). Clicking it opens the folder.
        self.kept_label = QLabel("")
        self.kept_label.setTextFormat(Qt.RichText)
        self.kept_label.setStyleSheet(theme.text("small", theme.TEXT_DIM))
        self.kept_label.linkActivated.connect(self._open_kept_folder)
        self.kept_label.setVisible(False)
        outer.addWidget(self.kept_label)

        self._set_pick_row_visible(False)
        self._set_relic_row_visible(False)

    def _show_kept(self, result) -> None:
        """Name the picture this reading left, or hide the line."""
        from html import escape

        from wft.relic import kept_pictures

        path = getattr(result, "kept_picture", None)
        folder = kept_pictures.folder_setting(self.cfg)
        if not path or folder is None:
            self.kept_label.setVisible(False)
            return
        name = escape(kept_pictures.relative_name(folder, path))
        self.kept_label.setText(f'<a href="folder">Picture kept: {name}</a>')
        self.kept_label.setVisible(True)

    def _open_kept_folder(self, _link: str = "") -> None:
        from wft.relic import kept_pictures

        folder = kept_pictures.folder_setting(self.cfg)
        if folder is not None and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _briefing_card(self, row, heading: str) -> QLabel:
        """One column of the idle briefing, and the label to fill it with."""
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(*theme.card_margins())
        layout.setSpacing(theme.px(0.55))
        title = QLabel(heading)
        title.setObjectName("SectionHeader")
        layout.addWidget(title)
        body = QLabel("")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        body.setAlignment(Qt.AlignTop)
        layout.addWidget(body, 1)
        row.addWidget(card, 1)
        return body

    # -- states --------------------------------------------------------
    def _open_required(self) -> list:
        """What stops a reward screen from being read, from the one place that knows.

        The same checks `wf doctor` prints and the Setup page draws. This page
        said "Ready  ·  press F23" with no game data, no usable key and no
        keyboard access, which sent a newcomer to press a key that did nothing.
        """
        from wft import readiness

        try:
            return readiness.open_required(self.cfg)
        except Exception:  # noqa: BLE001 - a status line must not break the page
            return []

    def refresh(self):
        """Called when the page is shown: the state of Setup may have changed.

        Only the idle state is redrawn. A result on screen is an answer the user
        may be reading or has yet to record, and is not ours to replace.
        """
        if self._idle:
            self.set_idle()

    def set_idle(self):
        self._idle = True
        open_ = self._open_required()
        if open_:
            todo = open_[0].todo
            self.idle_label.setText(f"Not ready  ·  {todo[:1].upper()}{todo[1:]}")
        else:
            key = str(self.cfg.get("hotkey.key", "") or "").strip()
            self.idle_label.setText(f"Ready  ·  press {key} on the reward screen")
        self.setup_row.setVisible(bool(open_))
        self.calibrate_row.setVisible(False)
        self.kept_label.setVisible(False)
        self.idle_label.setVisible(True)
        self.subtitle.setVisible(True)
        self.verdict_banner.setVisible(False)
        self.failure_label.setVisible(False)
        for card in self.cards:
            card.setVisible(False)
        self._set_pick_row_visible(False)
        self._show_briefing()

    def _show_briefing(self) -> None:
        """The relics worth opening, while there is nothing else to show.

        Read fresh each time the page goes idle rather than cached: a run just
        finished, so the shelf and the gaps it fills have both just changed.
        """
        from wft.data import db as dbq
        from wft.relic import openings as openings_mod

        try:
            connection = dbq.connect(self.cfg.db_path)
        except Exception:  # noqa: BLE001 - a missing database is not a crash
            self.briefing.setVisible(False)
            return
        try:
            ranked = [entry for entry in openings_mod.rank(connection)
                      if entry.expected > 0][:6]
            closest = self._nearly_done(connection)
        finally:
            connection.close()

        if not ranked:
            self.briefing_body.setText(
                "No relic recorded here can drop a piece you still need. "
                "Import your relics, or this page will only ever be useful "
                "after the run rather than before it.")
        else:
            lines = []
            for entry in ranked:
                wants = ", ".join(name for name, _r, _c in entry.wanted[:2])
                vault = (f"  <span style='color:{theme.VAULTED}'>vaulted</span>"
                         if entry.vaulted else "")
                lines.append(
                    f"<b>{entry.name}</b> ×{entry.held}{vault}<br>"
                    f"<span style='color:{theme.GOOD}'>"
                    f"{openings_mod.percent(entry.expected)}</span> "
                    f"chance of a part you need · <span style='color:{theme.TEXT_DIM}'>"
                    f"{wants}</span>")
            self.briefing_body.setText("<br><br>".join(lines))

        self.briefing_needs.setText(self._closest_sets(closest))
        self.briefing.setVisible(True)

    def _nearly_done(self, connection, limit: int = 6):
        """Sets short of at most two pieces, closest first.

        Mastered sets are left out for the same reason the Plan page leaves
        them out: the points are already banked, so finishing one is worth it
        only for the set itself.
        """
        from wft.data import db as dbq

        rows = []
        for row in connection.execute(
                "SELECT i.unique_name, i.name FROM items i "
                "WHERE i.unique_name IN (SELECT DISTINCT item_unique FROM parts) "
                "AND i.category NOT IN ('reward_other', 'component_only')"):
            if dbq.is_mastered(connection, row["unique_name"]):
                continue
            held, total = dbq.set_progress(connection, row["unique_name"])
            if not total or held == 0 or held >= total:
                continue
            short = []
            for part in connection.execute(
                    "SELECT name, count_needed, unique_name FROM parts "
                    "WHERE item_unique = ? AND alias_of IS NULL",
                    (row["unique_name"],)):
                # One set's own recipe, on purpose: this lists sets close to
                # done, by what their own recipe still lacks.
                owing = part["count_needed"] - dbq.crafting_stock(
                    connection, part["unique_name"])
                if owing > 0:
                    short.append(f"{part['name']}"
                                 + (f" ×{owing}" if owing > 1 else ""))
            if not short or len(short) > 2:
                continue
            rows.append((total - held, row["name"], held, total,
                         ", ".join(short)))
        rows.sort()
        return [(name, held, total, missing)
                for _gap, name, held, total, missing in rows[:limit]]

    def _closest_sets(self, rows) -> str:
        """The sets one or two pieces short, as the column beside the relics.

        The pair is the point: a relic worth opening and a set worth finishing
        are the same decision seen from two ends, and having to change page to
        see the other half is what made this one feel empty.
        """
        if not rows:
            return ("Nothing is close to finished. Import your inventory and "
                    "this fills itself in.")
        lines = []
        for name, held, total, missing in rows:
            lines.append(
                f"<b>{name}</b> "
                f"<span style='color:{theme.WARN}'>{held}/{total}</span><br>"
                f"<span style='color:{theme.TEXT_DIM}'>{missing}</span>")
        return "<br><br>".join(lines)

    def _set_stale(self, stale: bool):
        for card in self.cards:
            if card.isVisible():
                card.set_stale(stale)

    def _take_back(self, connection) -> None:
        """Undo the pick already recorded for the result on screen.

        Restores the count that was there before rather than subtracting one,
        so a mis-click followed by a correction leaves exactly what a single
        correct click would have.
        """
        if self._recorded is None:
            # A Forma booking has no part to restore, only its relic.
            self._give_relic_back(connection)
            return
        unique, before, source = self._recorded
        from wft.data import db as dbq

        # Only if the count is still what this panel wrote. Between a mis-click
        # and its correction an import can land, and putting a stale snapshot
        # back over it would undo work this panel knows nothing about.
        if dbq.owned_count(connection, unique) != before + 1:
            self._recorded = None
            return
        # Back to the source the row carried, not to "pick". The stamp is what
        # a later import reads to work out what a recording covered, so a
        # correction that rewrites it costs more than the count it fixes.
        dbq.set_part_count(connection, unique, before, source or "manual")
        self._recorded = None
        # The relic went with the pick, so it comes back with it.
        self._give_relic_back(connection)

    def _mark_answered(self) -> None:
        self._answered = True
        self.pick_prompt.setStyleSheet(theme.text("body", theme.TEXT_DIM))

    def _set_relic_row_visible(self, visible: bool):
        self.relic_prompt.setVisible(visible)
        for button in self.relic_buttons:
            button.setVisible(visible)

    def _clear_relic_choice(self) -> None:
        """Drop the buttons of a previous screen's question."""
        for button in self.relic_buttons:
            self.relic_row.removeWidget(button)
            button.deleteLater()
        self.relic_buttons = []
        self._set_relic_row_visible(False)

    def _logged_relics(self) -> list[str]:
        """The relic identifiers this screen's log line named, in order."""
        result = self._last_result
        return list(dict.fromkeys(
            getattr(result, "opened_relics", ()) if result else ()))

    def _own_relic_plan(self, connection):
        """The user's own relic from the equip dialog or, failing that, the local
        loader line in the log.

        Returns None when the log named none (the caller narrows by the leftmost
        card), else `(candidates, unasked, name, label)`: the held identifiers it
        can be, whether a single one may be taken without a question, the
        relic's printed name, and the text for the panel log. The refinement rule of `_spend_relic` stays: a relic
        is taken unasked only at the refinement the log gave and the shelf
        holds; an unknown refinement, or one not held, asks among the held
        refinements of this relic, even when only one is held (refining turns an
        Intact into a Radiant without the tool seeing it). Refining in the
        mission's relic choice is the exception the tool does see: with
        the log's refinement not held, the refinement it was refined from is
        taken unasked when held.
        """
        from wft.data import db as dbq
        from wft.relic import eelog

        own = getattr(self._last_result, "own_relic", None)
        if own is None:
            return None
        narrowed, refinement, every = eelog.own_relic_uniques(
            connection, own, self._logged_relics())
        if not every:
            return None
        name = own.name or dbq.bare_relic_name(connection, every[0])

        def level(suffix):
            word = next((k for k, v in dbq.REFINEMENTS.items() if v == suffix), None)
            return word.title() if word else None

        label = name
        if refinement and level(refinement):
            label += f" [{level(refinement)}]"
        held = [u for u in narrowed if dbq.relic_count(connection, u) > 0]
        if refinement and held:
            return held, True, name, f"{label} ({own.source})"
        source = getattr(own, "refined_from", None)
        if refinement and source:
            before = [u for u in every if u.endswith(source)
                      and dbq.relic_count(connection, u) > 0]
            if before:
                return before, True, name, (
                    f"{label} ({own.source}, refined from {level(source)})")
        return ([u for u in every if dbq.relic_count(connection, u) > 0], False, name,
                f"{label} ({own.source})")

    def _relic_candidates(self, connection) -> list[str]:
        """Which of the opened relics was the user's, narrowed twice.

        The log names the four relics the squad opened and never says whose is
        whose. Two things narrow that down, and they are independent:

        * **The leftmost reward is always the user's own**, whoever else is in
          the squad and whatever is taken in the end. So the player's relic is one that
          can drop that reward - which usually names it outright, because the
          four relics of a screen are rarely the same one.
        * **Only a relic that is held can have been spent.** Three of the four
          are not on the player's shelf.

        Applied in that order and the second only as a filter on the first, so
        a reward the reading could not name costs the sharper test and not the
        answer. If the pair leaves one relic, it is spent without asking.
        """
        from wft.data import db as dbq

        plan = self._own_relic_plan(connection)
        if plan is not None:
            return plan[0]
        result = self._last_result
        opened = list(dict.fromkeys(
            getattr(result, "opened_relics", ()) if result else ()))
        if not opened:
            return []

        left = None
        if result is not None and result.verdict and result.verdict.rewards:
            left = result.verdict.rewards[0].part_unique
        if left:
            drops = {row["relic_unique"] for row in connection.execute(
                "SELECT DISTINCT relic_unique FROM relic_rewards "
                "WHERE part_unique = ? AND relic_unique IN (%s)"
                % ",".join("?" * len(opened)), [left, *opened])}
            if drops:
                opened = [unique for unique in opened if unique in drops]

        held = [unique for unique in opened
                if dbq.relic_count(connection, unique) > 0]
        if held:
            return held

        # Nothing held at the refinement the log names. Refining is why: it
        # turns an Intact into a Radiant and the tool never sees it happen, so
        # a shelf reading "Axi S20 ×2 Intact" is a player holding one Intact
        # and one Radiant - and the Radiant is the one just spent.
        #
        # Measured on a real run: the log said Axi S20 Radiant, the shelf said
        # two Intact, and the panel silently charged nothing.
        # So the same relic at another refinement is offered instead, always
        # as a question - the refinement is part of the identifier and picking
        # one for the player would spend a relic that is visibly still held.
        names = {}
        for unique in opened:
            row = connection.execute(
                "SELECT name FROM relics WHERE unique_name = ?",
                (unique,)).fetchone()
            if row:
                names[row["name"]] = unique
        if not names:
            return []
        placeholders = ",".join("?" * len(names))
        return [row["unique_name"] for row in connection.execute(
            "SELECT r.unique_name FROM relics r "
            "JOIN owned_relics o ON o.relic_unique = r.unique_name "
            f"WHERE r.name IN ({placeholders}) AND o.count > 0 "
            "ORDER BY r.name", list(names))]

    def _spend_relic(self, connection) -> None:
        """Take the opened relic off the shelf, or ask which one it was."""
        from wft.data import db as dbq

        self._clear_relic_choice()
        plan = self._own_relic_plan(connection)
        if plan is not None:
            candidates, unasked, name, label = plan
            self.log_line.emit(f"relic: yours is {label}")
            nothing_held = f"no {name} is recorded as held"
        else:
            candidates, unasked = self._relic_candidates(connection), False
            self.log_line.emit("relic: the log names no own relic, narrowing "
                               "by the leftmost card")
            nothing_held = "none of the relics in play is recorded as held"
        if not candidates:
            # No guess - but no silence either. Nothing to subtract from means
            # the shelf is out of date, and a panel that quietly does nothing
            # looks exactly like one that worked.
            self.pick_feedback.setText(
                f"{self.pick_feedback.text()}  ·  no relic taken off the "
                f"shelf — {nothing_held}")
            return
        # One candidate is only spent unasked when the log's own refinement is
        # the one held. A fallback across refinements is a guess about
        # refining and gets a question, even alone.
        exact = [u for u in self._logged_relics()
                 if dbq.relic_count(connection, u) > 0]
        if len(candidates) == 1 and (unasked or (plan is None
                                                 and candidates[0] in exact)):
            self._take_relic(connection, candidates[0])
            return
        for unique in candidates:
            name = dbq.relic_label(dbq.bare_relic_name(connection, unique),
                                   dbq.refinement_of(unique), always=True)
            button = QPushButton(name)
            button.clicked.connect(
                lambda _checked, u=unique: self._relic_chosen(u))
            self.relic_row.insertWidget(
                self.relic_row.count() - 1, button)
            self.relic_buttons.append(button)
        self._set_relic_row_visible(True)

    def _take_relic(self, connection, unique: str) -> None:
        from wft.data import db as dbq

        before = dbq.relic_count(connection, unique)
        source = dbq.source_of(connection, "relic", unique)
        dbq.set_relic_count(connection, unique, before - 1, "pick")
        self._spent = (unique, before, source)
        name = dbq.name_for(connection, "relic", unique)
        self.pick_feedback.setText(
            f"{self.pick_feedback.text()}  ·  {name} {before} → {before - 1}")

    def _relic_chosen(self, unique: str) -> None:
        """The user answered which of their relics this screen cost."""
        from wft.data import db as dbq

        connection = dbq.connect(self.cfg.db_path)
        try:
            self._give_relic_back(connection)
            self._take_relic(connection, unique)
        finally:
            connection.close()
        self._clear_relic_choice()
        self.recorded.emit()

    def _give_relic_back(self, connection) -> None:
        """Undo the relic already spent for the screen on display."""
        from wft.data import db as dbq

        if self._spent is None:
            return
        unique, before, source = self._spent
        # Same guard as the reward: only if the count is still what this panel
        # left behind. An import between the mis-click and its correction owns
        # the number now.
        if dbq.relic_count(connection, unique) == before - 1:
            dbq.set_relic_count(connection, unique, before, source or "manual")
        self._spent = None

    def _set_pick_row_visible(self, visible: bool):
        self.pick_prompt.setVisible(visible)
        # The numbered buttons stay hidden: the cards carry the choice. They
        # never had keyboard shortcuts, and the window never has focus while
        # the game does, so a key here would cost a click to reach anyway.
        self.pick_manual.setVisible(visible)
        if not visible:
            self.pick_manual.clear()
        self.pick_feedback.setVisible(visible)

    def _picture_connection(self):
        """A connection kept open for icon lookups.

        Opened once rather than per card: the panel has fifteen seconds and
        four cards, and opening a database four times in them is four times
        more than necessary.
        """
        from wft.data import db as dbq

        if getattr(self, "_pictures", None) is None:
            try:
                self._pictures = dbq.connect(self.cfg.db_path)
            except Exception:  # noqa: BLE001 - a card without a picture is fine
                self._pictures = False
        return self._pictures or None

    def _show_verdict(self, text: str, colour: str, tooltip: str = "") -> None:
        """Put the answer in the banner, in the colour of what kind it is."""
        self.verdict_banner.setText(text)
        self.verdict_banner.setToolTip(tooltip)
        self.verdict_banner.setStyleSheet(
            f"font-family: {theme.DISPLAY_FAMILY}; "
            f"font-size: {theme.size('display'):.3f}em; font-weight: 800; "
            f"letter-spacing: 2px; color: {theme.readable_on(colour)}; "
            f"background: {colour}; "
            f"padding: {theme.px(0.45)}px {theme.px(0.9)}px; "
            f"border-top-right-radius: {theme.RADIUS['medium']}px; "
            f"border-bottom-right-radius: {theme.RADIUS['medium']}px;")
        self.verdict_banner.setVisible(True)
        self.subtitle.setVisible(False)

    def show_failure(self, message: str):
        self._idle = False
        self.briefing.setVisible(False)
        hotkey = str(self.cfg.get("hotkey.key", "") or "").strip()
        self.idle_label.setVisible(False)
        self.setup_row.setVisible(False)
        self.calibrate_row.setVisible(False)
        self.kept_label.setVisible(False)
        self._show_verdict(f"RESCAN — {hotkey}" if hotkey else "RESCAN",
                           theme.BAD, message)
        for card in self.cards:
            card.setVisible(False)
        self._set_pick_row_visible(False)
        self.log_line.emit(f"capture failed: {message}")

    def show_result(self, result: TriggerResult):
        self._idle = False
        #: The same screen read again: what was booked for it stays booked, and
        #: a later booking moves it.
        carry = same_screen(self._last_result, result)
        self._last_result = result
        self.idle_label.setVisible(False)
        self.setup_row.setVisible(False)
        self.calibrate_row.setVisible(False)
        self.verdict_banner.setVisible(False)
        self._show_kept(result)

        verdict = result.verdict
        if verdict is not None and verdict.best is not None:
            # One colour for one statement. The banner said it in the accent,
            # the card said it again in green, and the card's border was green
            # too - three marks for one recommendation, which is the thing the
            # left-edge motif exists to stop. "Take this" is a judgement, so it
            # wears the judgement colour.
            self._show_verdict(banner_text(verdict), theme.GOOD, verdict.headline)
        elif verdict is not None and verdict.rewards:
            # Something was read and nothing could be recommended. Saying so is
            # the answer; leaving the banner blank makes the user hunt for a
            # green border that is not there.
            self._show_verdict(banner_text(verdict), theme.JUNK,
                               verdict.headline)
        for index, card in enumerate(self.cards):
            if verdict is None or index >= len(verdict.rewards):
                card.setVisible(False)
                self.pick_buttons[index].setVisible(False)
                continue
            reward = verdict.rewards[index]
            # `best` is None when nothing was identified; no card is then
            # highlighted and the panel shows its failure state instead of a
            # confident pointer at an unreadable tile.
            is_best = verdict.best is not None and reward.position == verdict.best.position
            is_alt = verdict.alternative is not None and reward.position == verdict.alternative.position
            card.set_reward(reward, is_best, is_alt)
            card.show_picture(self._picture_connection(), reward,
                              is_best or is_alt)
            card.setVisible(True)
            card.appear(index)


        # A result arriving on top of an unanswered one is the moment the
        # chance is lost, so it is the moment to say so.
        missed = None
        if not carry and not self._answered and self._last_shown_best:
            missed = self._last_shown_best
        if not carry:
            self._answered = verdict is None or not verdict.rewards
            self._recorded = None
            # Not carried over: the relic belonged to the screen that is gone,
            # and its question would be answered against the wrong four.
            self._spent = None
            self._booked_via, self._booked_index = None, None
            self._clear_relic_choice()
        # The briefing is for the minutes either side of the run, not for the
        # fifteen seconds. Left up it took half the page and pushed the cards
        # into a strip, which is the opposite of what those seconds need.
        self.briefing.setVisible(False)
        self.idle_label.setVisible(False)
        self._last_shown_best = (
            verdict.best.name if verdict and verdict.best else None)

        if not carry:
            self.pick_feedback.setText("")
        self._set_pick_row_visible(verdict is not None and len(verdict.rewards) > 0)
        if carry and self._answered:
            self._mark_answered()
        if not self._answered:
            self.pick_prompt.setStyleSheet(theme.text("body", theme.WARN, 600))
            if missed:
                self.pick_feedback.setStyleSheet(theme.text("small", theme.WARN))
                self.pick_feedback.setText(
                    f"The last one was never recorded — nothing was added for "
                    f"it. ({missed} was the pick.)")

        # Nothing recognised at all is almost never four unreadable rewards.
        # It is the capture pointing somewhere that is not the reward row, and
        # the regions ship as an estimate that has been wrong before — one
        # measurement read "SELEC A INVEN", text from the sell screen. Saying
        # so here is the difference between a tool that looks broken and one
        # that says what to do about it.
        if verdict is not None and verdict.rewards and verdict.best is None:
            readings = [r.raw_text.strip() for r in verdict.rewards if r.raw_text.strip()]
            self.pick_feedback.setStyleSheet(theme.text("small", theme.WARN))
            self.pick_feedback.setText(
                "None of them read. If this keeps happening the capture is "
                "looking in the wrong place — Settings → Capture → Calibrate reward regions."
                + (f"  Saw: {readings[0][:40]!r}" if readings else ""))

        self.log_line.emit(verdict.headline if verdict else "nothing recognised")

    # -- triggering ------------------------------------------------------
    def trigger(self):
        """Start (or ignore, if one is already running) a capture-read-rank pass.

        Grey out whatever is currently shown immediately, so a leftover result
        from the previous mission can never be mistaken for the current one.
        """
        if self._worker is not None and self._worker.isRunning():
            return
        self._set_stale(True)

        self._triggered_at = time.time()
        worker = TaskWorker(run_trigger, self.cfg)
        worker.progress.connect(self.log_line.emit)
        worker.finished_ok.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        self._worker = worker
        worker.start()

    def _on_failed(self, message: str):
        if self.overlay is not None:
            self.overlay.settle_now()
        self.show_failure(message)
        self._offer_calibration()
        if self.overlay is not None:
            self.overlay.hide()
        self._notify(TriggerResult(None, message, 0))

    def _notify(self, result: TriggerResult) -> None:
        """One desktop notification per reading, unless Settings turned it off."""
        if not self.cfg.get("notifications.enabled", True):
            return
        try:
            notify.notify(*notification_for(result))
        except Exception as exc:  # noqa: BLE001 - never worth a broken page
            self.log_line.emit(f"notification failed: {exc}")

    def _on_finished(self, result: TriggerResult):
        # The choice made on the previous reading is booked before this one
        # replaces it on the page: it belongs to that reading.
        # The same screen read again is not a new choice: its tick is handed on
        # and nothing is booked until the end.
        same = same_screen(self._last_result, result)
        carried = None
        if self.overlay is not None:
            if same:
                carried = self.overlay.take_over_pick()
            else:
                self.overlay.settle_now()
        if same and carried is None and self._booked_via == "click":
            carried = self._booked_index
        if result.error:
            self.show_failure(result.error)
        else:
            self.show_result(result)
        self._offer_calibration()
        # The notification is the fallback of the overlay: sent unless the
        # helper really showed the overlay for this reading.
        if not self._show_overlay(result, carried):
            self._notify(result)
        self.result_ready.emit(result)

    def _show_overlay(self, result: TriggerResult, picked: int | None = None) -> bool:
        """Draw this reading over the game. True when that was asked of the
        helper, which then decides (`_overlay_unavailable`) whether the
        notification still has to go out."""
        overlay = self.overlay
        if overlay is None:
            return False
        if not overlay.enabled:
            overlay.hide()
            return False
        args = overlay_args(result)
        if args is None or result.placement is None:
            overlay.hide()
            return False
        from wft.relic import last_screen

        screen = last_screen.current()
        started = (screen.taken_at if screen is not None
                   and screen.taken_at >= self._triggered_at else None)
        if picked is not None and picked < len(args["tiles"]):
            args["picked"] = picked
        self._overlay_result = result
        self._overlay_token = overlay.show(args, result.placement,
                                           result.header_mask, started)
        return True

    def _overlay_unavailable(self, token: int) -> None:
        """The helper did not show the overlay of this reading: the
        notification goes out as it would without an overlay."""
        if token != self._overlay_token or self._overlay_result is None:
            return
        result, self._overlay_result = self._overlay_result, None
        self.log_line.emit(f"overlay not shown: {self.overlay.last_error}")
        self._notify(result)

    def _overlay_settled(self, token: int, index: int) -> None:
        """The overlay ended with the card the player clicked last: book
        it the way the "I took this" button does, unless that already happened
        for this reading or the card is not recognised."""
        if (token != self._overlay_token or self._overlay_result is None
                or self._overlay_result is not self._last_result):
            return
        # Booked by the button: that stands. Booked by an earlier click of this
        # same screen: the last click still decides.
        if self._answered and self._booked_via != "click":
            return
        if self._booked_via == "click" and self._booked_index == index:
            return
        verdict = self._last_result.verdict
        if (verdict is None or index >= len(verdict.rewards)
                or booking_kind(verdict.rewards[index]) is None):
            return
        self.log_line.emit(f"picked in the game: card {index + 1}")
        self._confirm_pick(index)
        self._booked_via, self._booked_index = "click", index

    def _offer_calibration(self) -> None:
        """Offer "Calibrate with this screen" when this press read badly.

        Bad is decided in `wft.relic.last_screen` (an error, i.e. RESCAN, or no
        tile recognised) and nowhere else. Only a screen kept by *this* press:
        a button that opened the dialog on an older picture would calibrate on
        something other than the screen the page is complaining about.
        """
        from wft.relic import last_screen

        screen = last_screen.current()
        if (screen is None or screen.taken_at < self._triggered_at
                or not screen.reading.bad):
            self.calibrate_row.setVisible(False)
            return
        self.calibrate_note.setText(
            "Nothing on this screen was recognised. If you can read the names "
            "on it yourself, the regions are probably not where your screen has "
            "them: calibrating on this very screen takes one dragged box. If it "
            "was not a reward screen, ignore this."
            if not screen.reading.failed else
            "This screen could not be read. If it was a reward screen, "
            "calibrating on this very screen takes one dragged box; if not, "
            "ignore this.")
        self.calibrate_row.setVisible(True)

    # -- pick confirmation -------------------------------------------------
    def _confirm_pick(self, index: int):
        if self._last_result is None or self._last_result.verdict is None:
            return
        rewards = self._last_result.verdict.rewards
        if index >= len(rewards):
            return
        reward = rewards[index]
        kind = booking_kind(reward)
        if kind == "forma":
            self._confirm_forma(FORMA_NAME if is_forma(reward) else reward.name)
            return
        if kind is None:
            self.pick_feedback.setStyleSheet(theme.text("small", theme.WARN))
            self.pick_feedback.setText(
                "Not recognised — type the name instead.")
            self.pick_manual.setFocus()
            return

        from wft.data import db as dbq

        connection = dbq.connect(self.cfg.db_path)
        try:
            self._take_back(connection)
            current = dbq.owned_count(connection, reward.part_unique)
            source = dbq.source_of(connection, "part", reward.part_unique)
            dbq.set_part_count(connection, reward.part_unique, current + 1, "pick")
            self._recorded = (reward.part_unique, current, source)
            self.pick_feedback.setStyleSheet(
                theme.text("small", theme.GOOD))
            self.pick_feedback.setText(f"Recorded: {reward.name} (+1)")
            # After the reward, because the message appends to that line.
            self._spend_relic(connection)
        finally:
            connection.close()

        self._mark_answered()
        self._booked_via, self._booked_index = "button", None
        self.recorded.emit()
        self.pick_feedback.setToolTip(
            "Clicking another card moves this rather than adding to it.")

    def _confirm_forma(self, name: str = FORMA_NAME) -> None:
        """Forma was taken: no part to count, but the relic it came out of is
        spent all the same. The same take-back as for a part. Kuva and
        the rest of its kind book the same, under their own name."""
        from wft.data import db as dbq

        connection = dbq.connect(self.cfg.db_path)
        try:
            self._take_back(connection)
            self._recorded = None
            self.pick_feedback.setStyleSheet(theme.text("small", theme.GOOD))
            self.pick_feedback.setText(f"Recorded: {name}")
            self._spend_relic(connection)
        finally:
            connection.close()
        self._mark_answered()
        self._booked_via, self._booked_index = "button", None
        self.recorded.emit()
        self.pick_feedback.setToolTip(
            "Clicking another card moves this rather than adding to it.")

    def _part_names(self) -> list[str]:
        """The part names to suggest, read once for this panel."""
        if self._typed_part_names is None:
            from wft.data import db as dbq

            connection = dbq.connect(self.cfg.db_path)
            try:
                self._typed_part_names = sorted(
                    {row["name"] for row in connection.execute(
                        "SELECT name FROM parts WHERE alias_of IS NULL")},
                    key=str.casefold)
            finally:
                connection.close()
        return self._typed_part_names

    def _confirm_typed(self):
        """Record a reward the reading could not name.

        Matched against the catalogue rather than taken as written: a typo
        stored as a name is a row nothing will ever join to, which is worse
        than the missing row it replaced. The threshold is high because the
        user is typing a name they can see on screen.
        """
        from rapidfuzz import fuzz, process

        from wft.data import db as dbq

        typed = self.pick_manual.text().strip()
        if not typed:
            return

        connection = dbq.connect(self.cfg.db_path)
        try:
            names = {row["name"]: row["unique_name"] for row in connection.execute(
                "SELECT name, unique_name FROM parts WHERE alias_of IS NULL")}
            hit = process.extractOne(typed, list(names), scorer=fuzz.ratio,
                                     processor=str.casefold)
            if not hit or hit[1] < 85:
                self.pick_feedback.setStyleSheet(theme.text("small", theme.WARN))
                self.pick_feedback.setText(
                    f"No part called {typed!r}" +
                    (f" — closest is {hit[0]!r}" if hit else ""))
                return
            # Typed Forma is Forma as on a card: the relic, no part.
            # So is everything else that is never counted.
            unique = names[hit[0]]
            forma = (looks_like_forma(hit[0])
                     or dbq.is_uncounted(connection, unique))
            if not forma:
                self._take_back(connection)
                current = dbq.owned_count(connection, unique)
                source = dbq.source_of(connection, "part", unique)
                dbq.set_part_count(connection, unique, current + 1, "pick")
                self._recorded = (unique, current, source)
                self.pick_feedback.setStyleSheet(
                    theme.text("small", theme.GOOD))
                self.pick_feedback.setText(f"Recorded: {hit[0]} (+1)")
                # A reward the reading could not name still cost a relic.
                self._spend_relic(connection)
        finally:
            connection.close()

        self.pick_manual.clear()
        if forma:
            self._confirm_forma(FORMA_NAME if looks_like_forma(hit[0])
                                else hit[0])
            return
        self._mark_answered()
        self._booked_via, self._booked_index = "button", None
        self.recorded.emit()
