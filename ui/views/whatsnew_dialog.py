"""The "What's new" window: every release, newest first, the unseen ones marked.

Shown once after an update (`start`), and again from the Settings footer. It is
not modal: the program behind it keeps working. Closing it, by OK or otherwise,
records the newest release as seen.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from ui import geometry, theme
from ui.widgets import dialog_size
from wft import whatsnew

#: How the heading of a release reads: "1.2" while it has no date.
def heading_text(release: whatsnew.Release) -> str:
    return f"{release.version} — {release.date}" if release.date else release.version


def is_fresh_install(cfg) -> bool:
    """True when neither the database nor the settings file exists yet.

    Asked before the window is built: building it creates both. A person who
    has either one is updating, even from a version that stored no marker.
    """
    return not (Path(cfg.db_path).exists() or Path(geometry.settings_file()).exists())


def decide(fresh: bool, seen: str, current: str) -> str:
    """"silent" (mark as seen, show nothing), "show" or "none"."""
    if not seen:
        return "silent" if fresh else "show"
    return "show" if whatsnew.is_newer(current, seen) else "none"


def start(window, fresh: bool):
    """After the main window is up: show the window if there is news.

    Returns the dialog (kept alive by the caller's window) or None. Never called
    by the self-test, the guide pictures or the release script.
    """
    seen = geometry.whatsnew_seen()
    current = whatsnew.newest().version
    what = decide(fresh, seen, current)
    if what == "silent":
        geometry.set_whatsnew_seen(current)
        return None
    if what == "none":
        return None
    dialog = WhatsNewDialog(window, seen)
    window._whatsnew_dialog = dialog
    QTimer.singleShot(0, dialog.show)
    return dialog


class WhatsNewDialog(QDialog):
    def __init__(self, parent=None, seen: str | None = None, releases=None):
        super().__init__(parent)
        self.setWindowTitle("What's new")
        self.setModal(False)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        dialog_size.open_at(self, 34, 30)
        if seen is None:
            seen = geometry.whatsnew_seen()
        every = list(releases) if releases is not None else list(whatsnew.releases())
        self.releases = every
        self.newest = every[0].version
        self.new_versions = {r.version for r in whatsnew.unseen(seen)
                             if r.version in {x.version for x in every}}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(theme.px(1.1), theme.px(0.95),
                                  theme.px(1.1), theme.px(0.95))
        layout.setSpacing(theme.space("close"))

        title = QLabel("What's new")
        title.setStyleSheet(theme.text("section", weight=600))
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(scroll, 1)
        body = QWidget()
        scroll.setWidget(body)
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, theme.px(0.8), 0)
        inner.setSpacing(theme.space("step"))

        self.headings: list[QLabel] = []
        self.badges: dict[str, QLabel] = {}
        for release in every:
            row = QHBoxLayout()
            head = QLabel(heading_text(release))
            head.setStyleSheet(theme.text("body", weight=600))
            self.headings.append(head)
            row.addWidget(head)
            if release.version in self.new_versions:
                badge = QLabel("new")
                badge.setStyleSheet(theme.text("micro", theme.ACCENT, 600))
                self.badges[release.version] = badge
                row.addWidget(badge)
            row.addStretch(1)
            inner.addLayout(row)
            for line in release.lines:
                item = QHBoxLayout()
                item.setSpacing(theme.space("close"))
                bullet = QLabel("•")
                bullet.setStyleSheet(f"color: {theme.TEXT_DIM};")
                bullet.setAlignment(Qt.AlignTop | Qt.AlignRight)
                text = QLabel(line)
                text.setWordWrap(True)
                item.addWidget(bullet, 0, Qt.AlignTop)
                item.addWidget(text, 1)
                inner.addLayout(item)
        inner.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.ok = QPushButton("OK")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept)
        buttons.addWidget(self.ok)
        layout.addLayout(buttons)

        self.finished.connect(self._mark_seen)

    def _mark_seen(self, _result=0) -> None:
        geometry.set_whatsnew_seen(self.newest)


def open_from_settings(parent) -> WhatsNewDialog:
    """The Settings button: the same window, nothing marked as new once seen."""
    dialog = WhatsNewDialog(parent)
    dialog.show()
    parent._whatsnew_dialog = dialog
    return dialog
