"""One reward card for the relic panel.

Deliberately minimal: a big position number, the item name, and at most three
badges. No scores, no percentages, no buttons on
the card itself — colour carries the message, the recommendation gets a heavy
border. Has to be readable from a few metres away on a 4K screen, so every
font size here is large by normal desktop standards on purpose.
"""
from __future__ import annotations

from ui import theme
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from wft.relic.score import DUCAT_THRESHOLD, Reward, Tier, looks_like_forma

def _badge_colour(key: str) -> str:
    """The colour for one badge, asked of the theme at paint time.

    A table of hex values written next to the class was a table of the Orokin
    theme's colours under another name: switching to Light left "4/4" in a
    pale green on white. Asked rather than stored, because the theme's globals
    are rebound when it changes and a dictionary built at import time is not.
    """
    return {
        "text": theme.TEXT, "bad": theme.BAD, "warn": theme.WARN,
        "good": theme.GOOD, "vaulted": theme.VAULTED, "dim": theme.TEXT_DIM,
        "junk": theme.JUNK,
    }.get(key, theme.TEXT)


def mastery_line(reward: Reward) -> tuple[str, str] | None:
    """(text, colour-key) of the mastery fact, or None. One place for the words:
    the card's detail lines and the reward overlay both use it."""
    if reward.item_mastered:
        # Not dimmed. "Already mastered" is the single fact that decides
        # whether a reward is worth taking for anything but ducats, and grey
        # is the colour this card uses for detail nobody has to read.
        return ("already mastered", "warn")
    if reward.item_name:
        return ("not mastered yet", "good")
    return None


def ducat_line(reward: Reward) -> tuple[str, str] | None:
    """(text, colour-key) of the ducat value, or None. Also the text of the
    ducat badge (`badges_for`), so the three places cannot word it differently."""
    if reward.ducats:
        return (f"{reward.ducats} ducats", "dim")
    return None


def overlay_lines(reward: Reward) -> list[str]:
    """Up to three short lines for the reward overlay, under the card.

    Not a second formulation: the ducat line and the mastery line are the
    card's own (`ducat_line`, `mastery_line`), the third is the card's badges
    (`badges_for`) joined, without the ducat badge that the first line repeats.
    A reward that is no prime part gets its badge alone ("FORMA").
    """
    ducats = ducat_line(reward)
    facts = []
    if reward.part_unique is not None and reward.tier is not Tier.JUNK:
        facts = [line for line in (ducats, mastery_line(reward)) if line]
    lines = [text for text, _key in facts]
    badges = [text for text, _key in badges_for(reward)
              if not (ducats and text == ducats[0])
              and text != "NOT RECOGNISED"]
    if badges:
        lines.append("  ·  ".join(badges))
    return lines[:3]


def details_for(reward: Reward) -> list[tuple[str, str]]:
    """Up to four lines of why, under the name.

    Facts, not scores. "3/4 pieces" is something to act on in four seconds;
    a number out of a hundred is not, which is the rule the badges follow too.
    """
    if reward.part_unique is None or reward.tier is Tier.JUNK:
        return []

    lines: list[tuple[str, str]] = []
    if reward.item_name:
        progress = (f"  ·  {reward.set_owned}/{reward.set_total} pieces"
                    if reward.set_total else "")
        # What the set is short of, but only on a card that is not it. A
        # duplicate over "3/4 pieces" reads as progress towards that fraction,
        # and it is not: the third Lavos Prime Chassis Blueprint left the set
        # exactly where it was, and the piece it wanted was Systems.
        # Only where the fraction is a to-do. A built or mastered set is not
        # being assembled, so "needs 4 other pieces" under a finished weapon
        # is a shopping list for something nobody is shopping for.
        short = ""
        if (not reward.is_missing and reward.set_missing
                and not reward.item_built and not reward.item_mastered
                and 0 < reward.set_owned < reward.set_total):
            short = ("  ·  needs " + reward.set_missing[0]
                     if len(reward.set_missing) == 1 else
                     f"  ·  needs {len(reward.set_missing)} other pieces")
        lines.append((f"{reward.item_name}{progress}{short}", "text"))
    # Said the way it would be said out loud. "Mastery still owed" was
    # accurate and meant nothing to the person reading it in four seconds.
    for line in (mastery_line(reward), ducat_line(reward)):
        if line:
            lines.append(line)
    if reward.own_chance >= 0.05:
        lines.append((f"your relics give ~{reward.own_chance:.0%}", "dim"))
    elif reward.active_relics == 1:
        lines.append(("1 relic still drops it", "dim"))
    elif reward.active_relics:
        lines.append((f"{reward.active_relics} relics still drop it", "dim"))
    return lines[:4]


def badges_for(reward: Reward) -> list[tuple[str, str]]:
    """Up to three (text, colour-key) badges — the whole vocabulary the panel
    is allowed to show: no scores, no percentages.
    """
    if reward.part_unique is None:
        if looks_like_forma(reward.raw_text):
            return [("FORMA", "junk")]
        return [("NOT RECOGNISED", "bad")]

    if reward.tier is Tier.JUNK:
        return [("FORMA", "junk")]

    badges: list[tuple[str, str]] = []
    if reward.is_missing:
        badges.append(("MISSING", "bad"))
        # Completing a set is the one thing worth a badge over the fraction:
        # it is the difference between pieces and a Warframe.
        if reward.completes_set:
            badges.append(("COMPLETES SET", "good"))
        elif 0 < reward.set_owned < reward.set_total:
            badges.append((f"{reward.set_owned}/{reward.set_total} parts", "warn"))
        if reward.reach == "unreachable":
            badges.append(("NO OTHER SOURCE", "vaulted"))
        elif reward.reach == "scarce":
            badges.append(("SCARCE", "warn"))
    else:
        headline = "BUILT" if reward.owned_count == 0 and reward.item_built else "OWNED"
        # How many. "OWNED" over a third copy says the same thing as over a
        # first one, and they are not the same offer.
        if headline == "OWNED" and reward.owned_count > 1:
            headline = f"OWNED ×{reward.owned_count}"
        badges.append((headline, "good"))
        if reward.ducats:
            colour = "good" if reward.ducats >= DUCAT_THRESHOLD else "dim"
            badges.append((ducat_line(reward)[0], colour))

    return badges[:3]


#: The name carries the card. Four cards fill the page for the fifteen seconds
#: this panel exists for, and the decision is read at a glance from across a
#: desk - so the name is set at the size the space allows rather than the size
#: a dense table would want.
def _name_style() -> str:
    return theme.text("display", theme.TEXT, weight=600)

#: The recommendation, as a bar rather than a line of small caps.
def _take_style() -> str:
    return (f"font-size: {theme.size('title'):.3f}em; font-weight: 800; "
            f"color: {theme.BG}; letter-spacing: 3px; "
            f"background: {theme.GOOD}; "
            f"border-radius: {theme.RADIUS['small']}px; padding: {theme.px(0.55)}px 0px;")


class RewardCard(QFrame):
    """A single card. `set_reward` (fresh data) or `set_placeholder` (idle)."""

    #: The user says they took this one.
    taken = Signal(int)

    def __init__(self, position: int, parent=None):
        super().__init__(parent)
        self.position = position
        self.setObjectName("RewardCard")
        # A floor for the height only, in ems: 11em is 293px at 20pt, as the 300
        # it was; 18em (480px) took 180px from the page and the briefing above
        # the cards lost a line. No width floor: an explicit minimum replaces
        # the layout's own, and at 10em the button inside, which needs 261px
        # at 20pt, was squeezed to 235. Without it the card is as narrow as
        # its button and its text allow. The cards are made equal by the row
        # (stretch 1 each, which a box layout divides evenly whatever the
        # hints), not by a number of pixels: they were 305 to 630px wide at
        # 20pt because each took its own text's width.
        self.setMinimumHeight(theme.px(11))
        self.setMaximumHeight(theme.px(42))
        self.setFrameShape(QFrame.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.px(1.2), theme.px(0.95),
                                  theme.px(1.2), theme.px(1.1))
        layout.setSpacing(theme.space("tight") + 1)

        # Number and picture on one line. The game shows four tiles with these
        # icons on them; matching a picture to a picture is faster from three
        # metres than matching a name to a name, which is the same argument as
        # the verdict banner one level down.
        head = QHBoxLayout()
        head.setSpacing(theme.px(0.5))
        self.number_label = QLabel(str(position))
        self.number_label.setStyleSheet(
            theme.text("display", theme.TEXT_FAINT, weight=700))
        head.addWidget(self.number_label)
        head.addStretch(1)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignRight | Qt.AlignTop)
        self.picture.setVisible(False)
        head.addWidget(self.picture)
        layout.addLayout(head)

        self.name_label = QLabel("")
        self.name_label.setWordWrap(True)
        # Big, because the card has the whole page and four seconds of
        # attention. At 1.7em the name was a caption on an empty rectangle.
        self.name_label.setStyleSheet(_name_style())
        layout.addWidget(self.name_label)

        # The empty middle used to say nothing, and the badges alone answer
        # "which" without ever saying why. These are the facts behind the
        # badges, in the order they matter to the decision: what set it feeds
        # and how far along that set is, whether the mastery is still owed,
        # what it is worth in ducats, and whether a relic on hand already
        # drops it.
        #
        # Four lines, not a table. The panel has about four seconds of
        # attention and everything added to it is taken from that.
        self.detail_labels: list[QLabel] = []
        for _ in range(4):
            label = QLabel("")
            label.setWordWrap(True)
            label.setStyleSheet(f"font-size: 1.100em; color: {theme.TEXT_DIM};")
            label.setVisible(False)
            layout.addWidget(label)
            self.detail_labels.append(label)

        layout.addStretch(1)

        self.badge_labels: list[QLabel] = []
        for _ in range(3):
            label = QLabel("")
            label.setStyleSheet("font-size: 1.450em; font-weight: 700;")
            label.setVisible(False)
            layout.addWidget(label)
            self.badge_labels.append(label)

        # No second banner. The page's banner is the answer; repeating it
        # on the card, above a green border that says the same thing again, is
        # one statement wearing three marks. The border and the dimming of the
        # other three are what makes this card the answer's reason.

        # The card is mostly empty space and the thing to do with it was a
        # small numbered button in a row underneath. Recording the pick is the
        # one action on this page, so it goes where the eye already is: on the
        # card, the size of the card.
        self.took_button = QPushButton(f"I took this   ({position})")
        self.took_button.setCursor(Qt.PointingHandCursor)
        self.took_button.setMinimumHeight(theme.px(3))
        self.took_button.clicked.connect(lambda: self.taken.emit(self.position))
        self.took_button.setVisible(False)
        layout.addWidget(self.took_button)

        self._set_border(theme.BORDER, 1)

    def _set_border(self, color: str, width: int, bg: str | None = None):
        self.setStyleSheet(
            f"#RewardCard {{ background-color: {bg or theme.BG_CARD}; "
            f"border: {width}px solid {color}; "
            f"border-radius: {theme.RADIUS['large']}px; }}")

    def set_placeholder(self):
        self.picture.setVisible(False)
        self.number_label.setText(str(self.position))
        self.name_label.setText("—")
        for label in self.detail_labels:
            label.setVisible(False)
        self.took_button.setVisible(False)
        self.name_label.setStyleSheet(theme.text("display", theme.TEXT_FAINT, weight=600))
        for label in self.badge_labels:
            label.setVisible(False)
        self._set_border(theme.BORDER, 1)

    #: How large the icon is on a card, in ems. Big enough to recognise across
    #: a desk, small enough that the name and the badges stay the decision.
    PICTURE_EM = 3.2

    def show_picture(self, connection, reward: Reward, chosen: bool) -> None:
        """Put the item's own icon on the card, if there is one.

        The part's picture where the part has one and the item's otherwise: a
        component often carries a generic blueprint texture, and forty
        identical blueprint icons say nothing.

        Cards that are not the recommendation dim their picture with the rest
        of the card - four icons at equal brightness would undo the hierarchy
        the dimming exists to create.
        """
        from PySide6.QtGui import QPixmap
        from ui.widgets import thumbs

        icon = None
        if reward.part_unique:
            icon = thumbs.icon(connection, "part", reward.part_unique)
        if icon is None and getattr(reward, "item_unique", None):
            icon = thumbs.icon(connection, "item", reward.item_unique)
        if icon is None:
            self.picture.setVisible(False)
            return
        side = theme.px(self.PICTURE_EM)
        pixmap = icon.pixmap(side, side)
        if not chosen:
            faded = QPixmap(pixmap.size())
            faded.fill(Qt.transparent)
            from PySide6.QtGui import QPainter

            painter = QPainter(faded)
            painter.setOpacity(0.38)
            painter.drawPixmap(0, 0, pixmap)
            painter.end()
            pixmap = faded
        self.picture.setPixmap(pixmap)
        self.picture.setVisible(True)

    def set_reward(self, reward: Reward, is_best: bool, is_alternative: bool):
        self.number_label.setText(str(reward.position))
        name = reward.name or reward.raw_text or "?"
        self.name_label.setText(name)
        # The recommendation is the bright one. Four names at equal weight
        # meant the green border had to carry the whole answer on its own, and
        # a border is what you notice second - after reading four names to
        # find out which one it is around.
        chosen = is_best or is_alternative
        self.name_label.setStyleSheet(_name_style() if chosen else
                                      theme.text("display", theme.TEXT_DIM,
                                                 weight=600))

        details = details_for(reward)
        for index, label in enumerate(self.detail_labels):
            if index < len(details):
                text, key = details[index]
                label.setText(text)
                label.setStyleSheet(
                    f"font-size: 1.100em; color: {_badge_colour(key)};")
                label.setVisible(True)
            else:
                label.setVisible(False)

        badges = badges_for(reward)
        for index, label in enumerate(self.badge_labels):
            if index < len(badges):
                text, key = badges[index]
                label.setText(text)
                label.setStyleSheet(
                    f"font-size: 1.100em; font-weight: 700; color: {_badge_colour(key)};")
                label.setVisible(True)
            else:
                label.setVisible(False)

        self.took_button.setText(f"I took this   ({reward.position})")
        self.took_button.setVisible(True)
        if is_best:
            self._set_border(theme.GOOD, 5)
        elif is_alternative:
            self._set_border(theme.WARN, 3)
        else:
            self._set_border(theme.BORDER, 1)

    #: How long a card takes to fade, and how long each one waits behind the
    #: one to its left. Movement only on a change of state, never in a loop,
    #: and short enough that it is felt rather than watched: the fifteen
    #: seconds this panel exists for cannot be spent on an entrance.
    FADE_MS = 150
    STAGGER_MS = 55

    def set_stale(self, stale: bool):
        """Grey out a leftover result the moment a new trigger fires.

        Faded rather than switched. The hard jump read as a redraw - as though
        something had gone wrong - where a fade reads as the answer stepping
        back, which is what it is: the previous verdict is still true and no
        longer current.
        """
        # QLabel and QFrame ignore CSS opacity, so this is a graphics effect.
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        effect = self.graphicsEffect()
        if not isinstance(effect, QGraphicsOpacityEffect):
            effect = QGraphicsOpacityEffect(self)
            effect.setOpacity(1.0)
            self.setGraphicsEffect(effect)

        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(self.FADE_MS)
        animation.setStartValue(effect.opacity())
        animation.setEndValue(0.35 if stale else 1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        # Kept on the card: a QPropertyAnimation with no owning reference is
        # collected mid-flight and the card stops halfway.
        self._fade = animation
        animation.start()

    def appear(self, index: int = 0):
        """Fade in when a result arrives, one card behind the next.

        Left to right, which is the order the game draws them and therefore
        the order the eye is already moving in. The verdict banner does not
        wait for this - the answer appears at once, and the reasons arrive
        behind it.
        """
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QGraphicsOpacityEffect

        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b"opacity", self)
        animation.setDuration(self.FADE_MS)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.OutCubic)
        self._fade = animation
        from PySide6.QtCore import QTimer

        QTimer.singleShot(index * self.STAGGER_MS, animation.start)
