"""A name field that offers names while the user types, misspellings included."""
from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import QCompleter, QLineEdit

from wft.data import db as dbq

#: How many names a name field offers at once.
SUGGESTIONS_SHOWN = 20


def suggest_on_typing(edit: QLineEdit, names) -> QStringListModel:
    """Make `edit` offer names while the user types, misspellings included.

    `names` is a callable returning the list to choose from (loaded once by the
    caller, not queried per key). The popup is filled by
    `dbq.name_suggestions` on `textEdited`, so only the user's typing refills
    it; taking a suggestion sets the text and does not. Nothing is written
    from here: what the field's text resolves to is still decided by the
    field's own Save or Enter.
    """
    model = QStringListModel(edit)
    completer = QCompleter(model, edit)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
    edit.setCompleter(completer)

    def refill(text: str) -> None:
        model.setStringList(
            dbq.name_suggestions(text, names(), limit=SUGGESTIONS_SHOWN))
        if model.rowCount():
            completer.complete()

    edit.textEdited.connect(refill)
    return model
