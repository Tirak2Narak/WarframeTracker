"""Does the interface survive being used?

The self-test builds every page and walks between them, which catches a page
that cannot be constructed and nothing else. This drives the parts a person
actually touches: it presses the controls, in the states they are reachable
in, against databases that are empty, broken and full.

It found three faults the first time it ran, all of them in code written the
same day: a database that was not a database took the whole application down
through the Settings page, which was the one view built without a net.

Run: python3 tests/test_ui_smoke.py
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import testdb  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from ui.widgets.filter_chips import ALL as ALL_KEY  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QListWidget,
    QMessageBox, QPushButton, QTabWidget, QTableWidget, QToolButton,
)

#: Controls that reach outside the machine or throw work away. Pressed by a
#: person on purpose; not pressed by a test that presses everything.
LEAVE_ALONE = ("update now", "force re-download", "remove everything",
               "forget all", "fetch", "restore", "trigger", "save capture",
               "discard", "take this import back", "refresh the board",
               "look up platinum prices",
               # Setup: the download reaches DE's server; choosing or testing a
               # key waits on the real keyboard; "use this file" writes the
               # configuration. Each is driven by its own check below.
               "download game data", "update game data", "choose a key",
               "test the key", "use this file", "stop testing",
               # Settings, Reset: driven by their own check, with the trash and
               # the restart replaced.
               "delete my inventory", "delete everything")


def _offline() -> None:
    """No test reaches anybody else\u2019s server.

    The fissure page asks warframestat.us the first time it is refreshed, and
    this suite refreshes it. A test suite that depends on somebody else being
    up is a test suite that fails on a train - and it would be sending them a
    request every time anybody runs it.
    """
    from wft.world import fissures as fissures_mod

    def refuse(*_args, **_kwargs):
        raise fissures_mod.FissureError("offline in tests")

    # The real function is kept reachable: one check drives its fallback
    # wiring with both sources stubbed, which is as offline as this stub.
    refuse.replaced = fissures_mod.active
    fissures_mod.active = refuse

    # warframe.market, for the same reason and one more: the ducat page now
    # offers a lookup over four dozen spare parts, and their rules are
    # explicit about unattended traffic. Stubbed at the one function that
    # opens a socket, so anything that would have reached them fails here
    # instead - a page that fetches on sight is a bug this catches.
    from wft.market import client as market_mod

    def unreachable(*_args, **_kwargs):
        raise market_mod.MarketError("offline in tests")

    market_mod._get = unreachable

    # And DE's weekly riven file. The riven page fetches once per program run
    # when the current week is missing, and every fresh test database is
    # missing every week - so without this stub the suite would send DE one
    # request per walked case.
    from wft.riven import prices as prices_mod

    def no_week(*_args, **_kwargs):
        raise prices_mod.RivenError("offline in tests")

    no_week.replaced = prices_mod.refresh_if_stale
    prices_mod.refresh_if_stale = no_week

    # And the profile address. The Import page has a button that fetches
    # it, the walk presses every button, and a config may hold a
    # real account id: this endpoint once locked a player out of the game. The
    # checks of the fetch itself put the real function back for their own run,
    # behind a stub of urlopen.
    from wft.profile import fetch as profile_fetch_mod

    def no_profile(*_args, **_kwargs):
        return profile_fetch_mod.Fetched(None, error="offline in tests")

    no_profile.replaced = profile_fetch_mod.request
    profile_fetch_mod.request = no_profile

    # And DE's content host. Pictures are fetched by themselves after an
    # applied import, and several checks apply one; the walk presses the reload
    # button. The checks of the picture fetch put the real function back behind
    # their own counting stand-in.
    from wft.data import pictures as pictures_mod

    def no_picture(*_args, **_kwargs):
        raise OSError("offline in tests")

    no_picture.replaced = pictures_mod._fetch
    pictures_mod._fetch = no_picture

    # And the two requests of the reference data update: DE's manifest
    # index and the drop-table page. The Settings page presses "Update now" in
    # the walk, and the clock starts the same update by itself.
    from wft.data import publicexport as pe_mod
    from wft.data import vaulted as vaulted_mod

    def no_reference(*_args, **_kwargs):
        raise OSError("offline in tests")

    no_reference.replaced = (pe_mod._fetch, vaulted_mod._download)
    pe_mod._fetch = no_reference
    vaulted_mod._download = no_reference


def _quiet() -> None:
    """Stop dialogs waiting for somebody who is not there."""
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
    QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: "")
    QDialog.exec = lambda self: 0


def _settle(app) -> None:
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    for _ in range(6):
        app.processEvents()


def _press_everything(app, page, label: str, problems: list) -> int:
    pressed = 0
    for kind in (QPushButton, QToolButton, QCheckBox):
        for widget in page.findChildren(kind):
            if not (widget.isVisible() and widget.isEnabled()):
                continue
            text = (widget.text() or "").lower()
            if any(word in text for word in LEAVE_ALONE):
                continue
            try:
                widget.click()
                pressed += 1
                for _ in range(2):
                    app.processEvents()
            except Exception as exc:  # noqa: BLE001 - that is the point
                problems.append(f"{label} / {text[:28]}: "
                                f"{type(exc).__name__}: {exc}")
    for box in page.findChildren(QComboBox):
        if not box.isVisible():
            continue
        for index in range(min(3, box.count())):
            try:
                box.setCurrentIndex(index)
                app.processEvents()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label} / combo: {type(exc).__name__}: {exc}")
    for listing in page.findChildren(QListWidget):
        if not listing.isVisible():
            continue
        for row in range(min(6, listing.count())):
            try:
                listing.setCurrentRow(row)
                app.processEvents()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label} / list: {type(exc).__name__}: {exc}")
    for table in page.findChildren(QTableWidget):
        if not (table.isVisible() and table.rowCount()):
            continue
        for row in range(min(3, table.rowCount())):
            try:
                table.cellClicked.emit(row, 0)
                app.processEvents()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{label} / table: {type(exc).__name__}: {exc}")
    return pressed


def _walk(app, cfg, problems: list) -> int:
    from ui.app import build_application

    _app, window = build_application(cfg)
    window.resize(1500, 950)
    window.show()
    _settle(app)
    pressed = 0
    for name in window.NAV_ITEMS:
        try:
            window.select(name)
            _settle(app)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"opening {name}: {type(exc).__name__}: {exc}")
            continue
        page = window.stack.currentWidget()
        tabs = page.findChildren(QTabWidget)
        if tabs:
            for tab in tabs:
                for index in range(tab.count()):
                    tab.setCurrentIndex(index)
                    _settle(app)
                    pressed += _press_everything(
                        app, page, f"{name}/{tab.tabText(index)}", problems)
        else:
            pressed += _press_everything(app, page, name, problems)
    window.close()
    return pressed


def _check_manual_accepts_refinement(problems: list) -> None:
    """What the field approves is what the review writes.

    These were two rules until they disagreed in front of the user. The card
    compared with `casefold`; applying the review looked the same text up with
    SQL equality, which is case-sensitive on TEXT. Typing "Neo c9 relic
    [radiant]" - the shape the placeholder asks for - turned the card green, and
    the end of the review said that it matched nothing and could not be written.

    So both sides call `resolve_typed_name` now, and this drives them together:
    every case asserts the card's verdict, and then asserts that the same call
    yields a row the database really holds. A green tick that cannot be written
    is the fault being pinned, so proving the tick is not enough.

    The abbreviations count as well: "neo c9 radi" should be enough to say what
    a tile plainly showed, without typing brackets.
    """
    import pathlib
    import tempfile

    from wft.data import db as dbq
    from ui.widgets.review_widget import ReviewWidget

    home = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(home / "warframe.sqlite")
    for suffix in ("Bronze", "Silver", "Gold", "Platinum"):
        for era, code in (("Neo", "C9"), ("Meso", "B9")):
            connection.execute(
                "INSERT INTO relics(unique_name, name, tier) VALUES(?,?,?)",
                (f"/R/{era}{code}{suffix}", f"{era} {code} Relic", era))
    connection.execute(
        "INSERT INTO items(unique_name, name, category) "
        "VALUES('W:braton', 'Braton Prime', 'weapon')")
    connection.execute(
        "INSERT INTO parts(unique_name, name, item_unique, count_needed) "
        "VALUES('P:barrel', 'Braton Prime Barrel', 'W:braton', 1)")
    connection.commit()
    known = sorted({row[0] for row in connection.execute(
        "SELECT name FROM parts UNION SELECT name FROM items "
        "UNION SELECT name FROM relics")})

    def resolve(text):
        return dbq.resolve_typed_name(connection, text, known)

    widget = ReviewWidget("Neo C9 Relic", 1,
                          [("Neo C9 Relic", 100.0, None, None, "x")],
                          known=known, resolve=resolve, count=2)
    widget.manual_radio.setChecked(True)
    #: text -> the identifier it must be written under, or None for a refusal.
    #: A text whose name half is a catalogue name outright must also come back
    #: marked exact, whatever its case. That is the fault itself: SQL equality
    #: on TEXT is case-sensitive, so "neo c9 relic" was not a name to the
    #: writer while it was one to the card. Resolving it by resemblance
    #: instead would hide that again.
    named_outright = {"Neo C9 Relic", "NEO C9 RELIC", "Neo c9 relic [radiant]",
                      "Meso B9 Relic [Exceptional]", "Braton Prime Barrel",
                      "braton prime barrel"}
    cases = [
        ("Neo C9 Relic", "/R/NeoC9Bronze"),
        ("NEO C9 RELIC", "/R/NeoC9Bronze"),
        ("Neo c9 relic [radiant]", "/R/NeoC9Platinum"),
        ("neo c9 radi", "/R/NeoC9Platinum"),
        ("Meso B9 Relic [Exceptional]", "/R/MesoB9Silver"),
        ("meso b9 flaw", "/R/MesoB9Gold"),
        ("Braton Prime Barrel", "P:barrel"),
        ("braton prime barrel", "P:barrel"),
        # Names too many things to be an answer, and pure noise. Both must be
        # refused rather than guessed: an unresolved name is reported back to
        # the user, an invented one lands in the inventory.
        ("Relic", None),
        ("Nonsense [Radiant]", None),
    ]
    # the manual field offers names while typing, typos included
    widget.manual_edit.textEdited.emit("braton prim barel")
    offered = widget._manual_model.stringList()
    if "Braton Prime Barrel" not in offered[:3]:
        problems.append(f"manual name popup offers {offered}")
    widget.manual_edit.textEdited.emit("xyzzy")
    if widget._manual_model.stringList():
        problems.append("manual name popup offered names for nonsense")
    for text, expected in cases:
        wanted = expected is not None
        widget.manual_edit.setText(text)
        if widget.manual_is_resolvable() != wanted:
            problems.append(
                f"manual name {text!r}: accepted={not wanted}, should be "
                f"{wanted} — hint said {widget.manual_hint.text()!r}")
        found = resolve(text)
        if (found.unique_name if found else None) != expected:
            problems.append(
                f"manual name {text!r}: would be written as "
                f"{found.unique_name if found else None}, expected {expected}")
        if found is not None and (text in named_outright) != found.exact:
            problems.append(
                f"manual name {text!r}: exact={found.exact}, and it "
                f"{'names' if text in named_outright else 'does not name'} "
                f"something outright")
        # A tick the writer cannot honour is the whole fault. Green here has
        # to mean a row that exists.
        if found is not None and not connection.execute(
                "SELECT 1 FROM relics WHERE unique_name = ? "
                "UNION ALL SELECT 1 FROM parts WHERE unique_name = ? "
                "UNION ALL SELECT 1 FROM items WHERE unique_name = ?",
                (found.unique_name,) * 3).fetchone():
            problems.append(f"manual name {text!r}: approved, but "
                            f"{found.unique_name} is in no table")
    widget.deleteLater()
    connection.close()
    print("manual names       : "
          f"{len(cases)} case(s) checked, card and writer agree")


def _check_a_question_keeps_its_refinement(problems: list) -> None:
    """A relic answered by hand keeps the refinement the scan saw.

    The scan gathers a refinement across every hypothesis a cell produced and
    stores it on the candidate - which is why "Axi A21 Relic [Radiant]" came
    out right while the reading shown beside it was only "AXi A21 Relic". The
    apply step read it back out of that one reading instead, so the same tile
    answered as a question was recorded Intact: the same relic, the wrong
    stack, and a fifth of the odds on its rare slot.

    Unanimity is required, because a cluster is one answer for every tile in
    it. Tiles that disagree fall back to the reading, which is what this did
    before and is no worse than it was.
    """
    from types import SimpleNamespace

    from ui.views.import_view import _cluster_refinement

    def cluster(reading, *refinements):
        return SimpleNamespace(
            reading=reading,
            candidates=[SimpleNamespace(refinement=r) for r in refinements])

    cases = [
        # The fault: the tiles knew, the reading did not.
        (cluster("oY Neo C9 Relic", "RADIANT", "RADIANT"), "RADIANT"),
        # No tile knew, but the reading carried it. Unchanged.
        (cluster("Neo C9 Relic [Radiant]", None, None), "RADIANT"),
        # Tiles disagree: the reading decides, as it always did.
        (cluster("Neo C9 Relic [Flawless]", "RADIANT", "INTACT"), "FLAWLESS"),
        # Nobody knows anything, and nothing is invented.
        (cluster("oY Neo C9 Relic", None), None),
    ]
    for group, wanted in cases:
        got = _cluster_refinement(group)
        if got != wanted:
            problems.append(
                f"refinement: reading {group.reading!r} with tiles "
                f"{[c.refinement for c in group.candidates]} gave {got}, "
                f"expected {wanted}")
    print(f"question refinement: {len(cases)} shape(s), tiles outrank the "
          f"one reading")


def _check_ocr_confusions_pick_the_right_numeral(problems: list) -> None:
    """A reading is explained by what OCR trades, not by raw character distance.

    Five Requiem relics sit in one row of the inventory and their names differ
    by a roman numeral: I, II, III, IV. Tesseract renders "III" as "Ill" about
    as often as it gets it right, and against the raw names that reading is
    closer to "Requiem I Relic" (93.8) than to the truth (88.2) - one character
    wrong is one character wrong, whichever character it is.

    Collapse the substitutions first and the answer is unambiguous: "requiem
    iii reiic" against "Requiem III Relic" is 100 and against "Requiem I Relic"
    is 93.8. Measured on a real relics capture: the tile holding ten
    Requiem II read as "Requiem Il Relic", resolved to Requiem I - a name
    another tile had already claimed - and never reached the database at all.
    """
    from wft.inventory import scan as scan_mod

    names = ["Requiem Eterna Relic", "Requiem I Relic", "Requiem II Relic",
             "Requiem III Relic", "Requiem IV Relic", "Neo C9 Relic",
             "Lith O4 Relic", "Lith Z4 Relic", "Ash", "Bo"]
    cases = [
        ("Requiem Ill Relic", "Requiem III Relic"),
        ("Requiem Il Relic", "Requiem II Relic"),
        ("Requiem lll Relic", "Requiem III Relic"),
        # The ones that already read correctly must stay correct: a rescue
        # that rewrites a perfect reading is worse than no rescue.
        ("Requiem I Relic", "Requiem I Relic"),
        ("Requiem II Relic", "Requiem II Relic"),
        ("Requiem III Relic", "Requiem III Relic"),
        ("Requiem IV Relic", "Requiem IV Relic"),
        ("Requiem Eterna Relic", "Requiem Eterna Relic"),
        # The case the confusion table was built for, still working.
        ("Lith 04 Relic", "Lith O4 Relic"),
        # And the case its comment warns about: an exact reading of a short
        # name must not be traded for its look-alike.
        ("Bo", "Bo"),
    ]
    for reading, wanted in cases:
        got = scan_mod.decide_cell([reading], names, [], set()).name
        if got != wanted:
            problems.append(f"reading {reading!r} decided as {got!r}, "
                            f"should be {wanted!r}")
    print(f"ocr confusions     : {len(cases)} reading(s), roman numerals kept "
          f"apart")


def _check_a_bar_is_a_letter_inside_a_name(problems: list) -> None:
    """The vertical bar carries a numeral; at the edge it carries nothing.

    "Requiem | Relic" is what a tile holding nine Requiem I reads as, sixteen
    times over on a real capture. Stripping the bar as punctuation left
    "Requiem Relic" - the exact name of a resource in the decoy list - so the
    decoy matched at 100 against the relic's 92.9 and the tile was dismissed as
    scenery. Every one of the sixteen readings went the same way and nine
    relics never reached the database.

    The bar is kept only where a word would be. At the edge of a reading it is
    the tile's own border caught by the crop, and carrying it there costs real
    matches: "Xa lueprinit |" scores 84.6 against Xaku Blueprint without the
    trailing bar and 78.6 with it, which was measured as the whole cost of the
    change on `crop_c.png` in the fixtures.
    """
    from wft.inventory import scan as scan_mod
    from wft.relic import match as match_mod

    names = ["Requiem I Relic", "Requiem II Relic", "Requiem III Relic",
             "Xaku Blueprint", "Xaku", "Zenith"]
    #: The decoy that did the damage, and it is a real resource in the game.
    decoys = ["Requiem Relic", "Xaku Prex", "Xiran Blueprint"]
    cases = [
        # The bar stands where the numeral stands, so the numeral survives.
        ("Requiem | Relic", "Requiem I Relic"),
        (", W Requiem | Relic", "Requiem I Relic"),
        ("> Requiem |l Relic", "Requiem II Relic"),
        # At the edge it is the crop's border and must not be paid for.
        ("Xa lueprinit |", "Xaku Blueprint"),
        ("| Zenith", "Zenith"),
    ]
    for reading, wanted in cases:
        got = scan_mod.decide_cell([reading], names, decoys, set()).name
        if got != wanted:
            problems.append(f"reading {reading!r} decided as {got!r}, "
                            f"should be {wanted!r}")
    # And the split itself, so a future change to the pattern is visible here
    # rather than only in a scan nobody runs.
    if match_mod.normalise("Requiem | Relic") != "Requiem | Relic":
        problems.append("normalise dropped a bar standing between words")
    if match_mod.normalise("Xa lueprinit |") != "Xa lueprinit":
        problems.append("normalise kept a bar at the edge of a reading")
    print(f"bar as a letter    : {len(cases)} reading(s), edge told from middle")


def _check_review_keys_stay_on_the_list(app, cfg, problems: list) -> None:
    """Arrow keys move the list; they never answer, and focus never strays.

    Clicking an answer put the focus in the open card. Down then moved the
    radio group (Ignore became Manual), and once the card was swapped for its
    closed row the focus fell to a filter chip and the arrows moved nothing.
    """
    import pickle

    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "scan.pickle"
    if not fixture.exists():
        print("review keys        : skipped, no scan fixture")
        return
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from ui.views.import_view import ImportView

    view = ImportView(cfg)
    view.resize(1900, 1000)
    view.show()
    view.activateWindow()
    QTest.qWaitForWindowActive(view)
    view._hand_over(pickle.loads(fixture.read_bytes()))
    _settle(app)

    def press(key):
        QTest.keyClick(QApplication.focusWidget() or view.window(), key)
        _settle(app)

    def on_list(label):
        got = QApplication.focusWidget()
        if got is not view.review_list:
            problems.append(f"{label}: focus is on "
                            f"{type(got).__name__ if got else 'nothing'}, "
                            f"not the list")

    qs = [r for r, e in enumerate(view.rows) if e["kind"] == "question"]
    if len(qs) < 4:
        problems.append("review keys: fixture has fewer than 4 questions")
        view.close()
        return
    first = qs[1]
    view.review_list.setCurrentRow(first)
    _settle(app)
    card = view._current_review_widget()
    QTest.mouseClick(card.ignore_radio, Qt.LeftButton)
    _settle(app)
    for key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_Down):
        row = view.review_list.currentRow()
        press(key)
        want = row + (1 if key == Qt.Key_Down else -1)
        if view.review_list.currentRow() != want:
            problems.append(f"{Qt.Key(key).name} moved row {row} to "
                            f"{view.review_list.currentRow()}, wanted {want}")
        on_list(f"after {Qt.Key(key).name}")
    answered = view.review_widgets[view.rows[first]["cluster"]]
    if not answered.ignore_radio.isChecked() or answered.manual_radio.isChecked():
        problems.append("arrow keys changed the answer Ignore to something else")

    # Focus in the count field: Up/Down move the list, not the number.
    view.review_list.setCurrentRow(qs[2])
    _settle(app)
    card = view._current_review_widget()
    card.count.setFocus()
    _settle(app)
    before, row = card.count.value(), view.review_list.currentRow()
    press(Qt.Key_Down)
    if card.count.value() != before or view.review_list.currentRow() == row:
        problems.append("Down in the count field changed the number "
                        "instead of moving the list")
    on_list("after Down from the count field")

    # Enter, I and a click on another row all land on the list.
    view.review_list.setCurrentRow(qs[2])
    view.review_list.setFocus()
    _settle(app)
    card = view._current_review_widget()
    QTest.mouseClick(card.ignore_radio, Qt.LeftButton)
    _settle(app)
    press(Qt.Key_Return)
    on_list("after Enter")
    card = view._current_review_widget()
    if card is not None:
        card.radios[0].setFocus() if card.radios else card.ignore_radio.setFocus()
        _settle(app)
        view._on_ignore()
        _settle(app)
        on_list("after I")
    QTest.mouseClick(view.review_list.viewport(), Qt.LeftButton,
                     pos=view.review_list.visualItemRect(
                         view.rows[qs[0]]["item"]).center())
    _settle(app)
    on_list("after a click on another row")

    # M puts the cursor in the name field; Esc gives it back unchanged.
    view.review_list.setCurrentRow(qs[3])
    view.review_list.setFocus()
    _settle(app)
    card = view._current_review_widget()
    view._on_manual()
    _settle(app)
    if QApplication.focusWidget() is not card.manual_edit:
        problems.append("M did not put the focus in the name field")
    press(Qt.Key_Escape)
    on_list("after Esc in the name field")
    if not card.manual_radio.isChecked():
        problems.append("Esc in the name field changed the answer")

    # Left/Right on a focused radio used to tick its neighbour.
    card = view._current_review_widget()
    for radio, label in ((card.ignore_radio, "Ignore"),
                         (card.manual_radio, "Manual")):
        radio.setFocus()
        radio.setChecked(True)
        _settle(app)
        card.manual_radio.setChecked(radio is card.manual_radio)
        card.ignore_radio.setChecked(radio is card.ignore_radio)
        for key in (Qt.Key_Left, Qt.Key_Right):
            QTest.keyClick(radio, key)
            _settle(app)
        if not radio.isChecked():
            problems.append(f"Left/Right changed the answer {label}")

    # The wheel over the count field scrolls the list and never the number.
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    view.review_list.setCurrentRow(qs[0])
    view.review_list.setFocus()
    _settle(app)
    card = view._current_review_widget()
    card.count.setValue(3)
    bar = view.review_list.verticalScrollBar()
    bar.setValue(0)
    centre = QPointF(card.count.rect().center())
    wheel = QWheelEvent(centre, card.count.mapToGlobal(centre), QPoint(0, 0),
                        QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                        Qt.NoScrollPhase, False)
    QApplication.sendEvent(card.count, wheel)
    _settle(app)
    if card.count.value() != 3:
        problems.append("the mouse wheel changed the count")
    if bar.value() == 0:
        problems.append("the mouse wheel over the count did not scroll the list")
    bar.setValue(0)
    view.close()

    # Numbered options (the fixture has none): Up/Down/PageDown go to the
    # list, Left/Right do nothing.
    from PySide6.QtCore import QObject
    from ui.widgets.review_widget import ReviewWidget
    solo = ReviewWidget("Ash Prime", 1, [("Ash Prime Systems", 90.0, None, None),
                                         ("Ash Prime Chassis", 80.0, None, None)])
    solo.show()
    solo.activateWindow()
    QTest.qWaitForWindowActive(solo)
    seen = []
    solo.navigate.connect(seen.append)
    solo.radios[0].setFocus()
    for key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_Left, Qt.Key_Right):
        QTest.keyClick(solo.radios[0], key)
    _settle(app)
    if seen != [int(Qt.Key_Down), int(Qt.Key_Up), int(Qt.Key_PageDown)]:
        problems.append(f"numbered radio reported {seen} for Down, Up, PageDown")
    if not solo.radios[0].isChecked() or solo.radios[1].isChecked():
        problems.append("a key on a numbered radio changed the answer")
    solo.close()
    print("review keys        : arrows move the list, focus stays on it")


def _review_with_questions(app, cfg, readings, doubt="test"):
    """An `ImportView` on its review page with one question per reading.

    Built from real catalogue names so each card has numbered options. One
    sure reading is added as well: the keys must step over it.
    """
    from PySide6.QtTest import QTest
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    parts = connection.execute(
        "SELECT unique_name, name FROM parts WHERE alias_of IS NULL "
        "AND name LIKE '% Prime %' ORDER BY unique_name LIMIT 30").fetchall()
    tiles = []
    for column, reading in enumerate(readings):
        tiles.append(scan_mod.Candidate(
            column=column, row=0, name=reading, score=70.0, reading=reading,
            sightings=3, verified_box=None, quantity=None,
            sources=frozenset({1}), lead=1.0, evidence=None, doubt=doubt))
    sure = parts[0]
    tiles.append(scan_mod.Candidate(
        column=len(readings), row=1, name=sure["name"], score=100.0,
        reading=sure["name"], sightings=3, part_unique=sure["unique_name"],
        verified_box=None, quantity=None, sources=frozenset({1}), lead=30.0,
        evidence=None))
    view = ImportView(cfg)
    view.resize(1900, 1000)
    view.show()
    view.activateWindow()
    QTest.qWaitForWindowActive(view)
    view.scan_result = scan_mod.ScanResult(
        candidates=tiles, frames_read=1, words_total=len(tiles),
        words_after_ui_filter=len(tiles), cells_found=len(tiles), grid=None)
    view._prepare_review()
    view.stack.setCurrentIndex(2)  # PAGE_REVIEW
    _settle(app)
    return view, [p["name"] for p in parts[1:8]]


def _check_review_one_handed_keys(app, cfg, problems: list) -> None:
    """The right hand alone: arrows, Ctrl, Shift, Enter and the number pad.

    Number pad 1-6 pick, 0 ignores, + and - (and Ctrl+Up/Down) change the
    count, Shift+Up/Down/Enter go to the next/previous question. They work
    with the focus on the list and in the open card, and not in the name
    field, where digits and signs are text.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    pad = Qt.KeypadModifier
    view, names = _review_with_questions(
        app, cfg, [n[:-2] for n in ("Ash Prime Systems", "Ash Prime Chassis",
                                    "Ash Prime Neuroptics", "Ash Prime Blueprint",
                                    "Ash Prime Helmet")])

    def press(key, mod=Qt.NoModifier, target=None):
        QTest.keyClick(target or QApplication.focusWidget() or view.window(),
                       key, mod)
        _settle(app)

    qs = [r for r, e in enumerate(view.rows) if e["kind"] == "question"]
    if len(qs) < 5:
        problems.append(f"one-handed keys: only {len(qs)} questions")
        view.close()
        view.deleteLater()
        return
    row = view.review_list.currentRow
    view.review_list.setCurrentRow(qs[0])
    view.review_list.setFocus()
    _settle(app)

    # Number pad 1-6 pick, on the list and with the focus in the card.
    card = view._current_review_widget()
    if len(card.radios) < 3:
        problems.append(f"one-handed keys: card has {len(card.radios)} options")
        view.close()
        view.deleteLater()
        return
    press(Qt.Key_3, pad)
    if not card.radios[2].isChecked():
        problems.append("Num 3 did not pick option 3")
    card.radios[2].setFocus()
    _settle(app)
    press(Qt.Key_2, pad)
    if not card.radios[1].isChecked():
        problems.append("Num 2 with the focus on a radio did not pick option 2")
    card.ignore_radio.setFocus()
    _settle(app)
    press(Qt.Key_1, pad)
    if not card.radios[0].isChecked():
        problems.append("Num 1 with the focus on Ignore did not pick option 1")

    # Count: Num + / - and Ctrl+Up / Down, floor 0, on the list and in the card.
    view.review_list.setFocus()
    _settle(app)
    start = card.count.value()
    press(Qt.Key_Plus, pad)
    press(Qt.Key_Up, Qt.ControlModifier)
    if card.count.value() != start + 2:
        problems.append(f"Num + and Ctrl+Up: count {start} became "
                        f"{card.count.value()}, wanted {start + 2}")
    card.count.lineEdit().setFocus()
    _settle(app)
    press(Qt.Key_Minus, pad)
    press(Qt.Key_Down, Qt.ControlModifier)
    if card.count.value() != start:
        problems.append(f"Num - and Ctrl+Down in the count field: {start} "
                        f"became {card.count.value()}")
    if row() != qs[0]:
        problems.append("a count key moved the list")
    card.radios[0].setFocus()
    _settle(app)
    card.count.setValue(0)
    press(Qt.Key_Minus, pad)
    press(Qt.Key_Down, Qt.ControlModifier)
    if card.count.value() != 0:
        problems.append("the count went below 0")
    press(Qt.Key_Plus, pad)
    press(Qt.Key_Plus, pad)
    index = view.rows[qs[0]]["cluster"]
    view._remember_count(index)
    if view._typed_counts.get(index) != 2:
        problems.append(f"a count set by Num + was not remembered: "
                        f"{view._typed_counts.get(index)}")
    # The main row's + - 0 are not the pad's.
    before = card.count.value()
    press(Qt.Key_Minus, Qt.NoModifier)
    press(Qt.Key_0, Qt.NoModifier)
    if card.count.value() != before or card.ignore_radio.isChecked():
        problems.append("the main row's - or 0 acted like the number pad's")

    # Num 0 ignores and goes on, from the list and from the card.
    view.review_list.setFocus()
    view.review_list.setCurrentRow(qs[1])
    _settle(app)
    card = view._current_review_widget()
    press(Qt.Key_0, pad)
    if not card.ignore_radio.isChecked() or row() != qs[2]:
        problems.append(f"Num 0 on the list: ignored={card.ignore_radio.isChecked()}"
                        f", row {row()} (wanted {qs[2]})")
    card = view._current_review_widget()
    card.radios[0].setFocus()
    _settle(app)
    press(Qt.Key_0, pad)
    if not card.ignore_radio.isChecked() or row() != qs[3]:
        problems.append("Num 0 with the focus in the card did not ignore and move on")

    # Shift+Down / Shift+Up / Shift+Enter: open questions first, sure rows and
    # visited questions are skipped, the key still moves when none is open.
    view.visited.clear()
    view.review_list.setCurrentRow(qs[0])
    view.visited.update({view.rows[qs[0]]["cluster"], view.rows[qs[3]]["cluster"]})
    view.review_list.setFocus()
    _settle(app)
    walk = []
    for _ in range(3):
        press(Qt.Key_Down, Qt.ShiftModifier)
        walk.append(row())
    if walk != [qs[1], qs[2], qs[4]]:
        problems.append(f"Shift+Down went {walk}, wanted {[qs[1], qs[2], qs[4]]}")
    on_list = QApplication.focusWidget() is view.review_list
    if not on_list:
        problems.append("the focus left the list after Shift+Down")
    press(Qt.Key_Down, Qt.ShiftModifier)
    if row() not in (qs[0], qs[1], qs[2], qs[3]):
        problems.append(f"Shift+Down with every question opened stopped on "
                        f"row {row()}")
    view.visited.clear()
    view.visited.add(view.rows[qs[4]]["cluster"])
    view.review_list.setCurrentRow(qs[4])
    view.visited.add(view.rows[qs[4]]["cluster"])
    _settle(app)
    press(Qt.Key_Up, Qt.ShiftModifier)
    if row() != qs[3]:
        problems.append(f"Shift+Up from the last question went to {row()}, "
                        f"wanted {qs[3]}")
    view.visited.update(view.rows[r]["cluster"] for r in qs[1:4])
    press(Qt.Key_Return, Qt.ShiftModifier)
    if row() != qs[0]:
        problems.append(f"Shift+Enter went to {row()}, wanted the open "
                        f"question {qs[0]}")
    view.visited.add(view.rows[qs[0]]["cluster"])
    press(Qt.Key_Enter, Qt.ShiftModifier | pad)
    if row() != qs[4]:
        problems.append(f"Shift+Num Enter with every question opened went to "
                        f"{row()}, wanted the previous one ({qs[4]})")
    # With the focus in the card the same keys move the list.
    view.review_list.setCurrentRow(qs[2])
    _settle(app)
    card = view._current_review_widget()
    card.radios[0].setFocus()
    _settle(app)
    press(Qt.Key_Down, Qt.ShiftModifier)
    if row() == qs[2]:
        problems.append("Shift+Down with the focus on a radio did not move")
    if QApplication.focusWidget() is not view.review_list:
        problems.append("Shift+Down from a card did not leave the focus on the list")

    # Number pad Enter is Enter.
    view.visited.clear()
    view.review_list.setCurrentRow(qs[0])
    view.review_list.setFocus()
    _settle(app)
    press(Qt.Key_Enter, pad)
    if row() != qs[1]:
        problems.append(f"Num Enter went to {row()}, wanted {qs[1]}")

    # The name field keeps digits and signs as text.
    view.review_list.setCurrentRow(qs[3])
    view.review_list.setFocus()
    _settle(app)
    card = view._current_review_widget()
    view._on_manual()
    _settle(app)
    count = card.count.value()
    for key in (Qt.Key_1, Qt.Key_0, Qt.Key_Plus, Qt.Key_Minus):
        QTest.keyClick(card.manual_edit, key, pad)
    _settle(app)
    if card.manual_edit.text() != "10+-":
        problems.append(f"the name field got {card.manual_edit.text()!r} "
                        f"for Num 1, 0, +, -; wanted '10+-'")
    if (card.count.value() != count or card.ignore_radio.isChecked()
            or not card.manual_radio.isChecked() or row() != qs[3]):
        problems.append("a number pad key in the name field acted as a command")

    # A count typed on the number pad: 1 then 0 is ten, not one and Ignore.
    view.review_list.setCurrentRow(qs[4])
    view.review_list.setFocus()
    _settle(app)
    card = view._current_review_widget()
    card.count.setFocus()
    card.count.lineEdit().selectAll()
    _settle(app)
    for key in (Qt.Key_1, Qt.Key_0):
        QTest.keyClick(card.count.lineEdit(), key, pad)
    _settle(app)
    if card.count.value() != 10 or card.ignore_radio.isChecked() \
            or row() != qs[4]:
        problems.append(f"Num 1, Num 0 in the count field gave "
                        f"{card.count.value()}, ignored "
                        f"{card.ignore_radio.isChecked()}, row {row()}; "
                        f"wanted 10 on the same row")
    view.close()
    view.deleteLater()
    print("review one hand    : pad 1-6 0 + -, Ctrl+Up/Down, Shift+Up/Down/Enter")


def _check_a_doubted_word_sends_its_names_last(app, cfg, problems: list) -> None:
    """Names with the word the reading did not carry come after the others.

    Badge icons leave noise letters before a name, and the Ak twin won the fuzzy
    match by under a point ("i A Lex Prime Blueprint": Aklex 90.9, Lex 90.5).
    The scan doubts the word, the card offered the Ak twin first and option 1 is
    preselected. Driven through the real ImportView path.
    """
    from PySide6.QtTest import QTest
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    cases = (("i A Lex Prime Blueprint", "Aklex Prime Blueprint", "Aklex",
              "Lex Prime Blueprint"),
             ("A S | | Magnus Prime Blueprint", "Akmagnus Prime Blueprint",
              "Akmagnus", "Magnus Prime Blueprint"),
             ("I A U Vasto Prime Blueprint", "Akvasto Prime Blueprint",
              "Akvasto", "Vasto Prime Blueprint"))
    view = ImportView(cfg)
    view.resize(1900, 1000)
    view.show()
    QTest.qWaitForWindowActive(view)
    try:
        def tile(column, name, reading, word):
            return scan_mod.Candidate(
                column=column, row=0, name=name, score=88.0, reading=reading,
                sightings=3, verified_box=None, quantity=1,
                sources=frozenset({1}), lead=0.5, evidence=None,
                doubt=(f"the reading does not clearly carry the word {word!r}"
                       if word else "test"),
                doubt_word=word)
        tiles = [tile(i, name, reading, word)
                 for i, (reading, name, word, _want) in enumerate(cases)]
        tiles.append(tile(3, "Akmagnus Prime Blueprint",
                          "Akmagnus Prime Blueprint", None))
        # Truncated readings: the doubted name is the right one and stays first.
        kept = (("ntis Prime Handle", "Afentis Prime Handle", "Afentis"),
                ("AR Prime", "War Prime", "War"))
        for k, (reading, name, word) in enumerate(kept):
            tiles.append(tile(4 + k, name, reading, word))
        view.scan_result = scan_mod.ScanResult(
            candidates=tiles, frames_read=1, words_total=len(tiles),
            words_after_ui_filter=len(tiles), cells_found=len(tiles), grid=None)
        view._prepare_review()
        view.stack.setCurrentIndex(2)
        _settle(app)
        for reading, _name, _word, want in cases:
            cluster = next((c for c in view.clusters if c.reading == reading), None)
            if cluster is None:
                problems.append(f"doubted word: no question for {reading!r}")
                continue
            if cluster.options[0][0] != want:
                problems.append(f"doubted word: option 1 for {reading!r} is "
                                f"{cluster.options[0][0]!r}, wanted {want!r} "
                                f"({[o[0] for o in cluster.options]})")
        for reading, name, _word in kept:
            cluster = next((c for c in view.clusters if c.reading == reading), None)
            if cluster is None:
                problems.append(f"doubted word: no question for {reading!r}")
            elif cluster.options[0][0] != name:
                problems.append(f"doubted word: a truncated reading {reading!r} "
                                f"got {cluster.options[0][0]!r} first, wanted "
                                f"{name!r}")
        plain = next((c for c in view.clusters
                      if c.reading == "Akmagnus Prime Blueprint"), None)
        if plain is not None and plain.options[0][0] != "Akmagnus Prime Blueprint":
            problems.append(f"doubted word: a reading without a doubt was "
                            f"reordered: {[o[0] for o in plain.options]}")
    finally:
        view.close()
        view.deleteLater()


def _check_requiem_relics_keep_their_keys(app, cfg, problems: list) -> None:
    """Requiem I, II, III, IV stand on 1-4 in that order.

    They differ by one numeral and are mixed up, and sorted by score they sat
    on changing keys. Any Requiem among the suggestions brings all four, the
    rest follow, and one that was not suggested shows no invented score.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from ui.views.import_view import requiem_in_order

    want = ["Requiem I Relic", "Requiem II Relic", "Requiem III Relic",
            "Requiem IV Relic"]
    view, _names = _review_with_questions(
        app, cfg, ["Requiem Ill Relic", "Requiem Relic", "Ash Prime Systems"])
    lookup = view._lookup
    if not all(name in lookup for name in want):
        problems.append("requiem: the catalogue has no Requiem relics")
        view.close()
        view.deleteLater()
        return
    mixed = [("Requiem III Relic", 95.0, *lookup["Requiem III Relic"]),
             ("Requiem I Relic", 90.0, *lookup["Requiem I Relic"]),
             ("Requiem Eterna Relic", 80.0, *lookup["Requiem Eterna Relic"]),
             ("Ash Prime Systems", 70.0, *lookup["Ash Prime Systems"])]
    got = requiem_in_order(mixed, lookup)
    if [o[0] for o in got] != want + ["Requiem Eterna Relic", "Ash Prime Systems"]:
        problems.append(f"requiem order: {[o[0] for o in got]}")
    kept = {o[0]: o for o in got}
    if not all(name in kept for name in want):
        kept = {name: (name, 99.0, None, None, None) for name in want}
    if kept["Requiem III Relic"] != mixed[0] or kept["Requiem I Relic"] != mixed[1]:
        problems.append("requiem: a suggested relic lost its score or ids")
    if kept["Requiem II Relic"][1] != 0.0 or kept["Requiem IV Relic"][1] != 0.0:
        problems.append("requiem: a relic that was not suggested got a score")
    if kept["Requiem II Relic"][2:] != tuple(lookup["Requiem II Relic"]):
        problems.append("requiem: a relic that was not suggested lost its ids")
    plain = [("Ash Prime Systems", 70.0, *lookup["Ash Prime Systems"])]
    if requiem_in_order(plain, lookup) != plain:
        problems.append("requiem: options without a Requiem were reordered")
    only_eterna = [("Requiem Eterna Relic", 80.0, *lookup["Requiem Eterna Relic"])]
    if requiem_in_order(only_eterna, lookup) != only_eterna:
        problems.append("requiem: Eterna alone brought the four")

    # Through the real review: every question that offers a Requiem has them
    # on 1-4, and key 3 (and number pad 3) picks Requiem III.
    asked = [c for c in view.clusters
             if any(o[0] in want for o in c.options)]
    if not asked:
        problems.append("requiem: no question offered a Requiem relic")
    for cluster in asked:
        if [o[0] for o in cluster.options[:4]] != want:
            problems.append(f"requiem: {cluster.reading!r} offered "
                            f"{[o[0] for o in cluster.options[:4]]}")
    if asked:
        index = view.clusters.index(asked[0])
        view._focus_question(index)
        view.review_list.setFocus()
        _settle(app)
        card = view._current_review_widget()
        shown = [r.text() for r in card.radios[:4]]
        if not shown[2].startswith("3.  Requiem III Relic"):
            problems.append(f"requiem: card shows {shown}")
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_3, Qt.KeypadModifier)
        _settle(app)
        decision = card.decision()
        if decision is None or decision[-1] != "Requiem III Relic":
            problems.append(f"requiem: key 3 chose {decision}")
        QTest.keyClick(QApplication.focusWidget(), Qt.Key_4)
        _settle(app)
        decision = card.decision()
        if decision is None or decision[-1] != "Requiem IV Relic" or not decision[2]:
            problems.append(f"requiem: key 4 chose {decision}")
    view.close()
    view.deleteLater()
    print("requiem order      : I, II, III, IV on 1-4, rest after")


def _check_a_relic_question_writes_the_stack_the_tile_shows(app, cfg, problems: list) -> None:
    """A relic question offers, ticks and writes the refinement the tiles showed.

    Import #18: tiles read "IV Meso V16 Relic" (Radiant, 4), "VA Meso V17 Relic"
    (Radiant, 2) and "1w Neo K10 Relic" (Exceptional). The scan carried the
    refinement, the cards offered the bare names, option 1 was picked and the
    write went to the Intact stacks while the refined ones were offered as
    "gone?". The path from the pick to the written row is driven here, not the
    helper beside it.
    """
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    names = []
    for family in ("Meso", "Neo", "Axi"):
        row = connection.execute(
            "SELECT name FROM relics WHERE name LIKE ? GROUP BY name "
            "HAVING COUNT(*) = 4 AND SUM(unique_name LIKE '%Platinum') = 1 "
            "AND SUM(unique_name LIKE '%Silver') = 1 "
            "AND SUM(unique_name LIKE '%Bronze') = 1 ORDER BY name LIMIT 1",
            (f"{family} %",)).fetchone()
        if row:
            names.append(row["name"])
    if len(names) < 3:
        problems.append(f"relic question: the catalogue offers only {names}")
        return
    first, second, third = names

    def stack(name, level):
        return dbq.relic_unique_for(connection, name, level)

    saved = connection.execute("SELECT * FROM owned_relics").fetchall()
    connection.execute("DELETE FROM owned_relics")
    connection.commit()

    def reset():
        connection.execute("DELETE FROM owned_relics")
        for unique, count in ((stack(first, None), 10), (stack(second, None), 35),
                              (stack(third, None), 14)):
            connection.execute(
                "INSERT INTO owned_relics(relic_unique, count, source, updated_at)"
                " VALUES(?,?,'video',datetime('now'))", (unique, count))
        connection.commit()

    def tile(column, name, reading, level, quantity):
        return scan_mod.Candidate(
            column=column, row=0, name=name, score=70.0, reading=reading,
            sightings=3, verified_box=None, quantity=quantity,
            sources=frozenset({1}), lead=1.0, evidence=None, doubt="test",
            relic_unique=stack(name, None), refinement=level)

    def view_of(tiles):
        view = ImportView(cfg)
        view.resize(1900, 1000)
        view.show()
        QTest.qWaitForWindowActive(view)
        view.scan_result = scan_mod.ScanResult(
            candidates=tiles, frames_read=1, words_total=len(tiles),
            words_after_ui_filter=len(tiles), cells_found=len(tiles), grid=None)
        view._prepare_review()
        view.stack.setCurrentIndex(2)
        _settle(app)
        return view

    def card_for(view, reading):
        index = next(i for i, c in enumerate(view.clusters)
                     if c.reading == reading)
        return index, view._open_question(index)

    def write(view):
        view._remember_all_counts()
        view._build_apply_table([])
        original = QMessageBox.information
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            view._write_apply_table()
        finally:
            QMessageBox.information = original

    def counts():
        return {(n, lv): dbq.relic_count(connection, stack(n, lv))
                for n in names for lv in (None, "EXCEPTIONAL", "FLAWLESS", "RADIANT")}

    try:
        # 1. A pick. Option 1 carries the refinement and writes that stack;
        #    tiles that disagree keep the old behaviour.
        reset()
        r1, r2, r3 = f"IV {first}", f"VA {second}", f"1w {third}"
        view = view_of([tile(0, first, r1, "RADIANT", 4),
                        tile(1, second, r2, "EXCEPTIONAL", 2),
                        tile(2, third, r3, "RADIANT", 1),
                        tile(3, third, r3, "INTACT", 1)])
        for reading, name, level in ((r1, first, "RADIANT"),
                                     (r2, second, "EXCEPTIONAL")):
            index, card = card_for(view, reading)
            shown = card.options[0][0]
            if shown != dbq.relic_label(name, level) or "[" not in shown:
                problems.append(f"relic question: option 1 for {reading!r} is "
                                f"{shown!r}, wanted {name} with [{level.title()}]")
            if f"[{level.title()}]" not in card.radios[0].text():
                problems.append(f"relic question: the card shows "
                                f"{card.radios[0].text()!r}")
            card.radios[0].setChecked(True)
        index, card = card_for(view, r3)
        if "[" in card.options[0][0]:
            problems.append(f"relic question: tiles that disagree got "
                            f"{card.options[0][0]!r}")
        card.radios[0].setChecked(True)
        write(view)
        view.close()
        view.deleteLater()
        got = counts()
        want = {key: 0 for key in got}
        want[(first, None)] = 10
        want[(first, "RADIANT")] = 4
        want[(second, None)] = 35
        want[(second, "EXCEPTIONAL")] = 2
        want[(third, None)] = 2   # a capture sets the count, it does not add
        if got != want:
            problems.append("relic question: after picking option 1 "
                            + str({k: (got[k], want[k]) for k in got if got[k] != want[k]})
                            + " (got, wanted) differ")

        # 2. Typed. A bare name takes the tiles' refinement and the tick says
        #    so; a typed refinement, Intact included, wins.
        reset()
        view = view_of([tile(0, first, r1, "RADIANT", 4),
                        tile(1, second, r2, "RADIANT", 2)])
        index, card = card_for(view, r1)
        card.manual_radio.setChecked(True)
        card.manual_edit.setText(first.lower())
        hint = card.manual_hint.text()
        label = dbq.relic_label(first, "RADIANT")
        if label not in hint:
            problems.append(f"relic question: the tick for a bare name says {hint!r}")
        index, card = card_for(view, r2)
        card.manual_radio.setChecked(True)
        card.manual_edit.setText(f"{second} [Intact]")
        hint = card.manual_hint.text()
        if "[Radiant]" in hint:
            problems.append(f"relic question: the tick for [Intact] says {hint!r}")
        write(view)
        view.close()
        view.deleteLater()
        got = counts()
        want = {key: 0 for key in got}
        want[(first, None)] = 10
        want[(first, "RADIANT")] = 4
        want[(second, None)] = 2
        want[(third, None)] = 14
        if got != want:
            problems.append("relic question: after typing "
                            + str({k: (got[k], want[k]) for k in got if got[k] != want[k]})
                            + " (got, wanted) differ")

        # 3. A row for a refined stack that was not seen names the refinement.
        reset()
        platinum = stack(first, "RADIANT")
        dbq.set_relic_count(connection, platinum, 4, "video")
        view = ImportView(cfg)
        seen = {("relic", stack(first, None)): {}, ("relic", stack(second, None)): {},
                ("relic", stack(third, None)): {},
                ("relic", stack(second, "RADIANT")): {}}
        dbq.set_relic_count(connection, stack(second, "RADIANT"), 1, "video")
        rows = view._absent_rows(seen)
        view.deleteLater()
        gone = {unique: name for _kind, unique, name, _count, _source in rows}
        if gone.get(platinum) != dbq.relic_label(first, "RADIANT") \
                or "[Radiant]" not in gone.get(platinum, ""):
            problems.append(f"relic question: a gone? row for the Radiant "
                            f"stack reads {gone}")

        # 4. The Requiem relics keep keys 1-4, now with the refinement.
        requiem = [f"Requiem {n} Relic" for n in ("I", "II", "III", "IV")]
        if all(stack(n, "RADIANT") for n in requiem):
            view = view_of([tile(0, "Requiem III Relic", "Requiem Ill Relic",
                                 "RADIANT", 1)])
            index, card = card_for(view, "Requiem Ill Relic")
            offered = [o[0] for o in card.options[:4]]
            wanted = [dbq.relic_label(n, "RADIANT") for n in requiem]
            if offered != wanted:
                problems.append(f"relic question: Requiem keys 1-4 are {offered}")
            view.close()
            view.deleteLater()
    finally:
        connection.execute("DELETE FROM owned_relics")
        for row in saved:
            connection.execute(
                "INSERT INTO owned_relics(relic_unique, count, source, updated_at)"
                " VALUES(?,?,?,?)", (row["relic_unique"], row["count"],
                                      row["source"], row["updated_at"]))
        connection.commit()
        connection.close()
    print("relic refinement   : pick, typed name and gone? row use the stack the tile shows")


def _check_gone_rows_come_from_every_source(app, cfg, problems: list) -> None:
    """A capture proposes "gone?" for rows of any source in a covered category.

    Hand entries and relic picks used to stay for ever because only the video
    source was looked at. Rows in a category the capture does not cover, and
    rows at 0 (mastery only), are never offered; the note names the foundry.
    """
    from ui.views.import_view import ImportView
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)
    part = connection.execute(
        "SELECT unique_name FROM parts WHERE alias_of IS NULL ORDER BY unique_name "
        "LIMIT 1").fetchone()
    item = connection.execute(
        "SELECT unique_name FROM items ORDER BY unique_name LIMIT 1").fetchone()
    if part is None or item is None:
        problems.append("gone rows: the catalogue has no part or item")
        return
    saved = {
        "owned_relics": connection.execute("SELECT * FROM owned_relics").fetchall(),
        "owned_parts": connection.execute(
            "SELECT * FROM owned_parts WHERE part_unique = ?",
            (part["unique_name"],)).fetchall(),
        "owned_items": connection.execute(
            "SELECT * FROM owned_items WHERE item_unique = ?",
            (item["unique_name"],)).fetchall(),
    }
    view = None
    try:
        connection.execute("DELETE FROM owned_relics")
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?",
                           (part["unique_name"],))
        connection.execute("DELETE FROM owned_items WHERE item_unique = ?",
                           (item["unique_name"],))
        seen = {}
        for n in range(8):
            unique = f"/Test/Seen/{n}"
            connection.execute(
                "INSERT INTO owned_relics(relic_unique, count, source) VALUES(?,?,?)",
                (unique, 1, "video"))
            seen[("relic", unique)] = {}
        for unique, count, source in (("/Test/ByHand", 2, "manual"),
                                      ("/Test/Picked", 1, "pick"),
                                      ("/Test/Video", 3, "video"),
                                      ("/Test/Zero", 0, "manual")):
            connection.execute(
                "INSERT INTO owned_relics(relic_unique, count, source) VALUES(?,?,?)",
                (unique, count, source))
        # Outside every covered category: a hand entry for a part, and a
        # mastery-only item at 0.
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source) VALUES(?,2,'manual')",
            (part["unique_name"],))
        connection.execute(
            "INSERT INTO owned_items(item_unique, count, mastered, source) "
            "VALUES(?,0,1,'profile')", (item["unique_name"],))
        connection.commit()
        view = ImportView(cfg)
        rows = view._absent_rows(seen)
        got = {unique: (count, source) for _k, unique, _n, count, source in rows}
        want = {"/Test/ByHand": (2, "manual"), "/Test/Picked": (1, "pick"),
                "/Test/Video": (3, "video")}
        if got != want:
            problems.append(f"gone rows: offered {got!r}, wanted {want!r}")
        # The real table path: unchecked, labelled, ordered, counted.
        from wft.inventory import scan as scan_mod
        view.scan_result = scan_mod.ScanResult(
            candidates=[], frames_read=1, words_total=0,
            words_after_ui_filter=0, cells_found=0, grid=None)
        view._prepare_review()
        view._remember_all_counts()
        view._absent_rows = lambda combined, real=view._absent_rows: real(seen)
        view._built_rows = lambda combined: []
        view._build_apply_table([])
        table = view.apply_table
        kinds = {table.item(r, 2).text(): table.item(r, 1).text()
                 for r in range(table.rowCount())}
        if kinds.get("/Test/ByHand") != "relic, gone? (by hand)" \
                or kinds.get("/Test/Picked") != "relic, gone? (picked)" \
                or kinds.get("/Test/Video") != "relic, gone?":
            problems.append(f"gone rows: the kind column reads {kinds!r}")
        if any(row["checkbox"].isChecked() for row in view.apply_rows):
            problems.append("gone rows: a gone? row starts checked")
        if not view.apply_note.text().startswith("3 row(s) at the top"):
            problems.append(f"gone rows: the count line reads {view.apply_note.text()[:80]!r}")
        if "foundry" not in view.apply_note.text():
            problems.append("gone rows: the note does not mention the foundry")
    finally:
        if view is not None:
            view.deleteLater()
        connection.execute("DELETE FROM owned_relics")
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?",
                           (part["unique_name"],))
        connection.execute("DELETE FROM owned_items WHERE item_unique = ?",
                           (item["unique_name"],))
        for table_name, rows_ in saved.items():
            for row in rows_:
                keys = row.keys()
                connection.execute(
                    f"INSERT INTO {table_name}({','.join(keys)}) "
                    f"VALUES({','.join('?' * len(keys))})", tuple(row))
        connection.commit()
        connection.close()
    print("gone rows          : any source in a covered category, labelled, unchecked; none for other categories or count 0")


def _check_absent_rows_are_judged_per_kind_and_category(app, cfg, problems: list) -> None:
    """Coverage and the absent share are judged per (kind, category).

    A category holds built items and parts. A Prime Parts recording can never
    show a built Warframe, so judging the category as one pool made the built
    items "absent" and swallowed every part. Three captures over one shelf:
    parts only (the absent parts are offered, no built item, no "too many"
    note for the parts), built items only (no part), and a Warframes capture
    (no weapon part). The last one is the protection against that.
    """
    from ui.views.import_view import ImportView
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)

    def pick(table, category, count):
        if table == "parts":
            query = ("SELECT p.unique_name FROM parts p JOIN items i "
                     "ON i.unique_name = p.item_unique WHERE p.alias_of IS NULL "
                     "AND i.category = ? ORDER BY p.unique_name LIMIT ?")
        else:
            query = ("SELECT unique_name FROM items WHERE category = ? "
                     "ORDER BY unique_name LIMIT ?")
        return [r[0] for r in connection.execute(query, (category, count))]

    frame_parts = pick("parts", "warframe", 10)
    frame_items = pick("items", "warframe", 10)
    weapon_parts = pick("parts", "primary", 10)
    if min(len(frame_parts), len(frame_items), len(weapon_parts)) < 10:
        problems.append("per-kind absent: the catalogue is too small for the scenario")
        connection.close()
        return
    tables = {"owned_parts": "part_unique", "owned_items": "item_unique",
              "owned_relics": "relic_unique"}
    saved = {t: connection.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    view = None
    try:
        for t in tables:
            connection.execute(f"DELETE FROM {t}")
        for unique in frame_parts + weapon_parts:
            connection.execute(
                "INSERT INTO owned_parts(part_unique, count, source) VALUES(?,1,'video')",
                (unique,))
        for unique in frame_items:
            connection.execute(
                "INSERT INTO owned_items(item_unique, count, mastered, source) "
                "VALUES(?,1,0,'video')", (unique,))
        connection.commit()
        view = ImportView(cfg)

        def offered(capture):
            view.apply_note.setText("")
            rows = view._absent_rows(capture)
            return {(k, u) for k, u, _n, _c, _s in rows}, view.apply_note.text()

        # 1. Prime Parts style after a big sale: 6 of 10 Warframe parts seen
        # (40% absent, above the old share limit) and every weapon part.
        seen = {("part", u): {} for u in frame_parts[:6] + weapon_parts}
        got, note = offered(seen)
        want = {("part", u) for u in frame_parts[6:]}
        if got != want:
            problems.append(f"per-kind absent: parts capture offered {sorted(got)!r}, "
                            f"wanted {sorted(want)!r}")
        if "too many" in note:
            problems.append(f"per-kind absent: parts capture was refused: {note!r}")
        if "leave them unticked" not in note or "too many" in note:
            problems.append(f"per-kind absent: the many-parts note reads {note!r}")
        # 2. Built items only: 9 of 10, no part in the capture.
        seen = {("item", u): {} for u in frame_items[:9]}
        got, note = offered(seen)
        want = {("item", frame_items[9])}
        if got != want:
            problems.append(f"per-kind absent: items capture offered {sorted(got)!r}, "
                            f"wanted {sorted(want)!r}")
        # 3. Warframes tab: parts and items of Warframes, nothing of weapons.
        seen = {("part", u): {} for u in frame_parts[:9]}
        seen.update({("item", u): {} for u in frame_items[:9]})
        got, note = offered(seen)
        if any(u in weapon_parts for _k, u in got):
            problems.append("per-kind absent: a Warframes capture offered weapon parts")
        if got != {("part", frame_parts[9]), ("item", frame_items[9])}:
            problems.append(f"per-kind absent: Warframes capture offered {sorted(got)!r}")
        # 4. The note names what it counts.
        seen = {("part", u): {} for u in frame_parts[:3]}
        _got, note = offered(seen)
        if "warframe parts" not in note:
            problems.append(f"per-kind absent: the note does not name the parts: {note!r}")
        # 5. Beasts are never in a recording, so a capture's silence
        # about them is no evidence: held beasts are not offered as gone?,
        # even when three of them happen to be in the capture.
        beasts = pick("items", "beast", 6)
        if len(beasts) < 6:
            problems.append("per-kind absent: the catalogue has too few beasts")
        else:
            for unique in beasts:
                connection.execute(
                    "INSERT INTO owned_items(item_unique, count, mastered, source) "
                    "VALUES(?,1,1,'manual')", (unique,))
            connection.commit()
            seen = {("item", u): {} for u in beasts[:3]}
            got, _note = offered(seen)
            if any(u in beasts for _k, u in got):
                problems.append(f"per-kind absent: a beast was offered as gone?: "
                                f"{sorted(got)!r}")
    finally:
        if view is not None:
            view.deleteLater()
        for t in tables:
            connection.execute(f"DELETE FROM {t}")
        for t, rows_ in saved.items():
            for row in rows_:
                keys = row.keys()
                connection.execute(
                    f"INSERT INTO {t}({','.join(keys)}) "
                    f"VALUES({','.join('?' * len(keys))})", tuple(row))
        connection.commit()
        connection.close()
    print("absent per kind    : parts and built items judged apart within a category")


def _check_the_summary_puts_lowered_rows_first(app, cfg, problems: list) -> None:
    """Rows that write 0 or less come first, and Enter ticks the row.

    One scenario with all four kinds of row: gone? (0 proposed, unchecked),
    built (the foundry took it, checked), mastery and the rows the capture
    saw. The write reads `apply_rows` by row index, so the table and the list
    must stay the same sequence, and what is written must be what each row
    says.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    parts = connection.execute(
        "SELECT unique_name, name FROM parts p WHERE alias_of IS NULL "
        "AND unique_name NOT IN (SELECT unique_name FROM items) "
        "ORDER BY unique_name LIMIT 5").fetchall()
    item = connection.execute(
        "SELECT unique_name, name FROM items WHERE category NOT IN "
        "('beast','amp','special','other','reward_other','component_only') "
        "AND unique_name NOT IN (SELECT item_unique FROM owned_items "
        "WHERE mastered = 1) ORDER BY unique_name LIMIT 1").fetchone()
    if len(parts) < 5 or item is None:
        problems.append("summary order: the catalogue has too few parts")
        return
    seen_a, seen_b, gone_a, gone_b, built = parts
    uniques = [p["unique_name"] for p in parts] + [item["unique_name"]]
    marks = ",".join("?" * len(uniques))
    saved = {
        "owned_parts": connection.execute(
            f"SELECT * FROM owned_parts WHERE part_unique IN ({marks})",
            uniques).fetchall(),
        "owned_items": connection.execute(
            f"SELECT * FROM owned_items WHERE item_unique IN ({marks})",
            uniques).fetchall(),
    }
    try:
        for unique, count in ((seen_a, 5), (gone_a, 4), (gone_b, 2), (built, 3)):
            dbq.set_part_count(connection, unique["unique_name"], count, "video")

        def tile(column, row, name, **fields):
            return scan_mod.Candidate(
                column=column, row=row, name=name, score=100.0, reading=name,
                sightings=3, verified_box=None, sources=frozenset({1}),
                lead=30.0, evidence=None, **fields)

        tiles = [tile(0, 0, seen_a["name"], part_unique=seen_a["unique_name"],
                      quantity=3),
                 tile(1, 0, seen_b["name"], part_unique=seen_b["unique_name"],
                      quantity=2),
                 tile(2, 0, item["name"], item_unique=item["unique_name"],
                      rank=30)]
        view = ImportView(cfg)
        view.resize(1900, 1000)
        view.show()
        view.activateWindow()
        QTest.qWaitForWindowActive(view)
        view.scan_result = scan_mod.ScanResult(
            candidates=tiles, frames_read=1, words_total=3,
            words_after_ui_filter=3, cells_found=3, grid=None)
        view._prepare_review()
        view._remember_all_counts()
        view._absent_rows = lambda combined: [
            ("part", gone_a["unique_name"], gone_a["name"], 4, "video"),
            ("part", gone_b["unique_name"], gone_b["name"], 2, "manual")]
        view._built_rows = lambda combined: [
            ("part", built["unique_name"], built["name"], 3, 1)]
        view._build_apply_table([])
        view.stack.setCurrentIndex(3)  # PAGE_APPLY
        view.apply_table.setFocus()
        _settle(app)

        table = view.apply_table
        kinds = [table.item(r, 1).text() for r in range(table.rowCount())]
        want = ["part, gone?", "part, gone? (by hand)", "part, built", "mastery"]
        if kinds[:4] != want or len(kinds) != 7:
            problems.append(f"summary order: kinds are {kinds}, wanted "
                            f"{want} and then the two rows the capture saw")
        if len(view.apply_rows) != table.rowCount():
            problems.append("summary order: apply_rows and the table differ "
                            "in length")
        for r, row in enumerate(view.apply_rows):
            label = table.item(r, 2).text()
            expected = dbq.name_for(connection, row["kind"], row["unique_name"]) \
                if row["kind"] != "mastery" else item["name"]
            if label != expected:
                problems.append(f"summary order: row {r} shows {label!r} but "
                                f"writes {expected!r}")
        gone_boxes = [view.apply_rows[r]["checkbox"].isChecked() for r in (0, 1)]
        if any(gone_boxes) or not view.apply_rows[2]["checkbox"].isChecked():
            problems.append("summary order: gone? rows must start unchecked "
                            "and built rows checked")
        note = view.apply_note.text()
        if not note.startswith("3 row(s) at the top") or "2 gone?" not in note \
                or "1 built" not in note:
            problems.append(f"summary order: the note does not say how many "
                            f"rows are at the top: {note[:90]!r}")

        # Up/Down move, Enter and Num Enter tick the current row.
        def press(key, mod=Qt.NoModifier):
            QTest.keyClick(QApplication.focusWidget() or table, key, mod)
            _settle(app)

        if table.currentRow() != 0:
            problems.append(f"summary keys: the cursor starts on row "
                            f"{table.currentRow()}, wanted 0")
        press(Qt.Key_Down)
        if table.currentRow() != 1:
            problems.append("summary keys: Down did not move to row 1")
        press(Qt.Key_Return)
        if not view.apply_rows[1]["checkbox"].isChecked():
            problems.append("summary keys: Enter did not tick row 1")
        press(Qt.Key_Enter, Qt.KeypadModifier)
        if view.apply_rows[1]["checkbox"].isChecked():
            problems.append("summary keys: Num Enter did not untick row 1")
        press(Qt.Key_Up)
        press(Qt.Key_Return)
        if table.currentRow() != 0 or not view.apply_rows[0]["checkbox"].isChecked():
            problems.append("summary keys: Up and Enter did not tick row 0")
        press(Qt.Key_Down)
        press(Qt.Key_Return)  # row 1 on again: written as 0 below

        # A count being typed keeps Enter: it confirms the number, not the tick.
        last = table.rowCount() - 1
        table.setCurrentCell(last, 4)
        _settle(app)
        before = view.apply_rows[last]["checkbox"].isChecked()
        editor = QApplication.focusWidget()
        if editor is None or editor is table:
            problems.append("summary keys: the count did not open for typing")
        else:
            editor.selectAll()
            QTest.keyClicks(editor, "7")
            press(Qt.Key_Return)
            if table.item(last, 4).text() != "7":
                problems.append(f"summary keys: Enter in the count left "
                                f"{table.item(last, 4).text()!r}, wanted '7'")
            if view.apply_rows[last]["checkbox"].isChecked() != before:
                problems.append("summary keys: Enter in the count ticked the row")

        # The write: each row writes what it says.
        original = QMessageBox.information
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            view._write_apply_table()
        finally:
            QMessageBox.information = original
        got = {
            "gone_a": dbq.owned_count(connection, gone_a["unique_name"]),
            "gone_b": dbq.owned_count(connection, gone_b["unique_name"]),
            "built": dbq.owned_count(connection, built["unique_name"]),
            "seen_a": dbq.owned_count(connection, seen_a["unique_name"]),
            "seen_b": dbq.owned_count(connection, seen_b["unique_name"]),
            "mastered": dbq.is_mastered(connection, item["unique_name"]),
        }
        # Rows 0 and 1 were ticked (gone: 0), the built row stayed ticked (1),
        # the capture's rows are written; the count typed on the last row (the
        # item, 3 tiles read as 1) is 7.
        wanted = {"gone_a": 0, "gone_b": 0, "built": 1, "seen_a": 3,
                  "seen_b": 2, "mastered": True}
        if got != wanted:
            problems.append(f"summary write: wrote {got}, wanted {wanted}")
        if dbq.item_built(connection, item["unique_name"]) != 7:
            problems.append(f"summary write: the typed 7 became "
                            f"{dbq.item_built(connection, item['unique_name'])}")
        view.close()
        view.deleteLater()
    finally:
        for unique in uniques:
            connection.execute("DELETE FROM owned_parts WHERE part_unique = ?",
                               (unique,))
            connection.execute("DELETE FROM owned_items WHERE item_unique = ?",
                               (unique,))
        for table_name, rows in saved.items():
            for row in rows:
                connection.execute(
                    f"INSERT INTO {table_name} ({','.join(row.keys())}) "
                    f"VALUES ({','.join('?' * len(row.keys()))})", tuple(row))
        connection.commit()
    print("summary order      : gone?, built, mastery, then seen; Enter ticks")


def _check_review_is_one_shape(app, cfg, problems: list) -> None:
    """One list should look like one list.

    The smoke pass presses four hundred controls and never looks at any of
    them, which is how a review that switched between tall cards and compact
    rows halfway down passed every test here and was caught by a person
    looking at it. Heights are the cheapest thing that would have said so:
    within a list, rows of the same kind should be the same height, and the
    two kinds should be within reach of each other.
    """
    import pickle

    fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "scan.pickle"
    if not fixture.exists():
        print("review shape       : skipped, no scan fixture")
        return
    from ui.views.import_view import ImportView

    result = pickle.loads(fixture.read_bytes())
    view = ImportView(cfg)
    view.resize(1900, 1000)
    view.show()
    view._hand_over(result)
    _settle(app)

    heights = {"question": set(), "accepted": set()}
    for row, entry in enumerate(view.rows):
        # The open card is meant to be taller - but it must not be shorter
        # than it needs, which is the one thing skipping it hid. The first
        # card in the list shipped clipped: its height was taken before Qt had
        # laid it out, so the picture was cut off at the top.
        wants = entry["widget"].sizeHint().height()
        got = entry["item"].sizeHint().height()
        if got < wants:
            problems.append(f"row {row} ({entry['kind']}) is {got} pixels "
                            f"tall and its contents need {wants} - clipped")
        if row == view.review_list.currentRow():
            continue
        heights[entry["kind"]].add(got)

    # Answering a question and going back to it. The cursor moves on when a
    # question is answered, so re-opening the one you just answered is the
    # ordinary way to check it - and it was the one row a click could not open,
    # because moving to the row you are already on is silent in Qt.
    first = next((r for r, e in enumerate(view.rows)
                  if e["kind"] == "question"), None)
    if first is not None:
        view.review_list.setCurrentRow(first)
        _settle(app)
        view._on_ignore()
        _settle(app)
        view._focus_question(view.rows[first]["cluster"])
        _settle(app)
        if type(view.rows[first]["widget"]).__name__ != "ReviewWidget":
            problems.append("an answered question does not open again when "
                            "it is clicked")

    # The search box, on a list of two hundred rows where the user's question
    # was "is this name anywhere else".
    if view.rows:
        view.review_search.setText("relic")
        _settle(app)
        shown = sum(1 for e in view.rows if not e["item"].isHidden())
        if not 0 < shown < len(view.rows):
            problems.append(f"searching narrowed 220 rows to {shown}")
        # a misspelt name still finds its row
        word = next((w for e in view.rows for t in view._row_texts(e)
                     for w in t.split() if len(w) >= 6 and w.isalpha()), None)
        if word is not None:
            view.review_search.setText(word[:3] + word[4:])
            _settle(app)
            kept = [e for e in view.rows if not e["item"].isHidden()]
            if not kept or not any(word.casefold() in " ".join(
                    view._row_texts(e)).casefold() for e in kept):
                problems.append(f"review search with a typo in {word!r} found "
                                f"{len(kept)} row(s)")
        view.review_search.setText("xyzzyqq")
        _settle(app)
        if any(not e["item"].isHidden() for e in view.rows):
            problems.append("review search for nonsense left rows showing")
        view.review_search.setText("")
        _settle(app)
        if any(e["item"].isHidden() for e in view.rows):
            problems.append("clearing the search left rows hidden")
    view.close()

    for kind, found in heights.items():
        if len(found) > 1 and max(found) > min(found) * 1.6:
            problems.append(f"{kind} rows vary from {min(found)} to "
                            f"{max(found)} pixels in one list")
    if heights["question"] and heights["accepted"]:
        tallest = max(max(heights["question"]), max(heights["accepted"]))
        shortest = min(min(heights["question"]), min(heights["accepted"]))
        if tallest > shortest * 2:
            problems.append(f"closed rows range from {shortest} to {tallest} "
                            f"pixels - the list has two shapes, not one")
    print(f"review shape       : questions {sorted(heights['question'])}, "
          f"accepted {sorted(heights['accepted'])}")


def _check_relic_panel_reads_a_screen(cfg, problems: list) -> None:
    """Drive the relic panel's chain without a game window.

    The panel is the one thing in here that runs under time pressure, and it
    is the one thing the smoke test never touched: "trigger" is in
    LEAVE_ALONE, because pressing it grabs the screen. So the chain behind it
    went unexercised - and `candidates_for_relics` spent two days passing five
    arguments into a four-field `Candidate`, which made every trigger fail
    with "capture failed: TypeError" and nothing else noticed.

    Everything from the relic identifiers onwards is testable without a
    screen, which is where the fault was. The grab is not, and is not what
    breaks.
    """
    from wft.data import db as dbq
    from wft.relic import match as M, score as S

    connection = dbq.connect(cfg.db_path)
    relics = [row["unique_name"] for row in connection.execute(
        "SELECT unique_name FROM relics LIMIT 4")]
    if not relics:
        return

    try:
        candidates = M.candidates_for_relics(connection, relics)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"relic panel: candidates_for_relics raised {exc!r}")
        return
    if not candidates:
        problems.append("relic panel: four relics produced no candidates")
        return
    if not M.all_prime_parts(connection):
        problems.append("relic panel: the fallback candidate list is empty")

    # And the identification-to-ranking spine underneath `_read_at`: one clean
    # reading and one piece of wreckage, because a reward screen with an
    # unreadable tile is the normal case rather than the exception. The full
    # `_read_at` — wide-list ruler included — is driven in its own check below.
    readings = [candidates[0].name, "\\ ) ) W 9 D [ gth"]
    try:
        rewards = []
        for position, raw in enumerate(readings, start=1):
            found = M.match(raw, candidates)
            if found.part_unique:
                rewards.append(S.load_reward(connection, found.part_unique,
                                             position, raw, found.score))
            else:
                rewards.append(S.Reward(position=position, raw_text=raw))
        verdict = S.rank(rewards)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"relic panel: reading and ranking raised {exc!r}")
        return
    if verdict is None or not verdict.headline:
        problems.append("relic panel: ranking produced no verdict")
    if rewards[0].part_unique is None:
        problems.append("relic panel: a reward named exactly did not match")
    print(f"relic panel        : {len(candidates)} candidate(s) from "
          f"{len(relics)} relic(s)")


def _check_read_at_reads_the_squad_screen(cfg, problems: list) -> None:
    """Drive `_read_at` itself against the squad screen that once broke the layout score.

    Four players, one relic in the log: three of the four names on screen are
    somebody else's and cannot be in the narrow list. The layout score and the
    wide-list fallback were fixed and pinned — but against
    `pick_layout`, with the clean counts written in by hand. So the counting
    itself, and the fallback, were exercised by no test, which is exactly the
    gap a regression walks through.

    Tesseract is not needed: `grab` is already injected and the engine is a
    one-method dependency, so the readings of that screen go in as text.
    """
    from types import SimpleNamespace

    import numpy

    from ui.workers import relic_trigger as RT
    from wft.data import db as dbq
    from wft.relic import match as M

    connection = dbq.connect(cfg.db_path)
    relic = connection.execute(
        "SELECT DISTINCT relic_unique FROM relic_rewards LIMIT 1").fetchone()
    if relic is None:
        connection.close()
        return
    narrow = M.candidates_for_relics(connection, [relic["relic_unique"]])
    wide = M.all_prime_parts(connection)
    in_narrow = {c.part_unique for c in narrow}
    foreign = [c for c in wide if c.part_unique not in in_narrow]
    if not narrow or len(foreign) < 2:
        connection.close()
        return

    def read_at_texts(texts, tiles):
        """Drive `_read_at` with canned readings; count the engine calls.

        Exhausted texts read as empty rather than raising, so a fault in how
        often the engine is asked reports as a finding instead of killing the
        suite.
        """
        reads = [0]

        def next_text(_image):
            reads[0] += 1
            return SimpleNamespace(text=texts.pop(0) if texts else "")

        engine = SimpleNamespace(read=next_text)
        rewards, clean = RT._read_at(tiles, engine,
                                     lambda _region: numpy.ones((2, 2),
                                                                numpy.uint8),
                                     narrow, connection, lambda _msg: None,
                                     wide)
        return rewards, clean, reads[0]

    def read_at(readings):
        rewards, clean, _reads = read_at_texts(list(readings), len(readings))
        return rewards, clean

    # The squad screen: both outer names read perfectly, and both are absent
    # from the narrow list. All four tiles must count as read, or the layout
    # tie-break cuts the screen down to its middle two — and the outer two
    # must come back *named*, because a perfect reading shown as NOT
    # RECOGNISED is the fault the decision records.
    own = narrow[0].name
    rewards, clean = read_at([foreign[0].name, own, own, foreign[1].name])
    if clean != 4:
        problems.append(f"read_at: the squad screen counted {clean} of 4 "
                        f"tiles as read")
    named = sum(1 for r in rewards if r.part_unique)
    if named != 4:
        problems.append(f"read_at: the squad screen named {named} of 4 — "
                        f"a squadmate's perfect reading was dropped")

    # A reading the narrow list rescues — the answer intact inside glyph
    # noise, under the ratio threshold — is an identification, and must count
    # as one. Under the earlier counting it did not: the clean count asked only
    # the wide list, where the rescue never runs, so a fully identified
    # layout could lose the tie-break and drop a real reward.
    rescued_text = None
    for candidate in narrow[:6]:
        # Not Forma: an unmatched Forma reading counts as read by its own
        # rule, which would let a broken rescue count hide behind it.
        if "forma" in candidate.name.lower():
            continue
        for pad in ("g Y TR {} Qu fi", "g Y TR {} Qu fi xw v",
                    "gh Y TR w {} Qu fi xw vb", "gh Yj TR wq {} Qux fi xw vbn"):
            noisy = pad.format(candidate.name)
            found = M.match(noisy, narrow)
            if found.part_unique == candidate.part_unique \
                    and found.score < M.MIN_SCORE \
                    and not M.match(noisy, wide).part_unique:
                rescued_text = noisy
                break
        if rescued_text:
            break
    if rescued_text is None:
        print("read_at            : no rescuable reading found, rescue "
              "count not pinned")
    else:
        rewards, clean = read_at([rescued_text])
        if clean != 1:
            problems.append("read_at: a reading the rescue pass identified "
                            "did not count as read")
        if not rewards[0].part_unique:
            problems.append("read_at: a reading the rescue pass identified "
                            "came back unnamed")

    # And the Knell-for-Kompressa shape: a squadmate's name, read perfectly,
    # that the narrow list answers with a similar-but-wrong part. The wide
    # list knows the true name at 100; a confident answer that outscores the
    # narrow one must win, or the panel shows the wrong part with a plausible
    # score attached.
    collision = None
    for candidate in foreign:
        found = M.match(candidate.name, narrow)
        if found.part_unique and found.score < 100.0:
            collision = candidate
            break
    if collision is None:
        print("read_at            : no narrow/wide collision in this "
              "database, override not pinned")
    else:
        rewards, _clean = read_at([collision.name])
        if rewards[0].part_unique != collision.part_unique:
            problems.append(
                f"read_at: {collision.name!r} read perfectly and came back "
                f"as {rewards[0].name!r} — the narrow list's wrong answer "
                f"blocked the wide list's right one")

    # The Daikyu screen: a perfectly legible tile whose raw reading comes
    # back as glyph wreckage below every threshold on every list — measured
    # live as 'DaRvUPImeBaEnrnt' for 'Daikyu Prime Blueprint', 63 against a
    # cutoff of 70, while the same crop enlarged three times read at 95. A
    # tile no list identified gets exactly one more look, enlarged; a tile
    # that identified gets none — the second read is a rescue, not a habit.
    plain = next((c for c in narrow
                  if "forma" not in c.name.lower()), None)
    wreckage = "xq | zk vv |"
    if plain is None or M.match(wreckage, narrow).part_unique \
            or M.match(wreckage, wide).part_unique:
        print("read_at            : no pinnable wreckage reading, second "
              "look not pinned")
    else:
        rewards, clean, reads = read_at_texts([wreckage, plain.name], tiles=1)
        if reads < 2:
            problems.append("read_at: a tile no list identified was not "
                            "read a second time")
        elif reads > 2:
            problems.append(f"read_at: the failed tile was read {reads} "
                            f"times — one second look, not a loop")
        elif not rewards[0].part_unique \
                or rewards[0].part_unique != plain.part_unique:
            problems.append("read_at: the enlarged second look identified "
                            f"{plain.name!r} and it was not adopted")
        elif clean != 1:
            problems.append("read_at: the second look's identification did "
                            "not count as read")
        _rewards, _clean, reads = read_at_texts([plain.name], tiles=1)
        if reads != 1:
            problems.append("read_at: an identified tile was read again — "
                            "the second look must be reserved for failures")

    connection.close()
    print(f"read_at            : squad screen 4 of 4, rescue counted, "
          f"wide beats a wrong narrow match, second look pinned")


def _check_a_build_consumes_its_pieces(app, cfg, problems: list) -> None:
    """Building something has to take its pieces off the shelf.

    A player built Vadarya Prime, levelled it to thirty and imported. The scan
    saw the finished weapon and recorded it; the four pieces it was made from
    stayed on the shelf from the import before, so the plan went on offering
    a complete set for ducats that no longer existed.

    Absence could not catch it - two of the four pieces carried a source the
    "gone?" list does not look at, and it proposes its rows unchecked anyway.
    What catches it is the copy that appeared: that is an event, not a silence.

    Driven at both layers, because the fault needed both. The recipe side
    decides what a build eats; the import page decides whether anybody is ever
    asked about it, and that is the half that was missing.
    """
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)
    recipe = connection.execute(
        "SELECT p.item_unique, COUNT(*) AS kinds FROM parts p "
        "JOIN items i ON i.unique_name = p.item_unique "
        "WHERE p.alias_of IS NULL GROUP BY p.item_unique "
        "HAVING kinds >= 3 LIMIT 1").fetchone()
    if recipe is None:
        return
    item = recipe["item_unique"]

    pieces = connection.execute(
        "SELECT unique_name, count_needed FROM parts "
        "WHERE item_unique = ? AND alias_of IS NULL", (item,)).fetchall()
    for piece in pieces:
        dbq.set_part_count(connection, piece["unique_name"],
                           int(piece["count_needed"]), "video")
    dbq.set_item_count(connection, item, 0, "video")

    held, total = dbq.set_progress(connection, item)
    if held != total:
        problems.append("build: the fixture did not produce a complete set")
        return

    eaten = dbq.pieces_of_one_set(connection, item, 1)
    if len(eaten) != len(pieces):
        problems.append(f"build: a set of {len(pieces)} piece(s) consumed "
                        f"{len(eaten)}")
    if any(after != 0 for _kind, _unique, _name, _now, after in eaten):
        problems.append("build: one copy of a full set left pieces behind")

    # And the page. A helper nobody calls is what the last two days of this
    # bug looked like from the outside.
    from ui.views.import_view import ImportView

    page = ImportView(cfg)
    combined = {("item", item): {"name": "built now", "held": 1}}
    rows = page._built_rows(combined)
    if len(rows) != len(pieces):
        problems.append(f"build: the import page proposed {len(rows)} "
                        f"row(s) for a {len(pieces)}-piece set")
    if any(proposed != 0 for _k, _u, _n, _current, proposed in rows):
        problems.append("build: the import page proposed keeping a piece the "
                        "foundry ate")

    # A piece the capture itself photographed is not guesswork. The shelf wins.
    seen = pieces[0]["unique_name"]
    combined[("part", seen)] = {"name": "on the shelf", "held": 2}
    if any(unique == seen for _k, unique, _n, _c, _p in page._built_rows(combined)):
        problems.append("build: a piece the capture saw was still proposed "
                        "as consumed")
    # And the gun that is a component of another gun. Bronco Prime is a piece
    # of Akbronco Prime; the import writes it down as an item, so its
    # own build is seen like any other.
    dual = connection.execute(
        "SELECT p.unique_name FROM parts p "
        "WHERE p.unique_name IN (SELECT unique_name FROM items) "
        "AND EXISTS (SELECT 1 FROM parts q WHERE q.item_unique = p.unique_name "
        "            AND q.alias_of IS NULL "
        "            AND q.unique_name <> p.unique_name) LIMIT 1").fetchone()
    if dual is not None:
        gun = dual["unique_name"]
        own = connection.execute(
            "SELECT unique_name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL "
            "AND unique_name <> ?", (gun, gun)).fetchall()
        dbq.set_part_count(connection, gun, 0, "video")
        dbq.set_item_count(connection, gun, 0, "video")
        for piece in own:
            dbq.set_part_count(connection, piece["unique_name"],
                               int(piece["count_needed"]), "video")
        made = page._built_rows({("item", gun): {"name": "made one", "held": 1}})
        if len(made) != len(own):
            problems.append(f"build: a component gun's own build proposed "
                            f"{len(made)} of {len(own)} piece(s)")

    page.deleteLater()
    print(f"build consumption  : {len(eaten)} piece(s) taken by one build")


def _check_a_built_weapon_that_is_a_component_goes_to_items(app, cfg, problems: list) -> None:
    """Lex Prime, three tiles: three in owned_items, none in owned_parts.

    The video import used to write a built weapon that is a piece of another
    item (Lex Prime in Aklex Prime) into owned_parts, where owned_count added
    it to the owned_items row and counted it twice. Also an ordinary part
    still goes to owned_parts, undo puts both back, and the startup repair
    moves old rows and does not add the two numbers.
    """
    from PySide6.QtWidgets import QMessageBox
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    gun = connection.execute(
        "SELECT p.unique_name, p.name FROM parts p "
        "JOIN items i ON i.unique_name = p.unique_name "
        "WHERE p.item_unique <> p.unique_name AND p.alias_of IS NULL "
        "AND i.category <> 'component_only' "
        "ORDER BY p.unique_name LIMIT 1").fetchone()
    chroma = connection.execute(
        "SELECT p.unique_name FROM parts p "
        "JOIN items i ON i.unique_name = p.unique_name "
        "WHERE p.item_unique <> p.unique_name AND p.alias_of IS NULL "
        "AND i.category = 'component_only' LIMIT 1").fetchone()
    if chroma is not None and dbq.is_built_component(
            connection, chroma["unique_name"]):
        problems.append("component weapon: a component-only item (a Warframe "
                        "part) was treated as a built weapon")
    plain = connection.execute(
        "SELECT p.unique_name, p.name FROM parts p "
        "WHERE p.alias_of IS NULL AND p.item_unique <> p.unique_name "
        "AND p.unique_name NOT IN (SELECT unique_name FROM items) "
        "ORDER BY p.unique_name LIMIT 1").fetchone()
    if gun is None or plain is None:
        problems.append("component weapon: the database has no component "
                        "weapon or no ordinary part to test with")
        return
    # The later checks read this copy, so what is touched here is put back.
    names = (gun["unique_name"], plain["unique_name"])
    saved = {
        "owned_parts": connection.execute(
            "SELECT * FROM owned_parts WHERE part_unique IN (?,?)", names).fetchall(),
        "owned_items": connection.execute(
            "SELECT * FROM owned_items WHERE item_unique IN (?,?)", names).fetchall(),
    }
    for unique in names:
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?", (unique,))
        connection.execute("DELETE FROM owned_items WHERE item_unique = ?", (unique,))
    connection.commit()

    def tile(row, column, unique, quantity=None):
        return scan_mod.Candidate(
            column=column, row=0, name=row["name"], score=100.0,
            reading=row["name"], sightings=3, part_unique=unique,
            verified_box=None, quantity=quantity, sources=frozenset({1}),
            lead=30.0, evidence=None)

    tiles = [tile(gun, c, gun["unique_name"]) for c in range(3)]
    tiles.append(tile(plain, 3, plain["unique_name"], quantity=2))
    view = ImportView(cfg)
    view.scan_result = scan_mod.ScanResult(
        candidates=tiles, frames_read=1, words_total=4,
        words_after_ui_filter=4, cells_found=4, grid=None)
    view._prepare_review()
    view._remember_all_counts()
    view._build_apply_table([])
    kinds = {(r["kind"], r["unique_name"]): r["held"] for r in view.apply_rows}
    if kinds.get(("item", gun["unique_name"])) != 3 or (
            "part", gun["unique_name"]) in kinds:
        problems.append(f"component weapon: 3 tiles of {gun['name']} became "
                        f"{kinds}, wanted item x3 and no part row")
    if kinds.get(("part", plain["unique_name"])) != 2:
        problems.append(f"component weapon: an ordinary part became {kinds}")

    original = QMessageBox.information
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    try:
        view._write_apply_table()
    finally:
        QMessageBox.information = original
    in_parts = connection.execute(
        "SELECT count FROM owned_parts WHERE part_unique = ?",
        (gun["unique_name"],)).fetchone()
    if in_parts is not None or dbq.item_built(connection, gun["unique_name"]) != 3:
        problems.append(f"component weapon: after the write owned_parts holds "
                        f"{in_parts and in_parts['count']} and owned_items "
                        f"{dbq.item_built(connection, gun['unique_name'])}")
    if dbq.owned_count(connection, gun["unique_name"]) != 3:
        problems.append(f"component weapon: owned_count is "
                        f"{dbq.owned_count(connection, gun['unique_name'])}, "
                        f"wanted 3")
    if dbq.owned_count(connection, plain["unique_name"]) != 2:
        problems.append("component weapon: an ordinary part was not written "
                        "to owned_parts")
    journal = connection.execute("SELECT MAX(id) FROM imports").fetchone()[0]
    dbq.undo_import(connection, journal)
    if dbq.owned_count(connection, gun["unique_name"]) != 0 or dbq.owned_count(
            connection, plain["unique_name"]) != 0:
        problems.append("component weapon: undo did not put both back to 0")

    # The repair: an old pair (parts 3 newer, items 1) is 3, not 4; an old
    # pair where items is newer keeps items; mastery survives.
    unique = gun["unique_name"]
    connection.execute("DELETE FROM owned_parts WHERE part_unique = ?", (unique,))
    connection.execute("DELETE FROM owned_items WHERE item_unique = ?", (unique,))
    connection.execute("INSERT INTO owned_items(item_unique,count,source,updated_at,mastered) "
                       "VALUES(?,1,'manual','2026-10-10T00:00:00+00:00',1)", (unique,))
    connection.execute("INSERT INTO owned_parts(part_unique,count,source,updated_at) "
                       "VALUES(?,3,'video','2026-10-14T00:00:00+00:00')", (unique,))
    connection.commit()
    moved = dbq.settle_built_components(connection)
    row = connection.execute("SELECT count, mastered FROM owned_items "
                             "WHERE item_unique = ?", (unique,)).fetchone()
    if (len(moved) != 1 or row["count"] != 3 or not row["mastered"]
            or dbq.owned_count(connection, unique) != 3):
        problems.append(f"component weapon repair: {moved}, items {dict(row)}")
    if dbq.settle_built_components(connection):
        problems.append("component weapon repair: a second run moved something")
    connection.execute("INSERT INTO owned_parts(part_unique,count,source,updated_at) "
                       "VALUES(?,9,'video','2026-01-01T00:00:00+00:00')", (unique,))
    dbq.settle_built_components(connection)
    if dbq.owned_count(connection, unique) != 3:
        problems.append("component weapon repair: an older parts row "
                        "overwrote a newer items row")
    for unique in names:
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?", (unique,))
        connection.execute("DELETE FROM owned_items WHERE item_unique = ?", (unique,))
    for table, rows in saved.items():
        for row in rows:
            connection.execute(
                f"INSERT INTO {table} ({','.join(row.keys())}) "
                f"VALUES ({','.join('?' * len(row.keys()))})", tuple(row))
    connection.commit()
    view.deleteLater()
    print(f"component weapon   : {gun['name']} x3 to items, repair ok")


def _check_the_akimbo_primes_count_their_built_guns(app, cfg, problems: list) -> None:
    """Two built Lex Primes make a whole Aklex Prime, for each of the four pairs.

    The real import path writes two tiles of the single gun. Then every reader
    has to see two: `owned_count`, the set's progress ("needs 2x"), the
    shortfall list, the spare-part floor (a built gun is not a spare part) and
    the build, which takes both guns off the shelf. Done on the copy; the rows
    it touches are put back.
    """
    from PySide6.QtWidgets import QMessageBox
    from ui.views.import_view import ImportView
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod

    connection = dbq.connect(cfg.db_path)
    pairs = [("Bronco Prime", "Akbronco Prime"), ("Lex Prime", "Aklex Prime"),
             ("Magnus Prime", "Akmagnus Prime"), ("Vasto Prime", "Akvasto Prime")]
    report = []
    for gun_name, set_name in pairs:
        gun = connection.execute(
            "SELECT p.unique_name, p.name, p.item_unique, p.count_needed "
            "FROM parts p JOIN items i ON i.unique_name = p.unique_name "
            "WHERE p.name = ? AND p.alias_of IS NULL", (gun_name,)).fetchone()
        owner = connection.execute(
            "SELECT unique_name, name FROM items WHERE name = ?",
            (set_name,)).fetchone()
        if gun is None or owner is None or gun["item_unique"] != owner["unique_name"]:
            problems.append(f"akimbo {set_name}: the database has no {gun_name} "
                            f"as a piece of it")
            continue
        gun_u, set_u = gun["unique_name"], owner["unique_name"]
        pieces = connection.execute(
            "SELECT unique_name, count_needed FROM parts WHERE item_unique = ? "
            "AND alias_of IS NULL AND unique_name <> ?", (set_u, gun_u)).fetchall()
        names = [gun_u, set_u] + [p["unique_name"] for p in pieces]
        marks = ",".join("?" * len(names))
        saved = {
            "owned_parts": connection.execute(
                f"SELECT * FROM owned_parts WHERE part_unique IN ({marks})",
                names).fetchall(),
            "owned_items": connection.execute(
                f"SELECT * FROM owned_items WHERE item_unique IN ({marks})",
                names).fetchall(),
        }
        connection.execute(f"DELETE FROM owned_parts WHERE part_unique IN ({marks})", names)
        connection.execute(f"DELETE FROM owned_items WHERE item_unique IN ({marks})", names)
        connection.commit()
        label = f"akimbo {set_name}"
        try:
            tiles = [scan_mod.Candidate(
                column=c, row=0, name=gun["name"], score=100.0,
                reading=gun["name"], sightings=3, part_unique=gun_u,
                verified_box=None, quantity=None, sources=frozenset({1}),
                lead=30.0, evidence=None) for c in range(2)]
            view = ImportView(cfg)
            view.scan_result = scan_mod.ScanResult(
                candidates=tiles, frames_read=1, words_total=2,
                words_after_ui_filter=2, cells_found=2, grid=None)
            view._prepare_review()
            view._remember_all_counts()
            view._build_apply_table([])
            original = QMessageBox.information
            QMessageBox.information = staticmethod(lambda *a, **k: None)
            try:
                view._write_apply_table()
            finally:
                QMessageBox.information = original
            view.deleteLater()
            in_parts = connection.execute(
                "SELECT count FROM owned_parts WHERE part_unique = ?", (gun_u,)).fetchone()
            if in_parts is not None or dbq.item_built(connection, gun_u) != 2:
                problems.append(f"{label}: after the import owned_parts holds "
                                f"{in_parts and in_parts['count']} and owned_items "
                                f"{dbq.item_built(connection, gun_u)}, wanted none and 2")
            if dbq.owned_count(connection, gun_u) != 2:
                problems.append(f"{label}: owned_count is "
                                f"{dbq.owned_count(connection, gun_u)}, wanted 2")
            for piece in pieces:
                dbq.set_part_count(connection, piece["unique_name"],
                                   int(piece["count_needed"]), "manual")
            held, total = dbq.set_progress(connection, set_u)
            if held != total or dbq.pieces_short_for(connection, set_u):
                problems.append(f"{label}: the set reads {held} of {total} with "
                                f"two guns and every other piece in hand")
            # A built gun is not a spare part: nothing is offered and nothing
            # is written, whatever mastery says about the set.
            for mastered in (False, True):
                if mastered:
                    dbq.set_item_mastered(connection, set_u)
                if any(r["unique_name"] == gun_u for r in dbq.spare_parts(connection)):
                    problems.append(f"{label}: a built gun was offered as a "
                                    f"spare part (mastered={mastered})")
                if dbq.trade_spare(connection, gun_u, None, "manual") != (0, 0) or (
                        dbq.owned_count(connection, gun_u) != 2):
                    problems.append(f"{label}: trade_spare touched a built gun "
                                    f"(mastered={mastered})")
            if dbq.trade_built(connection, gun_u, 1, "manual") != (2, 1) or (
                    dbq.owned_count(connection, gun_u) != 1):
                problems.append(f"{label}: trade_built did not take one of two")
            dbq.set_item_count(connection, gun_u, 2, "manual")
            gone = dbq.build_one_set(connection, set_u, 1, "manual")
            if (dbq.owned_count(connection, gun_u) != 0
                    or dbq.item_built(connection, set_u) != 1
                    or not any(k == "item" and u == gun_u for k, u, *_ in gone)):
                problems.append(f"{label}: a build left {dbq.owned_count(connection, gun_u)} "
                                f"guns and {dbq.item_built(connection, set_u)} set(s); "
                                f"took {[(k, n) for k, _u, n, *_ in gone]}")
            report.append(f"{gun_name} 2/2 of {total}")
        finally:
            connection.execute(f"DELETE FROM owned_parts WHERE part_unique IN ({marks})", names)
            connection.execute(f"DELETE FROM owned_items WHERE item_unique IN ({marks})", names)
            for table, rows in saved.items():
                for row in rows:
                    connection.execute(
                        f"INSERT INTO {table} ({','.join(row.keys())}) "
                        f"VALUES ({','.join('?' * len(row.keys()))})", tuple(row))
            connection.commit()
    print(f"akimbo primes      : {', '.join(report)}")


def _check_a_sold_set_leaves_the_shelf(app, cfg, problems: list) -> None:
    """One click has to be able to say "I traded that away".

    Three mastered sets given away at once leave every piece of them as a row
    in a table that would have to be found by hand, four at a time, and a
    tool that makes correcting it a chore is a tool that goes stale.

    What must survive the removal is the mastery: those sets are worth giving
    away precisely because the points are already banked, and taking them back
    would undo the reason for the trade.
    """
    from wft.data import db as dbq
    from ui.views.ducat_view import DucatView

    connection = dbq.connect(cfg.db_path)
    row = connection.execute(
        "SELECT p.item_unique, COUNT(*) AS kinds FROM parts p "
        "JOIN items i ON i.unique_name = p.item_unique "
        "WHERE p.alias_of IS NULL AND i.category = 'warframe' "
        "GROUP BY p.item_unique HAVING kinds >= 4 LIMIT 1").fetchone()
    if row is None:
        return
    item = row["item_unique"]

    for piece in connection.execute(
            "SELECT unique_name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL", (item,)).fetchall():
        dbq.set_part_count(connection, piece["unique_name"],
                           int(piece["count_needed"]), "video")
    dbq.set_item_count(connection, item, 0, "video")
    dbq.set_item_mastered_manually(connection, item, True)

    page = DucatView(cfg)
    entry = next((s for s in page._survey() if s["unique"] == item), None)
    if entry is None or entry["held"] != entry["total"]:
        problems.append("sold set: the fixture is not a sellable set")
        page.deleteLater()
        return

    from PySide6.QtWidgets import QMessageBox as _Box
    said_yes = staticmethod(lambda *a, **k: _Box.Yes)
    original, _Box.question = _Box.question, said_yes
    try:
        page._gave_away(entry)
    finally:
        _Box.question = original

    held, total = dbq.set_progress(connection, item)
    if held:
        problems.append(f"sold set: {held} of {total} piece(s) stayed behind")
    if not dbq.is_mastered(connection, item):
        problems.append("sold set: giving a set away took the mastery with it")
    if any(s["unique"] == item for s in page._survey()):
        problems.append("sold set: the set is still offered for selling")
    page.deleteLater()
    print(f"sold set           : {total} piece(s) off the shelf, mastery kept")


def _check_a_built_set_leaves_the_shelf(app, cfg, problems: list) -> None:
    """"Ready to build" has to be able to hear "I built it".

    The pieces are consumed when the foundry starts, and the import cannot
    see the result until the thing is finished, levelled and photographed -
    days later. Until then the page went on offering a set that was already
    in the oven.

    Mastery must not follow: a fresh build is rank 0. Marking it here would
    credit points the account has not earned and quietly move the ratio the
    whole page is about.
    """
    from wft.data import db as dbq
    from ui.views.plan_view import PlanView

    connection = dbq.connect(cfg.db_path)
    row = connection.execute(
        "SELECT p.item_unique, COUNT(*) AS kinds FROM parts p "
        "JOIN items i ON i.unique_name = p.item_unique "
        "WHERE p.alias_of IS NULL AND i.category = 'melee' "
        "GROUP BY p.item_unique HAVING kinds >= 3 LIMIT 1").fetchone()
    if row is None:
        return
    item = row["item_unique"]

    for piece in connection.execute(
            "SELECT unique_name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL", (item,)).fetchall():
        dbq.set_part_count(connection, piece["unique_name"],
                           int(piece["count_needed"]), "video")
    dbq.set_item_count(connection, item, 0, "video")
    dbq.set_item_mastered_manually(connection, item, False)

    page = PlanView(cfg)
    page.refresh()
    entry = next((s for s in page._survey() if s["unique"] == item), None)
    if entry is None or entry["held"] != entry["total"] or entry["mastered"]:
        problems.append("built set: the fixture is not a ready-to-build set")
        page.deleteLater()
        return

    from PySide6.QtWidgets import QMessageBox as _Box
    original, _Box.question = _Box.question, staticmethod(
        lambda *a, **k: _Box.Yes)
    try:
        page._built_it(entry)
    finally:
        _Box.question = original

    held, total = dbq.set_progress(connection, item)
    if held:
        problems.append(f"built set: {held} of {total} piece(s) stayed behind")
    if dbq.item_built(connection, item) != 1:
        problems.append("built set: no copy reached the arsenal")
    if dbq.is_mastered(connection, item):
        problems.append("built set: a fresh build was credited with mastery")
    page.deleteLater()
    print(f"built set          : {total} piece(s) into the foundry, rank 0")


def _check_the_keep_one_mark_is_set_in_the_item_dialog(app, cfg, problems: list) -> None:
    """The "keep one" box appears for a chain item only, writes at once, and
    the page that opened the dialog redraws. Done on the copy; the mark
    is cleared again."""
    from PySide6.QtWidgets import QCheckBox
    from ui.views.inventory_view import InventoryView
    from ui.widgets.item_detail import ItemDetail
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)
    chain = connection.execute(
        "SELECT unique_name, name FROM items WHERE name = 'Lex Prime'").fetchone()
    edges = dbq._consumers(connection)
    plain = connection.execute(
        "SELECT unique_name, name FROM items WHERE category = 'warframe' "
        "ORDER BY name").fetchall()
    plain = next((r for r in plain if r["unique_name"] not in edges), None)
    if chain is None or chain["unique_name"] not in edges or plain is None:
        problems.append("keep one: the database has no Lex Prime chain to test")
        return

    def boxes(unique, name):
        dialog = ItemDetail(connection, unique, name)
        found = dialog.findChildren(QCheckBox)
        return dialog, found

    dialog, found = boxes(plain["unique_name"], plain["name"])
    if found:
        problems.append(f"keep one: {plain['name']} (nothing is built from it) "
                        f"shows the box")
    dialog.deleteLater()

    dialog, found = boxes(chain["unique_name"], chain["name"])
    if len(found) != 1 or "Aklex Prime" not in found[0].text():
        problems.append(f"keep one: Lex Prime shows {[b.text() for b in found]}")
    else:
        found[0].setChecked(True)
        if not dbq.is_kept(connection, chain["unique_name"]) or not dialog.kept_changed:
            problems.append("keep one: ticking the box wrote nothing")
    dialog.deleteLater()

    # The opening page redraws when the mark changed.
    dbq.set_kept(connection, chain["unique_name"], False)
    view = InventoryView(cfg)
    redrawn = []
    view.refresh = lambda: redrawn.append(1)
    original = ItemDetail.exec

    def tick(self):
        self.findChildren(QCheckBox)[0].setChecked(True)
        return 0

    ItemDetail.exec = tick
    try:
        view._open_detail({"unique_name": chain["unique_name"], "name": chain["name"]})
    finally:
        ItemDetail.exec = original
    if not redrawn:
        problems.append("keep one: the Collection page did not redraw after the mark")
    dbq.set_kept(connection, chain["unique_name"], False)
    view.deleteLater()

    # The Collection page itself shows the Items pane, not the dialog, so the
    # box has to be there too, and the pane of the gun it goes into has to
    # count the kept copy beside the number and not in it.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem, QTableWidget
    from ui.views.library_view import LibraryView

    library = LibraryView(cfg)

    def pane(unique):
        entry = QListWidgetItem("x")
        entry.setData(Qt.UserRole, unique)
        library._show_item_detail(entry)
        return library.item_detail_layout.parentWidget()

    found = pane(chain["unique_name"]).findChildren(QCheckBox)
    if len(found) != 1:
        problems.append(f"keep one: the Items pane of Lex Prime shows "
                        f"{len(found)} box(es)")
    else:
        found[0].setChecked(True)
        for _ in range(3):
            app.processEvents()
        again = library.item_detail_layout.parentWidget().findChildren(QCheckBox)
        if not dbq.is_kept(connection, chain["unique_name"]):
            problems.append("keep one: ticking the Items pane box wrote nothing")
        elif not any(box.isChecked() for box in again):
            problems.append("keep one: the Items pane was not drawn again ticked")
        aklex = connection.execute(
            "SELECT unique_name FROM items WHERE name = 'Aklex Prime'").fetchone()
        if aklex is not None:
            tables = pane(aklex["unique_name"]).findChildren(QTableWidget)
            cells = [t.item(r, 3).text() for t in tables
                     for r in range(t.rowCount())
                     if t.item(r, 0) is not None and t.item(r, 0).text() == "Lex Prime"
                     and t.item(r, 3) is not None]
            if not cells or "(1 kept)" not in cells[0] and "(1 to keep)" not in cells[0]:
                problems.append(f"keep one: Aklex Prime's pane shows Lex Prime as {cells}")
    dbq.set_kept(connection, chain["unique_name"], False)
    library.deleteLater()
    connection.close()
    print("keep one           : box only on a chain item, in the dialog and the "
          "Items pane, written at once, pages redraw")


def _check_the_collection_marks_mastery(app, cfg, problems: list) -> None:
    """The list has to say what has already been levelled.

    "built" was the only state a row could carry, and it answers a different
    question: a copy in the arsenal is not a rank thirty, and a rank thirty
    that was sold months ago leaves no copy at all. Whether something has been
    mastered is a plain question, and the answer was nowhere on the page,
    though the database has held the flag all along and the status bar counts it.

    The filter is tested through the query, not the rendered list: filtering
    after a LIMIT of 500 would cut the alphabet at M and say nothing about it.
    """
    from PySide6.QtCore import Qt

    from wft.data import db as dbq
    from ui.views.library_view import LibraryView, MASTERED, UNMASTERED, MARK

    connection = dbq.connect(cfg.db_path)
    rows = connection.execute(
        "SELECT unique_name, name FROM items WHERE category = 'melee' "
        "AND name LIKE '%Prime' ORDER BY name LIMIT 2").fetchall()
    if len(rows) < 2:
        return
    banked, open_ = rows[0], rows[1]
    dbq.set_item_mastered_manually(connection, banked["unique_name"], True)
    dbq.set_item_mastered_manually(connection, open_["unique_name"], False)

    page = LibraryView(cfg)

    def shown(row, *chips) -> str | None:
        """The label this item gets, under the given chips, or None if gone."""
        page.item_chips._buttons[ALL_KEY].click()
        for chip in chips:
            page.item_chips._buttons[chip].click()
        page.item_search.setText(row["name"])
        page._reload_items()
        for index in range(page.item_list.count()):
            entry = page.item_list.item(index)
            if entry.data(Qt.UserRole) == row["unique_name"]:
                return entry.text()
        return None

    marked = shown(banked)
    plain = shown(open_)
    if marked is None or not marked.startswith(MARK):
        problems.append(f"collection: {banked['name']} is mastered and its "
                        f"row reads {marked!r}")
    if plain is None or plain.startswith(MARK):
        problems.append(f"collection: {open_['name']} is not mastered and its "
                        f"row reads {plain!r}")

    # The names have to stay in one column. A mark on some rows and nothing on
    # the others is a list that jitters down its own left edge.
    if plain is not None and plain.startswith(open_["name"]):
        problems.append("collection: an unmastered row has no placeholder, so "
                        "the names no longer line up")

    if shown(banked, UNMASTERED) is not None:
        problems.append("collection: 'not mastered' kept a mastered item")
    if shown(open_, UNMASTERED) is None:
        problems.append("collection: 'not mastered' dropped an open item")
    if shown(banked, MASTERED) is None:
        problems.append("collection: 'mastered' dropped a mastered item")
    if shown(open_, MASTERED) is not None:
        problems.append("collection: 'mastered' kept an open item")

    # Both chips together say the same as neither: every row, not none.
    if shown(banked, MASTERED, UNMASTERED) is None:
        problems.append("collection: both mastery chips together emptied the "
                        "list instead of leaving it alone")

    # Thirteen chips in a window that fits nine. A row that answers the
    # shortfall by shrinking its children answers it by eliding every label:
    # "W...s", "P...y", "S...y" - a filter nobody can read. Every chip must
    # get the width it asks for, whatever that costs in lines.
    page.item_chips.setFixedWidth(1100)
    page.item_chips.adjustSize()
    app.processEvents()
    squeezed = [button.text() for button in page.item_chips._buttons.values()
                if button.width() < button.sizeHint().width()]
    if squeezed:
        problems.append(f"collection: {len(squeezed)} chip(s) were squeezed "
                        f"narrower than their label, e.g. {squeezed[0]!r}")
    lines = {button.y() for button in page.item_chips._buttons.values()}
    if len(lines) < 2:
        problems.append("collection: the chips did not wrap at 1100px, so "
                        "they are being squeezed into one line")

    # The detail beside the list has to say it too, in the sentence that is
    # already there for the pieces and the copies.
    from ui.widgets import item_detail

    line = item_detail.state_line(connection, banked["unique_name"])
    if "mastered" not in line or "not mastered" in line:
        problems.append(f"collection detail: a mastered set reads {line!r}")
    line = item_detail.state_line(connection, open_["unique_name"])
    if "not mastered" not in line:
        problems.append(f"collection detail: an open set reads {line!r}")

    page.deleteLater()
    print(f"collection mastery : {banked['name']} marked, "
          f"{open_['name']} left open")

def _check_settings_tabs_and_quick_missions(app, cfg, problems: list) -> None:
    """Settings has six tabs, and the Fissures tab decides what is quick.

    The tabs: order, what each holds, every former control still inside one.
    The quick missions: a box writes the list to the config in the test's
    folder and `is_quick` follows at once; reset restores the shipped two; a
    jump through `show_tab` lands on the tab asked for.
    """
    from datetime import datetime, timedelta, timezone

    from PySide6.QtWidgets import QCheckBox, QGroupBox, QPushButton

    from ui import settings_io
    from ui.views.fissure_view import FissureView
    from ui.views.settings_view import SettingsView
    from wft.world import fissures as fissures_mod

    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not str(settings_io.target_config_path()).startswith(scratch):
        problems.append("settings tabs: the config would be written outside "
                        "the test's folder; not checked")
        return
    saved = cfg.get("fissures.quick_missions")
    page = board_page = None
    try:
        page = SettingsView(cfg)
        want = {"Appearance": ["Appearance"], "Game data": ["Reference data"],
                "Capture": ["Capture", "Reward pictures"],
                "Rewards": ["Rewards page", "Order of the recommendation"],
                "Fissures": ["Quick missions"],
                "Reset": ["Reset"]}
        names = [page.tabs.tabText(i) for i in range(page.tabs.count())]
        if names != list(want):
            problems.append(f"settings tabs: the tabs are {names}")
        for index, name in enumerate(names):
            if name not in want:
                continue
            groups = [g.title() for g in page.tabs.widget(index).findChildren(QGroupBox)]
            if groups[:len(want[name])] != want[name]:
                problems.append(f"settings tabs: {name} holds {groups}, "
                                f"not {want[name]}")
        # Every control the page had before the tabs is on one of them.
        for attr in ("theme_box", "font_slider", "learned_label", "update_btn",
                     "force_btn", "auto_box", "log_view", "window_class_edit",
                     "hotkey_edit", "regions_status", "notify_box", "keep_box",
                     "keep_dir_edit", "keep_days_spin", "log_folder_btn",
                     "quick_reset_btn", "whatsnew_button"):
            widget = getattr(page, attr, None)
            if widget is None:
                problems.append(f"settings tabs: {attr} is gone")
            elif attr != "whatsnew_button" and page.tabs.indexOf(
                    next((w for w in (page.tabs.widget(i) for i in range(6))
                          if w.isAncestorOf(widget)), None)) < 0:
                problems.append(f"settings tabs: {attr} is on no tab")
        if page.rewards_layout is None:
            problems.append("settings tabs: no layout left on Rewards")

        # Jumps: each name lands on its tab, an unknown one changes nothing.
        for name in names:
            page.show_tab(name)
            if page.tabs.tabText(page.tabs.currentIndex()) != name:
                problems.append(f"settings tabs: show_tab({name!r}) landed on "
                                f"{page.tabs.tabText(page.tabs.currentIndex())!r}")
        page.show_tab("Nope")
        if page.tabs.tabText(page.tabs.currentIndex()) != names[-1]:
            problems.append("settings tabs: an unknown tab moved the page")

        # The boxes show the shipped choice and cover the mission types.
        cfg.override("fissures.quick_missions", list(fissures_mod.QUICK_MISSIONS))
        page._sync_quick_boxes()
        if list(page.quick_boxes) != list(fissures_mod.MISSION_TYPES):
            problems.append("settings tabs: boxes are not the mission types")
        if {m for m, b in page.quick_boxes.items() if b.isChecked()} != {
                "Capture", "Extermination"}:
            problems.append("settings tabs: the default boxes are not Capture "
                            "and Extermination")

        later = datetime.now(timezone.utc) + timedelta(hours=1)
        spy = fissures_mod.Fissure("Lith", 1, "A", "Spy", "Orokin", later, False, False)
        ext = fissures_mod.Fissure("Lith", 1, "B", "Extermination", "Orokin",
                                   later, False, False)
        storm = fissures_mod.Fissure("Lith", 1, "C", "Spy", "Orokin", later, False, True)
        if spy.is_quick or not ext.is_quick:
            problems.append("settings tabs: the default quick set is wrong")

        page.quick_boxes["Spy"].setChecked(True)
        page.quick_boxes["Extermination"].setChecked(False)
        written = settings_io.target_config_path().read_text()
        if 'quick_missions = ["Capture", "Spy"]' not in written:
            problems.append(f"settings tabs: the list was not written: {written!r}")
        import tomllib

        reread = tomllib.loads(written).get("fissures", {}).get("quick_missions")
        if reread != ["Capture", "Spy"]:
            problems.append(f"settings tabs: the file reads back as {reread!r}")
        if cfg.get("fissures.quick_missions") != ["Capture", "Spy"]:
            problems.append("settings tabs: the running config did not change")
        if not spy.is_quick or ext.is_quick or storm.is_quick:
            problems.append("settings tabs: is_quick did not follow the boxes "
                            "(a storm must stay out)")

        # The Fissures page shows it without a restart.
        board_page = FissureView(cfg)
        board_page._got_board([spy, ext])
        marked = {board_page._board_table.item(r, 1).text():
                  board_page._board_table.item(r, 2).toolTip() != ""
                  for r in range(board_page._board_table.rowCount())}
        if marked != {"A": True, "B": False}:
            problems.append(f"settings tabs: the Fissures page marks {marked}")

        page.quick_reset_btn.click()
        if (cfg.get("fissures.quick_missions") != ["Capture", "Extermination"]
               ):
            problems.append("settings tabs: reset did not restore the default")
        if spy.is_quick or not ext.is_quick:
            problems.append("settings tabs: after reset is_quick is not the default")
        if "quick_missions" in settings_io.target_config_path().read_text():
            problems.append("settings tabs: reset left the key in the file")
        if {m for m, b in page.quick_boxes.items() if b.isChecked()} != {
                "Capture", "Extermination"}:
            problems.append("settings tabs: reset left the boxes unchanged")

        # An empty choice is a choice: nothing is quick.
        for box in page.quick_boxes.values():
            box.setChecked(False)
        if cfg.get("fissures.quick_missions") != [] or ext.is_quick:
            problems.append("settings tabs: unticking everything did not empty the list")
    finally:
        settings_io.remove_keys(cfg, "fissures", ["quick_missions"])
        if saved is not None:
            cfg.override("fissures.quick_missions", saved)
        for widget in (page, board_page):
            if widget is not None:
                widget.deleteLater()
    print("settings tabs      : six tabs in order, quick missions written, "
          "followed by is_quick and the page, reset restores the two")


def _check_the_recommendation_order_in_settings(app, cfg, problems: list) -> None:
    """Settings -> Rewards holds the order of the recommendation.

    A fixed screen: a melee part that finishes its set against a Warframe part
    that starts one. By default finishing wins. Moving "Item type" to the top
    with the arrows writes the temporary config and the Warframe wins; item
    type numbers do the same the other way round; reset restores the default
    in the file, in the running config, on the page and in the ranking.
    """
    import tomllib

    from PySide6.QtWidgets import QGroupBox

    from ui import settings_io
    from ui.views.settings_view import SettingsView
    from wft.relic import score as S

    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not str(settings_io.target_config_path()).startswith(scratch):
        problems.append("recommendation order: the config would be written "
                        "outside the test's folder; not checked")
        return

    def screen():
        def part(name, set_owned, set_total, category, position):
            return S.Reward(
                position=position, raw_text=name, part_unique=f"/p/{name}",
                name=name, item_name=name.split(" Prime")[0] + " Prime",
                ducats=15, reach="farmable", active_relics=3, own_chance=0.5,
                set_owned=set_owned, set_total=set_total, category=category)

        return [part("Dakra Prime Blade", 2, 3, "melee", 1),
                part("Titania Prime Neuroptics", 0, 4, "warframe", 2)]

    def pick():
        return S.rank(screen(), S.order_from_config(cfg)).best.name

    def stored():
        path = settings_io.target_config_path()
        return tomllib.loads(path.read_text()).get("ranking", {}) \
            if path.is_file() else {}

    page = other = None
    try:
        page = SettingsView(cfg)
        index = next(i for i in range(page.tabs.count())
                     if page.tabs.tabText(i) == "Rewards")
        titles = [g.title() for g in page.tabs.widget(index).findChildren(QGroupBox)]
        if "Order of the recommendation" not in titles:
            problems.append(f"recommendation order: the card is not on the "
                            f"Rewards tab ({titles})")
        default_texts = [label.text() for label in page.rank_names]
        if (len(default_texts) != len(S.CRITERIA)
                or [label.text() for label in page.rank_numbers]
                != [f"{n}." for n in range(1, len(S.CRITERIA) + 1)]):
            problems.append("recommendation order: the list is not numbered "
                            "1 to 9")
        if page.ranking_criteria() != list(S.DEFAULT_CRITERIA):
            problems.append("recommendation order: not the default at the start")
        if {k: s.value() for k, s in page.type_spins.items()} != S.DEFAULT_TYPE_NUMBERS:
            problems.append("recommendation order: the type numbers are not "
                            "the default at the start")
        if page.rank_up_btns[0].isEnabled() or page.rank_down_btns[-1].isEnabled():
            problems.append("recommendation order: the first line can move up "
                            "or the last down")
        if pick() != "Dakra Prime Blade":
            problems.append(f"recommendation order: the default pick is {pick()}")
        if stored():
            problems.append("recommendation order: opening the page wrote the "
                            "config")

        # Item type to the top: four clicks on its up arrow.
        row = S.DEFAULT_CRITERIA.index("type")
        for at in range(row, 0, -1):
            page.rank_up_btns[at].click()
        if page.ranking_criteria()[0] != "type":
            problems.append(f"recommendation order: moving up gave "
                            f"{page.ranking_criteria()}")
        if stored().get("criteria") != page.ranking_criteria():
            problems.append(f"recommendation order: the file holds "
                            f"{stored().get('criteria')}")
        if cfg.get("ranking.criteria") != page.ranking_criteria():
            problems.append("recommendation order: the running config did not "
                            "change")
        if page.rank_names[0].text() != "Item type" or page.rank_numbers[0].text() != "1.":
            problems.append("recommendation order: the list does not show the move")
        if pick() != "Titania Prime Neuroptics":
            problems.append(f"recommendation order: with the type first the pick "
                            f"is {pick()}")
        # And one step down again.
        page.rank_down_btns[0].click()
        if page.ranking_criteria().index("type") != 1:
            problems.append("recommendation order: the down arrow did not move")
        page.rank_up_btns[1].click()

        # The numbers: melee 1, Warframe 2 puts the melee part first again.
        page.type_spins["melee"].setValue(1)
        page.type_spins["warframe"].setValue(2)
        if stored().get("types") != {"melee": 1, "warframe": 2}:
            problems.append(f"recommendation order: the numbers in the file are "
                            f"{stored().get('types')}")
        if pick() != "Dakra Prime Blade":
            problems.append(f"recommendation order: with melee 1 above Warframe 2 "
                            f"the pick is {pick()}")

        # A second page shows what was saved.
        other = SettingsView(cfg)
        if other.ranking_criteria()[0] != "type" or other.type_spins["melee"].value() != 1:
            problems.append("recommendation order: a new page does not show "
                            "the saved order")

        page.rank_reset_btn.click()
        if stored().get("criteria") is not None or stored().get("types"):
            problems.append(f"recommendation order: reset left {stored()} in "
                            f"the file")
        if S.order_from_config(cfg) != S.DEFAULT_ORDER:
            problems.append("recommendation order: reset did not restore the "
                            "default order")
        if pick() != "Dakra Prime Blade":
            problems.append(f"recommendation order: after reset the pick is {pick()}")
        if ([label.text() for label in page.rank_names] != default_texts
                or {k: s.value() for k, s in page.type_spins.items()}
                != S.DEFAULT_TYPE_NUMBERS):
            problems.append("recommendation order: reset left the page unchanged")
    finally:
        settings_io.remove_keys(cfg, "ranking", ["criteria"])
        settings_io.remove_keys(cfg, "ranking.types", list(S.ITEM_TYPES))
        for widget in (page, other):
            if widget is not None:
                widget.deleteLater()
    print("recommendation     : the card is on the Rewards tab; arrows and "
          "numbers write the config and change the pick; reset restores the "
          "default")


def _check_the_fissure_page_shows_a_board(app, cfg, problems: list) -> None:
    """The board, and the one filter that has to reach both tables.

    Driven with a made-up board rather than the live one. What is being tested
    is the page, and a test that needs somebody else's server to answer is a
    test that fails on a train.

    The case worth pinning is Omnia. It is not an era, it is a fissure that
    takes any relic, so filtering to Lith must keep it: hiding it would remove
    the one open door for a Lith relic on an evening with no Lith fissure.
    """
    from datetime import datetime, timedelta, timezone
    from ui.views.fissure_view import FissureView
    from wft.world.fissures import Fissure

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    board = [
        Fissure("Lith", 1, "Everest (Earth)", "Excavation", "Grineer",
                later, False, False),
        Fissure("Axi", 4, "Stribog (Void)", "Sabotage", "Orokin",
                later, False, False),
        Fissure("Meso", 2, "Carpo (Jupiter)", "Extermination", "Corpus",
                later, True, False),
        Fissure("Omnia", 5, "Circulus (Lua)", "Extermination", "Orokin",
                later, False, True),
    ]

    # Capture and Extermination are the short runs, and a Void Storm is never
    # one whatever it is called: Railjack wraps a ship fight around it and the
    # payload gives both the same mission name.
    quick = {(f.node, f.is_quick) for f in board}
    if quick != {("Everest (Earth)", False), ("Stribog (Void)", False),
                 ("Carpo (Jupiter)", True), ("Circulus (Lua)", False)}:
        problems.append(f"fissures: wrong runs marked quick — {sorted(quick)}")

    page = FissureView(cfg)
    page._got_board(board)
    if page._board_table is None or page._board_table.rowCount() != len(board):
        problems.append("fissures: the board did not render its rows")
        page.deleteLater()
        return

    def visible():
        return {page._board_rows[r].tier
                for r in range(page._board_table.rowCount())
                if not page._board_table.isRowHidden(r)}

    if visible() != {"Lith", "Axi", "Meso", "Omnia"}:
        problems.append(f"fissures: unfiltered board shows {visible()}")

    if page._era_chips is None:
        problems.append("fissures: no era chips were built")
    else:
        page._era_chips._buttons["Lith"].click()
        if visible() != {"Lith", "Omnia"}:
            problems.append(f"fissures: filtering to Lith shows {visible()} — "
                            f"Omnia takes any era and must survive it")
        page._era_chips._buttons[ALL_KEY].click()

    page._board_chips._buttons["hard"].click()
    if visible() != {"Meso"}:
        problems.append(f"fissures: the Steel Path board shows {visible()}")
    page._board_chips._buttons[ALL_KEY].click()

    # Every heading, with a filter live on the table: the rows move and the
    # chips hide by index, so a sort that forgot to filter again would hide
    # whichever rows happened to land where the hidden ones used to be.
    page._era_chips._buttons["Lith"].click()
    _reordering_holds(page, "fissures", problems)
    if visible() != {"Lith", "Omnia"}:
        problems.append(f"fissures: after reordering, the Lith filter shows "
                        f"{visible()}")
    page._era_chips._buttons[ALL_KEY].click()

    # And the half that must not depend on the network at all.
    page._board_failed("no route to host")
    if page._openings_table is None and page._openings_rows:
        problems.append("fissures: an unreachable board took the openings "
                        "table with it")

    # An errored board must come back on its own. warframestat's poller has
    # fallen hours behind for real, and the page used to sit in its error note
    # until somebody pressed the button - the minute tick is expected to ask
    # again once the service's own cache window has passed, and not before.
    import time as time_mod
    asks = []
    page._fetch = lambda force=False: asks.append(True)
    page._last_asked = 0.0
    page._retime()
    if not asks:
        problems.append("fissures: an errored board is never asked again")
    asks.clear()
    page._last_asked = time_mod.monotonic()
    page._retime()
    if asks:
        problems.append("fissures: the retry ignores the service's cache "
                        "cadence and asks on every tick")

    # A rebuild happens while somebody is looking at this page - the board
    # answers a second or two after it opens. The chips pressed must be the
    # chips the filter reads, and the old row must be gone rather than merely
    # scheduled for deletion: a press on "Neo" landed on a row that had already
    # been replaced, the filter read the new row saying "every era", and the table
    # did not move.
    page._got_board(board)
    app.processEvents()
    stale = page._era_chips
    page._got_board(board)
    app.processEvents()
    if stale is page._era_chips:
        problems.append("fissures: the chips survived a rebuild")
    elif stale.parent() is not None:
        problems.append("fissures: the replaced chip row is still parented, "
                        "so it is still on screen and still clickable")

    if page._openings_rows:
        page._era_chips._buttons[page._era_chips._buttons and
                                 page._openings_rows[0].tier].click()
        hidden = sum(1 for r in range(page._openings_table.rowCount())
                     if page._openings_table.isRowHidden(r))
        if not hidden:
            problems.append("fissures: filtering to one era hid nothing")

    # One figure, a chance, and the Radiant one beside it in percent.
    if page._openings_table is not None:
        table = page._openings_table
        # The sorted heading carries an arrow after its name.
        heads = [table.horizontalHeaderItem(c).text().rstrip(" ▾▴")
                 for c in range(3, 5)]
        if heads != ["Chance", "At Radiant"]:
            problems.append(f"fissures: openings headings are {heads}")
        for r in range(table.rowCount()):
            chance, radiant = table.item(r, 3).text(), table.item(r, 4).text()
            row = page._openings_rows[r]
            if (not chance.endswith(" %") or not radiant.endswith(" %")
                    or chance != openings_percent(row.expected)):
                problems.append(f"fissures: openings row {r} shows "
                                f"{chance!r} / {radiant!r}")
                break

    # An answer of "nothing" is not the same as no answer yet.
    page._got_board([])
    app.processEvents()
    if not page.answered:
        problems.append("fissures: an empty board did not count as an answer")
    page.deleteLater()
    print(f"fissure board      : {len(board)} row(s), era and board filters")


def openings_percent(value):
    from wft.relic import openings
    return openings.percent(value)


def _check_the_official_feed_stands_in(problems: list) -> None:
    """The raw world state becomes a board when warframestat cannot.

    warframestat's poller once lagged for the better part of a day while the
    game's own feed stayed live, so active() now asks the official endpoint
    before showing an error. Pinned here: the raw-to-cooked translation
    (eras, mission names, Mongo timestamps, node table with and without a
    hit), that the stand-in answers only when the primary cannot, and that
    the primary's own error survives when both fail.
    """
    from datetime import datetime, timedelta, timezone
    from wft.world import fissures as fissures_mod

    def ms(when: datetime) -> dict:
        return {"$date": {"$numberLong": str(int(when.timestamp() * 1000))}}

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    nodes = {"SolNode1": {"value": "Galatea (Neptune)", "enemy": "Corpus",
                          "type": "Capture"},
             "CrewBattleNode1": {"value": "Ogal Cluster (Earth)",
                                 "enemy": "Grineer", "type": "Skirmish"}}
    payload = {
        "ActiveMissions": [
            {"Modifier": "VoidT1", "Node": "SolNode1",
             "MissionType": "MT_CAPTURE", "Expiry": ms(later)},
            {"Modifier": "VoidT4", "Node": "SolNode999",
             "MissionType": "MT_ARTIFACT", "Expiry": ms(later), "Hard": True},
            {"Node": "SolNode2", "MissionType": "MT_CAPTURE",
             "Expiry": ms(later)},  # no Modifier: skipped, not fatal
        ],
        "VoidStorms": [
            {"ActiveMissionTier": "VoidT6", "Node": "CrewBattleNode1",
             "Expiry": ms(later)},
        ],
    }
    board = fissures_mod._parse_worldstate(payload, nodes)
    seen = {(f.tier, f.node, f.mission, f.kind, f.is_quick) for f in board}
    want = {("Lith", "Galatea (Neptune)", "Capture", "normal", True),
            ("Axi", "SolNode999", "Disruption", "hard", False),
            ("Omnia", "Ogal Cluster (Earth)", "Skirmish", "storm", False)}
    if seen != want:
        problems.append(f"worldstate: raw feed translated to {sorted(seen)}")
    if fissures_mod._mission_name("MT_MOBILE_DEFENSE") != "Mobile Defense":
        problems.append("worldstate: MT_MOBILE_DEFENSE did not title-case")

    class _Cfg:
        def __init__(self, values):
            self.values = values

        def get(self, dotted, default=None):
            return self.values.get(dotted, default)

    # Unconfigured stand-in asks nobody and answers nothing.
    if fissures_mod._stand_in(_Cfg({})) != []:
        problems.append("worldstate: an unconfigured stand-in answered")

    # The wiring: primary fails -> stand-in answers; both fail -> the
    # primary's own error is the one shown.
    real_active = fissures_mod.active.replaced
    saved = (fissures_mod._from_primary, fissures_mod._stand_in,
             fissures_mod._cache)
    try:
        def fail(_cfg):
            raise fissures_mod.FissureError("primary is stale")

        fissures_mod._cache = None
        fissures_mod._from_primary = fail
        fissures_mod._stand_in = lambda _cfg: board
        if real_active(_Cfg({}), force=True) != board:
            problems.append("worldstate: a failing primary did not fall "
                            "back to the official feed")
        fissures_mod._cache = None
        fissures_mod._stand_in = lambda _cfg: []
        try:
            real_active(_Cfg({}), force=True)
        except fissures_mod.FissureError as exc:
            if "primary is stale" not in str(exc):
                problems.append(f"worldstate: both sources failed but the "
                                f"error shown was: {exc}")
        else:
            problems.append("worldstate: both sources failed and nobody "
                            "said so")
    finally:
        (fissures_mod._from_primary, fissures_mod._stand_in,
         fissures_mod._cache) = saved
    print(f"worldstate feed    : {len(board)} row(s) translated, "
          f"fallback wiring holds")


def _old_plan_survey(conn):
    """The Plan page's survey as it was before it moved to `wft.data.survey`.

    Copied verbatim (self.conn -> conn) so the move can be checked against the
    thing it replaced, on whatever the database looks like at that point.
    """
    from wft.data import db as dbq

    rows = conn.execute(
        "SELECT i.unique_name, i.name, i.category FROM items i "
        "WHERE i.unique_name IN (SELECT DISTINCT item_unique FROM parts) "
        "AND i.category NOT IN ('reward_other', 'component_only') "
        "ORDER BY i.name").fetchall()
    survey = []
    for row in rows:
        held, total = dbq.set_progress(conn, row["unique_name"])
        if not total:
            continue
        built = dbq.item_built(conn, row["unique_name"])
        if held == 0 and not built:
            continue
        missing = []
        for part in ([] if built else conn.execute(
                "SELECT unique_name, name, count_needed FROM parts "
                "WHERE item_unique=? AND alias_of IS NULL", (row["unique_name"],))):
            short = part["count_needed"] - dbq.owned_count(conn, part["unique_name"])
            if short > 0:
                vaulted, _share = dbq.vault_state(conn, part["unique_name"])
                missing.append((part["name"], short, vaulted))
        survey.append({
            "name": row["name"], "category": row["category"],
            "held": held, "total": total, "built": built, "missing": missing,
            "unique": row["unique_name"],
            "mastered": dbq.is_mastered(conn, row["unique_name"]),
        })
    return survey


def _check_the_survey_moved_unchanged(app, cfg, problems: list) -> None:
    """The Plan page asks `set_survey` and gets what its own method used to give.

    Also: asking for the unstarted sets adds sets with nothing held and changes
    nothing else about the sets the page already got.
    """
    from ui.views.plan_view import PlanView
    from wft.data import db as dbq
    from wft.data.survey import set_survey

    connection = dbq.connect(cfg.db_path)
    old = _old_plan_survey(connection)
    new = set_survey(connection)
    if new != old:
        problems.append(f"survey: set_survey differs from the old Plan survey "
                        f"({len(new)} vs {len(old)} sets)")
    page = PlanView(cfg)
    if page._survey() != old:
        problems.append("survey: PlanView._survey differs from the old survey")
    page.deleteLater()
    wider = set_survey(connection, include_unstarted=True)
    kept = [entry for entry in wider if entry in old]
    if kept != old:
        problems.append("survey: include_unstarted changed sets the page already had")
    if not len(wider) > len(old):
        problems.append("survey: include_unstarted added no unstarted set")
    connection.close()
    print(f"survey             : {len(old)} set(s) identical, {len(wider)} with unstarted")


def _check_built_sets_free_their_parts_and_nothing_else(cfg, problems: list) -> None:
    """A build moves the Ducats page's spare list for built, unmastered items only.

    The previous `wanted_items` is copied here; every part whose floor differs
    between it and the live `retained_counts` must belong to an item that is
    built and not mastered. Everything else keeps its floor.
    """
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)
    wanted = {row["unique_name"] for row in connection.execute(
        "SELECT i.unique_name FROM items i "
        "LEFT JOIN owned_items o ON o.item_unique = i.unique_name "
        "WHERE COALESCE(o.mastered, 0) = 0")}
    edges = dbq._consumers(connection)
    growing = True
    while growing:
        growing = False
        for child, parents in edges.items():
            if child not in wanted and not parents.keys().isdisjoint(wanted):
                wanted.add(child)
                growing = True
    old = {}
    copies = {}
    for row in connection.execute(
            "SELECT p.unique_name, p.count_needed, p.item_unique, i.category "
            "FROM parts p LEFT JOIN items i ON i.unique_name = p.item_unique"):
        released = (row["item_unique"] is not None and row["category"] is not None
                    and row["category"] not in dbq._NEVER_RELEASED
                    and row["item_unique"] not in wanted)
        if released:
            old[row["unique_name"]] = 0
            continue
        owner = row["item_unique"]
        multiplier = (1 if owner is None
                      else dbq._missing_copies(connection, edges, owner, copies,
                                               wanted=wanted))
        old[row["unique_name"]] = int(row["count_needed"]) * multiplier
    new = dbq.retained_counts(connection)
    moved = [u for u in new if new[u] != old[u]]
    built_open = {row["item_unique"] for row in connection.execute(
        "SELECT item_unique FROM owned_items WHERE count > 0 AND mastered = 0")}
    owner_of = dict(connection.execute("SELECT unique_name, item_unique FROM parts"))
    # The floor of a chain item whose built copies cover the recipes above it
    # is lowered too, built-and-open or not; Lex Prime is mastered.
    stray = [u for u in moved if new[u] > old[u] or (
        owner_of[u] not in built_open and owner_of[u] not in edges)]
    if stray:
        problems.append(f"{len(stray)} part(s) changed their floor without "
                        f"belonging to a built, unmastered item, e.g. {stray[0]}")
    connection.close()
    print(f"built-set spares   : {len(moved)} part(s) freed by a build, none else moved")


def _check_mastered_sets_let_their_parts_go(problems: list) -> None:
    """A set whose mastery is banked keeps nothing back.

    The floor under every spare pile used to be the recipe, always: hold two
    Acceltra Prime Receivers where the recipe wants one and exactly one was
    offered, though the weapon was built, levelled and its mastery banked
    months ago. Keeping a set back for a weapon that is already mastered makes
    no sense, so the logic was wrong.

    So the floor is now the recipe only while the set is still worth building,
    and that question has an answer the database already holds. What makes it
    more than a mastery flag is the chain: Bronco Prime is mastered and will
    never be built again for its own sake, but Akbronco Prime is built from two
    of them and is not mastered - so its pieces stay on the shelf.

    Driven against a database made here rather than a real one: every case
    below is a shape, and waiting for a real inventory to grow one is not a test.
    """
    import pathlib
    import tempfile

    from wft.data import db as dbq

    home = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(home / "warframe.sqlite")

    def item(unique, name, category="weapon"):
        connection.execute(
            "INSERT INTO items(unique_name, name, category) VALUES(?,?,?)",
            (unique, name, category))

    def part(unique, name, owner, needed=1, alias=None):
        connection.execute(
            "INSERT INTO parts(unique_name, name, item_unique, ducats, "
            "count_needed, alias_of) VALUES(?,?,?,45,?,?)",
            (unique, name, owner, needed, alias))

    def holds(unique, count):
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source) "
            "VALUES(?,?,'test')", (unique, count))

    def banked(unique):
        connection.execute(
            "INSERT INTO owned_items(item_unique, count, mastered, source) "
            "VALUES(?,0,1,'test')", (unique,))

    # A mastered gun that is still a component of an unmastered one, a mastered
    # gun that is nobody's component, a chain two deep, a self-referencing
    # consumable, an edge only the curated file knows, and three rows whose
    # owner cannot be established at all.
    item("W:bronco", "Bronco Prime")
    item("W:akbronco", "Akbronco Prime")
    item("W:braton", "Braton Prime")
    item("W:equinox", "Equinox Prime", "warframe")
    item("W:aspect", "Equinox Prime Day Aspect", "warframe")
    item("W:forma", "Forma", "reward_other")
    item("W:vasto", "Vasto")
    item("W:akvasto", "Akvasto")
    banked("W:bronco")
    banked("W:braton")
    banked("W:aspect")
    banked("W:vasto")

    part("P:bronco_barrel", "Bronco Prime Barrel", "W:bronco")
    part("W:bronco", "Bronco Prime", "W:akbronco", needed=2)
    part("P:braton_barrel", "Braton Prime Barrel", "W:braton")
    part("P:aspect_bp", "Equinox Prime Day Aspect Blueprint", "W:aspect")
    part("W:aspect", "Equinox Prime Day Aspect", "W:equinox")
    # Forma's blueprint is its own consumer in the export, and the recursion
    # has to survive that rather than chase it.
    part("W:forma", "Forma Blueprint", "W:forma")
    part("P:vasto_barrel", "Vasto Barrel", "W:vasto")
    part("P:nowhere", "Nowhere Part", None)
    part("P:ghost", "Ghost Part", "W:never-existed")
    part("P:braton_alias", "Braton Prime Barrel Blueprint", "W:braton",
         alias="P:braton_barrel")

    for unique, count in (("P:bronco_barrel", 3), ("P:braton_barrel", 1),
                          ("P:aspect_bp", 2), ("W:forma", 2),
                          ("P:vasto_barrel", 3), ("P:nowhere", 2),
                          ("P:ghost", 2), ("P:braton_alias", 5)):
        holds(unique, count)
    connection.commit()

    def spare_of(unique):
        return {row["unique_name"]: row
                for row in dbq.spare_parts(connection)}.get(unique)

    def expect(unique, spare, freed, why):
        row = spare_of(unique)
        if row is None:
            problems.append(f"spares: {unique} is not offered at all — {why}")
            return
        if int(row["spare"]) != spare:
            problems.append(f"spares: {unique} offers {row['spare']}, not "
                            f"{spare} — {why}")
        if bool(row["freed_by_mastery"]) != freed:
            problems.append(f"spares: {unique} says freed_by_mastery="
                            f"{bool(row['freed_by_mastery'])}, not {freed}")

    expect("P:bronco_barrel", 1, False,
           "no Bronco Prime is built and Akbronco Prime eats two, so two "
           "barrels are owed and only the third is spare")
    expect("P:braton_barrel", 1, True,
           "Braton Prime is mastered and nothing is built from it")
    expect("P:aspect_bp", 1, False,
           "the aspect is mastered but Equinox Prime is not")
    expect("W:forma", 1, False, "a consumable is never released by mastery")
    expect("P:vasto_barrel", 1, False,
           "only the curated list knows Akvasto is built from two Vastos, "
           "and its count of two has to reach the barrel under them")
    expect("P:nowhere", 1, False, "a part with no owner keeps its floor")
    expect("P:ghost", 1, False, "a part with an unknown owner keeps its floor")

    if spare_of("P:braton_alias") is not None:
        problems.append("spares: an alias row was offered as well as its "
                        "canonical part, which counts the same pile twice")

    # One of the two Broncos already built. This shape comes from a real shelf, and
    # the case that hid the multiplier for so long: with one copy standing, one
    # set of pieces is exactly what is left to find, so the floor drops back to
    # the recipe and the answer is the same either way.
    dbq.set_item_count(connection, "W:bronco", 1, "test")
    expect("P:bronco_barrel", 2, False,
           "one Bronco Prime is built, so only one more set of pieces is owed")

    # The chain, once its far end is banked too.
    dbq.set_item_mastered_manually(connection, "W:akbronco", True)
    expect("P:bronco_barrel", 3, True,
           "with Akbronco Prime mastered nothing wants a Bronco Prime")

    # And the half that actually writes. A row offered with no floor under it
    # must be able to go all the way to nothing: the kiosk took the piece, and
    # a page that shows it gone while the database still holds one is worse
    # than never offering it.
    before, after = dbq.trade_spare(connection, "P:braton_barrel")
    if (before, after) != (1, 0):
        problems.append(f"spares: trading a mastered set's last piece went "
                        f"{before} → {after}, not 1 → 0")
    if dbq.owned_count(connection, "P:braton_barrel") != 0:
        problems.append("spares: the traded piece is still on the shelf")

    connection.close()
    print("mastered spares    : 7 shape(s), chain and curated edge held")

def _check_the_ducat_page_counts_a_piece_once(cfg, problems: list) -> None:
    """Two ways to sell the same metal are not two piles of ducats.

    Once mastery began releasing a set's whole holding, every complete
    mastered set appeared twice on this page: piece by piece under "Spare
    parts" and whole under "Mastered sets". The headline added the two
    section totals, so Orthos Prime was counted once as three
    parts and again as a set - 2865 ducats on a shelf holding 2715.

    The other half is the write. A released row keeps nothing back, so
    trading it must leave zero; the old floor silently kept the last piece
    and the page went on claiming the player still had it after it had been handed
    over. That is the failure this page cannot be allowed to have, so the
    click is driven through the view rather than the query.
    """
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QMessageBox
    from ui.views.ducat_view import DucatView
    from wft.data import db as dbq

    home = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(home / "warframe.sqlite")
    connection.execute(
        "INSERT INTO items(unique_name, name, category) VALUES('i:pole', "
        "'Orthos Prime', 'weapon')")
    connection.execute(
        "INSERT INTO owned_items(item_unique, count, mastered, source) "
        "VALUES('i:pole', 0, 1, 'test')")
    for unique, name, needed, held in (("p:blade", "Blade", 2, 5),
                                       ("p:bp", "Blueprint", 1, 2),
                                       ("p:handle", "Handle", 2, 2)):
        connection.execute(
            "INSERT INTO parts(unique_name, name, item_unique, ducats, "
            "count_needed) VALUES(?,?,'i:pole',10,?)", (unique, name, needed))
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source) "
            "VALUES(?,?,'test')", (unique, held))
    # A piece of the same set the kiosk will not take. Baro trades for Prime
    # parts, so a plain blueprint carries no ducat value at all - and once
    # mastery released it, the page listed it with a dash where the price
    # goes, proposing a trade nobody will make.
    connection.execute(
        "INSERT INTO parts(unique_name, name, item_unique, ducats, "
        "count_needed) VALUES('p:token', 'Token', 'i:pole', NULL, 1)")
    connection.execute(
        "INSERT INTO owned_parts(part_unique, count, source) "
        "VALUES('p:token', 3, 'test')")
    connection.commit()
    connection.close()

    page_cfg = cfg.__class__({**cfg._data}, cfg.source)
    page_cfg.override("paths.data_dir", str(home))
    page = DucatView(page_cfg)
    page.refresh()

    spares = page._spares()
    sets = page._sellable_sets(page._survey())
    from_spares = sum(row["total"] for row in spares)
    from_sets = sum(ducats for _entry, ducats in sets)
    extra = page._only_as_a_set(sets)
    if not sets:
        problems.append("ducats: a complete mastered set was not offered whole")
    if from_sets and extra:
        problems.append(f"ducats: a set whose every piece is already spare "
                        f"still added {extra} to the headline")
    if from_spares + extra >= from_spares + from_sets and from_sets:
        problems.append("ducats: the headline still adds both lists outright")
    if not any(row["freed"] for row in spares):
        problems.append("ducats: a mastered set's pieces are not marked as "
                        "freed, so the user cannot see what is being traded")

    # Surplus in the database, absent from this page. The two questions are
    # different: what is spare stays true of a worthless blueprint, and the
    # inventory lists it for exactly that - but a page about the kiosk has
    # nothing to say about a piece the kiosk refuses.
    if any(row["unique"] == "p:token" for row in spares):
        problems.append("ducats: a part the kiosk will not take was offered "
                        "for ducats anyway")
    if not any(row["unique_name"] == "p:token"
               for row in dbq.spare_parts(page.conn)):
        problems.append("ducats: filtering the page hid the surplus from the "
                        "database as well, where it is still true")
    if from_spares + extra != 90:
        problems.append(f"ducats: dropping the untradeable row moved the "
                        f"headline to {from_spares + extra}, not 90")

    # The write, through the view. Held 2, recipe wants 2, mastery releases
    # both - so the row offers 2 and the shelf must end at zero.
    handle = next((row for row in spares if row["unique"] == "p:handle"), None)
    if handle is None or handle["spare"] != 2:
        problems.append(f"ducats: a mastered set's handle offered "
                        f"{handle['spare'] if handle else 'nothing'}, not 2")
    else:
        original = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        try:
            page._traded_spare(handle)
        finally:
            QMessageBox.question = original
        left = dbq.owned_count(page.conn, "p:handle")
        if left:
            problems.append(f"ducats: trading a released row left {left} on "
                            f"the shelf — the page offered what it will not "
                            f"write")
    page.deleteLater()
    print(f"ducat headline     : {from_spares} spare + {extra} set-only, "
          f"{from_sets} shared")


def _check_contested_tiles_get_their_own_question(app, cfg, problems: list) -> None:
    """Two contested tiles that read alike are two questions, not one.

    The review groups questions by their reading, which is right for the case
    it was built for: forty tiles reading "Neuroptics Blueprint" are one
    wrapped-name problem. It is wrong for the case the relic dedup now raises,
    where the readings agree precisely because one of them is a misreading.

    Merged, the two tiles would share one picture, take one answer, and have
    their counts added by `_cluster_count` - nine of one relic and twenty of
    another written as twenty-nine of whichever was answered. That is worse
    than the silent drop this whole change replaced.

    Driven through `_prepare_review` rather than through a copy of its keying,
    because a copy is exactly how this kind of check goes quietly out of date.
    """
    from ui.views.import_view import ImportView
    from wft.inventory import scan as scan_mod

    def tile(column, row, score, box, quantity, crop):
        return scan_mod.Candidate(
            column=column, row=row, name="Requiem I Relic", score=score,
            reading="Requiem I Relic", sightings=1,
            relic_unique="/Lotus/Types/Game/Projections/T5VoidProjectionA",
            verified_box=box, quantity=quantity, sources=frozenset({1}),
            lead=30.0, evidence=crop)

    # Distinct crops, because telling the two apart by eye is the entire point
    # of asking rather than dropping.
    held = tile(4, 9, 93.3, (174, 1968, 1125, 1569), 9, b"crop-of-requiem-i")
    misread = tile(0, 10, 100.0, (174, 288, 1569, 2013), 20, b"crop-of-requiem-iii")
    candidates = [held, misread]
    scan_mod._one_tile_per_relic(candidates, [])

    view = ImportView(cfg)
    view.resize(1600, 900)
    view.show()
    view.scan_result = scan_mod.ScanResult(
        candidates=candidates, frames_read=1, words_total=2,
        words_after_ui_filter=2, cells_found=2, grid=None)
    view._prepare_review()
    _settle(app)

    relic_clusters = [c for c in view.clusters
                      if any(m.relic_unique for m in c.candidates)]
    if len(relic_clusters) != 2:
        problems.append(f"contested tiles: {len(relic_clusters)} question(s) "
                        f"for two tiles that cannot both be right — one "
                        f"answer would write {9 + 20} of a relic the player holds 9 of")
    else:
        counted = sorted(view._cluster_count(view.clusters.index(cluster))
                         for cluster in relic_clusters)
        if counted != [9, 20]:
            problems.append(f"contested tiles: the questions carry {counted}, "
                            f"not each tile's own count")
        pictures = {id(cluster.evidence) for cluster in relic_clusters}
        if len(pictures) != len(relic_clusters):
            problems.append("contested tiles: the questions share a picture, "
                            "so the crops cannot settle which is which")
    view.deleteLater()
    print(f"contested tiles    : {len(relic_clusters)} question(s), each with "
          f"its own count")


def _check_the_ducat_page_can_empty_itself(app, cfg, problems: list) -> None:
    """A total that cannot be acted on is a total that goes stale.

    The page exists to say what can go to the kiosk, so it has to be able to
    hear that it went. Two actions, and each has one thing it must not get
    wrong: a pile of spares must never dig into what a recipe still asks for,
    and selling a built copy must not take the mastery with it.
    """
    from wft.data import db as dbq
    from ui.views.ducat_view import DucatView

    connection = dbq.connect(cfg.db_path)
    # A part whose set is still worth building. What this checks is the recipe
    # floor, and mastery now lifts that floor for sets nothing wants any more -
    # so taking whichever row came first, which was safe while the floor was
    # always the recipe, picked a released part and measured the wrong thing.
    retained = dbq.retained_counts(connection)
    # With ducats: the page lists only what the kiosk takes. On an empty
    # inventory the first part a recipe wants twice is Furis, the built gun
    # inside Afuris Prime, which has none.
    candidates = connection.execute(
        "SELECT unique_name, count_needed FROM parts "
        "WHERE alias_of IS NULL AND ducats > 0").fetchall()
    piece = next((row for row in candidates
                  if retained.get(row["unique_name"], 0) >= 2), None)
    if piece is None:
        piece = next((row for row in candidates
                      if retained.get(row["unique_name"], 0) >= 1), None)
    needed = int(piece["count_needed"])
    dbq.set_part_count(connection, piece["unique_name"], needed + 3, "video")

    page = DucatView(cfg)
    row = next((r for r in page._spares()
                if r["unique"] == piece["unique_name"]), None)
    if row is None:
        problems.append("ducats: a part held past its recipe was not spare")
        page.deleteLater()
        return
    if row["spare"] != 3:
        problems.append(f"ducats: {needed + 3} of a part needing {needed} "
                        f"reported {row['spare']} spare")

    from PySide6.QtWidgets import QMessageBox as _Box
    original, _Box.question = _Box.question, staticmethod(lambda *a, **k: _Box.Yes)
    try:
        page._traded_spare(row)
    finally:
        _Box.question = original

    left = dbq.owned_count(connection, piece["unique_name"])
    if left != needed:
        problems.append(f"ducats: trading the spares left {left}, not the "
                        f"{needed} the recipe asks for")

    # And the built copies, where mastery is the thing that must survive.
    item = connection.execute(
        "SELECT unique_name FROM items WHERE unique_name IN "
        "(SELECT DISTINCT item_unique FROM parts) LIMIT 1").fetchone()
    dbq.set_item_count(connection, item["unique_name"], 2, "video")
    dbq.set_item_mastered_manually(connection, item["unique_name"], True)
    before, after = dbq.trade_built(connection, item["unique_name"], 1)
    if (before, after) != (2, 1):
        problems.append(f"ducats: trading one built copy went {before} → {after}")
    dbq.trade_built(connection, item["unique_name"], 1)
    if not dbq.is_mastered(connection, item["unique_name"]):
        problems.append("ducats: selling the last built copy took the mastery")
    page.deleteLater()
    print(f"ducat page         : {row['spare']} spare traded, recipe's "
          f"{needed} kept, mastery kept")


def _reordering_holds(page, label: str, problems: list) -> int:
    """Press every heading of every reorderable table on one page.

    Sorting is forty-odd one-line keys reaching into a row by name or by
    position, one per column across five pages, and a wrong one is invisible
    until somebody clicks that column: it raises, or it quietly leaves the
    rows where they were. Both are caught by pressing all of them.

    What must hold after a click is that the table still holds the same rows -
    the same names, the same number of them, none lost and none doubled. A
    table is refilled cell by cell from a list that was reordered in place,
    and every click handler on these pages finds its row in that list by
    index, so rows going missing here is the shape the damage would take.
    """
    from PySide6.QtCore import Qt
    from ui.widgets.tables import Table

    import sys

    # A slot that raises does not reach the caller: PySide hands the error to
    # sys.excepthook and the click returns as if nothing happened, leaving the
    # rows where they were. A table that sorts by a None/bool mix did exactly
    # that and the row checks below could not see it, so catch it here.
    raised: list = []
    previous = sys.excepthook
    sys.excepthook = lambda kind, value, tb: raised.append(value)
    try:
        pressed = _press_headings(page, label, problems, Qt, Table)
    finally:
        sys.excepthook = previous
    for error in raised:
        problems.append(f"{label}: a heading click raised "
                        f"{type(error).__name__}: {error}")
    return pressed


def _press_headings(page, label: str, problems: list, Qt, Table) -> int:
    pressed = 0
    for table in page.findChildren(Table):
        columns = getattr(table, "sortable_columns", None)
        if not columns or not table.rowCount():
            continue

        def names():
            return [table.item(row, 0).text() if table.item(row, 0) else ""
                    for row in range(table.rowCount())]

        before = sorted(names())
        moved = False
        for column in sorted(columns):
            was = names()
            for _turn in (1, 2):
                table.horizontalHeader().sectionClicked.emit(column)
                pressed += 1
                if sorted(names()) != before:
                    problems.append(
                        f"{label}: sorting by column {column} changed which "
                        f"rows the table holds")
                    return pressed
                if table.rowCount() != len(before):
                    problems.append(f"{label}: sorting by column {column} "
                                    f"left {table.rowCount()} of "
                                    f"{len(before)} rows")
                    return pressed
                # After each click, not after the pair: the second click turns
                # the order round, and on a shelf whose values rise with the
                # names (45, 45, 100, 100) that is the order it started in.
                moved = moved or names() != was
            head = table.horizontalHeader()
            if not head.model().headerData(column, Qt.Horizontal).endswith(
                    ("\u25b4", "\u25be")):
                problems.append(f"{label}: column {column} sorts but its "
                                f"heading does not say so")
        if len(before) > 1 and not moved:
            problems.append(f"{label}: a table of {len(before)} rows came out "
                            f"in the same order however it was sorted")
    return pressed


def _tables_are_tall_enough(page, label: str, problems: list) -> int:
    """A table with its scrollbar off has to be as tall as its rows.

    These tables sit in pages that scroll, so they switch their own scrollbar
    off and are sized to their contents instead. Sized before the rows are
    told how tall they are - which is what a column of pictures does to them -
    the last row falls off the bottom with nothing to scroll it back.
    """
    from PySide6.QtCore import Qt
    from ui.widgets.tables import Table

    checked = 0
    for table in page.findChildren(Table):
        if (table.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff
                or not table.rowCount()):
            continue
        needed = (table.horizontalHeader().height() + 2 * table.frameWidth()
                  + sum(table.rowHeight(row)
                        for row in range(table.rowCount())))
        checked += 1
        if table.height() < needed:
            problems.append(
                f"{label}: a table of {table.rowCount()} row(s) is "
                f"{needed - table.height()}px short of showing them, with "
                f"no scrollbar to make up the difference")
    return checked


def _check_every_page_can_be_reordered(app, cfg, problems: list) -> None:
    """Every heading on every page, pressed against the real shelf."""
    from ui.views.ducat_view import DucatView
    from ui.views.inventory_view import InventoryView
    from ui.views.plan_view import PlanView
    from ui.views.riven_view import RivenView

    pressed = 0
    for label, build in (("inventory", InventoryView), ("plan", PlanView),
                         ("ducats", DucatView), ("rivens", RivenView)):
        page = build(cfg)
        page.refresh()
        pressed += _reordering_holds(page, label, problems)
        _tables_are_tall_enough(page, label, problems)
        page.deleteLater()
    print(f"table order        : {pressed} heading press(es) across four "
          f"pages, every table kept its rows")


def _check_a_table_lights_the_whole_row(app, problems: list) -> None:
    """The row is what can be clicked, so the row is what hover answers.

    Qt colours the one cell under the pointer. On a seven-column row that is
    a smear somewhere in the middle of the line you are actually reading; the
    hover should colour the whole row, not just the one cell.

    What is checked is that the table knows the row, not the cell - the paint
    is one line off that number, and a number that follows the pointer down
    the rows is the half a test can stand behind.
    """
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QTableWidgetItem
    from ui.widgets.tables import simple_table

    table = simple_table(["A", "B", "C"], 3)
    for row in range(3):
        for column in range(3):
            table.setItem(row, column, QTableWidgetItem(f"{row}{column}"))

    def point_at(row: int, column: int):
        x = table.columnViewportPosition(column) + table.columnWidth(column) // 2
        y = table.rowViewportPosition(row) + table.rowHeight(row) // 2
        where = QPointF(x, y)
        app.sendEvent(table.viewport(), QMouseEvent(
            QEvent.MouseMove, where, table.viewport().mapToGlobal(where),
            Qt.NoButton, Qt.NoButton, Qt.NoModifier))

    seen = []
    for row in (0, 2, 1):
        point_at(row, 2)
        seen.append(table.hovered_row())
    if seen != [0, 2, 1]:
        problems.append(f"tables: hovering the third column of rows 0, 2, 1 "
                        f"lit rows {seen}")

    app.sendEvent(table.viewport(), QEvent(QEvent.Leave))
    if table.hovered_row() != -1:
        problems.append("tables: a row stayed lit after the pointer left")
    table.deleteLater()
    print(f"table hover        : rows {seen} lit whole, cleared on leave")


def _check_a_capped_table_settles(app, problems: list) -> None:
    """A table cut off at a number of rows reflows once, not forever.

    `fit_height` switched the vertical bar off and back on for every capped
    table. Each switch resized the viewport, each resize scheduled a reflow,
    and the reflow fitted the table again: the Plan page's list of pieces kept
    one core at 100 % for as long as it was open, 409 reflows a second in this
    shape. Counted over a second of idle events after the table is shown.
    """
    import time

    from PySide6.QtWidgets import QTableWidgetItem, QVBoxLayout, QWidget
    from ui.widgets import tables

    calls = [0]
    original = tables.Table.reflow

    def counted(self):
        calls[0] += 1
        original(self)

    tables.Table.reflow = counted
    try:
        host = QWidget()
        layout = QVBoxLayout(host)
        table = tables.simple_table(["Part", "Belongs to", "Need"], 40,
                                    wrap=True, scroll_x=True)
        for row in range(40):
            for column, text in enumerate((f"Piece {row} with a longer name",
                                           "Some set", str(row))):
                table.setItem(row, column, QTableWidgetItem(text))
        tables.fit_height(table, 8)
        layout.addWidget(table)
        host.resize(900, 700)
        host.show()
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            app.processEvents()
        host.hide()
        host.deleteLater()
    finally:
        tables.Table.reflow = original
    if calls[0] > 5:
        problems.append(f"tables: a capped table reflowed {calls[0]} times in "
                        f"1 s of idle events")
    print(f"capped table       : {calls[0]} reflow(s) in 1 s idle")


def _check_the_ducat_page_can_be_reordered(cfg, problems: list) -> None:
    """The headings choose the order, and the rows keep their identity.

    Ordered by ducats the page answered one question well and the other badly:
    the list was not alphabetical. So the default is the
    name now and the value is a click away.

    The click is the risk. Every row on this page carries an action that ends
    in a part leaving the shelf, and the handler finds that part by row index.
    Reorder the widget without reordering the list behind it - which is what
    Qt's own sorting would do - and the wrong part goes to the kiosk. That is
    the half of this check that matters: after a reordering, the row the user
    presses has to name the item printed on it.

    On a shelf built here rather than a real one. A real shelf changes while the
    tool is open - and every one of these rows is a button that empties it -
    so a check that needs two piles worth the same amount cannot be written
    against it.
    """
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QMessageBox
    from ui.views.ducat_view import DucatView
    from wft.data import db as dbq

    home = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(home / "warframe.sqlite")
    connection.execute(
        "INSERT INTO items(unique_name, name, category) VALUES('i:pole', "
        "'Orthos Prime', 'weapon')")
    connection.execute(
        "INSERT INTO owned_items(item_unique, count, mastered, source) "
        "VALUES('i:pole', 0, 1, 'test')")
    # Mastered, so the whole holding is spare. Named and priced to put the
    # two orders as far apart as they go, and to leave a pair worth the same
    # amount - the tie is where a sort stops being a sort and becomes a heap.
    shelf = (("p:zeta", "Zeta Blade", 45, 5),      # 225
             ("p:beta", "Beta Barrel", 100, 1),    # 100
             ("p:alpha", "Alpha Stock", 15, 1),    # 15
             ("p:mu", "Mu Handle", 15, 1))         # 15
    for unique, name, ducats, held in shelf:
        connection.execute(
            "INSERT INTO parts(unique_name, name, item_unique, ducats, "
            "count_needed) VALUES(?,?,'i:pole',?,1)", (unique, name, ducats))
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source) "
            "VALUES(?,?,'test')", (unique, held))
    connection.commit()
    connection.close()

    page_cfg = cfg.__class__({**cfg._data}, cfg.source)
    page_cfg.override("paths.data_dir", str(home))
    page = DucatView(page_cfg)
    page.refresh()
    table = next((t for t in page.findChildren(QTableWidget)
                  if t.horizontalHeaderItem(0)
                  and t.horizontalHeaderItem(0).text().startswith(
                      "Spare part")), None)
    if table is None or table.rowCount() != len(shelf):
        problems.append("ducats: the spares table did not hold the shelf "
                        "this check built for it")
        page.deleteLater()
        return

    def column(index: int) -> list:
        return [table.item(row, index).text()
                for row in range(table.rowCount())]

    by_name = ["Alpha Stock", "Beta Barrel", "Mu Handle", "Zeta Blade"]
    if column(0) != by_name:
        problems.append(f"ducats: the spares table opens on {column(0)}, "
                        f"not in alphabetical order")

    head = table.horizontalHeader()
    head.sectionClicked.emit(4)
    # Dearest first, and the two fifteens alphabetical among themselves -
    # which is the whole reason the old order was unreadable.
    if column(0) != ["Zeta Blade", "Beta Barrel", "Alpha Stock", "Mu Handle"]:
        problems.append(f"ducats: clicking Total gave {column(0)}")

    # The one that costs something if it is wrong.
    asked = []
    original = QMessageBox.question
    QMessageBox.question = staticmethod(
        lambda _self, title, *a, **k: (asked.append(title), QMessageBox.No)[1])
    try:
        table.cellClicked.emit(0, table.columnCount() - 1)
    finally:
        QMessageBox.question = original
    if not asked:
        problems.append("ducats: the last column asked nothing after a sort")
    elif not asked[0].startswith("Zeta Blade"):
        problems.append(f"ducats: after reordering, the top row reads "
                        f"{column(0)[0]!r} and offered {asked[0]!r}")

    head.sectionClicked.emit(4)
    if column(0) != ["Alpha Stock", "Mu Handle", "Beta Barrel", "Zeta Blade"]:
        problems.append(f"ducats: a second click on Total gave {column(0)}")
    head.sectionClicked.emit(0)
    if column(0) != by_name:
        problems.append("ducats: the name column did not come back to A-Z")
    head.sectionClicked.emit(0)
    if column(0) != list(reversed(by_name)):
        problems.append("ducats: the name column does not turn round")
    page.deleteLater()
    print(f"ducat order        : {len(by_name)} row(s), name → total → name, "
          f"ties held, row 'Zeta Blade' kept its action")


def _check_two_tiles_for_one_relic_are_asked_about(problems: list) -> None:
    """A contradiction is a question, not a decision the scan makes alone.

    Relics stack, so two tiles claiming one relic is normally one tile filed
    twice by a drifting scroll estimate, and dropping the weaker is right.
    But it can also be a misreading, and then dropping costs two numbers at
    once. Measured on a real relics capture: a tile of nine Requiem I
    beside a tile of twenty Requiem III that read as "Requiem I Relic" at a
    flat 100. The nine vanished and the twenty were filed as Requiem I.

    The boxes tell the two cases apart. One tile cannot stand in two places of
    one frame, so boxes that neither overlap nor sit near each other are two
    tiles; a drifting scroll re-reads both copies onto the same name band and
    gets one box twice.

    The last case here is the one that makes the rest safe. The review groups
    questions by their reading, and two contested tiles that read alike would
    otherwise be one question with one picture and both counts added together
    - twenty-nine of a relic the player holds nine of.
    """
    from wft.inventory import scan as scan_mod

    def tile(column, row, name, score, box, quantity=None, sources=(1,)):
        return scan_mod.Candidate(
            column=column, row=row, name=name, score=score,
            reading=name, sightings=1, relic_unique=f"R:{name}",
            verified_box=box, quantity=quantity,
            sources=frozenset(sources), lead=30.0)

    # Two tiles, one frame, boxes far apart: the measured Requiem case.
    left = tile(4, 9, "Requiem I Relic", 93.3, (174, 1968, 1125, 1569), 9)
    right = tile(0, 10, "Requiem I Relic", 100.0, (174, 288, 1569, 2013), 20)
    candidates = [left, right]
    notes = []
    scan_mod._one_tile_per_relic(candidates, notes)
    if len(candidates) != 2:
        problems.append(f"relic conflict: {2 - len(candidates)} tile(s) "
                        f"dropped — a contradiction lost a holding")
    if not (left.contested and right.contested):
        problems.append("relic conflict: the tiles were not both asked about "
                        f"— left {left.contested}, right {right.contested}")
    if left.confident or right.confident:
        problems.append("relic conflict: a contested tile stayed confident, "
                        "so it is written without being asked about")

    # The drift double the dedup was written for: same column, next row, and
    # the re-read snapped both onto one name band.
    box = (174, 288, 1125, 1569)
    early = tile(0, 9, "Lith A10 Relic", 88.0, box, 3)
    late = tile(0, 10, "Lith A10 Relic", 95.0, box, 13)
    candidates = [early, late]
    notes = []
    scan_mod._one_tile_per_relic(candidates, notes)
    if len(candidates) != 1 or candidates[0] is not late:
        problems.append(f"relic drift: {len(candidates)} tile(s) kept — the "
                        f"same tile filed twice must still collapse to one")
    if late.contested:
        problems.append("relic drift: a scroll drift was asked about as if it "
                        "were a contradiction")

    # Two overlapping screenshots file one tile under two positions. Different
    # captures, so the columns prove nothing.
    first = tile(0, 9, "Meso F5 Relic", 90.0, None, 4, sources=(1,))
    second = tile(3, 2, "Meso F5 Relic", 95.0, None, 4, sources=(2,))
    candidates = [first, second]
    scan_mod._one_tile_per_relic(candidates, [])
    if len(candidates) != 1:
        problems.append(f"relic stills: {len(candidates)} tile(s) kept — one "
                        f"tile in two overlapping screenshots is one tile")

    # The pass order the sort depends on: quantities are read later, so a
    # tie-break on them here would compare nothing against nothing.
    import inspect

    body = inspect.getsource(scan_mod.scan)
    if body.index("_one_tile_per_relic") > body.index("_read_quantities"):
        problems.append("relic conflict: stack counts are now read before the "
                        "relic dedup — its sort comment is out of date")

    print("relic conflicts    : 3 shape(s), pictures tell two tiles from one")


def _check_the_layout_with_no_empty_slots_wins(problems: list) -> None:
    """A two-reward screen must be read as two rewards.

    Its two tiles sit either side of the centre, which is exactly where slots
    two and three of a four-tile layout fall — so the four-up read names both
    of them and then reports two empty slots beside them, and numbers the
    user's own card 2 instead of 1. His card is always the leftmost, and the
    squadmate rule is built on that.

    Both layouts name two, and the tie is the whole case. Driven against the
    chooser rather than the reader: what went wrong was a comparison, and a
    test that needs Tesseract and a game window to exercise a comparison is a
    test that will be deleted the first time it is inconvenient.
    """
    from ui.workers.relic_trigger import pick_layout
    from wft.relic.score import Reward

    def read(names):
        return [Reward(position=i, raw_text=n or "", part_unique=("/p/" + n)
                       if n else None, name=n)
                for i, n in enumerate(names, start=1)]

    four = read(["", "Quassus Prime Handle", "Akbronco Prime Blueprint", ""])
    two = read(["Quassus Prime Handle", "Akbronco Prime Blueprint"])
    kept = pick_layout([(4, 2, four), (3, 1, read(["", "x", ""])), (2, 2, two)])
    if kept is not two:
        problems.append("layout: a two-reward screen was kept as a four-up "
                        "read, so the player's own card is not position 1")
    if kept and kept[0].name != "Quassus Prime Handle":
        problems.append("layout: the leftmost reward is not position 1")

    # The screen that makes the rule above dangerous, and the reason the score
    # counts *readable* tiles rather than identified ones. Four players, one
    # relic in the log: only the middle two names are in the candidate list,
    # so counting identifications gives four-up a two and two-up a two, and
    # the tie would throw away two real rewards. All four tiles read as names.
    squad = read(["Cedo Prime Barrel", "Kompressa Prime Receiver",
                  "Kompressa Prime Receiver", "Lex Prime Receiver"])
    middle = read(["Kompressa Prime Receiver", "Kompressa Prime Receiver"])
    kept = pick_layout([(4, 4, squad), (2, 2, middle)])
    if kept is not squad:
        problems.append("layout: a full four-reward screen was cut down to "
                        "its middle two")

    # And the case that must not move: a four-reward screen read in full.
    full = read(["a", "b", "c", "d"])
    if pick_layout([(4, 4, full)]) is not full:
        problems.append("layout: a full four-up read was not kept")

    # More names always beats fewer, whatever the layout size.
    three = read(["a", "b", "c"])
    if pick_layout([(4, 2, read(["a", "b", "", ""])), (3, 3, three)]) is not three:
        problems.append("layout: a smaller layout won on fewer names")
    print("reward layout      : ties go to the layout with no empty slots")


def _check_the_riven_page_shows_the_week(app, problems: list) -> None:
    """The riven page, against seeded weeks and against nothing at all.

    Seeded through `prices.store` rather than hand-written SQL, so the check
    exercises the same write the fetch uses — a fixture that bypasses the
    writer proves nothing about it. Two weeks, so the pooled trade count and
    the history dialog have something to pool.
    """
    from ui.views import riven_view as riven_view_mod
    from wft import config as config_mod
    from wft.data import db as dbq
    from wft.riven import prices as prices_mod

    def entry(item_type, weapon, rerolled, pop, median, price_max=None):
        return {"item_type": item_type, "weapon": weapon, "rerolled": rerolled,
                "avg": float(median), "stddev": 1.0, "price_min": 2,
                "price_max": price_max or int(median) * 10, "pop": pop,
                "median": median}

    home = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(home / "warframe.sqlite")
    prices_mod.store(connection, "2026-08-24", [
        entry("Rifle Riven Mod", "", 0, 15, 10),
        entry("Rifle Riven Mod", "Acceltra", 0, 10, 30),
        entry("Rifle Riven Mod", "Acceltra", 1, 12, 80),
        entry("Melee Riven Mod", "Ack & Brunt", 0, 1, 25),
    ])
    prices_mod.store(connection, "2026-08-31", [
        entry("Rifle Riven Mod", "", 0, 20, 12),
        entry("Rifle Riven Mod", "Acceltra", 0, 4, 35),
        entry("Rifle Riven Mod", "Acceltra", 1, 6, 90, price_max=4000),
    ])
    connection.close()

    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(home))
    page = riven_view_mod.RivenView(cfg)
    page.refresh()
    riven_view_mod.wait_for_fetches()
    _settle(app)

    table = page._board_table
    if table is None or table.rowCount() != 2:
        problems.append("rivens: the weapons table did not render its rows")
        page.deleteLater()
        return

    def row_of(weapon):
        return next((i for i, r in enumerate(page._board_rows)
                     if r["weapon"] == weapon), None)

    acceltra = row_of("Acceltra")
    if acceltra is None:
        problems.append("rivens: Acceltra is missing from the board")
        return
    # The newest week's trades against the pool of every stored week:
    # 4 + 6 this week, 10 + 12 + 4 + 6 ever.
    trades = table.item(acceltra, 5).text()
    if trades != "10 / 32":
        problems.append(f"rivens: Acceltra trades read {trades!r}, "
                        f"expected '10 / 32'")
    if table.item(acceltra, 3).text() != "90":
        problems.append(f"rivens: Acceltra rolled median reads "
                        f"{table.item(acceltra, 3).text()!r}, expected '90'")

    # Ack & Brunt did not trade in the newest week: current columns dashed,
    # and one trade ever is a rumour - the row must be faint.
    ack = row_of("Ack & Brunt")
    if ack is None:
        problems.append("rivens: a weapon absent from the newest week lost "
                        "its row entirely")
    else:
        if table.item(ack, 3).text() != "—":
            problems.append(f"rivens: Ack & Brunt rolled median reads "
                            f"{table.item(ack, 3).text()!r}, expected the dash")
        from ui import theme
        from PySide6.QtGui import QColor
        if table.item(ack, 0).foreground().color() != QColor(theme.TEXT_FAINT):
            problems.append("rivens: a one-trade weapon is not dimmed")

    # The failed fetch (stubbed offline) must be a note, not the page. The
    # worker prefixes the exception type; the view shows what it is handed,
    # exactly as the fissure board does.
    if "offline in tests" not in (page.error or ""):
        problems.append(f"rivens: expected the offline note, got {page.error!r}")

    # The search narrows to the typed weapon.
    from PySide6.QtWidgets import QLineEdit
    search = next((w for w in page.findChildren(QLineEdit)
                   if "weapon" in (w.placeholderText() or "").lower()), None)
    if search is None:
        problems.append("rivens: no search box was built")
    else:
        search.setText("acce")
        _settle(app)
        visible = [r["weapon"] for i, r in enumerate(page._board_rows)
                   if not table.isRowHidden(i)]
        if visible != ["Acceltra"]:
            problems.append(f"rivens: searching 'acce' shows {visible}")
        search.setText("accelatra")                # a typo still finds it
        _settle(app)
        visible = [r["weapon"] for i, r in enumerate(page._board_rows)
                   if not table.isRowHidden(i)]
        if "Acceltra" not in visible:
            problems.append(f"rivens: searching 'accelatra' shows {visible}")

    # Veiled aggregates of the newest week, one Rifle row.
    veiled = prices_mod.veiled(page.conn)
    if len(veiled) != 1 or veiled[0]["pop"] != 20:
        problems.append(f"rivens: veiled aggregates came back wrong — "
                        f"{[dict(v) for v in veiled]}")

    # The history dialog holds both stored weeks for the clicked weapon.
    detail = riven_view_mod.RivenDetail(
        page.conn, "Rifle Riven Mod", "Acceltra")
    if detail.table.rowCount() != 2:
        problems.append(f"rivens: the history dialog shows "
                        f"{detail.table.rowCount()} week(s), expected 2")
    detail.deleteLater()
    page.deleteLater()

    # And the page against a database with no weeks at all: the offline stub
    # answers the fetch, and the page must land on its error text rather
    # than a blank or a crash.
    bare = pathlib.Path(tempfile.mkdtemp())
    cfg_bare = config_mod.load()
    cfg_bare.override("paths.data_dir", str(bare))
    empty_page = riven_view_mod.RivenView(cfg_bare)
    empty_page.refresh()
    riven_view_mod.wait_for_fetches()
    _settle(app)
    if "offline in tests" not in (empty_page.error or ""):
        problems.append(f"rivens: the empty page swallowed its fetch error "
                        f"— {empty_page.error!r}")
    if empty_page._board_table is not None:
        problems.append("rivens: an empty database rendered a weapons table")
    empty_page.deleteLater()
    _settle(app)
    print("riven page         : 2 weapon(s) over 2 week(s), pooled trades, "
          "dialog and empty state held")


def _check_page_argument(problems: list) -> None:
    """`--page` names a page case-insensitively; a typo is refused up front.

    The desktop entry's quick actions pass `import` and `settings` in lower
    case. An unknown name must exit non-zero before any window is built.
    """
    import contextlib
    import io

    from ui import app as app_mod
    from ui.main_window import MainWindow

    for name in MainWindow.NAV_ITEMS:
        for spelled in (name, name.lower(), name.upper()):
            if app_mod.resolve_page(spelled) != name:
                problems.append(f"--page {spelled} did not resolve to {name}")
    if app_mod.resolve_page("nonesuch") is not None:
        problems.append("--page nonesuch resolved to a page")
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = app_mod.main(["--page", "nonesuch"])
    except SystemExit as exit_:
        code = exit_.code
    if not code:
        problems.append("--page nonesuch did not exit non-zero")
    if "Settings" not in err.getvalue():
        problems.append("--page nonesuch did not list the valid pages")


def _check_focus_does_not_change_the_page(app, problems: list) -> None:
    """Focus reaching a navigation list must not pick a page.

    The page on display lives in one of the two lists, so the other has no
    current row, and an item view selects its first row when it gains focus:
    Shift+Tab out of the footer, Tab out of the main list, or the window being
    activated again switched to Rewards or Settings unasked. Also covers the
    start on a footer page, which used to be held up by focusing the footer by
    hand.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from wft import config as config_mod

    cfg = config_mod.load()
    cfg.override("paths.data_dir", tempfile.mkdtemp())
    from ui.app import build_application

    for start in ("Settings", "Plan"):
        _app, window = build_application(cfg, start)
        window.resize(1200, 800)
        window.show()
        _settle(app)
        page = window.stack.currentWidget()
        other = (window.nav_list if start in window.NAV_FOOTER
                 else window.nav_footer)
        for reason in (Qt.TabFocusReason, Qt.BacktabFocusReason,
                       Qt.ActiveWindowFocusReason, Qt.OtherFocusReason):
            other.clearFocus()
            other.setFocus(reason)
            _settle(app)
            if window.stack.currentWidget() is not page:
                problems.append(f"focus ({reason.name}) on the other "
                                f"navigation list left {start}")
        # Keys, from the list that holds the page to the one that does not.
        here = (window.nav_footer if start in window.NAV_FOOTER
                else window.nav_list)
        for key in (Qt.Key_Tab, Qt.Key_Backtab):
            here.setFocus()
            _settle(app)
            QTest.keyClick(here, key)
            _settle(app)
            if window.stack.currentWidget() is not page:
                problems.append(f"{Qt.Key(key).name} out of the list left "
                                f"{start}")
        # A real choice still works from the no-current-row state.
        other.setFocus()
        _settle(app)
        QTest.keyClick(other, Qt.Key_Down)
        _settle(app)
        if window.stack.currentWidget() is page:
            problems.append(f"arrow key in the other list did not leave "
                            f"{start}")
        window.close()


def _check_the_apply_table_can_be_hit(app, cfg, problems: list) -> None:
    """At a large text size the apply table's check boxes are still targets.

    Rows kept Qt's default 30px while the theme's cell padding grew with the
    text, and at 20pt the Apply box was given 81x6 pixels - a line nobody could
    click. Built at 20pt, through the real `_build_apply_table`.
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from ui import theme
    from ui.views.import_view import PAGE_APPLY, ImportView
    from wft.inventory import scan as scan_mod

    previous = (theme.active(), theme.font_size(), app.styleSheet())
    app.setStyleSheet(theme.apply(previous[0], 20))
    try:
        relic = scan_mod.Candidate(
            column=0, row=0, name="Requiem I Relic", score=100.0,
            reading="Requiem I Relic", sightings=3,
            relic_unique="/Lotus/Types/Game/Projections/T5VoidProjectionA",
            verified_box=None, quantity=9, sources=frozenset({1}), lead=30.0,
            evidence=None)
        view = ImportView(cfg)
        view.resize(1600, 900)
        view.show()
        view.scan_result = scan_mod.ScanResult(
            candidates=[relic], frames_read=1, words_total=1,
            words_after_ui_filter=1, cells_found=1, grid=None)
        view._prepare_review()
        view._remember_all_counts()
        view._build_apply_table([])
        view.stack.setCurrentIndex(PAGE_APPLY)
        _settle(app)
        if not view.apply_rows:
            problems.append("apply table: no row for a confident relic tile")
            return
        box = view.apply_rows[0]["checkbox"]
        if box.height() < theme._mark + 4:
            problems.append(f"apply table: the Apply box is {box.width()}x"
                            f"{box.height()}px at 20pt, smaller than its own "
                            f"{theme._mark}px mark")
        before = box.isChecked()
        QTest.mouseClick(box, Qt.LeftButton, Qt.NoModifier,
                         QPoint(box.width() - 3, box.height() // 2))
        _settle(app)
        if box.isChecked() == before:
            problems.append("apply table: a click in the Apply cell beside the "
                            "box does not flip the row")
        table = view.apply_table
        if table.item(0, 2).flags() & Qt.ItemIsEditable:
            problems.append("apply table: the name can be typed over")
        if not table.item(0, 4).flags() & Qt.ItemIsEditable:
            problems.append("apply table: the count cannot be typed into")
        # The count's editor sits in the cell. With the padding every other
        # field has it needed 54px in a 35px cell at 14pt and showed half a
        # digit; at 20pt 75px in 51.
        from PySide6.QtWidgets import QLineEdit
        table.setCurrentCell(0, 4)
        table.editItem(table.item(0, 4))
        _settle(app)
        editors = [w for w in table.viewport().findChildren(QLineEdit)
                   if w.isVisible()]
        if not editors:
            problems.append("apply table: no editor opened on the count")
        elif editors[0].height() < editors[0].minimumSizeHint().height():
            problems.append(f"apply table: the count's editor is "
                            f"{editors[0].height()}px tall and needs "
                            f"{editors[0].minimumSizeHint().height()}px at 20pt; "
                            f"the number is cut")
        size = f"{box.width()}x{box.height()}"
        view.deleteLater()
        _settle(app)
        print(f"apply table        : box {size}px at 20pt")
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])


def _check_the_import_page_follows_a_theme_change(app, problems: list) -> None:
    """The Import page takes a new theme's colours and the history as stored.

    It had no `refresh`, so the Settings page's theme change passed it by: its
    dimmed and warning texts kept the old theme's colours, and after "Delete
    my inventory" its history went on listing the deleted imports.
    """
    from ui import theme
    from ui.views.import_view import ImportView
    from wft.data import db as dbq

    names = theme.names()
    previous = (theme.active(), theme.font_size(), app.styleSheet())
    first = names[0]
    theme.apply(first, previous[1])
    first_dim = theme.TEXT_DIM
    second = next((name for name in names[1:]
                   if (theme.apply(name, previous[1]) or True)
                   and theme.TEXT_DIM != first_dim), None)
    if second is None:
        problems.append("import theme: every theme has the same dimmed colour; "
                        "nothing to tell apart")
        theme.apply(previous[0], previous[1])
        return
    view = None
    try:
        cfg = _setup_cfg(data=True, key="F8")
        connection = dbq.connect(cfg.db_path)
        connection.execute("INSERT INTO imports(id, source) VALUES(1, 'a.mkv')")
        connection.execute("INSERT INTO observations(import_id, kind, unique_name, "
                           "after) VALUES(1, 'part', '/t/p', 2)")
        connection.commit()
        theme.apply(first, previous[1])
        app.setStyleSheet(theme.apply(first, previous[1]))
        view = ImportView(cfg)
        view.show()
        _settle(app)
        listed = view.history.isVisibleTo(view)
        if first_dim not in view.path_label.styleSheet():
            problems.append("import theme: the path line was not drawn in the "
                            "theme's dimmed colour to begin with")

        app.setStyleSheet(theme.apply(second, previous[1]))
        connection.execute("DELETE FROM observations")
        connection.execute("DELETE FROM imports")
        connection.commit()
        connection.close()
        refresh = getattr(view, "refresh", None)
        if not callable(refresh):
            problems.append("import theme: the Import page has no refresh, so a "
                            "theme change and a deleted history pass it by")
            return
        refresh()
        _settle(app)
        if theme.TEXT_DIM not in view.path_label.styleSheet():
            problems.append(f"import theme: after the change to {second} the path "
                            f"line still has {view.path_label.styleSheet()!r}")
        if not listed or view.history.isVisibleTo(view):
            problems.append(f"import theme: the history did not follow the "
                            f"database (listed before {listed}, still listed after "
                            f"the delete {view.history.isVisibleTo(view)})")
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])
        if view is not None:
            view.deleteLater()
        _settle(app)
    print(f"import theme       : {first} -> {second}, the colours and the history "
          f"follow")


def _check_a_muted_kde_is_said(app, problems: list) -> None:
    """Where KDE mutes notifications in fullscreen, Settings and doctor say so.

    Plasma 6 switches Do Not Disturb on by itself while a fullscreen window
    has focus (the default, measured on 6.7.5), so on a default desktop the
    notification of a reading never shows during play. Read through a
    stand-in kreadconfig6: true, false, and a desktop that is not KDE.
    """
    from ui.views.settings_view import SettingsView
    from wft import doctor, readiness

    patches = _Patches()
    stubs = pathlib.Path(tempfile.mkdtemp())
    page = None
    try:
        answer = stubs / "answer"
        tool = stubs / "kreadconfig6"
        tool.write_text(f'#!/bin/sh\ncat "{answer}"\n')
        tool.chmod(0o755)
        patches.env("PATH", f"{stubs}:{os.environ['PATH']}")
        patches.env("XDG_CURRENT_DESKTOP", "KDE")
        cfg = _setup_cfg(data=True, key="F8")
        cfg.override("notifications.enabled", True)
        for said, expected in (("true", True), ("false", False)):
            answer.write_text(said + "\n")
            if readiness.notifications_held_in_fullscreen() is not expected:
                problems.append(f"kde mute: kreadconfig6 said {said} and the check "
                                f"answered {readiness.notifications_held_in_fullscreen()}")
            lines = doctor.check_notifications(cfg)
            if expected and not any("While a fullscreen application is focused"
                                    in detail for _l, _n, detail in lines):
                problems.append(f"kde mute: doctor says nothing where KDE mutes ({lines})")
            if not expected and lines:
                problems.append(f"kde mute: doctor warns where KDE does not mute ({lines})")
            page = SettingsView(cfg)
            page.show_tab("Capture")   # the note sits on that tab
            if page.notify_note.isVisibleTo(page) is not expected:
                problems.append(f"kde mute: the Settings note is "
                                f"{'shown' if page.notify_note.isVisibleTo(page) else 'hidden'} "
                                f"where kreadconfig6 said {said}")
            page.deleteLater()
            page = None
        patches.env("XDG_CURRENT_DESKTOP", "GNOME")
        answer.write_text("true\n")
        if readiness.notifications_held_in_fullscreen() is not None:
            problems.append("kde mute: a desktop that is not KDE was judged by KDE's setting")
    finally:
        patches.restore()
        if page is not None:
            page.deleteLater()
        shutil.rmtree(stubs, ignore_errors=True)
        _settle(app)
    print("kde mute           : KDE's fullscreen mute is said in Settings and by "
          "doctor, only where it is on")


def _check_names_for_the_game_and_the_key(problems: list) -> None:
    """The window test and the key names accept what a user would type.

    "warframe", the configured class, is a substring of the tracker's own
    class; in an X11 session `wf doctor` called the tracker the game. And
    `key_pause` was refused where `pause` and `KEY_PAUSE` were taken.
    """
    from wft.capture.keys import key_code
    from wft.capture.window import is_game

    cases = [
        (("steam_app_230410", "Warframe"), True),
        (("", "Warframe"), True),
        (("warframe-tracker", "Warframe Tracker"), False),
        (('"warframe-tracker", "warframe-tracker"', '"Warframe Tracker"'), False),
        (("firefox", "Mozilla Firefox"), False),
    ]
    for (wm_class, title), expected in cases:
        if is_game(wm_class, title, "warframe") != expected:
            problems.append(f"is_game({wm_class!r}, {title!r}) is not {expected}")
    names = {"F23": True, "f23": True, "KEY_F23": True, "key_pause": True,
             "Pause": True, "nonsense": False}
    for name, valid in names.items():
        if key_code is not None and (key_code(name) is not None) != valid:
            problems.append(f"hotkey name {name!r} {'refused' if valid else 'accepted'}")
    print("names              : game window and hotkey names as typed")


def _measure_the_inventory_card(app, window, measure, tag: str) -> None:
    """Setup's inventory card with nothing imported, for the text measurement.

    This machine's database decides the other state, which the plain walk
    measures.
    """
    from wft import readiness as readiness_mod

    patches = _Patches()
    try:
        patches.set(readiness_mod, "inventory_check", lambda cfg, connection=None:
                    readiness_mod.Check("inventory", "empty", False,
                                        "nothing imported yet",
                                        "import your inventory"))
        window.select("Setup")
        _settle(app)
        measure(window._pages["Setup"], f"{tag} Setup / inventory empty")
    finally:
        patches.restore()
        window.select("Import")


def _check_every_page_tab_and_dialog_shows_its_text(app, cfg, problems: list) -> None:
    """Everything a person can open shows its text whole, at 14 and 20pt.

    Three runs: 14pt and 20pt in a window fixed at 2000x1150, and 20pt in one
    at 1600x900 - the size at which the Collection items list, a list with a
    sideways bar and the Fissures column showed up in the second audit.
    Each run also measures every control for a click target of 1.2 lines.

    The Import page had no scroll area, so a window shorter than its content
    squeezed every row below its text: at 17pt in 2000x1150 the step texts, the
    profile row and the history table were cut, 13 widgets in all. That
    check looked at what was visible when it ran, so it never saw the Collection
    tab "Add and correct" (24 widgets under their own minimum height at 20pt),
    the Import pages after the first, or any dialog. This opens each of
    them: every page, every tab of every page, the four Import pages, and the
    four dialogs that used to be a fixed number of pixels - the set window, the
    relic window, the riven history and the mastery list - and the What's new
    window.

    Measured by tests/ui_detect.py: text taller or wider than its label, widgets
    under their own `minimumSizeHint`, table cells and columns that cannot show
    their text, and tables that show fewer rows than they should. Built through
    build_application as the program starts, in a window fixed at 2000x1150 -
    fixed rather than resized, because here another page's test data sets a
    minimum taller than 1150 and the window grows past it, which hid the squeeze
    entirely the first time. A real window is the size its user gave it.
    """
    import sqlite3

    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QTabWidget
    import ui_detect
    from ui import app as app_mod, settings_io, theme
    from ui.views import import_view as import_mod, riven_view as riven_mod
    from ui.widgets import dialog_size
    from ui.widgets.item_detail import ItemDetail
    from ui.widgets.mastery_import import MasteryImport, MIN_ROWS as MASTERY_ROWS
    from ui.widgets.relic_detail import RelicDetail
    from wft.data import db as dbq
    from wft.inventory import scan as scan_mod
    from wft.riven import prices as prices_mod

    screen = QRect(0, 0, 2000, 1150)
    runs = ((14, 2000, 1150), (20, 2000, 1150), (20, 1600, 900))
    previous = (theme.active(), theme.font_size(), app.styleSheet(), app.font(),
                cfg.get("display.font_size"), settings_io.set_value,
                dialog_size.available_area)
    settings_io.set_value = lambda *a, **k: None   # never the user's config
    # The offscreen platform has an 800x800 screen, which no real one is; the
    # dialogs are sized against the screen, so it answers for the real one.
    dialog_size.available_area = lambda _widget: screen

    # Rivens are fetched once per program run and every test database is
    # missing every week, so the history the dialog shows is put in through the
    # writer the fetch uses.
    connection = dbq.connect(cfg.db_path)
    connection.row_factory = sqlite3.Row
    for week, factor in (("2026-10-05", 1), ("2026-10-12", 2)):
        prices_mod.store(connection, week, [
            {"item_type": kind, "weapon": weapon, "rerolled": rolled,
             "avg": 20.0 * factor, "stddev": 1.0, "price_min": 2,
             "price_max": 900 * factor if rolled else None, "pop": 7 * factor,
             "median": 10 + 40 * rolled}
            for kind, weapon in (
                ("Rifle Riven Mod", "Acceltra"),
                ("Melee Riven Mod",
                 "Tenet Arca Plasmor Carrier Of Very Long Names"))
            for rolled in (0, 1)])
    connection.commit()

    relics = connection.execute(
        "SELECT MIN(unique_name) AS unique_name, name FROM relics "
        "GROUP BY name ORDER BY name LIMIT 30").fetchall()
    item = connection.execute(
        "SELECT unique_name, name FROM items WHERE name = 'Akbronco Prime'"
    ).fetchone() or connection.execute(
        "SELECT i.unique_name, i.name FROM items i WHERE i.name LIKE '%Prime' "
        "AND (SELECT COUNT(*) FROM parts p WHERE p.item_unique = i.unique_name "
        "AND p.alias_of IS NULL) >= 4 ORDER BY LENGTH(i.name) DESC").fetchone()
    relic = relics[0]["name"] if relics else None

    cut: list[str] = []
    measured = 0

    small: list[str] = []

    def measure(root, label, floors=None):
        nonlocal measured
        measured += 1
        for found in ui_detect.findings(root, floors):
            cut.append(f"{label}: {found['detector']} {found['kind']} "
                       f"{found['have']} of {found['need']} "
                       f"{found['where'][-70:]} {found['note']}")
        for found in ui_detect.click_targets(root):
            small.append(f"{label}: {found['kind']} {found['have']} of "
                         f"{found['need']} {found['where'][-70:]}")

    try:
        for points, width, height in runs:
            screen = QRect(0, 0, width, height)
            cfg.override("display.font_size", points)
            _, window = app_mod.build_application(cfg, page="Import")
            window.setFixedSize(screen.width(), screen.height())
            window.show()
            _settle(app)
            tag = f"{points}pt {width}x{height}"
            # The navigation shows every page without a bar where the window
            # has the height for it (B3: "Import" was below the fold at 20pt).
            nav = window.nav_list.verticalScrollBar()
            if height >= 1150 and nav.maximum() > 0:
                cut.append(f"{tag}: the navigation scrolls by {nav.maximum()}px "
                           f"and hides an entry")
            for name in list(window._pages):
                window.select(name)
                _settle(app)
                page = window._pages[name]
                measure(page, f"{tag} {name}")
                # Tabs that are not showing are not measured by anything that
                # looks at what is visible: each one is brought up in turn.
                for tabs in page.findChildren(QTabWidget):
                    for index in range(tabs.count()):
                        tabs.setCurrentIndex(index)
                        _settle(app)
                        measure(page, f"{tag} {name} / {tabs.tabText(index)}")
                    tabs.setCurrentIndex(0)

            # Setup in its fullest state: the group command with its button, and
            # three game logs to choose from (long paths, which have no spaces
            # to wrap at). The pages above only show the state of this machine.
            from wft import readiness as readiness_mod

            worst = _Patches()
            worst.set(readiness_mod, "input_check", lambda user=None:
                      readiness_mod.Check(
                          "input", "missing", False, "x", "give yourself keyboard "
                          "access", {"group": "missing", "devices": 0,
                                     "user": "someone"}))
            shown = (cfg.get("paths.steam_roots"), cfg.get("paths.ee_log"))
            tree = pathlib.Path(tempfile.mkdtemp())
            _steam_tree(tree, {"ee_log_in_library": cfg.get("paths.ee_log_in_library")})
            cfg.override("paths.steam_roots", [str(tree / ".local/share/Steam"),
                                               str(tree / ".var/app/com.valvesoftware.Steam/.local/share/Steam")])
            cfg.override("paths.ee_log", str(tree / "deeply" / "nested" / "EE.log"))
            try:
                window.select("Setup")
                _settle(app)
                measure(window._pages["Setup"], f"{tag} Setup / fullest")

                # The first reward screen read badly: the longest text of that
                # card with its button, the offer on the Rewards page, and the
                # calibration dialog with the shape warning (the longest text
                # it can carry) over a long file name.
                import numpy

                from ui.views.calibrate_dialog import CalibrateDialog
                from wft.relic import last_screen as last_screen_mod

                blank = numpy.zeros((360, 640, 3), numpy.uint8)
                last_screen_mod.remember(blank, _trigger_result(["a"] * 4, 0))
                window._pages["Setup"].refresh()
                _settle(app)
                measure(window._pages["Setup"], f"{tag} Setup / bad reward screen")
                panel = window.relic_panel
                panel._triggered_at = 0.0
                panel._on_finished(_trigger_result(["a"] * 4, 0))
                window.select("Rewards")
                _settle(app)
                # The offer's own row, not the page: with any result showing,
                # the four cards ask for 1459px at 20pt and a 1600x900 window
                # gives the page 1303 (found by this measurement, not caused by
                # the offer: the same with a good reading).
                measure(panel.calibrate_row, f"{tag} Rewards / calibrate offer")
                if width >= 2000:
                    measure(panel, f"{tag} Rewards / no recognised name")
                dialog = CalibrateDialog(cfg, window)
                dialog.show()
                wide = numpy.zeros((1080, 3840, 3), numpy.uint8)
                dialog._set_image(dialog._pixmap_of(wide), wide,
                                  "Loaded from /home/someone/Pictures/Screenshots/"
                                  "a-rather-long-screenshot-name-2026-10-14.png.",
                                  check_shape=True)
                _settle(app)
                measure(dialog, f"{tag} Calibrate / shape warning")
                # With a card marked: the controls, the keys hint and the numbers.
                dialog._on_picked(0.2484, 0.2, 0.125, 0.22)
                _settle(app)
                measure(dialog, f"{tag} Calibrate / fitting the row")
                _drop(app, dialog)
                last_screen_mod.forget()
            finally:
                worst.restore()
                cfg.override("paths.steam_roots", shown[0])
                cfg.override("paths.ee_log", shown[1])

            view = window._pages["Import"]
            window.select("Import")
            for index, label in ((import_mod.PAGE_SELECT, "start"),
                                 (import_mod.PAGE_PROCESSING, "processing"),
                                 (import_mod.PAGE_REVIEW, "review")):
                view.stack.setCurrentIndex(index)
                _settle(app)
                measure(view, f"{tag} Import / {label}")
            _measure_the_inventory_card(app, window, measure, tag)
            if relics:
                tiles = [scan_mod.Candidate(
                    column=n, row=0, name=row["name"], score=100.0,
                    reading=row["name"], sightings=3,
                    relic_unique=row["unique_name"], verified_box=None,
                    quantity=n + 2, sources=frozenset({1}), lead=30.0,
                    evidence=None) for n, row in enumerate(relics)]
                view.scan_result = scan_mod.ScanResult(
                    candidates=tiles, frames_read=1, words_total=len(tiles),
                    words_after_ui_filter=len(tiles), cells_found=len(tiles),
                    grid=None)
                view._prepare_review()
                view._remember_all_counts()
                view._build_apply_table([])
                view.stack.setCurrentIndex(import_mod.PAGE_APPLY)
                _settle(app)
                # Ten rows were promised however much text is above them.
                measure(view, f"{tag} Import / apply",
                        {view.apply_table: import_mod.APPLY_MIN_ROWS})

            def show(dialog, label, floors=None, cramped=None):
                dialog.show()
                _settle(app)
                if cramped:
                    # A screen that gives the dialog less than it asked for.
                    # What the table keeps is its own minimum, not the room.
                    dialog.resize(*cramped)
                _settle(app)
                measure(dialog, f"{tag} {label}", floors)
                if (dialog.width() > screen.width()
                        or dialog.height() > screen.height()):
                    cut.append(f"{tag} {label}: dialog {dialog.width()}x"
                               f"{dialog.height()} is bigger than the screen")
                dialog.close()
                dialog.deleteLater()
                _settle(app)

            if item is not None:
                show(ItemDetail(connection, item["unique_name"], item["name"],
                                window), "set window")
            if relic is not None:
                show(RelicDetail(connection, relic, window), "relic window")
            for kind, weapon in (
                    ("Rifle Riven Mod", "Acceltra"),
                    ("Melee Riven Mod",
                     "Tenet Arca Plasmor Carrier Of Very Long Names")):
                show(riven_mod.RivenDetail(connection, kind, weapon, window),
                     f"riven history {weapon[:12]}")
            names = ("Baza Prime", "Garuda Prime Systems Blueprint", "Itzal",
                     "Kuva Bramma", "Tenet Arca Plasmor", "Xoris",
                     "Hildryn Prime", "Equinox Prime", "Dual Ichor",
                     "Mandonel", "Redeemer Prime", "Wukong Prime")
            found = {"mastered": [(f"/t/{n}", name, 500_000 + 1000 * n)
                                  for n, name in enumerate(names)],
                     "already": 202, "short": 3}
            for label, size in (("mastery list", None),
                                ("mastery list, short dialog", (900, 500))):
                dialog = MasteryImport(sqlite3.connect(":memory:"), found,
                                       window)
                show(dialog, label, {dialog.findChild(QTableWidget):
                                     MASTERY_ROWS}, cramped=size)
            from ui.views.whatsnew_dialog import WhatsNewDialog
            show(WhatsNewDialog(window, seen=""), "what's new")
            window.close()
            window.deleteLater()
            _settle(app)
    finally:
        connection.close()
        settings_io.set_value = previous[5]
        dialog_size.available_area = previous[6]
        cfg.override("display.font_size", previous[4])
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])
        app.setFont(previous[3])
    for line in cut[:12]:
        problems.append(f"text cut: {line}")
    for line in small[:12]:
        problems.append(f"click target: {line}")
    print(f"text at 14 and 20pt: {len(cut)} cut in {measured} view(s)"
          if cut else f"text at 14 and 20pt: {measured} views (pages, tabs, "
          f"import pages, 5 dialogs; 3 window sizes) show their text whole")
    print(f"click targets: {len(small)} under 1.2 lines"
          if small else f"click targets: every control and row of those "
          f"{measured} views is at least 1.2 lines")


def _check_a_cut_table_cell_shows_its_text(app, problems: list) -> None:
    """A table cell that cannot show its text shows all of it as a tooltip.

    The fissure page's "What you want from it" ended in "Akbolto Pr..." with
    nothing to say what came after. Wrapping makes that rare; what is
    left - a table that does not wrap, a word wider than its column - has to
    say what it holds. A cell that has a tooltip of its own keeps it, and a
    cell that fits has none.
    """
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QTableWidgetItem, QToolTip, QVBoxLayout, QWidget
    from ui.widgets.tables import simple_table

    long_text = "Kogake Prime Gauntlet (R2%), Akbolto Prime Barrel (U12%) and more"
    table = simple_table(["What", "N"], 3, stretch=0)
    table.setItem(0, 0, QTableWidgetItem(long_text))
    table.setItem(1, 0, QTableWidgetItem(long_text))
    table.item(1, 0).setToolTip("its own")
    table.setItem(2, 0, QTableWidgetItem("short"))
    for row in range(3):
        table.setItem(row, 1, QTableWidgetItem("1"))
    host = QWidget()
    QVBoxLayout(host).addWidget(table)
    host.resize(260, 300)
    host.show()
    _settle(app)

    def tip(row: int) -> str:
        where = table.visualItemRect(table.item(row, 0)).center()
        QToolTip.hideText()
        QApplication.sendEvent(table.viewport(), QHelpEvent(
            QEvent.ToolTip, where, table.viewport().mapToGlobal(where)))
        return QToolTip.text()

    # In this order: a tip that is not shown leaves the previous one in
    # `QToolTip.text()`, so the cell that must show nothing goes first.
    fits = tip(2)
    own = tip(1)
    cut = tip(0)
    if fits:
        problems.append(f"table: a cell that fits has a tooltip ({fits!r})")
    if own != "its own":
        problems.append(f"table: a cut cell's own tooltip is not kept ({own!r})")
    if cut != long_text:
        problems.append("table: a cut cell does not show its whole text as a "
                        f"tooltip (got {cut!r})")
    QToolTip.hideText()
    host.deleteLater()
    _settle(app)
    print("cut table cell     : whole text as a tooltip, own tooltip kept")


def _check_the_mastery_list_can_be_hit(app, problems: list) -> None:
    """The profile's mastery list: boxes that are boxes, rows that are targets.

    The second table with a box per row had the fault the apply table had -
    at 14pt the boxes were flat lines - and the whole row has to be
    clickable. Both tables now go through tables.check_rows.
    """
    import sqlite3

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QTableWidget
    from ui import theme
    from ui.widgets.mastery_import import MasteryImport

    previous = (theme.active(), theme.font_size(), app.styleSheet())
    app.setStyleSheet(theme.apply(previous[0], 14))
    try:
        rows = [(f"/test/{index}", name, 500_000 + index)
                for index, name in enumerate(("Baza Prime", "Garuda", "Itzal"))]
        dialog = MasteryImport(sqlite3.connect(":memory:"),
                               {"mastered": rows, "already": 0, "short": 0})
        dialog.resize(1600, 900)
        dialog.show()
        _settle(app)
        table = dialog.findChild(QTableWidget)
        box = dialog.boxes[0]
        if box.height() < theme._mark_row:
            problems.append(f"mastery list: the box is {box.width()}x"
                            f"{box.height()}px at 14pt, under its "
                            f"{theme._mark_row}px mark")
        before = dialog.boxes[1].isChecked()
        QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier,
                         table.visualItemRect(table.item(1, 1)).center())
        _settle(app)
        if dialog.boxes[1].isChecked() == before:
            problems.append("mastery list: a click on the name does not flip "
                            "the row")
        size = f"{box.width()}x{box.height()}"
        dialog.deleteLater()
        _settle(app)
        print(f"mastery list       : box {size}px at 14pt")
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])


def _check_a_board_behind_the_game_is_stale(problems: list) -> None:
    """A warframestat board that is only partly expired is still behind.

    On a real board, warframestat's newest fissure had opened 75 minutes earlier;
    24 of its 35 fissures had ended up to 73 minutes ago and 11 still ran. The
    page showed those 11 as the board while the game had 13 ordinary and 11
    Steel Path open - the official feed said the same. Only a board with
    *every* fissure ended counted as stale.
    """
    from datetime import datetime, timedelta, timezone
    from wft.world import fissures as fissures_mod

    now = datetime.now(timezone.utc)

    def row(minutes_left: float, hard: bool = False) -> dict:
        return {"tier": "Lith", "tierNum": 1, "node": "Node", "missionType": "Spy",
                "enemy": "Grineer", "isHard": hard, "isStorm": False,
                "expiry": (now + timedelta(minutes=minutes_left)).isoformat()}

    class _Cfg:
        def get(self, dotted, default=None):
            return {"sources.fissures_url": "https://example.invalid/{platform}",
                    "sources.fissures_stale_after_minutes": 10}.get(dotted, default)

    saved = fissures_mod._ask
    try:
        fissures_mod._ask = lambda _url: [row(20), row(15, True), row(-60), row(-73)]
        try:
            fissures_mod._from_primary(_Cfg())
            problems.append("fissures: a board holding fissures that ended an "
                            "hour ago passed as current")
        except fissures_mod.FissureError:
            pass
        # Their edge caches for two minutes: one that just ended is normal.
        fissures_mod._ask = lambda _url: [row(20), row(-2)]
        try:
            if len(fissures_mod._from_primary(_Cfg())) != 1:
                problems.append("fissures: a current board lost its live fissure")
        except fissures_mod.FissureError as exc:
            problems.append(f"fissures: a current board was called stale ({exc})")
    finally:
        fissures_mod._ask = saved
    print("fissure staleness  : checked a board an hour behind and one 2 min behind")


def _check_a_checked_radio_keeps_its_size(app, problems: list) -> None:
    """A radio button is as big chosen as it is not, and stays round.

    The ring of a chosen button was a thicker border drawn outside an inside
    that had not shrunk: 26px against 20 at 14pt and 37 against 27 at 20pt, and
    the radius worked out for the smaller one made it a rounded square.
    Measured as the indicator rectangle the style reports, at 11, 14 and 20pt.
    """
    import ui_detect
    from ui import theme

    previous = (theme.active(), theme.font_size(), app.styleSheet())
    try:
        for points in (11, 14, 20):
            plain, chosen = ui_detect.indicator_size(app, points)
            if plain != chosen:
                problems.append(
                    f"radio at {points}pt: the indicator is {plain[0]}px "
                    f"unchosen and {chosen[0]}px chosen")
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])
    print("radio              : same size chosen and not, at 11, 14 and 20pt")


def _check_the_combo_arrow_follows_the_text(app, problems: list) -> None:
    """The drop-down part of a combo box is as wide as the line beside it.

    Left to Qt it was 16px at every size: a dot of an arrow with a dark stripe
    beside it at 14pt (26px line) and 20pt (37px line). It has to be at least
    0.7 lines wide at the sizes the user runs.
    """
    import ui_detect
    from ui import theme

    previous = (theme.active(), theme.font_size(), app.styleSheet())
    try:
        for points in (14, 20):
            width, line = ui_detect.combo_arrow_width(app, points)
            if width < 0.7 * line:
                problems.append(
                    f"combo at {points}pt: the drop-down is {width}px wide "
                    f"beside a {line}px line")
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])
    print("combo arrow        : the drop-down follows the text, at 14 and 20pt")


def _check_text_colours_can_be_read_in_every_theme(problems: list) -> None:
    """Every colour the interface writes text in has a contrast of 4.5 or more.

    On each of the three backgrounds text sits on (the page, the raised panel
    and the card), in all five themes. The faint grey of table headings and
    hints was 1.6 to 3.1 in Dark, Light and Orokin, and Blue Steel's red
    "MISSING" 2.9 on a card. The ratio is the WCAG one: (lighter + 0.05)
    over (darker + 0.05) of the relative luminances.
    """
    from ui import theme

    def luminance(colour: str) -> float:
        channels = []
        for start in (1, 3, 5):
            value = int(colour[start:start + 2], 16) / 255
            channels.append(value / 12.92 if value <= 0.03928
                            else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def ratio(first: str, second: str) -> float:
        light, dark = sorted((luminance(first), luminance(second)),
                             reverse=True)
        return (light + 0.05) / (dark + 0.05)

    low = 0
    previous = (theme.active(), theme.font_size())
    for name, palette in theme.THEMES.items():
        # The text of a filled button on its accent: the other place the
        # theme puts text on a colour of its own.
        theme.apply(name, theme.DEFAULT_FONT_PT)
        value = ratio(theme.ON_ACCENT, theme.ACCENT)
        if value < 4.5:
            low += 1
            problems.append(f"contrast: {name} button text on its accent is "
                            f"{value:.2f}")
        for role in ("text", "text_dim", "text_faint", "accent", "good",
                     "warn", "bad", "vaulted", "junk"):
            for ground in ("bg", "bg_elevated", "bg_card"):
                value = ratio(palette[role], palette[ground])
                if value < 4.5:
                    low += 1
                    if low <= 12:
                        problems.append(
                            f"contrast: {name} {role} on {ground} is "
                            f"{value:.2f}")
    theme.apply(*previous)
    print(f"text colours       : {len(theme.THEMES)} themes x 9 colours x 3 "
          f"grounds and button text on its accent, {low} under 4.5")


def _check_a_checked_box_shows_a_tick(app, problems: list) -> None:
    """A chosen check box differs from an empty one by a mark, not only a fill.

    The boxes in the apply and mastery tables were filled squares: chosen and
    not differed in colour alone. Rendered, the chosen box has to hold
    pixels of the on-accent colour (the tick) inside the accent fill.
    """
    from PySide6.QtGui import QColor
    from ui import theme

    previous = (theme.active(), theme.font_size(), app.styleSheet())
    try:
        for name in theme.names():
            app.setStyleSheet(theme.apply(name, 14))
            box = QCheckBox()
            box.setChecked(True)
            box.show()
            _settle(app)
            image = box.grab().toImage()
            ink = QColor(theme.ON_ACCENT)
            fill = QColor(theme.ACCENT)
            ticks = sum(1 for x in range(image.width())
                        for y in range(image.height())
                        if QColor(image.pixel(x, y)).rgb() == ink.rgb())
            fills = sum(1 for x in range(image.width())
                        for y in range(image.height())
                        if QColor(image.pixel(x, y)).rgb() == fill.rgb())
            if ticks < 8 or fills < 8:
                problems.append(f"check box in {name}: {ticks} tick pixels in "
                                f"{fills} of fill - a chosen box is a plain "
                                f"square")
            box.close()
    finally:
        theme.apply(previous[0], previous[1])
        app.setStyleSheet(previous[2])
    print("check box          : a chosen box carries a tick, in all themes")


def _check_numbers_are_aligned_to_the_right(app, problems: list) -> None:
    """A column of figures is right-aligned, its heading with it.

    Done in the shared table so no page has to: "9", "12" and "1,204" lined
    up on the left do not line up. A column with a word in it, a sentence with
    a number in it ("3 / 40") and a name column with a stray figure stay left.
    The heading is written again on every sort click and keeps it.
    """
    from PySide6.QtWidgets import QTableWidgetItem
    from ui.widgets.tables import simple_table

    table = simple_table(["Name", "Each", "Of", "Mixed"], 3)
    rows = (("Ash", "9", "1 / 4", "2"), ("Boar", "1,204", "3 / 40", "Garuda"),
            ("Cobra", "—", "0 / 2", "+3"))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            table.setItem(r, c, QTableWidgetItem(text))
    table.setHorizontalHeaderLabels(["Name", "Each", "Of", "Mixed"])
    table.show()
    _settle(app)
    right = int(Qt.AlignRight | Qt.AlignVCenter)

    def align(r, c):
        return int(table.item(r, c).textAlignment())

    if align(0, 1) != right or align(1, 1) != right:
        problems.append("table: a column of figures is not right-aligned")
    if int(table.horizontalHeaderItem(1).textAlignment()) != right:
        problems.append("table: the heading of a figure column is not "
                        "right-aligned")
    for c, what in ((0, "a name column"), (2, "a 'n / m' column"),
                    (3, "a column with a word in it")):
        if align(0, c) == right:
            problems.append(f"table: {what} was right-aligned")
    table.close()
    print("table figures      : figure columns right, words and 'n / m' left")


def _check_a_long_path_is_shortened_with_its_whole_text(app, problems: list) -> None:
    """The footer of Settings shortens a path in the middle, tooltip in full.

    A path has no spaces to wrap at: with the checkout under a long directory
    the footer was 2374px of text on a page 1500px wide and just ended.
    The label may be narrower than its text only if it says all of it.
    """
    from ui.widgets.elided_label import ElidedLabel

    path = "/home/someone/a/very/long/directory/of/projects/" * 3 + "config.toml"
    label = ElidedLabel(path)
    label.resize(300, label.sizeHint().height())
    label.show()
    _settle(app)
    if label.shown_text() == path or "…" not in label.shown_text():
        problems.append("elided label: a path wider than the label is not "
                        "shortened")
    if not label.shown_text().endswith("config.toml"):
        problems.append("elided label: the file name, the part that says "
                        "which file, is cut")
    if label.toolTip() != path:
        problems.append("elided label: the tooltip is not the whole path")
    label.resize(4000, label.height())
    _settle(app)
    if label.shown_text() != path:
        problems.append("elided label: a path that fits is shortened")
    label.close()
    print("elided label       : shortened in the middle, whole text as tooltip")


# -- Setup, the hotkey and the first start --------------------------------

class _Patches:
    """Attribute patches taken back together, whatever the check does in between."""

    def __init__(self):
        self._undo: list = []

    def set(self, obj, name, value) -> None:
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def env(self, name, value) -> None:
        self._undo.append((os.environ, name, os.environ.get(name)))
        os.environ[name] = value

    def restore(self) -> None:
        for obj, name, old in reversed(self._undo):
            if obj is os.environ:
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
            else:
                setattr(obj, name, old)
        self._undo.clear()


def _wait(app, condition, seconds: float = 4.0) -> bool:
    """Run the event loop until `condition()` holds; False when it never did."""
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    app.processEvents()
    return bool(condition())


def _setup_cfg(data: bool, key: str, log: str | None = None):
    """A configuration over a scratch data directory, never the user's files.

    The Steam search paths and the log's place in a library come from the
    shipped defaults, not from whatever file the user has: the check is about
    the defaults.
    """
    import tomllib

    from wft import config as config_mod
    from wft.data import db as dbq

    directory = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(directory))
    cfg.override("hotkey.key", key)
    with open(config_mod._EXAMPLE, "rb") as handle:
        shipped = tomllib.load(handle)
    cfg.override("paths.steam_roots", shipped["paths"]["steam_roots"])
    cfg.override("paths.ee_log_in_library", shipped["paths"]["ee_log_in_library"])
    cfg.override("paths.ee_log", log or str(directory / "no-such-steam" / "EE.log"))
    cfg.override("game.window_class", "warframe")
    if data:
        connection = dbq.connect(cfg.db_path)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', "
            "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
        connection.commit()
        connection.close()
    return cfg


def _machine(patches: _Patches, group: str, devices: int | None) -> None:
    """Say what the keyboard side of the machine looks like, to `readiness`."""
    from wft import readiness

    patches.set(readiness, "input_group_state", lambda user=None: group)
    patches.set(readiness, "readable_key_devices", lambda: devices)


def _check_the_setup_page_shows_each_state(app, problems: list) -> None:
    """Empty, partly done and done: each card says what is the case.

    The statuses are `wft.readiness`'s, the same answers `wf doctor` prints, so
    this drives the page through the three states by changing what the machine
    looks like (group, readable devices, configured key, game data) rather than
    by poking the page.
    """
    from PySide6.QtWidgets import QLabel

    from ui import app as app_mod
    from ui.views.setup_view import SetupView
    from wft import readiness

    scenes = [
        ("empty", dict(data=False, key=""), ("missing", 0),
         {"data": "todo", "input": "todo", "hotkey": "todo"}, "0 of 3"),
        ("partly done", dict(data=True, key="F8"), ("pending", 0),
         {"data": "done", "input": "todo", "hotkey": "done"}, "2 of 3"),
        ("done", dict(data=True, key="F8"), ("active", 4),
         {"data": "done", "input": "done", "hotkey": "done"}, "All required"),
    ]
    for label, cfg_args, (group, devices), expected, summary in scenes:
        patches = _Patches()
        try:
            _machine(patches, group, devices)
            cfg = _setup_cfg(**cfg_args)
            page = SetupView(cfg)
            page.refresh()
            app.processEvents()
            for card, kind in expected.items():
                if page.cards[card].kind != kind:
                    problems.append(f"setup {label}: card {card} is "
                                    f"{page.cards[card].kind!r}, not {kind!r}")
            if not page.summary.text().startswith(summary):
                problems.append(f"setup {label}: summary says {page.summary.text()!r}")
            start = app_mod.start_page_for(cfg)
            if start != ("Plan" if label == "done" else "Setup"):
                problems.append(f"setup {label}: the app would open on {start}")
            if app_mod.start_page_for(cfg, "Import") != "Import":
                problems.append(f"setup {label}: --page Import did not win")

            texts = " ".join(w.text() for w in page.cards["input"].findChildren(QLabel))
            if label == "empty":
                command = f"sudo usermod -aG input {readiness.current_user()}"
                if command not in texts:
                    problems.append(f"setup empty: the group command is not shown "
                                    f"({texts[:80]!r})")
                copy = [b for b in page.cards["input"].findChildren(QPushButton)
                        if b.text() == "Copy"]
                if not copy:
                    problems.append("setup empty: no Copy button for the command")
                else:
                    QApplication.clipboard().setText("")
                    copy[0].click()
                    if QApplication.clipboard().text() != command:
                        problems.append("setup empty: Copy did not copy the command")
                if "log out" not in page.cards["input"].text.text().lower():
                    problems.append("setup empty: no word about logging out again")
            elif label == "partly done":
                if "usermod" in texts:
                    problems.append("setup partly done: shows the group command "
                                    "though the user is already in the group")
                if "log out" not in page.cards["input"].text.text().lower():
                    problems.append("setup partly done: does not say to log in again")
            elif "usermod" in texts:
                problems.append("setup done: still shows the group command")
        finally:
            patches.restore()
    print("setup page         : empty, partly done and done show their cards; "
          "the group command copies")


def _check_the_download_button_runs_the_shared_update(app, problems: list) -> None:
    """Setup's button and Settings' button are one job, with the log in both.

    Stubbed at `_run_update`, the one function both reach, so nothing leaves the
    machine. Held open on an event so the running state can be looked at.
    """
    import contextlib
    import io
    import threading

    from ui import app as app_mod
    from ui.views import settings_view as settings_mod
    from wft.data import db as dbq

    gate = threading.Event()

    def fake_update(cfg, *, force=False, report=lambda _m: None):
        report("stub: fetching")
        gate.wait(5)
        connection = dbq.connect(cfg.db_path)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('last_update_at', "
            "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
        connection.commit()
        connection.close()
        return {}

    def failing_update(cfg, *, force=False, report=lambda _m: None):
        report("stub: asking")
        raise RuntimeError("no network")

    patches = _Patches()
    try:
        _machine(patches, "active", 3)
        cfg = _setup_cfg(data=False, key="F8")
        _app, window = app_mod.build_application(cfg, "Setup")
        window.resize(1400, 900)
        window.show()
        _settle(app)
        setup = window._pages["Setup"]
        settings = window._pages["Settings"]

        patches.set(settings_mod, "_run_update", fake_update)
        if setup.cards["data"].kind != "todo":
            problems.append("download: the data card is not open before the download")
        setup.download_btn.click()
        _wait(app, lambda: setup.update_job.running and "stub: fetching"
              in setup.log_view.toPlainText())
        if setup.download_btn.isEnabled() or settings.update_btn.isEnabled():
            problems.append("download: a button stayed enabled while the job runs")
        if window.update_job.start():
            problems.append("download: a second update was started while one runs")
        if "stub: fetching" not in settings.log_view.toPlainText():
            problems.append("download: Settings does not show the log of the "
                            "download Setup started")
        gate.set()
        _wait(app, lambda: not setup.update_job.running
              and setup.cards["data"].kind == "done")
        if setup.cards["data"].kind != "done":
            problems.append("download: the data card did not turn to done")
        if not setup.download_btn.isEnabled() or not settings.update_btn.isEnabled():
            problems.append("download: the buttons were not given back")
        if "Done." not in setup.log_view.toPlainText():
            problems.append("download: the log does not end in Done.")

        # A failure is said, on the card, and the step stays open.
        cfg2 = _setup_cfg(data=False, key="F8")
        _app2, window2 = app_mod.build_application(cfg2, "Setup")
        window2.show()
        _settle(app)
        setup2 = window2._pages["Setup"]
        patches.set(settings_mod, "_run_update", failing_update)
        with contextlib.redirect_stderr(io.StringIO()):
            setup2.download_btn.click()
            _wait(app, lambda: not setup2.update_job.running
                  and "Failed" in setup2.log_view.toPlainText())
        if "no network" not in setup2.cards["data"].text.text():
            problems.append("download: the failure is not on the data card")
        if setup2.cards["data"].kind != "todo":
            problems.append("download: a failed download closed the step")
        window.close()
        window2.close()
    finally:
        gate.set()
        patches.restore()
    print("download           : Setup and Settings share one job, log in both, "
          "failure stays on the card")


class _FakeKeyboard:
    """An input device that the listeners can select on and read from.

    A pipe stands in for the device node; `press` queues an event and makes the
    pipe readable, which is all `selectors` and `read()` ask of a device.
    """

    def __init__(self, path: str = "/fake/event0"):
        self.path = path
        self._read_fd, self._write_fd = os.pipe()
        self._queue: list = []

    def fileno(self) -> int:
        return self._read_fd

    def read(self):
        os.read(self._read_fd, 4096)
        events, self._queue = self._queue, []
        return events

    def press(self, code: int, value: int = 1) -> None:
        from types import SimpleNamespace

        from wft.capture.keys import ecodes

        self._queue.append(SimpleNamespace(type=ecodes.EV_KEY, code=code, value=value))
        os.write(self._write_fd, b"x")

    def forget(self) -> None:
        """Drop what was pressed so far, as a device that nobody had open would."""
        self._queue = []
        os.set_blocking(self._read_fd, False)
        try:
            while os.read(self._read_fd, 4096):
                pass
        except BlockingIOError:
            pass
        os.set_blocking(self._read_fd, True)

    def close(self) -> None:
        pass

    def really_close(self) -> None:
        os.close(self._read_fd)
        os.close(self._write_fd)


def _fake_devices(patches: _Patches, *devices) -> None:
    from wft.capture import keys

    def listing(skip=frozenset()):
        return iter([d for d in devices if d.path not in skip])

    patches.set(keys, "key_devices", listing)


def _check_a_key_is_chosen_by_pressing_it(app, problems: list) -> None:
    """Press a key and it is shown, saved and in force; then press it to test.

    No real device: a pipe plays the keyboard. A mouse click on the button that
    starts the choosing must not become the hotkey, Esc cancels, and without
    readable devices the page says why instead of waiting.
    """
    from PySide6.QtWidgets import QMessageBox

    from ui import settings_io
    from ui.views.settings_view import SettingsView
    from ui.widgets.key_picker import KeyPicker
    from wft.capture import keys

    if keys.evdev is None:
        print("key choice         : skipped, python-evdev is not installed")
        return
    ecodes = keys.ecodes
    patches = _Patches()
    keyboard = _FakeKeyboard()
    try:
        config_home = pathlib.Path(tempfile.mkdtemp())
        patches.env("XDG_CONFIG_HOME", str(config_home))
        _fake_devices(patches, keyboard)
        cfg = _setup_cfg(data=True, key="")
        picker = KeyPicker(cfg)
        chosen: list = []
        heard: list = []
        picker.picked.connect(chosen.append)
        picker.received.connect(heard.append)

        picker.start_pick()
        keyboard.press(ecodes.BTN_LEFT)        # the click on the button itself
        keyboard.press(ecodes.KEY_F8)
        if not _wait(app, lambda: chosen):
            problems.append("key choice: pressing F8 did not choose it")
        else:
            if chosen != ["F8"]:
                problems.append(f"key choice: chose {chosen}, expected ['F8']")
            if cfg.get("hotkey.key") != "F8":
                problems.append("key choice: the running configuration still "
                                f"says {cfg.get('hotkey.key')!r}")
            saved = (config_home / "warframe-tracker" / "config.toml")
            if not saved.is_file() or 'key = "F8"' not in saved.read_text():
                problems.append("key choice: the key was not written to the file")
            if picker.mode != "check" or "saved" not in picker.message.text():
                problems.append("key choice: after choosing, the page does not ask "
                                f"to press it ({picker.message.text()!r})")
            keyboard.press(ecodes.KEY_A)
            if not _wait(app, lambda: "That was A" in picker.message.text()):
                problems.append("key choice: another key was not named as wrong")
            keyboard.press(ecodes.KEY_F8)
            if not _wait(app, lambda: heard):
                problems.append("key choice: pressing the chosen key was not received")
            elif not picker.message.text().startswith("Received F8"):
                problems.append(f"key choice: says {picker.message.text()!r} "
                                "after the key arrived")

        # Esc cancels and keeps what was there.
        picker.start_pick()
        keyboard.press(ecodes.KEY_ESC)
        if not _wait(app, lambda: picker.mode is None):
            problems.append("key choice: Esc did not cancel")
        if cfg.get("hotkey.key") != "F8":
            problems.append("key choice: Esc changed the key")

        # No device that can be read: say so, do not wait.
        _fake_devices(patches)
        picker.start_pick()
        if not _wait(app, lambda: picker.mode is None):
            problems.append("key choice: still waiting with no readable device")
        if "input" not in picker.message.text():
            problems.append("key choice: no word about the input group "
                            f"({picker.message.text()!r})")
        if cfg.get("hotkey.key") != "F8":
            problems.append("key choice: a failed choice changed the key")

        # Settings has the same widget, and its field follows it.
        _fake_devices(patches, keyboard)
        view = SettingsView(cfg)
        view.key_picker.start_pick()
        keyboard.press(ecodes.KEY_F7)
        if not _wait(app, lambda: view.hotkey_edit.text() == "F7"):
            problems.append("key choice: the Settings field did not follow the choice")
        view.key_picker.cancel("")
        warned: list = []
        patches.set(QMessageBox, "warning",
                    staticmethod(lambda *a, **k: warned.append(a[2] if len(a) > 2 else "")))
        view.hotkey_edit.setText("\\")
        view._save_capture_settings()
        if not warned or cfg.get("hotkey.key") != "F7":
            problems.append("key choice: a typed backslash was saved as the key")
        view.hotkey_edit.setText("F6")
        view._save_capture_settings()
        if cfg.get("hotkey.key") != "F6":
            problems.append("key choice: Save did not put a typed key in force")
        view.deleteLater()
        picker.cancel("")
    finally:
        patches.restore()
        keyboard.really_close()
    print("key choice         : pressed, shown, saved, in force, tested; Esc and "
          "no-device handled; Settings shares it")


def _check_the_listener_takes_a_new_key_without_a_restart(app, problems: list) -> None:
    """The running listener follows the configuration: unset, set, changed.

    The state a newcomer is in: the program started with no key at all, and
    choosing one on the Setup page has to work without closing it.
    """
    from ui import settings_io
    from ui.workers.hotkey import HotkeyWorker
    from wft.capture import keys

    if keys.evdev is None:
        print("listener           : skipped, python-evdev is not installed")
        return
    ecodes = keys.ecodes
    patches = _Patches()
    keyboard = _FakeKeyboard()
    worker = None
    try:
        patches.env("XDG_CONFIG_HOME", tempfile.mkdtemp())
        _fake_devices(patches, keyboard)
        cfg = _setup_cfg(data=True, key="")
        # No window class: the focus gate lets everything through, which is what
        # a test without a game window needs.
        cfg.override("game.window_class", "")
        fired: list = []
        said: list = []
        worker = HotkeyWorker(cfg)
        worker.triggered.connect(lambda: fired.append(1))
        worker.status.connect(said.append)
        worker.start()
        if not _wait(app, lambda: "no hotkey configured" in said):
            problems.append("listener: no word about having no hotkey")
        keyboard.press(ecodes.KEY_F8)
        _wait(app, lambda: False, 0.8)
        if fired:
            problems.append("listener: fired with no key configured")
        # The listener opens the devices only once a key is set, and a real
        # device keeps nothing for a file that was not open. The pipe would keep
        # that press and hand it over with the first read.
        keyboard.forget()

        def press_until(code) -> bool:
            """Press `code` until it fires once more, then let late signals land."""
            import time

            want = len(fired) + 1
            end = time.monotonic() + 5
            heard = False
            while time.monotonic() < end and not heard:
                keyboard.press(code)
                heard = _wait(app, lambda: len(fired) >= want, 0.25)
            # A press sent just before the signal of the last one arrived fires
            # too. Wait until the count stays put, or that press is counted
            # against the next step.
            reached = len(fired)
            while _wait(app, lambda: len(fired) != reached, 0.8):
                reached = len(fired)
            return heard

        settings_io.save_value(cfg, "hotkey", "key", "F8")
        if not press_until(ecodes.KEY_F8):
            problems.append("listener: a key chosen after the start was not heard")
        if "listening for F8" not in said:
            problems.append(f"listener: did not say it listens for F8 ({said})")
        settings_io.save_value(cfg, "hotkey", "key", "F9")
        if not press_until(ecodes.KEY_F9):
            problems.append("listener: a changed key was not heard")
        before = len(fired)
        keyboard.press(ecodes.KEY_F8)          # the old key
        _wait(app, lambda: False, 1.2)
        if len(fired) != before:
            problems.append("listener: the old key still fires after the change")
    finally:
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)
        patches.restore()
        keyboard.really_close()
    print("listener           : no key, then F8, then F9, each heard without a restart")


def _steam_tree(root: pathlib.Path, shipped: dict) -> dict:
    """A made-up Steam layout under `root`: normal, Flatpak and a second library."""
    import time

    relative = shipped["ee_log_in_library"]
    made = {}

    def log(library: pathlib.Path, age_hours: int):
        path = library / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        stamp = time.time() - age_hours * 3600
        os.utime(path, (stamp, stamp))
        return path

    standard = root / ".local/share/Steam"
    flatpak = root / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
    second = root / "games" / "SteamLibrary"
    (standard / "steamapps").mkdir(parents=True)
    (standard / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n'
        '\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % (standard, second))
    # ~/.steam/steam is a link to the normal Steam folder, as on a real install.
    (root / ".steam").mkdir()
    (root / ".steam" / "steam").symlink_to(standard)
    made["flatpak"] = log(flatpak, 1)
    made["second"] = log(second, 5)
    made["standard"] = log(standard, 10)
    return made


def _check_the_game_log_is_found_in_every_steam_layout(app, problems: list) -> None:
    """EE.log is searched in the normal Steam folder, the Flatpak and a second library.

    A made-up tree under a scratch home, the shipped default search paths. The
    normal folder is also reached through `~/.steam/steam`, which must not make
    it two finds. `wf doctor` names the same file as the Setup page.
    """
    import tomllib

    from ui.views.setup_view import SetupView
    from wft import config as config_mod, doctor, gamelog, readiness

    with open(config_mod._EXAMPLE, "rb") as handle:
        shipped = tomllib.load(handle)["paths"]
    patches = _Patches()
    try:
        home = pathlib.Path(tempfile.mkdtemp())
        made = _steam_tree(home, shipped)
        patches.env("HOME", str(home))
        _machine(patches, "active", 3)
        patches.env("XDG_CONFIG_HOME", str(home / ".config"))
        cfg = _setup_cfg(data=True, key="F8", log=str(home / "nowhere" / "EE.log"))

        found = gamelog.find_for(cfg)
        want = [made["flatpak"], made["second"], made["standard"]]
        if [p.resolve() for p in found] != [p.resolve() for p in want]:
            problems.append(f"game log: found {[str(p) for p in found]}, wanted "
                            f"{[str(p) for p in want]} (newest first, no duplicate)")
        for layout in ("standard", "flatpak", "second"):
            alone = pathlib.Path(tempfile.mkdtemp())
            one = _steam_tree(alone, shipped)
            for name, path in one.items():
                if name != layout:
                    path.unlink()
            patches.env("HOME", str(alone))
            hits = gamelog.find_for(cfg)
            if len(hits) != 1 or hits[0].resolve() != one[layout].resolve():
                problems.append(f"game log: {layout} alone gave "
                                f"{[str(p) for p in hits]}")
        patches.env("HOME", str(pathlib.Path(tempfile.mkdtemp())))
        if gamelog.find_for(cfg):
            problems.append("game log: found something in an empty home")
        patches.env("HOME", str(home))

        check = readiness.ee_log_check(cfg)
        if check.ok or [p.resolve() for p in check.info["found"]] != \
                [p.resolve() for p in want]:
            problems.append("game log: readiness does not offer what the search finds")
        line = " ".join(detail for _l, _n, detail in doctor.check_ee_log(cfg))
        if str(made["flatpak"]) not in line:
            problems.append(f"game log: wf doctor does not name the find ({line[:100]})")

        page = SetupView(cfg)
        page.refresh()
        app.processEvents()
        card = page.cards["ee_log"]
        buttons = [b for b in card.findChildren(QPushButton) if b.text() == "Use this file"]
        if len(buttons) != 3:
            problems.append(f"game log: {len(buttons)} 'Use this file' buttons, wanted 3")
        else:
            buttons[0].click()
            saved = pathlib.Path(os.environ["XDG_CONFIG_HOME"]) / "warframe-tracker" / "config.toml"
            if not saved.is_file() or str(made["flatpak"]) not in saved.read_text():
                problems.append("game log: the chosen log was not written to the config")
            if cfg.get("paths.ee_log") != str(made["flatpak"]):
                problems.append("game log: the running config did not take the log")
            if card.kind != "done":
                problems.append(f"game log: the card is {card.kind!r} after Use")
        page.deleteLater()

        # Nothing found anywhere: the card says so and has no empty line under
        # its button (an empty label for a later error message took one).
        from PySide6.QtWidgets import QLabel

        patches.env("HOME", str(pathlib.Path(tempfile.mkdtemp())))
        cfg = _setup_cfg(data=True, key="F8", log=str(home / "nowhere" / "EE.log"))
        page = SetupView(cfg)
        page.refresh()
        app.processEvents()
        if page.cards["ee_log"].badge.text() != "Not found":
            problems.append(f"game log: an empty home shows "
                            f"{page.cards['ee_log'].badge.text()!r}, not 'Not found'")
        for name, step in page.cards.items():
            # A line of its own in the card's column; an empty label beside a
            # button in a row (the key's message) takes no room.
            lines = [step.extra.itemAt(i).widget() for i in range(step.extra.count())]
            empty = [label for label in lines if isinstance(label, QLabel)
                     and label.isVisibleTo(step) and not label.text()]
            if empty:
                problems.append(f"setup: card {name} shows {len(empty)} empty line(s)")
        page.deleteLater()
    finally:
        patches.restore()
    print("game log           : normal Steam, Flatpak and a second library found, "
          "no duplicate, doctor and Setup agree; nothing found leaves no gap")


def _check_rewards_says_not_ready_until_setup_is_done(app, problems: list) -> None:
    """The Rewards page says Ready only when data, key access and a hotkey are there."""
    from ui import app as app_mod

    cases = [
        ("no data", dict(data=False, key="F8"), ("active", 3), "Download the game data"),
        ("group pending", dict(data=True, key="F8"), ("pending", 0), "Log out and back in"),
        ("group missing", dict(data=True, key="F8"), ("missing", 0), "Give yourself keyboard access"),
        ("no hotkey", dict(data=True, key=""), ("active", 3), "Choose a hotkey"),
        ("unknown key", dict(data=True, key="NOPE"), ("active", 3), "Choose the hotkey again"),
    ]
    patches = _Patches()
    try:
        for label, cfg_args, (group, devices), want in cases:
            _machine(patches, group, devices)
            cfg = _setup_cfg(**cfg_args)
            _app, window = app_mod.build_application(cfg, "Rewards")
            window.show()
            _settle(app)
            panel = window.relic_panel
            text = panel.idle_label.text()
            if not text.startswith("Not ready") or want not in text:
                problems.append(f"rewards {label}: says {text!r}, wanted 'Not ready' "
                                f"and {want!r}")
            if not panel.setup_btn.isVisible():
                problems.append(f"rewards {label}: no Open Setup button")
            else:
                window.select("Plan")
                window.select("Rewards")
                panel.setup_btn.click()
                _settle(app)
                if window.stack.currentWidget() is not window._pages["Setup"]:
                    problems.append(f"rewards {label}: Open Setup did not open Setup")
                window.select("Rewards")
            window.close()

        _machine(patches, "active", 3)
        cfg = _setup_cfg(data=True, key="F8")
        _app, window = app_mod.build_application(cfg, "Rewards")
        window.show()
        _settle(app)
        panel = window.relic_panel
        if panel.idle_label.text() != "Ready  ·  press F8 on the reward screen":
            problems.append(f"rewards ready: says {panel.idle_label.text()!r}")
        if panel.setup_btn.isVisible():
            problems.append("rewards ready: the Open Setup button is still there")
        # Setup changes under the page: coming back to it, it says so.
        cfg.override("hotkey.key", "")
        window.select("Plan")
        window.select("Rewards")
        if not panel.idle_label.text().startswith("Not ready"):
            problems.append("rewards: did not notice that the hotkey was cleared")
        # An answer on screen is not replaced by a status line.
        panel.show_failure("x")
        window.select("Plan")
        window.select("Rewards")
        if panel.verdict_banner.text() != "RESCAN":
            problems.append(f"rewards: banner {panel.verdict_banner.text()!r} "
                            "after a failure with no hotkey set")
        if not panel.verdict_banner.isVisible() or panel.idle_label.isVisible():
            problems.append("rewards: coming back to the page replaced the answer "
                            "on screen with the waiting state")
        window.close()
    finally:
        patches.restore()
    print("rewards            : Not ready with the first open step and an Open "
          "Setup button, Ready only when all three hold")


def _check_the_plan_points_at_the_page_that_fixes_it(app, problems: list) -> None:
    from PySide6.QtWidgets import QLabel

    from ui.views.plan_view import PlanView

    patches = _Patches()
    try:
        _machine(patches, "active", 3)
        for data, needle in ((False, "Setup"), (True, "Collection → Add and correct")):
            cfg = _setup_cfg(data=data, key="F8")
            page = PlanView(cfg)
            page.refresh()
            text = " ".join(w.text() for w in page.findChildren(QLabel))
            if needle not in text or "Add / Manage" in text:
                problems.append(f"plan empty state (data={data}): {text[:120]!r}")
            page.deleteLater()
    finally:
        patches.restore()
    print("plan empty state   : names Setup without data, the Collection tab with it")


#: Names pages and tabs used to have. A text that still says one sends the
#: reader somewhere that does not exist.
OLD_NAMES = ("Add / Manage", "relic panel", "reward panel", "rewards panel",
             "second monitor", "other monitor", "Inventory →", "Relic page",
             "listener daemon")


def _check_no_text_uses_an_old_page_name(app, problems: list) -> None:
    """Every label, button, tab and tooltip of every page, hidden ones included."""
    from PySide6.QtWidgets import QGroupBox, QLabel, QTabWidget

    from ui import app as app_mod

    patches = _Patches()
    try:
        _machine(patches, "missing", 0)
        for data in (False, True):
            cfg = _setup_cfg(data=data, key="")
            _app, window = app_mod.build_application(cfg, "Setup")
            window.show()
            _settle(app)
            texts: list = []
            for name in window._pages:
                window.select(name)
                _settle(app)
                page = window._pages[name]
                for label in page.findChildren(QLabel):
                    texts.append(label.text())
                    texts.append(label.toolTip())
                for button in page.findChildren(QPushButton):
                    texts.append(button.text())
                    texts.append(button.toolTip())
                for box in page.findChildren(QGroupBox):
                    texts.append(box.title())
                for tabs in page.findChildren(QTabWidget):
                    texts += [tabs.tabText(i) for i in range(tabs.count())]
            window.close()
            for old in OLD_NAMES:
                hits = [t for t in texts if old.lower() in t.lower()]
                if hits:
                    problems.append(f"old name {old!r} still shown (data={data}): "
                                    f"{hits[0][:80]!r}")
    finally:
        patches.restore()
    print("page names         : no label, button, tab or tooltip uses an old name")


def _check_no_hotkey_is_assumed(problems: list) -> None:
    """The shipped default is no key, and a missing key is "not set up", not F23."""
    import tomllib

    from wft import config as config_mod, readiness

    with open(config_mod._EXAMPLE, "rb") as handle:
        shipped = tomllib.load(handle)
    if shipped["hotkey"]["key"] != "":
        problems.append(f"hotkey default is {shipped['hotkey']['key']!r}, not empty")
    check = readiness.hotkey_check(_setup_cfg(data=False, key=""))
    if check.state != "unset" or check.ok:
        problems.append(f"an empty hotkey reads as {check.state!r}")
    print("hotkey default     : empty in the shipped config, reads as not set up")



# -- the last reward screen, the calibration dialog and the first reward screen --

#: The reward row as measured on live screens, written out here and
#: not taken from `wft.relic.regions`: a test that draws with the module's own
#: numbers can never find that they are wrong. One-line names sit on the lower
#: of the two lines, 0.401 to 0.425 of the window height. Cards are 480 px wide
#: at 3840, 4.67 px apart, the row centred on 1920.
_ROW_CENTRE, _TILE_W, _LINE2 = 0.5, 480 / 3840, (0.401, 0.425)
_PITCH = _TILE_W + 14 / 3 / 3840

#: The left edges of the cards in pixels at 3840x2160, as read off the coloured
#: bars under the cards of 278, 34 and 11 live screens, by reward count.
_MEASURED_LEFT_EDGES = {4: (953, 1438, 1922, 2407), 3: (1195, 1680, 2165),
                        2: (1438, 1922)}


def _reward_screen(names, width=3840, height=2160):
    """A BGR picture of a reward screen: the names, one line each, on black."""
    import numpy
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    image = QImage(width, height, QImage.Format_RGB888)
    image.fill(QColor("#0a0e14"))
    painter = QPainter(image)
    font = QFont("Noto Sans")
    font.setPixelSize(round(0.0139 * height))
    painter.setFont(font)
    painter.setPen(QColor("#d8dde6"))
    first = _ROW_CENTRE - (len(names) * _TILE_W + (len(names) - 1) * (_PITCH - _TILE_W)) / 2
    for index, name in enumerate(names):
        painter.drawText(
            QRectF((first + index * _PITCH) * width, _LINE2[0] * height,
                   _TILE_W * width, (_LINE2[1] - _LINE2[0]) * height),
            Qt.AlignCenter, name)
    painter.end()
    rows = numpy.frombuffer(image.constBits(), numpy.uint8).reshape(
        height, image.bytesPerLine())[:, :width * 3].reshape(height, width, 3)
    return numpy.ascontiguousarray(rows[:, :, ::-1])


def _drop(app, dialog) -> None:
    """Close a dialog and have it deleted while the application still exists.

    A dialog nobody parents is only freed when Python collects it, which for one
    holding a QPixmap can be after the QApplication is gone - the process then
    ends in a segmentation fault (exit 139) after printing OK.
    """
    from PySide6.QtGui import QPixmap

    dialog.close()
    dialog.picker._source = QPixmap()
    dialog.deleteLater()
    _settle(app)


def _scaled(image, width, height):
    import cv2

    return cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)


def _fake_game(patches, screen):
    """Make `find_game` and `grab` answer from a picture, as the game window would."""
    from types import SimpleNamespace

    import numpy

    from wft.capture import window as cap

    height, width = screen.shape[:2]
    game = SimpleNamespace(width=width, height=height, xid=1,
                           wm_class="warframe", title="Warframe")

    def grab(_window, region=None, display=None):
        x, y, w, h = 0, 0, width, height
        if region is not None:
            fx, fy, fw, fh = region
            x, y = int(fx * width), int(fy * height)
            w = min(max(1, int(fw * width)), width - x)
            h = min(max(1, int(fh * height)), height - y)
        part = screen[y:y + h, x:x + w]
        bgrx = numpy.dstack([part, numpy.full(part.shape[:2], 255, numpy.uint8)])
        return cap.Frame(w, h, w * 4, 32, bgrx.tobytes())

    patches.set(cap, "find_game", lambda _window_class, display=None: game)
    patches.set(cap, "grab", grab)
    return game


def _trigger_result(texts, named):
    """A `TriggerResult` for these raw readings; the first `named` count as parts."""
    from ui.workers.relic_trigger import TriggerResult
    from wft.relic import score as S

    rewards = [S.Reward(position=i + 1, raw_text=text,
                        part_unique=f"/Test/{i}" if i < named else None,
                        name=text if i < named else "")
               for i, text in enumerate(texts)]
    return TriggerResult(S.rank(rewards), None, len(texts))


def _check_the_hotkey_keeps_the_last_reward_screen(app, cfg, problems: list) -> None:
    """A press keeps the window's picture and its reading in memory, and only there.

    Driven through `run_trigger` itself with the window answered from a
    picture: the grab, the read and the keeping are the code a press runs.
    """
    import time

    import importlib

    import numpy

    from ui.workers import relic_trigger as RT
    from wft.data import db as dbq
    from wft.relic import last_screen, match as M, regions as regions_mod

    connection = dbq.connect(cfg.db_path)
    parts = [c.name for c in M.all_prime_parts(connection) if c.name.endswith("Prime Stock")]
    connection.close()
    if len(parts) < 4:
        return
    names = parts[:4]
    screen = _reward_screen(names, 1920, 1080)

    patches = _Patches()
    saved = {key: cfg.get(key) for key in
             ("paths.ee_log", "capture.keep_rewards", "capture.keep_rewards_dir",
              "regions.reward")}
    folder = pathlib.Path(tempfile.mkdtemp())
    try:
        cfg.override("paths.ee_log", str(folder / "none" / "EE.log"))
        cfg.override("capture.keep_rewards", False)
        cfg.override("capture.keep_rewards_dir", "")
        # The shipped regions, whatever the user's own file says: the module's
        # constants keep what an earlier load put there, so they are reset too.
        cfg.override("regions.reward", {})
        importlib.reload(regions_mod)
        _fake_game(patches, screen)

        # Nothing reaches the disk: every way an image could be written is
        # made to say so.
        written: list = []
        from PIL import Image as PilImage
        import cv2

        # Only a path counts: the OCR engine saves each crop into a BytesIO to
        # hand it to Tesseract, which is memory too.
        def on_disk(target) -> bool:
            return isinstance(target, (str, os.PathLike))

        real_save = PilImage.Image.save

        def save(image, target, *args, **kwargs):
            if on_disk(target):
                written.append(target)
                return None
            return real_save(image, target, *args, **kwargs)

        patches.set(PilImage.Image, "save", save)
        patches.set(cv2, "imwrite", lambda *a, **k: written.append(a) or True)
        patches.set(numpy, "save", lambda *a, **k: written.append(a))

        last_screen.forget()
        before = time.time()
        result = RT.run_trigger(cfg)
        kept = last_screen.current()
        if result.error or kept is None:
            problems.append(f"last screen: nothing kept after a read "
                            f"({result.error!r})")
            return
        if kept.image.shape != (1080, 1920, 3) or not numpy.array_equal(kept.image, screen):
            problems.append("last screen: the kept picture is not the window")
        if kept.reading.total != 4 or kept.reading.recognised < 3 or kept.reading.bad:
            problems.append(f"last screen: reading {kept.reading}, wanted 3 or "
                            f"4 of 4 recognised")
        if not before - 1 <= kept.taken_at <= time.time():
            problems.append("last screen: the time of the press is wrong")
        if written:
            problems.append("last screen: something was written to disk with "
                            "no folder configured")
        if kept.image.flags.writeable:
            problems.append("last screen: the kept picture can be altered by a reader")

        # A blank screen is read, found bad and kept: it is the one to calibrate on.
        _fake_game(patches, _reward_screen(["", "", "", ""], 1920, 1080))
        RT.run_trigger(cfg)
        blank = last_screen.current()
        if blank is kept or not blank.reading.bad or blank.reading.recognised != 0:
            problems.append(f"last screen: a blank screen was not kept as a bad "
                            f"reading ({blank.reading})")

        # No window, no picture: the previous one stays and the answer is an error.
        from wft.capture import window as cap

        patches.set(cap, "find_game", lambda _c, display=None: None)
        failed = RT.run_trigger(cfg)
        if not failed.error or last_screen.current() is not blank:
            problems.append("last screen: a press with no game window replaced "
                            "the kept picture")

        # A reading that raises is the RESCAN case too, kept and still raised.
        _fake_game(patches, screen)

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        patches.set(RT, "_read_and_rank", boom)
        try:
            RT.run_trigger(cfg)
            problems.append("last screen: a crashing reading did not raise")
        except RuntimeError:
            pass
        crashed = last_screen.current()
        if crashed is blank or not crashed.reading.failed or not crashed.reading.bad:
            problems.append("last screen: a crashed reading was not kept as failed")
        patches.restore()

        # The opt-in folder still gets its one picture, from the same grab.
        _fake_game(patches, screen)
        cfg.override("capture.keep_rewards_dir", str(folder / "kept"))
        # A folder alone is not enough: the switch is separate.
        cfg.override("capture.keep_rewards", False)
        RT.run_trigger(cfg)
        if (folder / "kept").exists():
            problems.append("last screen: a folder with keep_rewards off was written to")
        _fake_game(patches, screen)
        cfg.override("capture.keep_rewards", True)
        taken = RT.run_trigger(cfg)
        files = list((folder / "kept").glob("*/??:??:??:???.jpg"))
        if len(files) != 1:
            problems.append(f"last screen: keep_rewards_dir holds {len(files)} "
                            f"pictures after one press, wanted 1")
        elif taken.kept_picture != files[0]:
            problems.append(f"last screen: the result names {taken.kept_picture}, "
                            f"the picture is {files[0]}")
    finally:
        patches.restore()
        for key, value in saved.items():
            cfg.override(key, value)
        last_screen.forget()
        shutil.rmtree(folder, ignore_errors=True)
    print("last reward screen : kept in memory by a press (picture, reading, "
          "time), nothing written, bad and failed readings kept, no window "
          "keeps the old one")


def _check_kept_reward_pictures(app, cfg, problems: list) -> None:
    """Reward pictures: the path scheme, the switch, the status line, the
    tidying of old day folders, the Settings group and the Rewards line.

    One function (`wft.relic.kept_pictures`) names the file for the writer, the
    count and the Rewards line, so these checks drive that function and then the
    two pages that show it. The trash is replaced by a recorder.
    """
    import datetime

    from PIL import Image

    from ui.views.relic_panel import RelicPanel
    from ui.views.settings_view import SettingsView
    from ui.workers import relic_trigger as RT
    from ui.workers.relic_trigger import TriggerResult
    from ui import settings_io
    from wft.relic import kept_pictures as K

    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not str(settings_io.target_config_path()).startswith(scratch):
        problems.append("kept pictures: the config would be written outside the "
                        "test's folder; not checked")
        return
    keys = ("capture.keep_rewards", "capture.keep_rewards_dir",
            "capture.keep_rewards_days")
    saved = {key: cfg.get(key) for key in keys}
    root = pathlib.Path(tempfile.mkdtemp())
    page = panel = None
    try:
        folder = root / "pics"
        image = Image.new("RGB", (64, 36), "white")

        # The scheme, with milliseconds; two in one second stay two files.
        a = datetime.datetime(2026, 10, 21, 14, 32, 19, 123456)
        b = datetime.datetime(2026, 10, 21, 14, 32, 19, 124999)
        if K.picture_path(folder, a) != folder / "2026-10-21" / "14:32:19:123.jpg":
            problems.append(f"kept pictures: path scheme is {K.picture_path(folder, a)}")
        K.save(folder, image, a)
        K.save(folder, image, b)
        if sorted(p.name for p in (folder / "2026-10-21").iterdir()) != \
                ["14:32:19:123.jpg", "14:32:19:124.jpg"]:
            problems.append("kept pictures: two pictures in one second overwrote each other")

        # The switch and the folder, both needed.
        shutil.rmtree(folder)
        cfg.override("capture.keep_rewards_dir", str(folder))
        cfg.override("capture.keep_rewards", False)
        if RT._keep_the_screen(cfg, image, lambda _m: None) is not None or folder.exists():
            problems.append("kept pictures: written with keep_rewards off")
        cfg.override("capture.keep_rewards", True)
        cfg.override("capture.keep_rewards_dir", "")
        if RT._keep_the_screen(cfg, image, lambda _m: None) is not None:
            problems.append("kept pictures: written with an empty folder")
        cfg.override("capture.keep_rewards_dir", str(folder))
        written = RT._keep_the_screen(cfg, image, lambda _m: None)
        if written is None or not written.is_file() or written.parent.parent != folder:
            problems.append(f"kept pictures: switch on wrote {written}")

        # The status line: count, size, period; none; old flat files counted.
        shutil.rmtree(folder)
        if K.describe(K.summarize(folder)) != "No pictures yet":
            problems.append("kept pictures: an empty folder is not 'No pictures yet'")
        for day in ("2026-09-01", "2026-10-19"):
            (folder / day).mkdir(parents=True)
            for n in range(2):
                (folder / day / f"10:00:0{n}:000.jpg").write_bytes(b"x" * 500_000)
        (folder / "reward-20260801-101010.jpg").write_bytes(b"x" * 500_000)
        (folder / "notes.txt").write_text("not a picture")
        line = K.describe(K.summarize(folder))
        if line != "5 pictures, 2 MB, 2026-09-01 to 2026-10-19":
            problems.append(f"kept pictures: status line is {line!r}")

        # Which day folders have expired.
        for name in ("2026-13-99", "misc", "2026-10-1", "2026-09-01x", "20200101",
                     "2020-W01-1"):
            (folder / name).mkdir()
        (folder / "2026-09-02").write_text("a file named like a day")
        today = datetime.date(2026, 10, 21)
        got = sorted(p.name for p in K.expired_folders(folder, today, 30))
        if got != ["2026-09-01"]:
            problems.append(f"kept pictures: expired with 30 days is {got}, wanted "
                            f"['2026-09-01']")
        if K.expired_folders(folder, today, 0):
            problems.append("kept pictures: 0 days must mean never")
        got = sorted(p.name for p in K.expired_folders(folder, today, 2))
        if got != ["2026-09-01"]:
            problems.append(f"kept pictures: expired with 2 days is {got}")
        got = sorted(p.name for p in K.expired_folders(folder, today, 1))
        if got != ["2026-09-01", "2026-10-19"]:
            problems.append(f"kept pictures: expired with 1 day is {got}")

        # Tidying: trash recorder, off switch, never, and a refusing trash.
        trashed = []
        cfg.override("capture.keep_rewards_days", 30)
        cfg.override("capture.keep_rewards", False)
        K.tidy(cfg, lambda p: trashed.append(p.name) or True, today)
        if trashed:
            problems.append("kept pictures: tidied with keep_rewards off")
        cfg.override("capture.keep_rewards", True)
        cfg.override("capture.keep_rewards_days", 0)
        K.tidy(cfg, lambda p: trashed.append(p.name) or True, today)
        if trashed:
            problems.append("kept pictures: tidied with days = 0")
        cfg.override("capture.keep_rewards_days", 30)
        K.tidy(cfg, lambda p: trashed.append(p.name) or True, today)
        if trashed != ["2026-09-01"]:
            problems.append(f"kept pictures: tidy trashed {trashed}")
        messages = []
        K.tidy(cfg, lambda p: False, today, report=messages.append)
        K.tidy(cfg, lambda p: 1 / 0, today, report=messages.append)
        if len(messages) != 2 or not (folder / "2026-09-01").is_dir():
            problems.append(f"kept pictures: a refusing trash was not reported "
                            f"({messages})")

        # The Settings group: three keys, saved at once.
        cfg.override("capture.keep_rewards", False)
        cfg.override("capture.keep_rewards_days", 30)
        page = SettingsView(cfg)
        page.refresh()
        if page.keep_status.text() != "5 pictures, 2 MB, 2026-09-01 to 2026-10-19":
            problems.append(f"kept pictures: Settings line {page.keep_status.text()!r}")
        page.keep_box.setChecked(True)
        page.keep_days_spin.setValue(0)
        if page.keep_days_spin.text() != "never":
            problems.append(f"kept pictures: 0 shows as {page.keep_days_spin.text()!r}")
        page.keep_days_spin.setValue(7)
        original = QFileDialog.getExistingDirectory
        QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(root / "other"))
        try:
            page._choose_keep_dir()
        finally:
            QFileDialog.getExistingDirectory = original
        written = settings_io.target_config_path().read_text()
        for needle in ("keep_rewards = true", "keep_rewards_days = 7",
                       f'keep_rewards_dir = "{root / "other"}"'):
            if needle not in written:
                problems.append(f"kept pictures: Settings did not write {needle!r}")
        if (cfg.get("capture.keep_rewards") is not True
                or cfg.get("capture.keep_rewards_days") != 7
                or cfg.get("capture.keep_rewards_dir") != str(root / "other")):
            problems.append("kept pictures: Settings did not change the running config")
        if page.keep_status.text() != "No pictures yet":
            problems.append(f"kept pictures: after choosing a folder the line is "
                            f"{page.keep_status.text()!r}")

        # The Rewards line: only with a kept picture, relative, opens the folder.
        cfg.override("capture.keep_rewards_dir", str(folder))
        panel = RelicPanel(cfg)
        panel.show()
        result = _trigger_result(["Gauss Prime Blueprint", "b", "c", "d"], 4)
        panel.show_result(result)
        if not panel.kept_label.isHidden():
            problems.append("kept pictures: a line without a kept picture")
        result.kept_picture = K.picture_path(folder, a)
        panel.show_result(result)
        if panel.kept_label.isHidden() or \
                "Picture kept: 2026-10-21/14:32:19:123.jpg" not in panel.kept_label.text():
            problems.append(f"kept pictures: Rewards line is {panel.kept_label.text()!r}")
        opened = []
        original_open = RT_open = None
        from PySide6.QtGui import QDesktopServices
        original_open = QDesktopServices.openUrl
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()) or True)
        try:
            panel._open_kept_folder()
        finally:
            QDesktopServices.openUrl = original_open
        if opened != [str(folder)]:
            problems.append(f"kept pictures: clicking the line opened {opened}")
        panel.set_idle()
        if not panel.kept_label.isHidden():
            problems.append("kept pictures: the line stayed after set_idle")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"kept pictures: raised {type(exc).__name__}: {exc}")
    finally:
        for key, value in saved.items():
            cfg.override(key, value)
        for widget in (page, panel):
            if widget is not None:
                widget.close()
                widget.deleteLater()
        shutil.rmtree(root, ignore_errors=True)
    print("kept pictures     : day folder and millisecond name, off without the "
          "switch, status line, only date-named old folders tidied, Settings "
          "saves 3 keys, Rewards line")


def _check_1080p_reads_the_reward_row_with_the_default_regions(cfg, problems: list) -> None:
    """The shipped regions read a drawn reward screen at 1080p as at 4K.

    The screen is drawn at the layout measured on live screens, not with the
    module's numbers, and read by the path the hotkey takes. The lower bound of
    this drawn screen is about 1280x720 (measured: 4 of 4 down to 1600x900,
    3 of 4 at 1280x720 for these four names); the real screens' own bound is
    measured separately.
    """
    import importlib
    import tomllib

    from ui.workers import relic_trigger as RT
    from wft import config as config_mod
    from wft.data import db as dbq
    from wft.relic import match as M, regions as regions_mod

    # The constants keep whatever an earlier load of the user's file put there;
    # reset to what the source says, which is what a new install has.
    importlib.reload(regions_mod)
    connection = dbq.connect(cfg.db_path)
    parts = [c.name for c in M.all_prime_parts(connection)
             if c.name.endswith(("Prime Stock", "Prime Barrel", "Prime Blueprint"))]
    connection.close()
    if len(parts) < 4:
        return
    names = [parts[0], parts[len(parts) // 3], parts[2 * len(parts) // 3], parts[-1]]
    # The shipped defaults only: the example file, no user file and no override.
    with open(config_mod._EXAMPLE, "rb") as handle:
        shipped = config_mod.Config(tomllib.load(handle), config_mod._EXAMPLE)
    shipped.override("paths.data_dir", str(cfg.db_path.parent))
    drawn = _reward_screen(names)
    for width, height, wanted in ((3840, 2160, 4), (1920, 1080, 4),
                                  (1600, 900, 4), (1280, 720, 3)):
        picture = drawn if width == 3840 else _scaled(drawn, width, height)
        result = RT.run_on_image(shipped, picture, use_log=False)
        got = [r.name for r in result.verdict.rewards] if result.verdict else []
        right = sum(1 for read, name in zip(got, names) if read == name)
        if right < wanted:
            problems.append(f"{width}x{height}: the shipped regions read {right} "
                            f"of {len(names)} drawn names, wanted {wanted} ({got})")
    # Counter-check: the estimate that shipped before the region change points at the row of
    # player names and reads nothing, so this test would have said so.
    old = dict(tile_top=0.302, tile_height=0.209, tile_width=0.127,
               tile_pitch=0.127, row_centre_x=0.474, name_top=0.482,
               name_height=0.038)
    result = RT.run_on_image(shipped, _scaled(drawn, 1920, 1080), use_log=False,
                             regions=old)
    if result.verdict and any(r.name for r in result.verdict.rewards):
        problems.append("1080p: the old regions read a name, so this test "
                        "cannot tell them from the new ones")
    print("1080p              : the shipped regions read a drawn reward screen "
          "at 3840x2160, 1920x1080, 1600x900 and (3 of 4) 1280x720; the old ones read nothing")


def _check_the_calibration_dialog_takes_the_last_screen(app, problems: list) -> None:
    import time

    import cv2

    from ui.views import calibrate_dialog as dialog_mod
    from wft.capture import window as cap
    from wft.relic import last_screen

    patches = _Patches()
    folder = pathlib.Path(tempfile.mkdtemp())
    try:
        cfg = _setup_cfg(data=True, key="F8")
        last_screen.forget()

        # Empty: the surface says where a picture comes from.
        empty = dialog_mod.CalibrateDialog(cfg)
        empty.show()
        _settle(app)
        if not empty.last_btn.isHidden():
            problems.append("calibrate: the last-screen button shows with no screen")
        text = empty.picker.placeholder
        if "F8" not in text or "relic run" not in text or "wrong" not in text:
            problems.append(f"calibrate: the empty picture says {text[:80]!r}")
        shot = empty.picker.grab().toImage()
        colours = {shot.pixel(x, y) for x in range(0, shot.width(), 7)
                   for y in range(0, shot.height(), 5)}
        if len(colours) < 3:
            problems.append("calibrate: the empty picture area is still a dark "
                            "rectangle with no sentence on it")
        empty.picker.placeholder = ""
        empty.picker.update()
        shot = empty.picker.grab().toImage()
        if len({shot.pixel(x, y) for x in range(0, shot.width(), 7)
                for y in range(0, shot.height(), 5)}) != 1:
            problems.append("calibrate: the placeholder check cannot tell a "
                            "sentence from nothing")

        # A screen read after the dialog opened: the tick brings the button.
        screen = _reward_screen(["", "", "", ""], 1920, 1080)
        last_screen.remember(screen, _trigger_result(["x"] * 4, 3),
                             now=time.time() - 300)
        empty._tick.timeout.emit()
        if empty.last_btn.isHidden() or "5 min ago" not in empty.last_btn.text():
            problems.append(f"calibrate: the tick did not offer the screen "
                            f"({empty.last_btn.text()!r})")
        _drop(app, empty)

        # Opened with one: loaded at once, with the hint, and the button says its age.
        opened = dialog_mod.CalibrateDialog(cfg)
        opened.show()
        _settle(app)
        if opened._frame_data is not screen:
            problems.append("calibrate: the last reward screen was not loaded on open")
        if opened.last_btn.text() != "Use the last reward screen (5 min ago)":
            problems.append(f"calibrate: the button says {opened.last_btn.text()!r}")
        hint = opened.source_note.text()
        if "last reward screen" not in hint or "3 of 4" not in hint:
            problems.append(f"calibrate: the hint says {hint!r}")
        if opened.shape_warning:
            problems.append("calibrate: the game's own picture was warned about")
        if opened.picker._source.isNull():
            problems.append("calibrate: nothing is drawn after loading the screen")

        # Load something else, then take the last screen back with the button.
        other = folder / "other.png"
        cv2.imwrite(str(other), _reward_screen(["", "", "", ""], 1280, 720))
        patches.set(dialog_mod.file_dialogs, "open_file", lambda *a, **k: str(other))
        opened._load()
        if opened._frame_data is screen or opened.shape_warning:
            problems.append("calibrate: loading a file did not replace the picture")
        opened.last_btn.click()
        if opened._frame_data is not screen:
            problems.append("calibrate: the button did not bring the last screen back")

        # "Capture the game window" - it raised AttributeError on every press
        # (a PIL image has no .shape) until it used the same array as the rest.
        fake = _reward_screen(["", "", "", ""], 1280, 720)
        _fake_game(patches, fake)
        opened._capture()
        if (opened._frame_data is None or opened._frame_data.shape != (720, 1280, 3)
                or "captured" not in opened.source_note.text()):
            problems.append("calibrate: Capture the game window did not take the picture")
        _drop(app, opened)
        patches.restore()
    except Exception as exc:  # noqa: BLE001 - a crash is a finding
        problems.append(f"calibrate: last-screen check raised {type(exc).__name__}: {exc}")
    finally:
        patches.restore()
        last_screen.forget()
        shutil.rmtree(folder, ignore_errors=True)
    print("calibrate dialog   : the last reward screen is loaded on open, the "
          "button carries its age, an empty surface says where a picture comes "
          "from, Capture works")


def _check_a_picture_that_is_not_the_game_window_is_warned_about(app, problems: list) -> None:
    import cv2
    import numpy

    from ui.views import calibrate_dialog as dialog_mod
    from wft.relic import last_screen, shape

    # The tolerance, as numbers: same shape at two sizes, the near misses a real
    # screenshot has, and what it must catch.
    cases = [
        ((3840, 2160), None, False), ((1920, 1080), None, False),
        ((1366, 768), None, False), ((1360, 768), None, False),
        ((2560, 1600), None, False), ((2560, 1080), None, False),
        ((3840, 1080), None, True),    # two 1080p monitors side by side
        ((1920, 1110), None, True),    # 1080p with a 30px title bar on top
        ((1920, 1200), (1920, 1080), True), ((3840, 2160), (1920, 1080), False),
        ((1366, 768), (3840, 2160), False), ((3840, 1080), (1920, 1080), True),
    ]
    for picture, window, warns in cases:
        if (shape.check(picture, window) is not None) != warns:
            problems.append(f"shape: {picture} against {window} "
                            f"{'did not warn' if warns else 'warned'}")

    patches = _Patches()
    folder = pathlib.Path(tempfile.mkdtemp())
    saved: list = []
    try:
        last_screen.forget()
        cfg = _setup_cfg(data=True, key="F8")
        dialog = dialog_mod.CalibrateDialog(cfg)
        dialog.show()
        _settle(app)
        patches.set(dialog_mod.settings_io, "set_value",
                    lambda section, key, value: saved.append(key))
        patches.set(dialog_mod.cap, "find_game", lambda _c, display=None: None)

        def load(width, height):
            path = folder / f"p{width}x{height}.png"
            cv2.imwrite(str(path), numpy.zeros((height, width, 3), numpy.uint8))
            patches.set(dialog_mod.file_dialogs, "open_file", lambda *a, **k: str(path))
            dialog._load()
            _settle(app)

        for width, height, warns in ((3840, 2160, False), (1920, 1080, False),
                                     (3840, 1080, True)):
            load(width, height)
            shown = dialog.shape_note.isVisible()
            if bool(dialog.shape_warning) != warns or shown != warns:
                problems.append(f"calibrate: {width}x{height} "
                                f"{'did not warn' if warns else 'warned'} "
                                f"({dialog.shape_warning[:60]!r})")
        if "3840x1080" not in dialog.shape_warning \
                or "game window" not in dialog.shape_warning:
            problems.append(f"calibrate: the warning does not explain: "
                            f"{dialog.shape_warning!r}")

        # Saving with the warning asks, and the default answer is no.
        dialog._on_picked(0.25, 0.2, 0.125, 0.22)
        asked: list = []
        patches.set(QMessageBox, "question",
                    staticmethod(lambda *a, **k: asked.append(a[2]) or QMessageBox.Cancel))
        dialog._save()
        if len(asked) != 1 or saved:
            problems.append(f"calibrate: saving a warned picture asked {len(asked)} "
                            f"time(s) and wrote {len(saved)} value(s) after a no")
        patches.set(QMessageBox, "question",
                    staticmethod(lambda *a, **k: QMessageBox.Save))
        dialog._save()
        if not saved:
            problems.append("calibrate: saving after 'Save anyway' wrote nothing")

        # A good picture saves without asking.
        saved.clear()
        load(1920, 1080)
        dialog._on_picked(0.25, 0.2, 0.125, 0.22)
        asked.clear()
        patches.set(QMessageBox, "question",
                    staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Cancel))
        dialog._save()
        if asked or not saved:
            problems.append("calibrate: a picture of the right shape was asked "
                            "about, or not saved")

        # With the game running the picture is held against its window, not
        # against the usual shapes: 21:9 against a 16:9 window warns, 16:9
        # against a 21:9 window does.
        patches.set(dialog_mod.cap, "find_game", lambda _c, display=None: type(
            "G", (), {"width": 1920, "height": 1080})())
        load(2560, 1080)
        if not dialog.shape_warning or "1920x1080" not in dialog.shape_warning:
            problems.append("calibrate: 2560x1080 was not held against the "
                            "running 1920x1080 window")
        _drop(app, dialog)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"calibrate: shape check raised {type(exc).__name__}: {exc}")
    finally:
        patches.restore()
        shutil.rmtree(folder, ignore_errors=True)
    print("calibrate guard    : 3840x2160 and 1920x1080 pass, 3840x1080 and a "
          "title bar warn, saving a warned picture asks first")


def _check_a_bad_reading_offers_to_calibrate_on_its_screen(app, problems: list) -> None:
    import time

    from ui import app as app_mod
    from ui.views import calibrate_dialog as dialog_mod
    from wft.relic import last_screen

    patches = _Patches()
    opened: list = []
    try:
        _machine(patches, "active", 3)
        cfg = _setup_cfg(data=True, key="F8")
        _app, window = app_mod.build_application(cfg, "Rewards")
        window.show()
        _settle(app)
        panel = window.relic_panel
        patches.set(dialog_mod, "open_dialog",
                    lambda cfg_, parent, relic_panel=None: opened.append(
                        (parent, relic_panel)))
        screen = _reward_screen(["", "", "", ""], 1920, 1080)

        def press(result, kept_at_offset=0.0, remember=True):
            """Pretend a hotkey press that produced `result`."""
            panel._triggered_at = time.time() - 2
            if remember:
                last_screen.remember(screen, result, now=time.time() + kept_at_offset)
            panel._on_finished(result)
            _settle(app)

        last_screen.forget()
        press(_trigger_result(["a", "b", "c", "d"], 0))
        if not panel.calibrate_row.isVisible():
            problems.append("rewards: nothing recognised offered no calibration")
        panel.calibrate_btn.click()
        if opened != [(window, panel)]:
            problems.append(f"rewards: the button opened {opened!r}, not the dialog "
                            f"of the window with the panel")

        # Failed (RESCAN): offered as well.
        from ui.workers.relic_trigger import TriggerResult

        press(TriggerResult(None, "Game window not found", 0))
        if not panel.calibrate_row.isVisible():
            problems.append("rewards: a failed reading offered no calibration")

        # Counter-checks: a partly or fully recognised screen offers nothing, and
        # neither does a press whose screen was not kept (the old bad one is not
        # the screen the page complains about).
        press(_trigger_result(["a", "b", "c", "d"], 2))
        if panel.calibrate_row.isVisible():
            problems.append("rewards: 2 of 4 recognised offered calibration "
                            "(that is not a bad reading)")
        press(_trigger_result(["a", "b", "c", "d"], 4))
        if panel.calibrate_row.isVisible():
            problems.append("rewards: a good reading offered calibration")
        last_screen.remember(screen, _trigger_result(["a"] * 4, 0), now=time.time() - 60)
        panel._triggered_at = time.time()
        panel._on_failed("Game window not found")
        _settle(app)
        if panel.calibrate_row.isVisible():
            problems.append("rewards: an old bad screen was offered for a press "
                            "that kept none")
        panel.set_idle()
        if panel.calibrate_row.isVisible():
            problems.append("rewards: the offer stayed on the waiting state")

        # Setup carries the same button, and the window opens the same dialog.
        setup = window._pages["Setup"]
        opened.clear()
        last_screen.remember(screen, _trigger_result(["a"] * 4, 0))
        setup.refresh()
        buttons = [b for b in setup.cards["reward"].findChildren(QPushButton)
                   if b.text() == "Calibrate with this screen"]
        if len(buttons) != 1:
            problems.append("setup: no Calibrate with this screen button on a "
                            "bad reading")
        else:
            buttons[0].click()
            if opened != [(window, panel)]:
                problems.append("setup: the button did not open the calibration")
        window.close()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"rewards: calibrate offer raised {type(exc).__name__}: {exc}")
    finally:
        patches.restore()
        last_screen.forget()
    print("calibrate offer    : a bad or failed reading offers 'Calibrate with "
          "this screen' on Rewards and Setup; partly read, good, old or idle "
          "do not")


def _check_the_setup_card_for_the_first_reward_screen(app, problems: list) -> None:
    import time

    from ui.views.setup_view import SetupView
    from wft.relic import last_screen

    patches = _Patches()
    try:
        _machine(patches, "active", 3)
        cfg = _setup_cfg(data=True, key="F8")
        last_screen.forget()
        page = SetupView(cfg)
        page.refresh()
        card = page.cards["reward"]
        buttons = lambda: [b for b in card.findChildren(QPushButton)  # noqa: E731
                           if b.text() == "Calibrate with this screen"]

        if card.kind != "note" or card.required:
            problems.append(f"setup reward screen: none read is {card.kind!r}, "
                            f"required={card.required}")
        text = card.text.text()
        if "Do a relic run and press F8 on the reward screen" not in text \
                or "calibrate only if the names come out wrong" not in text:
            problems.append(f"setup reward screen: none read says {text!r}")
        if buttons():
            problems.append("setup reward screen: a calibrate button with no screen")

        screen = _reward_screen(["", "", "", ""], 1920, 1080)
        last_screen.remember(screen, _trigger_result(["a", "b", "c", "d"], 3),
                             now=time.time() - 240)
        page.refresh()
        if card.kind != "done" or "3 of 4 names recognised" not in card.text.text() \
                or "4 min ago" not in card.text.text() or buttons():
            problems.append(f"setup reward screen: read says {card.kind!r} "
                            f"{card.text.text()!r} with {len(buttons())} button(s)")

        last_screen.remember(screen, _trigger_result(["a", "b", "c", "d"], 0))
        page.refresh()
        if card.kind != "todo" or len(buttons()) != 1 \
                or "0 of 4 names recognised" not in card.text.text() \
                or "Calibrate" not in " ".join(
                    w.text() for w in card.findChildren(QPushButton)):
            problems.append(f"setup reward screen: bad read says {card.kind!r} "
                            f"{card.text.text()!r}, {len(buttons())} button(s)")
        # The 2 s tick must not rebuild the button under the mouse.
        first = buttons()[0] if buttons() else None
        page.refresh()
        if first is not None and (not buttons() or buttons()[0] is not first):
            problems.append("setup reward screen: the refresh rebuilt the button")
        fired: list = []
        page.calibrate_requested.connect(lambda: fired.append(1))
        if first is not None:
            first.click()
        if fired != [1]:
            problems.append("setup reward screen: the button did not ask for the dialog")

        from ui.workers.relic_trigger import TriggerResult

        last_screen.remember(screen, TriggerResult(None, "x", 4))
        page.refresh()
        if "could not be read" not in card.text.text() or card.kind != "todo":
            problems.append("setup reward screen: a failed read is not said")

        last_screen.remember(screen, _trigger_result(["a", "b", "c", "d"], 4))
        page.refresh()
        if card.kind != "done" or buttons():
            problems.append("setup reward screen: a good read after a bad one "
                            "left the button")
        page.deleteLater()
    finally:
        patches.restore()
        last_screen.forget()
    print("setup card         : First reward screen in its three states, a "
          "failed read, the button only on a bad one")


# -- The inventory card and the Import page without a hotkey capture --

def _check_versions_and_the_whats_new_window(app, problems: list) -> None:
    """Version numbers come from wft/whatsnew.toml; an update shows the window once.

    The file's order and numeric comparison, the five ways the program
    starts (new install, update without a marker, older marker, current marker,
    self-test), OK, the Settings footer and its button, the doctor line and the
    release check against PATCHNOTES.md.
    """
    import io
    import subprocess
    from contextlib import redirect_stdout

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QWidget

    from ui import geometry
    from ui.views import whatsnew_dialog as wd
    from wft import doctor as doctor_mod, version as version_mod, whatsnew

    root = pathlib.Path(__file__).resolve().parent.parent
    before = len(problems)

    def expect(condition, text):
        if not condition:
            problems.append(f"versions: {text}")

    # -- the file ------------------------------------------------------------
    every = whatsnew.releases()
    expect(len(every) >= 3, f"fewer than 3 releases ({len(every)})")
    keys = [whatsnew.key(r.version) for r in every]
    expect(keys == sorted(keys, reverse=True), "the file is not newest first")
    expect(version_mod.number() == every[0].version,
           f"the running version {version_mod.number()} is not the newest entry")
    expect(all(1 <= len(r.lines) <= 6 for r in every), "an entry has 0 or more than 6 lines")
    expect(all(len(r.date) == 10 for r in every[1:]), "a shipped release has no date")
    by = {r.version: r for r in every}
    expect(by.get("1.0") and by["1.0"].date == "2026-10-14"
           and by["1.1"].date == "2026-10-18", "1.0 and 1.1 are not dated 14 and 18 Oct")
    expect(whatsnew.is_newer("1.10", "1.9") and not whatsnew.is_newer("1.9", "1.10")
           and not whatsnew.is_newer("1.2", "1.2"), "1.10 is not newer than 1.9")
    scratch = pathlib.Path(tempfile.mkdtemp())
    sample = scratch / "w.toml"
    sample.write_text('[[release]]\nversion="1.9"\ndate="2026-01-01"\nlines=["a"]\n'
                      '[[release]]\nversion="1.10"\ndate=""\nlines=["b"]\n'
                      '[[release]]\nversion="1.2"\ndate="2025-01-01"\nlines=["c"]\n')
    expect([r.version for r in whatsnew.load(sample)] == ["1.10", "1.9", "1.2"],
           "1.10 does not sort above 1.9 in a file")
    expect([r.version for r in whatsnew.unseen("1.9", sample)] == ["1.10"]
           and len(whatsnew.unseen("", sample)) == 3, "unseen() is wrong")
    attributes = (root / ".gitattributes").read_text()
    script = (root / "packaging" / "make-release.sh").read_text()
    expect("whatsnew" not in attributes and "whatsnew" not in script.split("EXCLUDED=(")[1].split(")")[0],
           "the archive would leave out whatsnew.toml or whatsnew.py")

    # -- when the window shows ---------------------------------------------
    where = geometry.settings_file()
    scratch_config = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch_config or not where.startswith(scratch_config):
        problems.append(f"versions: the settings file is {where}, outside the test's "
                        f"config folder; not checked")
        return

    def forget_settings():
        QSettings(geometry.ORGANISATION, geometry.APPLICATION).sync()
        if os.path.exists(where):
            os.remove(where)

    def set_seen(value):
        forget_settings()
        if value:
            geometry.set_whatsnew_seen(value)

    current = whatsnew.newest().version
    host = QWidget()
    opened = []

    def run(seen, database):
        set_seen(seen)
        cfg = _setup_cfg(data=database, key="F8")
        fresh = wd.is_fresh_install(cfg)
        dialog = wd.start(host, fresh)
        if dialog is not None:
            opened.append(dialog)
            _settle(app)     # the window is shown from a zero timer
        return fresh, dialog

    fresh, dialog = run("", False)
    expect(fresh and dialog is None, "a new install showed the window or was not seen as new")
    expect(geometry.whatsnew_seen() == current, "a new install did not mark the version as seen")

    fresh, dialog = run("", True)
    expect(not fresh and dialog is not None, "an update with a database but no marker did not show")
    if dialog is not None:
        expect(dialog.new_versions == {r.version for r in every}
               and set(dialog.badges) == dialog.new_versions,
               "an update without a marker did not mark every version new")
        expect(not dialog.isModal(), "the window is modal")
        expect(dialog.isVisible(), "the window was not shown after the start")
        expect(geometry.whatsnew_seen() == "", "showing the window already marked it seen")
        # The newest entry has no date until its archive is built, then it has
        # one: the heading must hold in both states, or every release fails.
        # Every release, newest first, with its date once it has one: the
        # newest is undated until its archive is built. Compared as a whole
        # list so a new version in the file does not shift what is expected.
        wanted = [f"{r.version} — {r.date}" if r.date else r.version for r in every]
        expect([h.text() for h in dialog.headings] == wanted
               and wanted[0].split(" ")[0] == current
               and f"1.1 — {by['1.1'].date}" in wanted,
               f"headings read {[h.text() for h in dialog.headings]}")
        dialog.ok.click()
        _settle(app)
        expect(geometry.whatsnew_seen() == current and not dialog.isVisible(),
               "OK did not close the window and mark the version seen")

    # The settings file alone (no database yet) is an update too.
    set_seen("")
    geometry.set_whatsnew_seen("")
    QSettings(geometry.ORGANISATION, geometry.APPLICATION).setValue("main/x", 1)
    QSettings(geometry.ORGANISATION, geometry.APPLICATION).sync()
    cfg = _setup_cfg(data=False, key="F8")
    expect(not wd.is_fresh_install(cfg), "a settings file without a database counted as a new install")
    forget_settings()

    older = "1.1" if current != "1.1" else "1.0"
    fresh, dialog = run(older, True)
    expect(dialog is not None and dialog.new_versions
           == {r.version for r in whatsnew.unseen(older)} and current in dialog.new_versions
           and older not in dialog.new_versions,
           "a marker below the running version did not mark just the newer ones")
    if dialog is not None:
        dialog.close()
        _settle(app)

    fresh, dialog = run(current, True)
    expect(dialog is None, "a marker equal to the running version showed the window")
    fresh, dialog = run("99.0", True)
    expect(dialog is None, "a marker above the running version showed the window")
    expect(wd.decide(False, "1.9", "1.10") == "show" and wd.decide(False, "1.10", "1.9") == "none",
           "decide() compares 1.9 and 1.10 as text")

    # The self-test starts the real program: it shows nothing and marks nothing.
    set_seen("")
    home = tempfile.mkdtemp(prefix="wft-home-")
    env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, "c"),
               XDG_DATA_HOME=os.path.join(home, "d"), QT_QPA_PLATFORM="offscreen")
    done = subprocess.run([sys.executable, str(root / "bin" / "wf-gui"), "--self-test"],
                          env=env, capture_output=True, text=True, timeout=240)
    mine = pathlib.Path(env["XDG_CONFIG_HOME"]) / geometry.ORGANISATION / (geometry.APPLICATION + ".conf")
    marked = mine.exists() and "whatsnew" in mine.read_text()
    expect("SELF-TEST OK" in done.stdout and not marked,
           "the self-test failed or touched the What's new marker "
           f"({done.stdout.strip()[-80:]!r})")
    shutil.rmtree(home, ignore_errors=True)

    # -- Settings footer and button ---------------------------------------------
    set_seen(current)
    cfg = _setup_cfg(data=True, key="F8")
    window = _app_window(cfg)
    _settle(app)
    page = window._pages["Settings"]
    text = page.version_footer.fullText()
    expect(f"Version {version_mod.number()}" in text and version_mod.describe() in text,
           f"the Settings footer reads {text!r}")
    page.whatsnew_button.click()
    _settle(app)
    shown = getattr(window, "_whatsnew_dialog", None) or getattr(page, "_whatsnew_dialog", None)
    expect(shown is not None and shown.isVisible() and not shown.badges,
           "the Settings button did not open the window (or marked seen versions new)")
    if shown is not None:
        shown.close()
    window.remember_geometry = False
    window.close()
    window.deleteLater()
    _settle(app)

    # -- the doctor line ---------------------------------------------------------
    previous_run = doctor_mod.run
    doctor_mod.run = lambda: [("OK", "x", "y")]
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            doctor_mod.main()
    finally:
        doctor_mod.run = previous_run
    first = out.getvalue().splitlines()[0]
    expect(f"version {version_mod.number()} (" in first, f"the doctor's first line reads {first!r}")

    # -- the release check ---------------------------------------------------
    def release(top_version, top_date, heading):
        toml = scratch / "r.toml"
        toml.write_text(f'[[release]]\nversion="{top_version}"\ndate="{top_date}"\n'
                        f'lines=["a"]\n[[release]]\nversion="1.0"\ndate="2026-10-14"\nlines=["b"]\n')
        notes = scratch / "r.md"
        notes.write_text(f"# Notes\n\nintro\n\n{heading}\n\ntext\n\n## 1.0 — 2026-10-14\n")
        return whatsnew.check_release(toml, notes)

    expect(release("1.2", "2026-10-21", "## 1.2 — 2026-10-21") == [], "a matching pair was refused")
    got = release("1.2", "2026-10-21", "## 1.3 — 2026-10-21")
    expect(len(got) == 1 and "version differs" in got[0], f"a different number gave {got}")
    got = release("1.2", "", "## 1.2 — 2026-10-21")
    expect(any("no date" in g for g in got), f"a missing date gave {got}")
    got = release("1.2", "2026-10-21", "## 1.2 — 2026-10-22")
    expect(len(got) == 1 and "date differs" in got[0], f"a different date gave {got}")
    got = release("1.2", "2026-10-21", "## 1.2")
    expect(any("no date" in g for g in got), f"a heading without a date gave {got}")
    shell = subprocess.run(["python3", "-m", "wft.whatsnew", str(scratch / "r.toml"),
                            str(scratch / "r.md")], cwd=root, capture_output=True, text=True)
    expect(shell.returncode == 1, "the command line check did not fail on a bad pair")
    expect("python3 -m wft.whatsnew" in script and "$number, $hash, $date" in script,
           "make-release.sh does not run the check or put the number in the stamps")

    for d in opened:
        d.close()
    host.deleteLater()
    forget_settings()
    shutil.rmtree(scratch, ignore_errors=True)
    if len(problems) == before:
        print(f"versions           : {len(every)} releases newest first, 1.10 above 1.9, "
              f"new install silent, update shown with the right ones new, OK marks, "
              f"self-test silent, footer and doctor name {version_mod.number()}, "
              f"release check refuses 4 mismatches")


def _app_window(cfg):
    """The whole application over `cfg`, opened on Import, as the program builds it.

    One catalogue entry is put in first: with none, the page refuses to scan
    ("No catalogue yet"), which is right and not what these checks are about.
    """
    from ui import app as app_mod
    from wft.data import db as dbq

    connection = dbq.connect(cfg.db_path)
    connection.execute("INSERT OR IGNORE INTO items(unique_name, name, category) "
                       "VALUES('/t/item', 'Test Item', 'weapon')")
    connection.commit()
    connection.close()

    _app, window = app_mod.build_application(cfg, page="Import")
    window.resize(1500, 950)
    window.show()
    return window


def _check_the_window_remembers_its_size(app, problems: list) -> None:
    """The main window stores its size after a resize, a new window opens at it,
    and a window with `remember_geometry` off (the self-test's) stores nothing.

    Until this the size was read back on every start but never stored, so every
    start was 1400x900 although the guide says the size is kept.
    """
    from PySide6.QtCore import QSettings

    from ui import app as app_mod
    from ui import geometry

    def stored():
        return QSettings(geometry.ORGANISATION,
                         geometry.APPLICATION).value("main/geometry")

    def forget():
        settings = QSettings(geometry.ORGANISATION, geometry.APPLICATION)
        settings.remove("main")
        settings.sync()

    # Never against the user's own file: the check writes.
    where = QSettings(geometry.ORGANISATION, geometry.APPLICATION).fileName()
    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not where.startswith(scratch):
        problems.append(f"window size: the settings file is {where}, outside the "
                        f"test's config folder; not checked")
        return

    patches = _Patches()
    windows = []
    try:
        forget()
        cfg = _setup_cfg(data=True, key="F8")
        window = _app_window(cfg)
        windows.append(window)
        _settle(app)
        # Asked small: the window stops at its minimum size, far from the
        # 1400x900 a window opens at when nothing is stored. Only the width is
        # compared: the minimum height is taller than the offscreen screen, and
        # a restore clips a window to its screen (measured: 891 stored, 774 back).
        window.resize(640, 480)
        _settle(app)
        width = window.width()
        if not _wait(app, lambda: stored() is not None, 3.0):
            problems.append("window size: a resize of the shown window was not stored")
        window.close()
        _settle(app)

        _app, again = app_mod.build_application(cfg, page="Import")
        windows.append(again)
        if again.width() != width or width == 1400:
            problems.append(f"window size: a new window opened {again.width()} wide, "
                            f"not at the stored {width}")

        forget()
        again.remember_geometry = False
        again.show()
        again.resize(1111, 666)
        _wait(app, lambda: stored() is not None, 1.5)
        again.close()
        _settle(app)
        if stored() is not None:
            problems.append("window size: a window with remember_geometry off "
                            "stored its size (the self-test would overwrite the "
                            "user's)")
    finally:
        patches.restore()
        for window in windows:
            window.remember_geometry = False
            window.close()
            window.deleteLater()
        forget()
        _settle(app)
    print("window size        : a resize is stored, a new window opens at it, "
          "the self-test's window stores nothing")


def _check_the_hotkey_reads_rewards_on_every_page_and_imports_come_from_files(
        app, problems: list) -> None:
    """The Import page has no capture button, a hotkey press reads a reward screen
    there too, and nothing creates a folder of captures.

    The listener reaches the window through two slots, `show_rewards` and
    `relic_panel.trigger`, connected in `ui.app.main`; the press is played by
    calling them in that order, as the signal would.
    """
    import inspect

    from ui import app as app_mod
    from ui import file_dialogs

    wiring = inspect.getsource(app_mod.main)
    if "triggered.connect(window.show_rewards)" not in wiring \
            or "triggered.connect(window.relic_panel.trigger)" not in wiring:
        problems.append("hotkey: the listener is not connected to show_rewards "
                        "and relic_panel.trigger")

    patches = _Patches()
    window = None
    try:
        _machine(patches, "active", 4)
        cfg = _setup_cfg(data=True, key="F8")
        window = _app_window(cfg)
        view = window._pages["Import"]
        panel = window.relic_panel
        _settle(app)

        texts = [b.text() for b in view.findChildren(QPushButton)]
        left = {type(o).__name__: [n for n in dir(o) if "capture" in n.lower()]
                for o in (view, window, panel)}
        if any(left.values()) \
                or any("capture" in t.lower() for t in texts):
            problems.append(f"import: something of the hotkey capture is left "
                            f"(attributes {left}, buttons {texts})")

        triggers: list = []
        patches.set(panel, "trigger", lambda: triggers.append(1))
        window.select("Import")
        window.show_rewards()
        panel.trigger()
        if triggers != [1] or window.stack.currentWidget() is not panel:
            problems.append("hotkey: a press on the Import page did not read a "
                            "reward screen")

        asked: list = []

        def choose(parent, caption, *args):
            asked.append(args)
            return ""

        patches.set(file_dialogs, "open_directory", choose)
        window.select("Import")
        for button in view.findChildren(QPushButton):
            if button.text() == "Choose folder...":
                button.click()
        if asked != [()]:
            problems.append(f"import: \"Choose folder...\" opens with a start "
                            f"folder {asked}")

        if (pathlib.Path(cfg.data_dir_path()) / "captures").exists():
            problems.append("import: a captures folder exists under the data folder")
    finally:
        patches.restore()
        if window is not None:
            window.close()
            window.deleteLater()
        _settle(app)
    print("import page        : no capture button, a hotkey press reads a "
          "reward screen on the Import page, no captures folder")


def _no_real_notify_send() -> None:
    """No test shows a desktop notification: the sender is a stub for the whole run.

    `NotifySend` itself may only be built over a fake program (the one check of
    it does); built over the real `notify-send` it stops the suite.
    """
    from ui import notify

    notify.set_sender(lambda _summary, _body="": None)
    original = notify.NotifySend.__init__

    def guarded(self, program="notify-send", parent=None):
        if program == "notify-send":
            raise AssertionError("a test built the real notify-send sender")
        original(self, program, parent)

    notify.NotifySend.__init__ = guarded


def _check_the_reset_buttons_delete_what_they_say(app, problems: list) -> None:
    """"Delete my inventory" empties the holdings and nothing else, with a backup
    first; "Delete everything" takes the tracker's own files and only those, and
    starts the program again. Cancel deletes nothing, and Cancel is the
    default, so Enter does not delete.
    """
    import json

    from ui import geometry, reset as reset_mod, settings_io, theme
    from ui.views.settings_view import SettingsView
    from wft.data import backup, db as dbq

    patches = _Patches()
    patches.env("XDG_CACHE_HOME", tempfile.mkdtemp(prefix="wft-cache-"))
    page = None
    try:
        cfg = _setup_cfg(data=True, key="F8")
        data = cfg.data_dir_path()
        scratch = (os.environ["XDG_CONFIG_HOME"], os.environ["XDG_CACHE_HOME"],
                   str(data))
        # Never against the user's own files: this check deletes.
        outside = [str(path) for path in reset_mod.tracker_files(cfg)
                   + [pathlib.Path(geometry.settings_file()),
                      settings_io.target_config_path(),
                      pathlib.Path(theme.cache_dir())]
                   if not str(path).startswith(scratch)]
        if outside:
            problems.append(f"reset: paths outside the test's folders, not "
                            f"checked: {outside}")
            return

        connection = dbq.connect(cfg.db_path)
        connection.executescript("""
            INSERT INTO parts(unique_name, name) VALUES('/t/p', 'Test Part');
            INSERT INTO relics(unique_name, name) VALUES('/t/r', 'Test Relic');
            INSERT INTO owned_parts(part_unique, count) VALUES('/t/p', 3);
            INSERT INTO owned_items(item_unique, count, mastered)
                VALUES('/t/item', 1, 1);
            INSERT INTO owned_relics(relic_unique, count) VALUES('/t/r', 2);
            INSERT INTO imports(id, source) VALUES(1, 'test.mkv');
            INSERT INTO observations(import_id, kind, unique_name, after)
                VALUES(1, 'part', '/t/p', 3);
            INSERT INTO learned_ignores(reading) VALUES('SELEC A INVEN');
            INSERT INTO thumbnails(unique_name, kind, image) VALUES('/t/p', 'part', x'00');
            INSERT INTO riven_prices(week, item_type, rerolled)
                VALUES('2026-10-12', 'Rifle Riven Mod', 0);
            INSERT INTO market_prices(slug) VALUES('test_part');
            INSERT OR REPLACE INTO meta(key, value) VALUES('scan_revision', '7');
            INSERT OR REPLACE INTO meta(key, value) VALUES('tile_grid', '0.2,0.1');
        """)
        connection.commit()
        connection.close()
        (data / "manifests").mkdir(exist_ok=True)
        (data / "manifests" / "ExportWeapons_en.json").write_text("{}")
        (data / "droptables.html").write_text("<html></html>")
        (data / "trading").mkdir(exist_ok=True)
        (data / "trading" / "i-want.html").write_text("<html></html>")
        (data / "other-tool.json").write_text("{}")
        settings_io.set_value("hotkey", "key", "F8")
        glyphs = pathlib.Path(theme.cache_dir()) / "glyphs"
        glyphs.mkdir(parents=True)
        (glyphs / "check.png").write_bytes(b"")
        stored = geometry._settings()
        stored.setValue("main/geometry", b"x")
        stored.sync()

        trash = pathlib.Path(tempfile.mkdtemp(prefix="wft-trash-"))

        def to_trash(path):
            shutil.move(str(path), str(trash / path.name))
            return True

        patches.set(reset_mod, "to_trash", to_trash)
        said = []
        patches.set(QMessageBox, "information", lambda *a, **_k: said.append(a[2]))
        patches.set(QMessageBox, "warning", lambda *a, **_k: said.append(a[2]))
        page = SettingsView(cfg)

        def rows():
            connection = dbq.connect(cfg.db_path)
            try:
                return {table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in dbq.HOLDINGS_TABLES + (
                        "thumbnails", "riven_prices", "market_prices", "parts")}
            finally:
                connection.close()

        # The dialog: Cancel is the default and the escape, and leaves it all.
        defaults = []

        def run(box):
            defaults.append(box.defaultButton() is box.escapeButton()
                            and box.standardButton(box.defaultButton())
                            == QMessageBox.Cancel)
            return 0

        patches.set(QMessageBox, "exec", run)
        before = rows()
        page._delete_inventory()
        page._restart = lambda: problems.append("reset: a cancelled reset restarted")
        page._delete_everything()
        if defaults != [True, True]:
            problems.append(f"reset: Cancel is not the default of both dialogs "
                            f"({defaults})")
        if rows() != before or list(trash.iterdir()):
            problems.append("reset: a cancelled dialog deleted something")

        restarted = []
        page._restart = lambda: restarted.append(True)
        page._confirm = lambda *_a: True
        page._delete_inventory()
        if restarted != [True]:
            problems.append(f"reset: Delete my inventory restarted the program "
                            f"{len(restarted)} time(s), not once (a page without "
                            f"refresh keeps the deleted rows on screen)")
        after = rows()
        left = {table: after[table] for table in dbq.HOLDINGS_TABLES if after[table]}
        if left:
            problems.append(f"reset: Delete my inventory left rows behind: {left}")
        kept = {table: after[table] for table in
                ("thumbnails", "riven_prices", "market_prices", "parts")}
        if kept != {table: before[table] for table in kept}:
            problems.append(f"reset: Delete my inventory took more than the "
                            f"holdings: {before} -> {after}")
        connection = dbq.connect(cfg.db_path)
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        connection.close()
        if "scan_revision" in meta or "tile_grid" not in meta:
            problems.append(f"reset: meta after Delete my inventory: {sorted(meta)}")
        saved = sorted(trash.glob("inventory-before-delete-*.json"))
        counts = (json.loads(saved[0].read_text())["counts"] if saved else {})
        if counts != {"parts": 1, "items": 1, "relics": 1,
                          "dismissed": 1, "kept": 0}:
            problems.append(f"reset: the backup before deleting holds {counts}")
        elif backup.read(str(saved[0]))["counts"] != counts:
            problems.append("reset: the backup does not read back")
        if not said or "backup is in the trash" not in said[-1]:
            problems.append(f"reset: Delete my inventory said {said[-1:]}")

        # An update writing the files keeps them; afterwards they all go.
        restarted.clear()
        running = type("Running", (), {"running": True})()
        real_job, page.update_job = page.update_job, running
        page._delete_everything()
        page.update_job = real_job
        if restarted or not cfg.db_path.exists():
            problems.append("reset: Delete everything ran during an update")

        page._delete_everything()
        gone = sorted(path.name for path in trash.iterdir())
        expected = sorted(["config.toml", "warframe-tracker.conf",
                           "warframe.sqlite", "manifests", "droptables.html",
                           "trading", "warframe-tracker", saved[0].name if saved else ""])
        if gone != expected:
            problems.append(f"reset: Delete everything moved {gone}, "
                            f"expected {expected}")
        if not (data / "other-tool.json").exists():
            problems.append("reset: another tool's file in the data folder was "
                            "deleted")
        if restarted != [True]:
            problems.append(f"reset: the program was restarted {len(restarted)} "
                            f"time(s), not once")

        # Where the trash refuses, the file is deleted all the same.
        refused = pathlib.Path(tempfile.mkdtemp()) / "refused.txt"
        refused.write_text("x")
        if reset_mod.discard(refused, trash=lambda _p: False) != "deleted" \
                or refused.exists():
            problems.append("reset: a file the trash refused was kept")
    finally:
        patches.restore()
        if page is not None:
            page.deleteLater()
        _settle(app)
    print("reset buttons      : the inventory goes with a backup, everything goes "
          "to the trash, other files stay, both restart, Cancel is the default")


def _check_a_reading_sends_one_notification(app, problems: list) -> None:
    """One reading, one desktop notification, with the banner's words.

    The sender is replaced; the replace-id and the failure paths of the real
    sender are driven over a fake `notify-send` script that only logs.
    """
    import os
    import stat
    import time

    from ui import notify
    from ui import settings_io
    from ui.views.relic_panel import RelicPanel
    from ui.workers.relic_trigger import TriggerResult
    from wft.relic import last_screen, score as S

    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not str(settings_io.target_config_path()).startswith(scratch):
        problems.append("notification: the config to start from would be outside "
                        "the test's folder; not checked")
        return
    sent: list = []
    notify.set_sender(lambda summary, body="": sent.append((summary, body)))
    patches = _Patches()
    window = None
    try:
        _machine(patches, "active", 3)
        # The shipped default is on. The walk through every control writes the
        # notification switch to this folder too, on or off by how many boxes
        # it pressed before, so the file starts empty.
        settings_io.target_config_path().unlink(missing_ok=True)
        cfg = _setup_cfg(data=True, key="F8")
        window = _app_window(cfg)
        panel = window.relic_panel
        last_screen.forget()

        def read(result):
            sent.clear()
            panel._triggered_at = time.time()
            panel._on_finished(result)
            _settle(app)
            return list(sent)

        good = _trigger_result(["Gauss Prime Blueprint", "b", "c", "d"], 4)
        got = read(good)
        banner = panel.verdict_banner.text()
        if len(got) != 1:
            problems.append(f"notify: a reading sent {len(got)} messages, not 1")
        elif got[0][0] != banner or not banner.startswith("TAKE "):
            problems.append(f"notify: summary {got[0][0]!r} is not the banner {banner!r}")
        elif not got[0][1].strip():
            problems.append("notify: the recommendation came with an empty body")

        # Nothing worth taking: the banner's other text, once.
        junk = _trigger_result(["Forma Blueprint"] * 4, 0)
        got = read(junk)
        if len(got) != 1 or got[0][0] != panel.verdict_banner.text():
            problems.append(f"notify: nothing-to-take sent {got!r}, banner "
                            f"{panel.verdict_banner.text()!r}")

        # Bad readings: one error message each, and no banner words.
        for label, bad in (("nothing recognised", _trigger_result(["a"] * 4, 0)),
                           ("error", TriggerResult(None, "Game window not found", 0))):
            got = read(bad)
            if got != [("Could not read the reward screen",
                        "Open Warframe Tracker to see what happened.")]:
                problems.append(f"notify: {label} sent {got!r}")
        sent.clear()
        panel._on_failed("capture crashed")
        _settle(app)
        if [m[0] for m in sent] != ["Could not read the reward screen"]:
            problems.append(f"notify: a crashed reading sent {sent!r}")

        # Things that are not a reading send nothing.
        sent.clear()
        read(good)
        sent.clear()
        panel._confirm_pick(0)
        panel.refresh()
        panel.set_idle()
        panel.show_result(good)
        window.select("Plan")
        window.select("Rewards")
        _settle(app)
        if sent:
            problems.append(f"notify: a click, a redraw or a page change sent {sent!r}")

        # The switch: off sends nothing, on again sends again, and a click on
        # the Settings box is saved and applies at once.
        box = window._pages["Settings"].notify_box
        if not box.isChecked():
            problems.append("notify: the Settings box is not on by default")
        box.setChecked(False)
        if cfg.get("notifications.enabled") is not False:
            problems.append("notify: the box did not turn the setting off in memory")
        if "enabled = false" not in settings_io.target_config_path().read_text():
            problems.append("notify: the box did not write the setting")
        if read(good) or read(TriggerResult(None, "x", 0)):
            problems.append("notify: a reading sent a message with the switch off")
        box.setChecked(True)
        if len(read(good)) != 1:
            problems.append("notify: no message after the switch went on again")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"notify: raised {type(exc).__name__}: {exc}")
    finally:
        notify.set_sender(lambda _summary, _body="": None)
        patches.restore()
        last_screen.forget()
        if window is not None:
            window.close()
            window.deleteLater()
        _settle(app)

    # The real sender over a fake program: arguments, replace-id, failure.
    work = pathlib.Path(tempfile.mkdtemp(prefix="wft-notify-"))
    log = work / "calls.log"
    script = work / "fake-notify-send"
    script.write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                      f'echo $(( $(wc -l < {log}) + 40 ))\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    def wait(sender):
        deadline = time.time() + 10
        while sender._busy and time.time() < deadline:
            app.processEvents()

    sender = notify.NotifySend(str(script))
    sender.send("first", "one")
    wait(sender)
    sender.send("second", "two")
    wait(sender)
    lines = log.read_text().splitlines() if log.exists() else []
    if len(lines) != 2:
        problems.append(f"notify: the fake program was run {len(lines)} times, not 2")
    else:
        for needle in ("--app-name=Warframe Tracker", "--expire-time=15000", "--print-id"):
            if needle not in lines[0]:
                problems.append(f"notify: {needle} missing from {lines[0]!r}")
        if "--replace-id" in lines[0]:
            problems.append("notify: the first message asked to replace one")
        if "--replace-id=41" not in lines[1]:
            problems.append(f"notify: the second message did not replace the first: {lines[1]!r}")

    import io
    from contextlib import redirect_stderr

    errors = io.StringIO()
    with redirect_stderr(errors):
        for program in ("this-program-does-not-exist", "false"):
            broken = notify.NotifySend(program)
            for _ in range(3):
                broken.send("x", "y")
                wait(broken)
    if errors.getvalue().count("desktop notification not sent") != 2:
        problems.append(f"notify: failures printed {errors.getvalue()!r}, expected "
                        f"1 line per sender")
    print("notification       : one message per reading with the banner's words, "
          "the error text for a bad one, none for clicks, redraws or with the "
          "switch off; the next replaces the last; a failing sender is quiet")


def _check_the_setup_card_for_the_inventory(app, problems: list) -> None:
    """"Your inventory" in its two states, from the same answer as the Import page."""
    from ui.views.import_view import ImportView
    from ui.views.setup_view import SetupView
    from wft import readiness
    from wft.data import db as dbq

    patches = _Patches()
    try:
        _machine(patches, "active", 3)
        cfg = _setup_cfg(data=True, key="F8")
        page = SetupView(cfg)
        page.refresh()
        card = page.cards["inventory"]
        buttons = lambda: [b for b in card.findChildren(QPushButton)]  # noqa: E731
        asked: list = []
        page.import_requested.connect(lambda: asked.append(1))

        if card.kind != "note" or card.required \
                or "Import your inventory" not in card.text.text() \
                or [b.text() for b in buttons()] != ["Import your inventory"]:
            problems.append(f"setup inventory: empty is {card.kind!r} "
                            f"{card.text.text()!r} {[b.text() for b in buttons()]}")
        buttons()[0].click()
        if asked != [1]:
            problems.append("setup inventory: the button did not open Import")
        first = buttons()[0]
        page.refresh()
        if buttons()[0] is not first:
            problems.append("setup inventory: the refresh rebuilt the button")
        view = ImportView(cfg)
        if "No recording has been applied yet" not in view.last_import.text():
            problems.append("setup inventory: the Import page and the card "
                            "disagree about an empty inventory")
        view.deleteLater()

        # Held: three parts, two built items, one import in the journal. The
        # numbers on the card and on the Import page are the same answer.
        connection = dbq.connect(cfg.db_path)
        for unique, count in (("/t/a", 4), ("/t/b", 1), ("/t/c", 2)):
            connection.execute(
                "INSERT INTO owned_parts(part_unique, count, source) "
                "VALUES(?, ?, 'video')", (unique, count))
        for unique in ("/t/x", "/t/y"):
            connection.execute(
                "INSERT INTO owned_items(item_unique, count, source) "
                "VALUES(?, 1, 'video')", (unique,))
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source) "
            "VALUES('/t/hand', 9, 'manual')")
        connection.execute(
            "INSERT INTO imports(written_at, source, kind) "
            "VALUES('2026-10-12 08:00:00', 'a', 'scan')")
        connection.execute(
            "INSERT INTO imports(written_at, source, kind, undone_at) "
            "VALUES('2026-10-13 08:00:00', 'b', 'scan', '2026-10-13 09:00:00')")
        connection.execute(
            "INSERT INTO imports(written_at, source, kind) "
            "VALUES('2026-10-13 10:00:00', 'p', 'profile')")
        connection.commit()
        connection.close()

        check = readiness.inventory_check(cfg)
        info = check.info
        if (info["parts"], info["kinds"], info["items"]) != (7, 3, 2):
            problems.append(f"setup inventory: counts {info} for 7 parts of 3 "
                            f"kinds and 2 items (hand-entered rows are not an import)")
        import datetime

        day = datetime.datetime(2026, 10, 12, 8, tzinfo=datetime.timezone.utc) \
            .astimezone().strftime("%Y-%m-%d")
        if info["when"] != day:
            problems.append(f"setup inventory: last import {info['when']!r}, "
                            f"wanted {day} (an undone scan and a profile import "
                            f"come later and do not count)")
        page.refresh()
        text = card.text.text()
        if card.kind != "done" or check.detail not in text \
                or not text.startswith("7 parts, 2 built items, last import "):
            problems.append(f"setup inventory: held says {card.kind!r} {text!r}")
        if [b.text() for b in buttons()] != ["Import again"]:
            problems.append("setup inventory: held has no way back to Import")
        view = ImportView(cfg)
        shown = view.last_import.text()
        if "7 part(s) across 3 kind(s), 2 built item(s)" not in shown:
            problems.append(f"setup inventory: the Import page says {shown!r} "
                            f"where the card says {check.detail!r}")
        view.deleteLater()

        # A database from before the journal: counts, no date, no crash.
        connection = dbq.connect(cfg.db_path)
        connection.execute("DROP TABLE imports")
        connection.commit()
        connection.close()
        old = readiness.inventory_check(cfg)
        if old.state != "held" or "date unknown" not in old.detail:
            problems.append(f"setup inventory: no journal gave {old.state!r} "
                            f"{old.detail!r}")
        page.deleteLater()
    finally:
        patches.restore()
    print("setup card         : Your inventory empty (button opens Import) and "
          "held (7 parts, 2 built items, last import day), the same answer as "
          "the Import page, hand-entered rows and undone imports not counted")


def _check_1080p_folder_import_reads_as_well_as_4k(cfg, problems: list) -> None:
    """The inventory fixtures through the folder import, at 1920x1080.

    The import was only ever tried on 4K pictures. This runs the same three
    full-window fixtures through `scan.scan` on a folder, which is what "Choose
    folder..." and "Start scan" run, once at 3840x2160 and once scaled to 1920x1080 with
    `cv2.INTER_AREA` (a drawn-down 4K picture, not what the game renders at
    1080p), against `expected.json`. Measured: all 27 tracked tiles named
    at both sizes. The floors are those numbers, not a hope.

    Also the two faults the measurement found: the tile pitch was only looked
    for in the pixels of a 4K picture (None at 1080p, so the grid came from the
    text), and a tile cut by the top of a picture ended the scan with a
    KeyError.
    """
    import json
    import sqlite3

    import cv2
    from rapidfuzz import fuzz

    from wft.data import db as dbq
    from wft.inventory import pagemodel as pm, scan as scan_mod
    from wft.inventory.quantity import strip_rank
    from wft.relic import match as match_mod

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures" / "inventory"
    expected_path = fixtures / "expected.json"
    if not expected_path.is_file():
        return
    expected = json.loads(expected_path.read_text())

    # A scratch copy without the remembered tile grid: the first import of a
    # newcomer has none, and a remembered one would stand in for the
    # measurement this checks.
    work = pathlib.Path(tempfile.mkdtemp())
    shutil.copy(cfg.db_path, work / "warframe.sqlite")
    connection = dbq.connect(work / "warframe.sqlite")
    connection.execute("DELETE FROM meta WHERE key = 'tile_grid'")
    connection.commit()
    try:
        # The pitch is measured at both sizes, in proportion, on all three.
        for name in ("4k_a.png", "4k_b.png", "4k_c.png"):
            big = cv2.imread(str(fixtures / name))
            small = cv2.resize(big, (1920, 1080), interpolation=cv2.INTER_AREA)
            measured = (pm.measure_pitch(big), pm.measure_pitch(small))
            if None in measured:
                problems.append(f"1080p: {name} pitch measured {measured}")
                continue
            ratio = measured[0][0] / measured[1][0]
            if not 1.97 <= ratio <= 2.03:
                problems.append(f"1080p: {name} row pitch {measured[0][0]} at 4K "
                                f"and {measured[1][0]} at 1080p")

        # The old search, in pixels of a 4K picture, finds nothing at 1080p.
        real = pm.pitch_search
        pm.pitch_search = lambda _height: pm.PITCH_SEARCH
        try:
            if pm.measure_pitch(cv2.resize(
                    cv2.imread(str(fixtures / "4k_a.png")), (1920, 1080),
                    interpolation=cv2.INTER_AREA)) is not None:
                problems.append("1080p: the pixel-range search measures at "
                                "1080p, so the cause named earlier is wrong")
        finally:
            pm.pitch_search = real

        names = list(scan_mod._tracked_names(connection))
        from rapidfuzz import process

        def run(images):
            folder = tempfile.mkdtemp(dir=work)
            for filename, picture in images:
                cv2.imwrite(os.path.join(folder, filename), picture)
            return scan_mod.scan(folder, connection)

        def judge(result, specs):
            tracked = [t for s in specs for t in s["tiles"]
                       if process.extractOne(strip_rank(t["name"])[0], names,
                                             scorer=fuzz.ratio,
                                             processor=str.casefold)[1] >= 92]
            used, hits, counted = set(), 0, 0
            for tile in tracked:
                wanted, _ = strip_rank(tile["name"])
                for index, candidate in enumerate(result.candidates):
                    if index not in used and fuzz.ratio(
                            match_mod.normalise(wanted), candidate.name) >= 88:
                        used.add(index)
                        hits += 1
                        counted += candidate.quantity == tile["quantity"]
                        break
            wrong = sum(1 for i, c in enumerate(result.candidates)
                        if i not in used and c.confident)
            return len(tracked), hits, counted, wrong

        names3 = ("4k_a.png", "4k_b.png", "4k_c.png")
        for label, width, height in (("3840x2160", 3840, 2160),
                                     ("1920x1080", 1920, 1080)):
            images = []
            for filename in names3:
                picture = cv2.imread(str(fixtures / filename))
                if width != 3840:
                    picture = cv2.resize(picture, (width, height),
                                         interpolation=cv2.INTER_AREA)
                images.append((filename, picture))
            result = run(images)
            tracked, hits, counted, wrong = judge(
                result, [expected[n] for n in names3])
            grid = [n for n in result.notes if n.startswith("tile grid")]
            if (tracked, hits, counted) != (27, 27, 27) or wrong:
                problems.append(f"1080p: {label}, 3 frames in one folder: "
                                f"{hits}/{tracked} tiles named, {counted} counts "
                                f"right, {wrong} wrong row(s) accepted without "
                                f"review (measured 27/27, 27, 0)")
            if not grid or "measured off the picture" not in grid[0]:
                problems.append(f"1080p: {label}: the grid was not measured off "
                                f"the picture ({grid})")

        # A top row cut by the edge of the picture used to end the scan, at
        # both sizes (this fixture's top row is cut). The tiles named are held
        # to a floor only at full size: at half size the answer moves with one
        # pixel of height (1 tile at 1282x857, 6 at 1282x856, measured), which
        # was seen with this cropped fixture and not looked into further.
        for size in ((2564, 1713), (1282, 856)):
            picture = cv2.imread(str(fixtures / "crop_c.png"))
            if size[0] != 2564:
                picture = cv2.resize(picture, size, interpolation=cv2.INTER_AREA)
            try:
                result = run([("crop_c.png", picture)])
            except KeyError as exc:
                problems.append(f"1080p: crop_c at {size[0]}px wide ended the "
                                f"scan with KeyError {exc}")
                continue
            tracked, hits, _counted, _wrong = judge(result, [expected["crop_c.png"]])
            if size[0] == 2564 and hits < 6:
                problems.append(f"1080p: crop_c at {size[0]}px wide named "
                                f"{hits} of {tracked} tracked tiles (measured 6)")
    finally:
        connection.close()
        shutil.rmtree(work, ignore_errors=True)
    print("1080p folder import: 27 of 27 tracked tiles named and 27 counts right "
          "at 3840x2160 and at 1920x1080 (3 frames in one folder), the tile grid "
          "measured at both, a cut top row no longer ends the scan")


def _check_a_redirected_config_home_is_the_only_one(problems: list) -> None:
    """With XDG_CONFIG_HOME set, a config under ~/.config is not read.

    The search used to look in ~/.config as well, and fixed its list at
    import. Every test run therefore read the config of the machine it ran on, whose
    calibrated reward regions hid that the shipped ones read nothing.
    """
    import tempfile
    from pathlib import Path
    from wft import config as config_mod

    home = Path(tempfile.mkdtemp(prefix="wft-home-"))
    redirected = Path(tempfile.mkdtemp(prefix="wft-xdg-"))
    planted = home / ".config" / "warframe-tracker" / "config.toml"
    planted.parent.mkdir(parents=True)
    planted.write_text('[display]\ntheme = "planted"\n')
    saved = {key: os.environ.get(key) for key in ("HOME", "XDG_CONFIG_HOME")}
    try:
        os.environ["HOME"] = str(home)
        os.environ["XDG_CONFIG_HOME"] = str(redirected)
        cfg = config_mod.load()
        if cfg.get("display.theme") == "planted" or cfg.source == planted:
            problems.append("config: ~/.config was read although XDG_CONFIG_HOME "
                            "points elsewhere")
        del os.environ["XDG_CONFIG_HOME"]
        if config_mod.load().source != planted:
            problems.append("config: without XDG_CONFIG_HOME, ~/.config was not read")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("config home        : XDG_CONFIG_HOME is the only place when set")


# -- the guide: its button, the word check, the installer's ending, the PDF ------

def _check_the_guide_button_opens_what_there_is(app, problems: list) -> None:
    """"Open the guide" opens GUIDE.pdf; without it docs/GUIDE.md, said so; else says so.

    The desktop's opener is replaced (a test must not open a window on a screen
    that somebody is using), so what is checked is which file the button asks
    for and what the line beside it says.
    """
    from PySide6.QtCore import QUrl
    from ui.views import setup_view as setup_mod

    patches = _Patches()
    folder = pathlib.Path(tempfile.mkdtemp())
    opened: list = []
    answer = {"ok": True}
    try:
        patches.set(setup_mod.QDesktopServices, "openUrl",
                    staticmethod(lambda url: opened.append(url.toLocalFile())
                                 or answer["ok"]))
        _machine(patches, "active", 4)
        cfg = _setup_cfg(data=True, key="F8")
        page = setup_mod.SetupView(cfg)
        page.program_dir = folder
        if page.guide_btn.text() != "Open the guide":
            problems.append(f"guide: the button says {page.guide_btn.text()!r}")

        # Nothing in the folder: nothing is opened and the line says why.
        page.guide_btn.click()
        note = page.guide_note.text()
        if opened or "no guide" not in note.lower() or "GUIDE.pdf" not in note:
            problems.append(f"guide: an empty folder opened {opened} and said {note!r}")

        # A checkout: docs/GUIDE.md, and the line says that the PDF is not there.
        (folder / "docs").mkdir()
        (folder / "docs" / "GUIDE.md").write_text("# x\n")
        page.guide_btn.click()
        note = page.guide_note.text()
        if opened != [str(folder / "docs" / "GUIDE.md")] or "only in the release archive" not in note:
            problems.append(f"guide: a checkout opened {opened} and said {note!r}")

        # An archive: the PDF wins, and there is nothing to apologise for.
        opened.clear()
        (folder / "GUIDE.pdf").write_bytes(b"%PDF-1.4\n")
        page.guide_btn.click()
        if opened != [str(folder / "GUIDE.pdf")] or page.guide_note.text():
            problems.append(f"guide: the archive opened {opened} and said "
                            f"{page.guide_note.text()!r}")

        # The desktop refusing to open it is not silent.
        answer["ok"] = False
        page.guide_btn.click()
        if "did not open" not in page.guide_note.text():
            problems.append(f"guide: a refused open said {page.guide_note.text()!r}")

        # Counter-check: the real folder of the program holds docs/GUIDE.md, so a page
        # that was not pointed elsewhere would open it (the paths differ).
        fresh = setup_mod.SetupView(cfg)
        target, _ = fresh.guide_target()
        if target is None or not target.is_relative_to(setup_mod.PROGRAM_DIR):
            problems.append(f"guide: the default folder is {target}")
        page.deleteLater()
        fresh.deleteLater()
        _settle(app)
        del QUrl
    finally:
        patches.restore()
        shutil.rmtree(folder, ignore_errors=True)
    print("guide button       : opens GUIDE.pdf, else docs/GUIDE.md with a note, else "
          "says there is none; a refused open is said")


def _packaging_module(name: str):
    """A module of `packaging/`, loaded by path (it is not a package)."""
    import importlib.util

    path = pathlib.Path(__file__).resolve().parent.parent / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"wft_packaging_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text_picture(path: pathlib.Path, text: str, light_on_dark: bool = True) -> None:
    """A 1200x400 picture with one line of text, as a screenshot of a page has."""
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    image = QImage(1200, 400, QImage.Format_RGB888)
    image.fill(QColor("#0b0e14" if light_on_dark else "#f4f4f4"))
    painter = QPainter(image)
    font = QFont("Noto Sans")
    font.setPixelSize(22)
    painter.setFont(font)
    painter.setPen(QColor("#d8dde6" if light_on_dark else "#101010"))
    painter.drawText(80, 200, text)
    painter.end()
    image.save(str(path))


def _check_the_private_word_check_finds_whole_words(app, problems: list) -> None:
    """Whole words, any case, in text, in pictures and on PDF pages; "vexation" is not "vexa".

    The release script runs these functions over the archive. The checks that
    matter are the two that can be wrong in opposite directions: a word that is
    there must be found (also in a picture and on a rendered PDF page, which no
    text search sees), and a longer word that merely contains it must not.
    """
    import shutil as shutil_mod

    from PySide6.QtGui import QPageSize, QPdfWriter

    P = _packaging_module("private_words")
    words = ["vexa", "Zorbax"]

    # Text: case does not matter, a longer word does not count, punctuation is a border.
    for text, expected in (("Vexa said so", ["vexa"]), ("VEXA", ["vexa"]),
                           ("the vexation says", []), ("vexatious", []),
                           ("(zorbax), vexa.", ["vexa", "Zorbax"]),
                           ("ask Zorbax2", []), ("my_vexa", []), ("", [])):
        if P.find_in_text(text, words) != expected:
            problems.append(f"private words: {text!r} gave {P.find_in_text(text, words)}, "
                            f"wanted {expected}")

    folder = pathlib.Path(tempfile.mkdtemp())
    try:
        # The list: comments and blank lines are not words.
        listing = folder / "words.txt"
        listing.write_text("# who\n\nvexa  # a person\n  Zorbax\n")
        if P.load_words(listing) != ["vexa", "Zorbax"]:
            problems.append(f"private words: the list read as {P.load_words(listing)}")

        # A tree: a hit in a text file and in a file name; a picture is not read as text.
        tree = folder / "tree"
        (tree / "docs").mkdir(parents=True)
        (tree / "docs" / "a.md").write_text("Thanks to Vexa for the idea\n")
        (tree / "docs" / "b.md").write_text("See the vexation\n")
        (tree / "zorbax-notes.txt").write_text("nothing\n")
        (tree / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"vexa" * 10)
        found = sorted((where.split(" ")[0], word) for where, word in P.scan_tree(tree, words))
        if found != [("docs/a.md", "vexa"), ("zorbax-notes.txt", "Zorbax")]:
            problems.append(f"private words: the tree gave {found}")

        # Pictures: light on dark and dark on light; the word inside a longer word is not found.
        if shutil_mod.which("tesseract") is None:
            problems.append("private words: tesseract is not installed, the picture "
                            "check was not run")
        else:
            _text_picture(folder / "hit.png", "Send this to Zorbax if it breaks")
            _text_picture(folder / "hit-light.png", "Thanks, Vexa", light_on_dark=False)
            _text_picture(folder / "miss.png", "Read the vexation before you ask")
            shots = folder / "shots"
            shots.mkdir()
            for name in ("hit.png", "hit-light.png", "miss.png"):
                shutil_mod.copy(folder / name, shots / name)
            found = sorted(P.scan_images(shots, words))
            if found != [("hit-light.png", "vexa"), ("hit.png", "Zorbax")]:
                problems.append(f"private words: the pictures gave {found}")

            # A PDF: the word is drawn on page 2, so it is found by page, not by text.
            if shutil_mod.which("pdftoppm") is None:
                problems.append("private words: pdftoppm is not installed, the PDF "
                                "check was not run")
            else:
                pdf = folder / "two.pdf"
                writer = QPdfWriter(str(pdf))
                writer.setPageSize(QPageSize(QPageSize.A4))
                from PySide6.QtGui import QFont, QPainter

                # 72 dpi, so that a pixel is a point and 40 of them are readable text.
                writer.setResolution(72)
                painter = QPainter(writer)
                font = QFont("Noto Sans")
                font.setPixelSize(40)
                painter.setFont(font)
                painter.drawText(100, 300, "A page with nothing in it")
                writer.newPage()
                painter.drawText(100, 300, "Hello Zorbax here")
                painter.end()
                hits = P.scan_pdf(pdf, words)
                if [(where.split(" ", 1)[1], word) for where, word in hits] \
                        != [("page 2", "Zorbax")]:
                    problems.append(f"private words: the PDF gave {hits}")

        # The command line: 0 clean, 1 found, 2 without a list.
        clean = folder / "clean"
        clean.mkdir()
        (clean / "a.txt").write_text("the vexation\n")
        for argv, status in (
                (["--words", str(listing), "--tree", str(clean)], 0),
                (["--words", str(listing), "--tree", str(tree)], 1),
                (["--words", str(folder / "missing.txt"), "--tree", str(clean)], 2),
                (["--words", str(listing), "--word", "vexation", "--tree", str(clean)], 1)):
            import contextlib
            import io

            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                got = P.main(argv)
            if got != status:
                problems.append(f"private words: {argv[2:]} exited {got}, wanted {status}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("private words      : whole words in any case in text, file names, "
          "pictures and PDF pages; vexation is not vexa; exit status 0, 1 and 2")


def _check_the_installer_ends_with_the_start_question(problems: list) -> None:
    """The end of install.sh: start now?, where to find it, the login note, the guide.

    Run for real, in a copy of the folder with stand-ins for what needs the
    machine: `pacman` (everything is installed), `getent` and `sudo` (the
    `input` group exists and the user is not in it; `sudo` only writes down what
    it was asked), and `WFT_START_COMMAND` for the program, so the real window
    is never started. The terminal is a pty, as at a real terminal.
    """
    import pty
    import select
    import subprocess
    import time

    here = pathlib.Path(__file__).resolve().parent.parent
    base = pathlib.Path(tempfile.mkdtemp())
    try:
        tree = base / "program"
        (tree / "packaging").mkdir(parents=True)
        (tree / "bin").mkdir()
        for name in ("install.sh",):
            shutil.copy(here / name, tree / name)
        for name in ("arch-packages.txt", "warframe-tracker.desktop.in",
                     "warframe-tracker.svg"):
            shutil.copy(here / "packaging" / name, tree / "packaging" / name)
        (tree / "bin" / "wf-gui").write_text("#!/bin/sh\nexit 0\n")
        (tree / "bin" / "wf-gui").chmod(0o755)

        stubs = base / "stubs"
        stubs.mkdir()
        marker = base / "started"
        sudo_log = base / "sudo.log"
        scripts = {
            "pacman": "#!/bin/sh\nexit 0\n",
            "sudo": f'#!/bin/sh\necho "$@" >> "{sudo_log}"\n',
            "getent": '#!/bin/sh\necho "input:x:54321:${GROUP_MEMBERS}"\n',
            "start": f'#!/bin/sh\necho "$@" > "{marker}"\n',
        }
        for name, body in scripts.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)

        def run(label, answers, members="", no_terminal=False, guide=False, tty=True):
            marker.unlink(missing_ok=True)
            sudo_log.unlink(missing_ok=True)
            guide_file = tree / "GUIDE.pdf"
            guide_file.unlink(missing_ok=True)
            if guide:
                guide_file.write_bytes(b"%PDF-1.4\n")
            env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(base / "home"),
                   "USER": "tester", "DISPLAY": ":99", "GROUP_MEMBERS": members,
                   "WFT_START_COMMAND": f"{stubs}/start placeholder-for-the-program"}
            if no_terminal:
                env["WFT_NO_TERMINAL"] = "1"
            (base / "home").mkdir(exist_ok=True)
            if tty:
                master, slave = pty.openpty()
                process = subprocess.Popen(["bash", str(tree / "install.sh")], env=env,
                                           stdin=slave, stdout=slave, stderr=slave,
                                           close_fds=True)
                os.close(slave)
                os.write(master, answers.encode())
                chunks = []
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        try:
                            data = os.read(master, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        chunks.append(data)
                    elif process.poll() is not None:
                        break
                os.close(master)
                process.wait(timeout=10)
                output = b"".join(chunks).decode(errors="replace")
            else:
                done = subprocess.run(["bash", str(tree / "install.sh")], env=env,
                                      stdin=subprocess.DEVNULL, capture_output=True,
                                      text=True, timeout=30)
                process, output = done, done.stdout + done.stderr
            # `setsid -f` returns at once; the placeholder writes its marker a moment later.
            for _ in range(50):
                if marker.exists():
                    break
                time.sleep(0.05)
            return process.returncode, output, marker.exists(), \
                sudo_log.read_text() if sudo_log.exists() else ""

        # Yes to the group, yes (Enter) to starting.
        code, out, started, sudo = run("group yes, start", "y\n\n")
        if code != 0:
            problems.append(f"installer: exit {code}\n{out[-300:]}")
        if "usermod -aG input tester" not in sudo:
            problems.append(f"installer: the group was not added ({sudo!r})")
        if "Start Warframe Tracker now? [Y/n]" not in out:
            problems.append("installer: the start question is not asked")
        if not started or marker.read_text().strip() != "placeholder-for-the-program":
            problems.append("installer: the start command (the placeholder) was not run")
        for needle in ("Alt+Space", "Games", "log out of the desktop", "Setup page",
                       "wf doctor"):
            if needle not in out:
                problems.append(f"installer: the ending does not say {needle!r}")
        if "GUIDE.pdf" in out:
            problems.append("installer: points at GUIDE.pdf which is not there")

        # No to starting: nothing starts; no to the group: no word about logging in.
        code, out, started, sudo = run("no, no", "n\nn\n")
        if started:
            problems.append("installer: answered n and the program started")
        if sudo:
            problems.append(f"installer: answered n to the group and sudo ran ({sudo!r})")
        if "log out of the desktop" in out:
            problems.append("installer: tells to log in again though no group was added")
        if "Alt+Space" not in out:
            problems.append("installer: where to find the program is not said after n")

        # Already in the group, session older: the login note without a question.
        code, out, started, sudo = run("pending", "\n", members="tester", guide=True)
        if "log out of the desktop" not in out or "Add tester" in out:
            problems.append("installer: a pending group did not give the login note "
                            "(or asked again)")
        if "GUIDE.pdf" not in out:
            problems.append("installer: does not point at GUIDE.pdf that is there")
        if not started:
            problems.append("installer: the start question did not start the program")

        # Ctrl+D at the question (end of input) is a no, and the ending still comes.
        code, out, started, sudo = run("end of input", "\x04", members="tester")
        if started or "Alt+Space" not in out or code != 0:
            problems.append(f"installer: end of input at the start question gave "
                            f"exit {code}, started {started}, ending shown "
                            f"{'Alt+Space' in out}")

        # Scripted (no terminal): nothing is asked, nothing is started.
        code, out, started, sudo = run("scripted", "", no_terminal=True, tty=False)
        if code != 0 or started or "Start Warframe Tracker now?" in out:
            problems.append(f"installer: a scripted run asked or started ({code}, {started})")
        if "Alt+Space" not in out:
            problems.append("installer: a scripted run does not say where the program is")

        # A terminal with WFT_NO_TERMINAL set (a test of the test: the question is
        # what the variable switches off, not the lack of a tty).
        code, out, started, sudo = run("no_terminal with tty", "n\n", members="tester",
                                       no_terminal=True)
        if started or "Start Warframe Tracker now?" in out:
            problems.append("installer: WFT_NO_TERMINAL did not silence the start question")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("installer ending   : start question (yes, no, scripted), where to find "
          "it, the login note only when the group was added or pending, the guide")


def _check_the_installer_puts_the_new_folder_in_place_of_the_old(problems: list) -> None:
    """An update by install.sh: the old version is closed, its folder goes to the
    trash and the new folder takes its place; links and menu entry follow.

    Run for real in a scratch home, from a new folder in Downloads, with an old
    folder where the links point and a stand-in for the running old program,
    started through the `wf-gui` link as a terminal would. Also: answering n
    leaves the old folder and installs from Downloads, a scripted run asks and
    changes nothing, and a git checkout is never offered for the trash.
    """
    import pty
    import select
    import subprocess
    import time

    here = pathlib.Path(__file__).resolve().parent.parent
    base = pathlib.Path(tempfile.mkdtemp())
    sleepers = []

    def program(folder, label):
        (folder / "packaging").mkdir(parents=True)
        (folder / "bin").mkdir()
        (folder / "wft").mkdir()
        shutil.copy(here / "install.sh", folder / "install.sh")
        for name in ("arch-packages.txt", "warframe-tracker.desktop.in",
                     "warframe-tracker.svg"):
            shutil.copy(here / "packaging" / name, folder / "packaging" / name)
        for name in ("wf", "wf-gui"):
            (folder / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
            (folder / "bin" / name).chmod(0o755)
        (folder / "version.txt").write_text(label)

    def scene(git=False):
        """A home with the old version installed and the new one in Downloads."""
        shutil.rmtree(base / "home", ignore_errors=True)
        home = base / "home"
        old = home / "warframe-tracker"
        new = home / "Downloads" / "warframe-tracker"
        program(old, "old")
        program(new, "new")
        if git:
            (old / ".git").mkdir()
        links = home / ".local" / "bin"
        links.mkdir(parents=True)
        for name in ("wf", "wf-gui"):
            (links / name).symlink_to(old / "bin" / name)
        return home, old, new

    def run(home, new, answers, no_terminal=False):
        env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(home),
               "USER": "tester", "DISPLAY": ":99", "GROUP_MEMBERS": "tester",
               "WFT_START_COMMAND": f"{stubs}/start"}
        if no_terminal:
            env["WFT_NO_TERMINAL"] = "1"
        master, slave = pty.openpty()
        process = subprocess.Popen(["bash", str(new / "install.sh")], env=env,
                                   cwd=str(new), stdin=slave, stdout=slave,
                                   stderr=slave, close_fds=True)
        os.close(slave)
        os.write(master, answers.encode())
        chunks = []
        end = time.monotonic() + 30
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            elif process.poll() is not None:
                break
        os.close(master)
        process.wait(timeout=10)
        return process.returncode, b"".join(chunks).decode(errors="replace")

    def old_program_running(home):
        """The old version as a terminal starts it: through the `wf-gui` link."""
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)",
             str(home / ".local" / "bin" / "wf-gui")])
        sleepers.append(sleeper)
        return sleeper

    def trashed(home):
        folder = home / ".local" / "share" / "Trash" / "files"
        return sorted(path.name for path in folder.iterdir()) if folder.exists() else []

    try:
        stubs = base / "stubs"
        stubs.mkdir()
        sudo_log = base / "sudo.log"
        for name, body in {
                "pacman": "#!/bin/sh\nexit 0\n",
                "sudo": f'#!/bin/sh\necho "$@" >> "{sudo_log}"\n',
                "getent": '#!/bin/sh\necho "input:x:54321:${GROUP_MEMBERS}"\n',
                "start": "#!/bin/sh\nexit 0\n"}.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)

        # Yes, yes: closed, old folder in the trash, the new one in its place.
        home, old, new = scene()
        sleeper = old_program_running(home)
        time.sleep(0.3)
        code, out = run(home, new, "\n\nn\n")
        try:
            sleeper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            problems.append("installer update: the running old version was not closed")
        if code != 0 or "It is running. Close it now?" not in out:
            problems.append(f"installer update: exit {code}, close question asked "
                            f"{'Close it now?' in out}\n{out[-400:]}")
        if not (old / "version.txt").exists() \
                or (old / "version.txt").read_text() != "new" or new.exists():
            problems.append("installer update: the new folder did not take the old "
                            "one's place")
        if trashed(home) != ["warframe-tracker"]:
            problems.append(f"installer update: the trash holds {trashed(home)}")
        for name in ("wf", "wf-gui"):
            link = home / ".local" / "bin" / name
            if os.readlink(link) != str(old / "bin" / name):
                problems.append(f"installer update: {name} points at "
                                f"{os.readlink(link)}")
        entry_file = (home / ".local" / "share" / "applications"
                      / "warframe-tracker.desktop")
        entry = entry_file.read_text() if entry_file.exists() else ""
        if f'Exec="{old}/bin/wf-gui"' not in entry:
            problems.append("installer update: the menu entry does not start the "
                            "moved folder")
        if f"GUIDE.pdf in {old}" in out or "in this folder" in out:
            problems.append("installer update: the ending names a folder that moved")

        # No to the move: the old folder stays, the new one is installed where it is.
        home, old, new = scene()
        code, out = run(home, new, "n\nn\n")
        if "Close it now?" in out:
            problems.append("installer update: asked to close a version that was "
                            "not running")
        if (old / "version.txt").read_text() != "old" or not new.exists() \
                or trashed(home):
            problems.append("installer update: answered n and a folder moved")
        if os.readlink(home / ".local" / "bin" / "wf-gui") != str(new / "bin" / "wf-gui"):
            problems.append("installer update: answered n and the links did not "
                            "move to the new folder")

        # Scripted: says where the old folder is, closes and moves nothing.
        home, old, new = scene()
        sleeper = old_program_running(home)
        time.sleep(0.3)
        code, out = run(home, new, "", no_terminal=True)
        if sleeper.poll() is not None or trashed(home) or not new.exists() \
                or "Older version found" not in out or "Close it now?" in out:
            problems.append("installer update: a scripted run closed, moved or asked")
        sleeper.terminate()

        # A git checkout behind the links is somebody's working copy.
        home, old, new = scene(git=True)
        code, out = run(home, new, "n\n")
        if "Older version found" in out or trashed(home):
            problems.append("installer update: offered a git checkout for the trash")
    finally:
        for sleeper in sleepers:
            if sleeper.poll() is None:
                sleeper.terminate()
                sleeper.wait(timeout=5)
        shutil.rmtree(base, ignore_errors=True)
    print("installer update   : old version closed, its folder in the trash, the new "
          "one in its place; n, scripted and a git checkout leave it")


def _check_the_installer_reopens_itself_in_a_terminal(problems: list) -> None:
    """Started without a terminal, as from Dolphin, install.sh opens Konsole.

    From a file manager the script has no terminal: sudo cannot ask for the
    password and nothing it prints is seen. It reopens itself in Konsole with
    a marker that stops it doing that twice. Run for real, with a stand-in
    Konsole that writes down how it was called; the marker set, it runs where
    it is; and a scripted run (WFT_NO_TERMINAL) never opens a window.
    """
    import subprocess

    here = pathlib.Path(__file__).resolve().parent.parent
    base = pathlib.Path(tempfile.mkdtemp())
    try:
        tree = base / "program"
        (tree / "packaging").mkdir(parents=True)
        (tree / "bin").mkdir()
        shutil.copy(here / "install.sh", tree / "install.sh")
        for name in ("arch-packages.txt", "warframe-tracker.desktop.in",
                     "warframe-tracker.svg"):
            shutil.copy(here / "packaging" / name, tree / "packaging" / name)
        (tree / "bin" / "wf-gui").write_text("#!/bin/sh\nexit 0\n")
        (tree / "bin" / "wf-gui").chmod(0o755)
        stubs = base / "stubs"
        stubs.mkdir()
        called = base / "konsole.args"
        for name, body in {
                "konsole": f'#!/bin/sh\nprintf "%s\\n" "$@" > "{called}"\n',
                "pacman": "#!/bin/sh\nexit 0\n",
                "sudo": "#!/bin/sh\nexit 0\n",
                "getent": '#!/bin/sh\necho "input:x:54321:tester"\n'}.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        (base / "home").mkdir()

        def run(**extra):
            called.unlink(missing_ok=True)
            env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(base / "home"),
                   "USER": "tester", "DISPLAY": ":99", **extra}
            done = subprocess.run(["bash", str(tree / "install.sh")], env=env,
                                  stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=30)
            return done, (called.read_text().splitlines() if called.exists() else None)

        # As from Dolphin: no terminal, a desktop.
        done, args = run()
        wanted = ["--workdir", str(tree), "-e", "env", "WFT_IN_TERMINAL=1",
                  "bash", str(tree / "install.sh")]
        if args != wanted:
            problems.append(f"installer terminal: started without a terminal it "
                            f"called Konsole with {args}, expected {wanted}")
        if (base / "home" / ".local" / "bin" / "wf-gui").exists():
            problems.append("installer terminal: it installed in the background "
                            "instead of (only) opening Konsole")

        # Inside that Konsole: the marker is set, so it installs, opens no
        # second window, and waits for Enter before the window closes. At a
        # terminal, as Konsole gives it one: `read -p` shows its prompt only
        # then.
        import pty
        import select
        import time

        called.unlink(missing_ok=True)
        env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(base / "home"),
               "USER": "tester", "DISPLAY": ":99", "WFT_IN_TERMINAL": "1",
               "WFT_START_COMMAND": f"{stubs}/pacman"}
        master, slave = pty.openpty()
        process = subprocess.Popen(["bash", str(tree / "install.sh")], env=env,
                                   stdin=slave, stdout=slave, stderr=slave,
                                   close_fds=True)
        os.close(slave)
        os.write(master, b"n\n\n")
        chunks, end = [], time.monotonic() + 30
        while time.monotonic() < end:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            elif process.poll() is not None:
                break
        os.close(master)
        process.wait(timeout=10)
        said = b"".join(chunks).decode(errors="replace")
        if called.exists() or process.returncode != 0 \
                or not (base / "home" / ".local" / "bin" / "wf-gui").is_symlink():
            problems.append(f"installer terminal: with the marker set it opened "
                            f"Konsole again or did not install (exit "
                            f"{process.returncode})")
        if "Press Enter to close this window" not in said:
            problems.append("installer terminal: the window would close before "
                            "the ending can be read")

        # Scripted: never a window.
        done, args = run(WFT_NO_TERMINAL="1")
        if args is not None:
            problems.append("installer terminal: a scripted run opened Konsole")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("installer terminal : without a terminal it reopens in Konsole, once; "
          "a scripted run never does")


def _check_the_installer_and_doctor_speak_to_a_newcomer(problems: list) -> None:
    """What the first newcomer test found unclear, in install.sh and wf doctor.

    The uninstall names the real packages and how to leave the `input` group,
    not a placeholder that fish reads as a redirection; a failed package step
    says what usually helps; `wf doctor` does not show the shipped example as
    the person's own settings, and its game-log hint points at the Setup page.
    No tty and WFT_NO_TERMINAL: nothing here asks or opens a window.
    """
    import subprocess

    from wft import config as config_mod, doctor

    here = pathlib.Path(__file__).resolve().parent.parent
    base = pathlib.Path(tempfile.mkdtemp())
    patches = _Patches()
    try:
        tree = base / "program"
        (tree / "packaging").mkdir(parents=True)
        shutil.copy(here / "install.sh", tree / "install.sh")
        for name in ("arch-packages.txt", "warframe-tracker.desktop.in",
                     "warframe-tracker.svg"):
            shutil.copy(here / "packaging" / name, tree / "packaging" / name)
        stubs = base / "stubs"
        stubs.mkdir()
        for name, body in {
                # One package missing, and the download of it fails.
                "pacman": '#!/bin/sh\n[ "$1" = -T ] && { echo xdotool; exit 127; }\n'
                          'echo "error: failed retrieving file" >&2; exit 1\n',
                "sudo": '#!/bin/sh\nexec "$@"\n'}.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(base / "home"),
               "USER": "tester", "WFT_NO_TERMINAL": "1"}
        (base / "home").mkdir()

        done = subprocess.run(["bash", str(tree / "install.sh")], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=30)
        said = done.stdout + done.stderr
        if done.returncode == 0 or "garuda-update" not in said \
                or "sudo pacman -Syu" not in said:
            problems.append(f"installer: a failed package step gives no way on "
                            f"(exit {done.returncode}): {said[-300:]!r}")

        done = subprocess.run(["bash", str(tree / "install.sh"), "--uninstall"], env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=30)
        said = done.stdout + done.stderr
        wanted = [line.split("#")[0].strip() for line in
                  (here / "packaging" / "arch-packages.txt").read_text().splitlines()]
        command = "sudo pacman -Rns " + " ".join(w for w in wanted if w)
        if command not in said or "<names" in said:
            problems.append(f"installer: the uninstall does not print the package "
                            f"command ({said[-300:]!r})")
        if "sudo gpasswd -d tester input" not in said:
            problems.append("installer: the uninstall does not say how to leave the "
                            "input group")

        patches.env("HOME", str(base / "home"))
        patches.env("XDG_CONFIG_HOME", str(base / "home" / ".config"))
        cfg = config_mod.load()
        line = dict((name, detail) for _l, name, detail in doctor.check_config(cfg))
        if "no settings of your own" not in line.get("config", ""):
            problems.append(f"doctor: without a config file it shows "
                            f"{line.get('config')!r} as the settings")
        hint = " ".join(detail for _l, _n, detail in doctor.check_ee_log(cfg))
        if "Setup" not in hint or "config.example.toml" in hint:
            problems.append(f"doctor: a missing game log does not point at Setup ({hint})")
    finally:
        patches.restore()
        shutil.rmtree(base, ignore_errors=True)
    print("newcomer wording   : uninstall prints real commands, a failed package step "
          "says what helps, doctor names no example file as yours")


def _check_the_guide_is_built_from_markdown(problems: list) -> None:
    """GUIDE.md becomes a PDF; relative links do not leak the folder, a missing picture stops it."""
    import subprocess

    B = _packaging_module("build_guide")
    folder = pathlib.Path(tempfile.mkdtemp())
    try:
        from PIL import Image

        (folder / "GUIDE.md").write_text(
            "# Tiny guide\n\nText with a [local file](LICENSE) and an "
            "[outside page](https://example.org/x).\n\n## One\n\n| a | b |\n| - | - |\n"
            "| 1 | 2 |\n\n```\n./install.sh\n```\n\n![A picture](pic.png)\n\n## Two\n\n"
            "More text.\n")
        Image.new("RGB", (600, 300), "#223344").save(folder / "pic.png")

        page = B.markdown_to_html(folder / "GUIDE.md", "Release abc, 2026-10-14")
        if 'href="LICENSE"' in page or "a local file and" not in page:
            problems.append("guide pdf: a link to another file was kept as a link")
        if 'href="https://example.org/x"' not in page:
            problems.append("guide pdf: a link to the web was lost")
        if 'class="toc"' not in page or 'href="#one"' not in page:
            problems.append("guide pdf: no contents list with chapter links")
        # The patch notes are built without one (--no-contents).
        bare = B.markdown_to_html(folder / "GUIDE.md", "Release abc", contents=False)
        if 'class="toc"' in bare or "<h2" not in bare:
            problems.append("guide pdf: contents=False still has a contents list, "
                            "or lost the chapters")
        if "<table>" not in page or "<pre><code>" not in page:
            problems.append("guide pdf: table or code block missing in the HTML")
        if "Release abc, 2026-10-14" not in page:
            problems.append("guide pdf: the stamp is not under the title")
        if B.missing_pictures(page, folder):
            problems.append(f"guide pdf: a present picture was reported missing: "
                            f"{B.missing_pictures(page, folder)}")

        for engine in ("auto", "textdoc"):
            out = folder / f"{engine}.pdf"
            done = subprocess.run(
                [sys.executable, str(pathlib.Path(B.__file__)), "--source",
                 str(folder / "GUIDE.md"), "--out", str(out), "--engine", engine],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
            if done.returncode != 0 or not out.is_file():
                problems.append(f"guide pdf ({engine}): exit {done.returncode}: "
                                f"{(done.stderr or done.stdout)[-200:]}")
                continue
            info = subprocess.run(["pdfinfo", str(out)], capture_output=True, text=True).stdout
            text = subprocess.run(["pdftotext", str(out), "-"], capture_output=True,
                                  text=True).stdout
            if "Pages:" not in info or "Tiny guide" not in text or "More text." not in text:
                problems.append(f"guide pdf ({engine}): the PDF lacks its text ({info[:80]!r})")
            if b"file://" in out.read_bytes():
                problems.append(f"guide pdf ({engine}): the PDF carries a file:// link to the "
                                f"folder it was built in")

        # A list the PDF would show differently from the repository page stops
        # the build: a block indented by 3 spaces swallows the next items, a
        # nested list by 2 spaces comes out flat. 4 spaces keep both.
        for shape, words in (("1. One:\n\n   ```\n   wf doctor\n   ```\n\n   More.\n"
                              "2. Two\n3. Three\n", "merged"),
                             ("- Top\n  - Inner\n  - Inner too\n- Next\n", "flattened")):
            (folder / "lists.md").write_text("# Lists\n\n" + shape)
            try:
                B.markdown_to_html(folder / "lists.md")
                problems.append(f"guide pdf: a {words} list did not stop the build")
            except SystemExit as exc:
                if "shape of a list" not in str(exc):
                    problems.append(f"guide pdf: a {words} list stopped with {exc}")
        (folder / "lists.md").write_text(
            "# Lists\n\n1. One:\n\n        wf doctor\n\n    More.\n\n2. Two\n3. Three\n\n"
            "- Top\n    - Inner\n    - Inner too\n- Next\n")
        try:
            B.markdown_to_html(folder / "lists.md")
        except SystemExit as exc:
            problems.append(f"guide pdf: a list indented by 4 spaces stopped the build: {exc}")

        # Counter-check: a picture that is not there stops the build.
        (folder / "pic.png").unlink()
        stopped = False
        try:
            B.build(folder / "GUIDE.md", folder / "gone.pdf", engine="textdoc")
        except SystemExit as exc:
            stopped = "pic.png" in str(exc)
        if not stopped or (folder / "gone.pdf").exists():
            problems.append("guide pdf: a missing picture did not stop the build")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("guide pdf          : GUIDE.md to PDF through QtWebEngine and through "
          "QTextDocument, no file:// links, a missing picture or a list python-markdown "
          "would reshape stops it")


def _no_real_overlay_helper() -> None:
    """No test opens a window over the user's screen: the helper's command is a
    stub for the whole run, a program that ends at once with one line on stderr.

    The Overlays page starts the helper when Start is pressed, and the walk
    presses every button. The one check of the client replaces the command
    again for its own fakes.
    """
    from ui.overlay import client as client_mod

    def disabled(_socket_name):
        return sys.executable, ["-c", "import sys; "
                                "sys.stderr.write('overlay helper disabled in tests\\n'); "
                                "sys.exit(1)"]

    client_mod.helper_command = disabled

    # The test button of the reward overlay looks for the real game window; no
    # test asks the user's X server for it.
    from ui.overlay import rewards as rewards_mod

    rewards_mod.RewardOverlay.finder = lambda self: None


def _palette_pixels(style: dict, size: tuple[int, int]):
    from PySide6.QtGui import QColor, QImage, QPainter

    from ui.overlay import drawing

    image = QImage(size[0], size[1], QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    drawing.draw_grid(painter, image.rect(), style, False)
    painter.end()
    return image


def _anything_drawn(image, x0: int, y0: int, x1: int, y1: int) -> bool:
    return any(image.pixelColor(x, y).alpha() > 0
               for x in range(x0, x1) for y in range(y0, y1))


class _FakeOverlayClient:
    """What the Overlays page needs of the client, without a process."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class Signals(QObject):
            state = Signal(dict)
            reward_state = Signal(dict)
            failed = Signal(str)
            stopped = Signal()

        self._signals = Signals()
        self.state = self._signals.state
        self.reward_state = self._signals.reward_state
        self.failed = self._signals.failed
        self.stopped = self._signals.stopped
        self.running = False
        self.sent: list[dict] = []
        self.attempts: list[dict] = []
        self.stops = 0

    def send(self, command: dict) -> None:
        self.attempts.append(command)
        if not self.running and command.get("cmd") not in ("palette_show", "reward_show"):
            return
        self.running = True
        self.sent.append(command)

    def stop(self) -> None:
        self.stops += 1
        self.running = False
        self.stopped.emit()


_OVERLAY_TILES = [(x / 3840, 0.2065, _TILE_W, 0.219) for x in _MEASURED_LEFT_EDGES[4]]


def _reward_overlay_pixels(size, best, headline=None, picked=None, tiles=None,
                           lines=None, look=None):
    """The reward overlay drawn on a transparent picture of this size."""
    from PySide6.QtGui import QColor, QImage, QPainter

    from ui.overlay import drawing

    tiles = tiles or _OVERLAY_TILES
    lines = lines if lines is not None else [
        ["45 ducats", "not mastered yet", "MISSING  ·  COMPLETES SET"] for _ in tiles]
    image = QImage(size[0], size[1], QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    drawing.draw_rewards(painter, image.rect(), tiles, best, headline, lines, picked, look)
    painter.end()
    return image


def _overlay_box(size, x, y, w, h):
    """(x0, y0, x1, y1) in pixels of a box given as fractions of the picture."""
    return (int(x * size[0]), int(y * size[1]), int((x + w) * size[0]) + 1,
            int((y + h) * size[1]) + 1)


def _anything_drawn_fast(image, x0, y0, x1, y1) -> bool:
    """Whether any pixel of the box has alpha; looks at every 2nd pixel."""
    return any(image.pixelColor(x, y).alpha() > 0
               for x in range(x0, x1, 2) for y in range(y0, y1, 2))


def _header_window(strip, width=3840, height=2160):
    """A whole-window picture, black, with this header strip where the header is."""
    import numpy

    from wft.relic import header

    window = numpy.zeros((height, width, 3), numpy.uint8)
    x, y = int(header.REGION[0] * width), int(header.REGION[1] * height)
    window[y:y + strip.shape[0], x:x + strip.shape[1]] = strip
    return window


def _check_the_reward_overlay(app, problems: list) -> None:
    """The reward overlay: drawing, header check, lifetime, helper
    commands, the notification that gives way, the Overlays page.

    No window opens on a screen: the drawing goes to a QImage, the helper's
    window runs on the offscreen platform, the client is a fake and the clock
    and the grab of the game window are fakes too. What is not reachable here is
    named in the result line of the report, not tested.
    """
    import time

    import cv2
    import numpy

    from PySide6.QtCore import Qt

    from ui import notify, settings_io
    from ui.overlay import drawing, helper, rewards as R
    from ui.views.overlays_view import OverlaysView
    from ui.views.relic_panel import RelicPanel, overlay_args
    from ui.widgets.reward_card import overlay_lines
    from ui.workers.relic_trigger import TriggerResult
    from wft.capture import window as cap
    from wft.relic import header, last_screen, score as S

    fixtures = pathlib.Path(__file__).resolve().parent / "fixtures"
    patches = _Patches()
    pages = []
    try:
        # -- the drawing -------------------------------------------------------
        for size in ((1920, 1080), (3840, 2160)):
            tag = f"drawing {size[0]}"
            for best in range(4):
                image = _reward_overlay_pixels(size, best, picked=None)
                for index, (tx, ty, tw, th) in enumerate(_OVERLAY_TILES):
                    # The frame: a line over the top edge of the card taken, none at others
                    # (at the sides a neighbour's frame reaches into the gap).
                    row = int(ty * size[1]) - 2
                    framed = _anything_drawn_fast(image, int((tx + 0.05 * tw) * size[0]), row,
                                                  int((tx + 0.2 * tw) * size[0]), row + 2)
                    if framed != (index == best):
                        problems.append(f"{tag}: best {best}: frame at card {index} is {framed}")
                    # The card's middle is free whatever else is drawn.
                    if _anything_drawn_fast(image, *_overlay_box(
                            size, tx + 0.25 * tw, ty + 0.25 * th, 0.5 * tw, 0.5 * th)):
                        problems.append(f"{tag}: best {best}: card {index} is covered in the middle")
                    # TAKE THIS above the card taken only.
                    above = _anything_drawn_fast(image, *_overlay_box(
                        size, tx + 0.2 * tw, ty - 0.026, 0.6 * tw, 0.017))
                    if above != (index == best):
                        problems.append(f"{tag}: best {best}: TAKE THIS area of card {index} is {above}")
                    # Info lines under every card; the names above them stay clear.
                    if not _anything_drawn_fast(image, *_overlay_box(
                            size, tx + 0.15 * tw, 0.585, 0.7 * tw, 0.03)):
                        problems.append(f"{tag}: best {best}: no info lines under card {index}")
                    if _anything_drawn_fast(image, *_overlay_box(
                            size, tx + 0.05 * tw, 0.436, 0.9 * tw, 0.543 - 0.436)):
                        problems.append(f"{tag}: best {best}: card {index} covers the player names")
                # The countdown stays clear whichever card is taken.
                if _anything_drawn_fast(image, *_overlay_box(size, *drawing.EMBLEM_CLEAR)):
                    problems.append(f"{tag}: best {best}: the countdown is covered")
                # The frame is red.
                tx, ty, tw, th = _OVERLAY_TILES[best]
                red = image.pixelColor(
                    int(tx * size[0] - drawing.frame_line(size[1]) / 2),
                    int((ty + 0.1) * size[1]))
                if red.alpha() == 0 or red.red() < 200 or red.green() > 90:
                    problems.append(f"{tag}: best {best}: the frame is not red: {red.name()}")
            # Nothing to take: the banner's words, no frame, no TAKE THIS.
            image = _reward_overlay_pixels(size, None, headline="NOTHING WORTH TAKING")
            if not _anything_drawn_fast(image, *_overlay_box(
                    size, 0.26, drawing.NOTE_Y - 0.02, 0.14, 0.025)):
                problems.append(f"{tag}: the words for 'nothing to take' are missing")
            if _anything_drawn_fast(image, *_overlay_box(size, 0.2484 - 0.002, 0.3, 0.003, 0.1)):
                problems.append(f"{tag}: a frame without a card to take")
            if _anything_drawn_fast(image, *_overlay_box(size, 0.6230 + 0.02, 0.17, 0.08, 0.03)):
                problems.append(f"{tag}: TAKE THIS without a card to take")
            if _anything_drawn_fast(image, *_overlay_box(size, *drawing.EMBLEM_CLEAR)):
                problems.append(f"{tag}: the words for 'nothing to take' cover the countdown")
            # The tick on the chosen card, and only there.
            ticked = _reward_overlay_pixels(size, 2, picked=1)
            plain = _reward_overlay_pixels(size, 2, picked=None)
            tx, ty, tw, th = _OVERLAY_TILES[1]
            tick_box = _overlay_box(size, tx + tw - 0.036, ty + 0.012, 0.03, 0.05)
            if not _anything_drawn_fast(ticked, *tick_box) or _anything_drawn_fast(plain, *tick_box):
                problems.append(f"{tag}: the tick appears wrongly (picked set / not set)")
            if _anything_drawn_fast(ticked, *_overlay_box(
                    size, tx + 0.25 * tw, ty + 0.25 * th, 0.5 * tw, 0.5 * th)):
                problems.append(f"{tag}: the tick covers the card's middle")

        # -- where the lines stand, their box, the colours, the frame's edge -----
        # The game's own drawing under the cards, measured on the 338 reward screens,
        # in fractions of the window. Written out here on
        # purpose, not read from `drawing`: a wrong constant there must not agree
        # with itself. The Steel Path box (0.443 to 0.583, 0.597 to 0.666) may be covered.
        names_bottom = 0.575           # the 4th row of player names, lowest of the corpus
        endless_top = 0.706            # the "Endless Bonus" line
        info_font = 0.0155             # the font of the lines, as a share of the height
        four_names = ["45 ducats", "not mastered yet", "MISSING  ·  COMPLETES SET"]
        layouts = {n: [(x / 3840, 0.2065, _TILE_W, 0.219) for x in _MEASURED_LEFT_EDGES[n]]
                   for n in (2, 3, 4)}

        for size in ((1920, 1080), (3840, 2160)):
            width, height = size
            for count, tiles in layouts.items():
                tag = f"lines {width}x{height} {count} cards"
                cards = [four_names, four_names[:2], ["FORMA"], four_names][:count]
                blocks = drawing.info_blocks(width, height, tiles, cards)
                image = _reward_overlay_pixels(size, 0, tiles=tiles, lines=cards)
                for index, ((tx, _ty, tw, _th), block) in enumerate(zip(tiles, blocks)):
                    if block is None:
                        problems.append(f"{tag}: card {index} has no lines")
                        continue
                    box = block["box"]
                    if box.top() < names_bottom * height - 0.5:
                        problems.append(f"{tag}: card {index} the box starts at {box.top():.1f}, "
                                        f"inside the 4 rows of names (end {names_bottom * height:.1f})")
                    if box.top() > (names_bottom + 0.01) * height:
                        problems.append(f"{tag}: card {index} the box starts at {box.top():.1f}, "
                                        f"far under the names")
                    if box.bottom() > endless_top * height:
                        problems.append(f"{tag}: card {index} the box ends at {box.bottom():.1f}, "
                                        f"over the Endless Bonus line ({endless_top * height:.1f})")
                    if box.width() > tw * width + 0.5 or box.left() < tx * width - 0.5 \
                            or box.right() > (tx + tw) * width + 0.5:
                        problems.append(f"{tag}: card {index} the box is {box.width():.1f} wide, "
                                        f"the card {tw * width:.1f}")
                    if block["lines"] != cards[index][:3]:
                        problems.append(f"{tag}: card {index} lost lines: {block['lines']!r}")
                    if abs(block["pixels"] - info_font * height) > 1.0:
                        problems.append(f"{tag}: card {index} the font is {block['pixels']:.1f} px, "
                                        f"not the full {info_font * height:.1f}")
                    # Every line inside its box.
                    for baseline in block["baselines"]:
                        if baseline - block["pixels"] < box.top() or baseline > box.bottom():
                            problems.append(f"{tag}: card {index} a line at {baseline:.1f} is outside "
                                            f"its box {box.top():.1f} to {box.bottom():.1f}")
                    # The box is on the picture, dark and half transparent, and round.
                    inside = image.pixelColor(int(box.left() + box.width() * 0.04) + 2,
                                              int(box.top() + box.height() * 0.5))
                    gap = image.pixelColor(int(box.left() + box.width() * 0.5),
                                           int(box.top() + 1))
                    corner = image.pixelColor(int(box.left()), int(box.top()))
                    probe = gap if gap.alpha() else inside
                    if not 100 <= probe.alpha() <= 230 or max(probe.red(), probe.green(),
                                                              probe.blue()) > 60:
                        problems.append(f"{tag}: card {index} the box is not dark and half "
                                        f"transparent: {probe.name()} alpha {probe.alpha()}")
                    if corner.alpha() > probe.alpha() * 0.6:
                        problems.append(f"{tag}: card {index} the box has square corners "
                                        f"(alpha {corner.alpha()})")
                    # Nothing of the box outside the box: a pixel just beside it is clear.
                    beside = image.pixelColor(int(box.right()) + 3, int(box.top() + box.height() / 2))
                    if beside.alpha() and index == count - 1:
                        problems.append(f"{tag}: card {index} something is drawn beside the box")

        # The frame's inner edge is the card's edge, at the left, right and top, 2, 3, 4 cards.
        for size in ((1920, 1080), (3840, 2160)):
            width, height = size
            for count, tiles in layouts.items():
                for best in range(count):
                    image = _reward_overlay_pixels(size, best, tiles=tiles, look=None)
                    tx, ty, tw, th = tiles[best]
                    mid_y = int((ty + 0.5 * th) * height)
                    left, right, top = tx * width, (tx + tw) * width, ty * height
                    x = int(left) + 3
                    while x > 0 and image.pixelColor(x, mid_y).alpha() == 0:
                        x -= 1
                    got_left = x + 1
                    x = int(right) - 3
                    while x < width - 1 and image.pixelColor(x, mid_y).alpha() == 0:
                        x += 1
                    got_right = x
                    px = int(left + 0.05 * tw * width)
                    y = int(top) + 3
                    while y > 0 and image.pixelColor(px, y).alpha() == 0:
                        y -= 1
                    got_top = y + 1
                    for label, got, want in (("left", got_left, left), ("right", got_right, right),
                                             ("top", got_top, top)):
                        if abs(got - want) > 1.0:
                            problems.append(f"frame edge {width}x{height} {count} cards card {best}: "
                                            f"the inner {label} edge is at {got}, the card's at "
                                            f"{want:.1f}")

        # The colours: every preset, the custom pair and the fallbacks, resolved by
        # `looks.look_from_config` and drawn.
        from ui import theme as _theme
        from ui.overlay import looks

        class _Cfg:
            def __init__(self, **values):
                self.values = {f"overlay.{k}": v for k, v in values.items()}

            def get(self, key, default=None):
                return self.values.get(key, default)

        hex_re = re.compile(r"^#[0-9a-f]{6}$")
        size = (1920, 1080)
        tx, ty, tw, th = _OVERLAY_TILES[1]

        def seen(look):
            """(frame pixel, set of opaque colours in card 0's lines) of a drawing."""
            image = _reward_overlay_pixels(size, 1, look=look)
            block = drawing.info_blocks(*size, _OVERLAY_TILES, [four_names] * 4)[0]
            found = {image.pixelColor(x, y).name()
                     for x in range(int(block["box"].left()), int(block["box"].right()))
                     for y in range(int(block["box"].top()), int(block["box"].bottom()))
                     if image.pixelColor(x, y).alpha() == 255}
            return image.pixelColor(int(tx * size[0]) - 1, int((ty + 0.1) * size[1])).name(), found

        presets = looks.presets()
        if looks.FIXED != {"red-green": {"frame": "#ff2a2a", "text": "#3ddc6b"},
                           "blue-orange": {"frame": "#e69f00", "text": "#56b4e9"}}:
            problems.append(f"colours: the fixed presets are {looks.FIXED!r}")
        if "Light" in presets:
            problems.append("colours: the Light theme is offered, its green is too pale")
        for name, palette in _theme.THEMES.items():
            readable = looks.contrast(palette["good"]) >= 3.0
            if readable != (name in presets):
                problems.append(f"colours: theme {name} (contrast "
                                f"{looks.contrast(palette['good']):.2f}) listed: {name in presets}")
            if name in presets and presets[name] != {"frame": palette["accent"],
                                                     "text": palette["good"]}:
                problems.append(f"colours: preset {name} is {presets[name]!r}")
        if len(presets) < 3:
            problems.append(f"colours: only {sorted(presets)!r} are offered")
        for name, colours in presets.items():
            resolved = looks.look_from_config(_Cfg(colours=name))
            if resolved != colours or not all(hex_re.match(v) for v in resolved.values()):
                problems.append(f"colours {name}: resolves to {resolved!r}, not {colours!r}")
            for ground in (0, 255):
                if looks.contrast(colours["text"], ground) < 3.0:
                    problems.append(f"colours {name}: text contrast {looks.contrast(colours['text'], ground):.2f} "
                                    f"over ground {ground}")
            frame_px, found = seen(resolved)
            if frame_px != colours["frame"]:
                problems.append(f"colours {name}: the frame is {frame_px}, not {colours['frame']}")
            if colours["text"] not in found:
                problems.append(f"colours {name}: the text colour {colours['text']} is not in the lines")
        # Custom colours, drawn; an invalid half or an unknown name falls back to red-green.
        custom = looks.look_from_config(_Cfg(colours="custom", custom_frame="#FFFFFF",
                                             custom_text="#ffd400"))
        if custom != {"frame": "#ffffff", "text": "#ffd400"}:
            problems.append(f"colours custom: resolves to {custom!r}")
        frame_px, found = seen(custom)
        if frame_px != "#ffffff" or "#ffd400" not in found:
            problems.append(f"colours custom: frame {frame_px}, text found: {'#ffd400' in found}")
        red_green = looks.FIXED["red-green"]
        if looks.look_from_config(_Cfg(colours="nonsense")) != red_green \
                or looks.look_from_config(_Cfg()) != red_green:
            problems.append("colours: an unknown name did not fall back to red-green")
        half = looks.look_from_config(_Cfg(colours="custom", custom_frame="red", custom_text="#ffd400"))
        if half != {"frame": red_green["frame"], "text": "#ffd400"}:
            problems.append(f"colours custom: an invalid frame resolved to {half!r}")
        if seen({"frame": "nonsense", "text": "#12"})[0] != red_green["frame"]:
            problems.append("look: invalid colours did not fall back to red-green when drawn")
        if drawing.merge_look({"frame": 5, "text": None}) != red_green:
            problems.append("look: values that are not text did not fall back")
        # The helper must not pull in the tracker's themes (their import writes a glyph cache).
        import subprocess

        probe = subprocess.run([sys.executable, "-c",
                                "import ui.overlay.helper, sys; print('ui.theme' in sys.modules)"],
                               cwd=pathlib.Path(__file__).resolve().parent.parent,
                               capture_output=True, text=True, timeout=60)
        if probe.stdout.strip() != "False":
            problems.append(f"helper: importing it pulls in ui.theme ({probe.stdout!r} {probe.stderr[-200:]!r})")

        # -- the lines under a card come from the card's own words ------------------
        both = S.Reward(position=1, raw_text="x", part_unique="/P", name="P", item_name="I",
                        ducats=45, tier=S.Tier.MISSING_FARMABLE, owned_count=0,
                        set_owned=3, set_total=4)
        got = overlay_lines(both)
        if len(got) != 3 or got[:2] != ["45 ducats", "not mastered yet"] \
                or "MISSING" not in got[-1] or "COMPLETES SET" not in got[-1]:
            problems.append(f"overlay lines: {got!r}")
        owned = S.Reward(position=1, raw_text="x", part_unique="/P", name="P", item_name="I",
                         ducats=45, tier=S.Tier.OWNED, owned_count=1, set_owned=1, set_total=4)
        got = overlay_lines(owned)
        if sum("ducats" in line for line in got) != 1 or got[0] != "45 ducats":
            problems.append(f"overlay lines: the ducats appear twice or not first: {got!r}")
        if overlay_lines(S.Reward(position=2, raw_text="Forma Blueprint")) != ["FORMA"]:
            problems.append("overlay lines: Forma did not give FORMA")
        if overlay_lines(S.Reward(position=3, raw_text="???")) != []:
            problems.append("overlay lines: an unrecognised card got lines")

        # -- the header check ---------------------------------------------------------
        def strip(name):
            return cv2.imread(str(fixtures / "header" / f"{name}.png"))

        inventory = cv2.imread(str(fixtures / "inventory" / "4k_a.png"))
        if inventory is None:  # the inventory pictures are local only (fixtures/.gitignore)
            inventory = numpy.full((2160, 3840, 3), 90, numpy.uint8)
        for theme_name, first, second in (("blue", "blue_a", "blue_b"),
                                          ("light", "light_a", "light_b")):
            a, b = strip(first), strip(second)
            mask = header.reference_mask(_header_window(a))
            if mask is None:
                problems.append(f"header ({theme_name}): the picture of the reading has no header")
                continue
            if not header.is_present(mask, a):
                problems.append(f"header ({theme_name}): not there in the same picture")
            share = header.share_present(mask, b)
            if share < header.PRESENT_SHARE:
                problems.append(f"header ({theme_name}): not there over another background "
                                f"({share:.2f})")
            # Another background under the same word: the word's surroundings
            # taken from the other picture and brightened.
            wide = cv2.dilate(mask.astype(numpy.uint8), numpy.ones((3, 3), numpy.uint8)).astype(bool)
            moved = numpy.where(wide[..., None], a, cv2.convertScaleAbs(b, alpha=1.3, beta=25))
            if not header.is_present(mask, moved):
                problems.append(f"header ({theme_name}): not there with the background changed")
            # Half the size (1080p) of the same strip.
            half = cv2.resize(a, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
            if not header.is_present(mask, half):
                problems.append(f"header ({theme_name}): not there at half the size "
                                f"({header.share_present(mask, half):.2f})")
            # The word gone: its pixels painted over with the surrounding colour.
            gone_strip = a.copy()
            gone_strip[wide] = numpy.median(a.reshape(-1, 3), axis=0).astype(numpy.uint8)
            if header.is_present(mask, gone_strip) or header.share_present(mask, gone_strip) > 0.2:
                problems.append(f"header ({theme_name}): still there without the word "
                                f"({header.share_present(mask, gone_strip):.2f})")
            # Counter-probes: an inventory screen, and this strip taken from the
            # wrong place of the reward screen's own window.
            if header.share_present(mask, header.crop(inventory)) > 0.2:
                problems.append(f"header ({theme_name}): an inventory screen counts as the header")
            shifted = header.crop(_header_window(a), (0.57, 0.040, 0.115, 0.050))
            if header.is_present(mask, shifted):
                problems.append(f"header ({theme_name}): the wrong strip of the same window counts")
        if header.reference_mask(inventory) is not None:
            problems.append("header: an inventory screen has a reference mask")

        # -- the lifetime -------------------------------------------------------------
        life = R.Lifetime(0.0)
        seq = [life.keep(present, t) for present, t in
               ((True, 0.5), (True, 1.0), (False, 1.5), (False, 2.0))]
        if seq != [True, True, True, False] or life.reason != "header":
            problems.append(f"lifetime: present, present, gone, gone gave {seq} ({life.reason})")
        life = R.Lifetime(0.0)
        seq = [life.keep(p, t) for p, t in ((False, 0.5), (True, 1.0), (False, 1.5), (True, 2.0))]
        if seq != [True] * 4:
            problems.append(f"lifetime: a single miss ended it: {seq}")
        life = R.Lifetime(0.0)
        if not life.keep(True, 14.9) or life.keep(True, 15.0) or life.reason != "time":
            problems.append("lifetime: 15 seconds is not the limit")
        life = R.Lifetime(10.0)
        if not life.keep(None, 24.9) or life.keep(None, 25.0):
            problems.append("lifetime: the limit does not count from the reading")

        # -- the overlay with a fake client, clock and grab ----------------------------
        from wft import config as config_mod

        cfg = config_mod.load()
        fake = _FakeOverlayClient()
        clock = [100.0]
        grabbed = []
        answers = []

        def fake_grab(_placement):
            grabbed.append(clock[0])
            if not answers:
                raise RuntimeError("window gone")
            return answers.pop(0)

        overlay = R.RewardOverlay(cfg, fake, grab=fake_grab, clock=lambda: clock[0])
        where = cap.Placement(7, 100, 50, 1920, 1080)
        a, b = strip("blue_a"), strip("blue_b")
        gone = a.copy()
        gone[:] = numpy.median(a.reshape(-1, 3), axis=0).astype(numpy.uint8)
        mask = header.reference_mask(_header_window(a))
        args = {"tiles": _OVERLAY_TILES, "best": 1, "headline": None,
                "lines": [["a"]] * 4}
        events = []
        overlay.shown.connect(lambda t: events.append(("shown", t)))
        overlay.unavailable.connect(lambda t: events.append(("unavailable", t)))
        overlay.ended.connect(lambda r: events.append(("ended", r)))

        def confirm(token):
            fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                    "token": token})

        token = overlay.show(args, where, mask, started=100.0)
        sent = fake.sent[-1]
        if (sent.get("cmd") != "reward_show"
                or sent.get("window") != {"x": 100, "y": 50, "w": 1920, "h": 1080}
                or sent.get("token") != token or sent.get("best") != 1 or "picked" not in sent):
            problems.append(f"reward overlay: the command is {sent!r}")
        overlay.tick()
        if grabbed:
            problems.append("reward overlay: the header was looked at before the helper confirmed")
        confirm(token)
        if events != [("shown", token)] or not overlay._poll.isActive():
            problems.append(f"reward overlay: confirmation gave {events!r}")
        # present, present, gone, gone -> hidden
        answers[:] = [b, a, gone, gone]
        for _ in range(3):
            clock[0] += 0.5
            overlay.tick()
        if not overlay.active or fake.sent[-1].get("cmd") == "reward_hide":
            problems.append("reward overlay: gone once ended it")
        clock[0] += 0.5
        overlay.tick()
        if overlay.active or fake.sent[-1] != {"cmd": "reward_hide"} or events[-1] != ("ended", "header"):
            problems.append(f"reward overlay: present, present, gone, gone: {events[-1]!r}, "
                            f"{fake.sent[-1]!r}")
        # always there: ends at 15 s after the reading
        events.clear()
        clock[0] = 200.0
        token = overlay.show(args, where, mask, started=200.0)
        confirm(token)
        answers[:] = [a] * 40
        for _ in range(28):
            clock[0] += 0.5
            overlay.tick()
        if not overlay.active:
            problems.append("reward overlay: ended before 15 seconds while the header was there")
        clock[0] += 1.0
        overlay.tick()
        if overlay.active or events[-1] != ("ended", "time"):
            problems.append(f"reward overlay: still up after 15 seconds: {events!r}")
        # a window that cannot be read counts as gone
        clock[0] = 300.0
        token = overlay.show(args, where, mask, started=300.0)
        confirm(token)
        answers.clear()
        clock[0] += 0.5
        overlay.tick()
        clock[0] += 0.5
        overlay.tick()
        if overlay.active:
            problems.append("reward overlay: a window that cannot be grabbed kept it up")
        # a picture without a header: the time alone
        clock[0] = 400.0
        grabbed.clear()
        token = overlay.show(args, where, None, started=400.0)
        confirm(token)
        clock[0] += 14.0
        overlay.tick()
        if grabbed or not overlay.active:
            problems.append("reward overlay: without a header mask the window was grabbed "
                            "or it ended early")
        clock[0] += 1.5
        overlay.tick()
        if overlay.active:
            problems.append("reward overlay: without a header mask 15 seconds did not end it")
        # a new reading replaces the old overlay
        events.clear()
        first = overlay.show(args, where, mask, started=clock[0])
        second = overlay.show(args, where, mask, started=clock[0])
        if second == first or ("ended", "replaced") not in events or fake.sent[-1].get("token") != second:
            problems.append(f"reward overlay: a new reading did not replace the old one: {events!r}")
        confirm(first)
        if ("shown", second) in events or ("shown", first) in events \
                or overlay._pending != second:
            problems.append(f"reward overlay: the confirmation of the replaced overlay was "
                            f"taken: {events!r}")
        # the test button's overlay: 5 seconds, no comparison
        overlay.hide()
        grabbed.clear()
        clock[0] = 500.0
        token = overlay.show(args, where, None, None, seconds=R.TEST_SECONDS)
        confirm(token)
        clock[0] += 4.9
        overlay.tick()
        still = overlay.active
        clock[0] += 0.2
        overlay.tick()
        if not still or overlay.active or grabbed:
            problems.append(f"reward overlay: test overlay: up at 4.9 s {still}, up at 5.1 s "
                            f"{overlay.active}, grabbed {len(grabbed)}")
        # no confirmation in time, and a helper that fails: the reading is unavailable
        events.clear()
        overlay.confirm_ms = 30
        token = overlay.show(args, where, mask, started=clock[0])
        if not _wait(app, lambda: ("unavailable", token) in events, 2.0):
            problems.append("reward overlay: a helper that never answered was not given up on")
        events.clear()
        overlay.confirm_ms = 3000
        token = overlay.show(args, where, mask, started=clock[0])
        fake.failed.emit("The overlay helper stopped: boom")
        if ("unavailable", token) not in events or "boom" not in overlay.last_error:
            problems.append(f"reward overlay: a failing helper gave {events!r}")
        events.clear()
        token = overlay.show(args, where, mask, started=clock[0])
        fake.stopped.emit()
        if ("unavailable", token) not in events:
            problems.append("reward overlay: a helper that went away gave no 'unavailable'")
        overlay.hide()

        # -- the helper's side (offscreen, no process) ----------------------------------
        messages = []
        window = helper.PaletteWindow()
        controller = helper.Controller(window, messages.append)
        command = R.show_command({"x": 10, "y": 20, "w": 800, "h": 450}, _OVERLAY_TILES, 2, None,
                                 [["a"], ["b"], ["c"], ["d"]], 5, picked=None)
        controller.handle(command)
        reward = controller.rewards
        shown = [m for m in messages if m.get("type") == "reward"]
        if (not shown or shown[-1].get("shown") is not True or shown[-1].get("token") != 5
                or shown[-1].get("reason") != "show"):
            problems.append(f"reward helper: reward_show answered {shown!r}")
        g = reward.geometry()
        if (g.x(), g.y(), g.width(), g.height()) != (10, 20, 800, 450) or not reward.isVisible():
            problems.append(f"reward helper: window at {g.x(), g.y(), g.width(), g.height()}, "
                            f"visible {reward.isVisible()}")
        flags = reward.windowFlags()
        for flag, label in ((Qt.WindowTransparentForInput, "transparent for input"),
                            (Qt.WindowDoesNotAcceptFocus, "no focus"),
                            (Qt.WindowStaysOnTopHint, "stays on top"),
                            (Qt.X11BypassWindowManagerHint, "bypasses the window manager")):
            if not flags & flag:
                problems.append(f"reward helper: the window is not {label}")
        if reward.best != 2 or reward.picked is not None:
            problems.append(f"reward helper: best {reward.best}, picked {reward.picked}")
        controller.handle({"cmd": "reward_pick", "picked": 1})
        if reward.picked != 1 or messages[-1].get("picked") != 1 or messages[-1].get("reason") != "pick":
            problems.append(f"reward helper: reward_pick answered {messages[-1]!r}")
        controller.handle({"cmd": "reward_pick", "picked": 9})
        if reward.picked is not None:
            problems.append("reward helper: a card index out of range was kept")
        if reward.grab().toImage().width() != 800:
            problems.append("reward helper: the window does not paint at its size")
        controller.handle({"cmd": "reward_hide"})
        if reward.isVisible() or messages[-1].get("shown") is not False \
                or messages[-1].get("reason") != "hide":
            problems.append(f"reward helper: reward_hide answered {messages[-1]!r}")
        controller.handle({"cmd": "reward_show", "window": {"x": 0, "y": 0, "w": 0, "h": 0}})
        if messages[-1].get("type") != "error":
            problems.append("reward helper: a window without a size was accepted")
        reward.hide_rewards()
        reward.deleteLater()
        window.deleteLater()

        # -- the arguments of a reading ---------------------------------------------------
        good = _trigger_result(["Gauss Prime Blueprint", "b", "c", "d"], 4)
        got = overlay_args(good)
        if (not got or got["best"] is None or got["headline"] is not None
                or len(got["tiles"]) != 4 or len(got["lines"]) != 4):
            problems.append(f"overlay args: a reading with a card to take gave {got!r}")
        nothing = overlay_args(_trigger_result(["Forma Blueprint"] * 4, 0))
        if not nothing or nothing["best"] is not None or nothing["headline"] != "NOTHING WORTH TAKING":
            problems.append(f"overlay args: nothing to take gave {nothing!r}")
        if overlay_args(_trigger_result(["a"] * 4, 0)) is not None \
                or overlay_args(TriggerResult(None, "boom", 0)) is not None:
            problems.append("overlay args: a bad or failed reading got an overlay")

        # -- the notification gives way to the overlay --------------------------------------
        sent_notes: list = []
        notify.set_sender(lambda summary, body="": sent_notes.append((summary, body)))
        cfg = config_mod.load()
        fake = _FakeOverlayClient()
        overlay = R.RewardOverlay(cfg, fake, clock=lambda: 1.0)
        overlay.confirm_ms = 60
        panel = RelicPanel(cfg)
        pages.append(panel)
        panel.set_overlay(overlay)
        located = cap.Placement(7, 0, 0, 3840, 2160)

        def reading(result, how=None):
            sent_notes.clear()
            fake.sent.clear()
            result.placement = located
            panel._triggered_at = time.time()
            panel._on_finished(result)
            if how == "shown":
                confirm_on(fake, fake.sent[-1]["token"])
            elif how == "failed":
                fake.failed.emit("The overlay helper stopped: boom")
            elif how == "late":
                _wait(app, lambda: bool(sent_notes), 2.0)
            _settle(app)
            return list(sent_notes), [c.get("cmd") for c in fake.sent]

        def confirm_on(client, token):
            client.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                      "token": token})

        take4 = ["Gauss Prime Blueprint", "b", "c", "d"]
        notes, cmds = reading(_trigger_result(take4, 4), "shown")
        _wait(app, lambda: False, 0.2)
        if notes or cmds != ["reward_show"]:
            problems.append(f"overlay + notify: shown overlay gave notes {notes!r}, commands {cmds!r}")
        if sent_notes:
            problems.append(f"overlay + notify: a note followed the shown overlay: {sent_notes!r}")
        notes, cmds = reading(_trigger_result(take4, 4), "failed")
        if len(notes) != 1 or notes[0][0] != panel.verdict_banner.text():
            problems.append(f"overlay + notify: a failed helper gave notes {notes!r}")
        notes, cmds = reading(_trigger_result(take4, 4), "late")
        if len(notes) != 1:
            problems.append(f"overlay + notify: a helper that did not answer gave notes {notes!r}")
        notes, cmds = reading(_trigger_result(["Forma Blueprint"] * 4, 0), "shown")
        if notes or cmds != ["reward_show"]:
            problems.append(f"overlay + notify: nothing-to-take overlay gave {notes!r}, {cmds!r}")
        cfg.override("overlay.rewards", False)
        notes, cmds = reading(_trigger_result(take4, 4))
        if len(notes) != 1 or "reward_show" in cmds:
            problems.append(f"overlay + notify: a switched-off overlay gave {notes!r}, {cmds!r}")
        cfg.override("overlay.rewards", True)
        notes, cmds = reading(_trigger_result(["a"] * 4, 0))
        if notes != [("Could not read the reward screen",
                      "Open Warframe Tracker to see what happened.")] or "reward_show" in cmds:
            problems.append(f"overlay + notify: a bad reading gave {notes!r}, {cmds!r}")
        notes, cmds = reading(TriggerResult(None, "Game window not found", 0))
        if len(notes) != 1 or "reward_show" in cmds:
            problems.append(f"overlay + notify: a failed reading gave {notes!r}, {cmds!r}")
        no_place = _trigger_result(take4, 4)
        sent_notes.clear()
        fake.sent.clear()
        no_place.placement = None
        panel._on_finished(no_place)
        _settle(app)
        if len(sent_notes) != 1 or fake.sent:
            problems.append("overlay + notify: a reading without a window position tried an overlay")

        # -- the Overlays page ---------------------------------------------------------------
        fake = _FakeOverlayClient()
        overlay = R.RewardOverlay(cfg, fake, clock=lambda: 1.0)
        page = OverlaysView(cfg, fake, rewards=overlay)
        pages.append(page)
        page.resize(900, 900)
        page.show()
        _settle(app)
        last_screen.forget()
        page.refresh()
        if page.test_btn.isEnabled() or "No reward screen" not in page.test_reason.text():
            problems.append(f"overlays page: test button with no reading: enabled "
                            f"{page.test_btn.isEnabled()}, {page.test_reason.text()!r}")
        shot = numpy.zeros((108, 192, 3), numpy.uint8)
        last_screen.remember(shot.copy(), _trigger_result(take4, 4))
        page.refresh()
        if page.test_btn.isEnabled() or "game window" not in page.test_reason.text():
            problems.append(f"overlays page: test button without a game window: "
                            f"{page.test_reason.text()!r}")
        overlay.finder = lambda: located
        page.refresh()
        if not page.test_btn.isEnabled():
            problems.append("overlays page: the test button is off with a reading and a window")
        page.test_btn.click()
        _settle(app)
        sent = fake.sent[-1] if fake.sent else {}
        if sent.get("cmd") != "reward_show" or sent.get("window") != located.as_rect() \
                or overlay._life is None or overlay._life.max_seconds != 5.0 \
                or overlay._mask is not None:
            problems.append(f"overlays page: the test button sent {sent!r}")
        overlay.hide()
        last_screen.remember(shot.copy(), _trigger_result(["a"] * 4, 0))
        page.refresh()
        if page.test_btn.isEnabled():
            problems.append("overlays page: the test button is on for a reading with nothing to show")
        # The switch is saved, applies at once.
        page.rewards_box.setChecked(False)
        written = settings_io.target_config_path().read_text()
        if "rewards = false" not in written or cfg.get("overlay.rewards") is not False:
            problems.append("overlays page: the switch was not saved to the file and the config")
        page.rewards_box.setChecked(True)
        if cfg.get("overlay.rewards") is not True:
            problems.append("overlays page: switching on again did not apply")
        # The colours list: shows the setting, saves a choice at once, and the next
        # `reward_show` carries the resolved colours to the helper, which draws with them.
        from ui.overlay import looks as _looks
        from ui import theme as _theme

        want_entries = 2 + len(_looks.theme_presets()) + 1
        if page.colours_box.currentData() != "red-green" or page.colours_box.count() != want_entries:
            problems.append(f"overlays page: colours list shows {page.colours_box.currentData()!r} "
                            f"with {page.colours_box.count()} entries, expected {want_entries}")
        if [page.colours_box.itemData(i) for i in range(page.colours_box.count())][-1] != "custom" \
                or page.colours_box.findData("Light") != -1 or page.colours_box.findData("Orokin") < 0:
            problems.append("overlays page: the colours list has the wrong entries")
        if not all(widget.isHidden() for widget in (*page.custom_edits.values(),
                                                    *page.custom_swatches.values())):
            problems.append("overlays page: the custom colour fields show for a preset")
        page.colours_box.setCurrentIndex(page.colours_box.findData("blue-orange"))
        written = settings_io.target_config_path().read_text()
        if 'colours = "blue-orange"' not in written or cfg.get("overlay.colours") != "blue-orange":
            problems.append("overlays page: the colours choice was not saved to the file and the config")
        overlay.finder = lambda: located
        fake.sent.clear()
        overlay.show(dict(args), located, None)
        shown_look = (fake.sent[-1] if fake.sent else {}).get("look")
        if shown_look != {"frame": "#e69f00", "text": "#56b4e9"}:
            problems.append(f"overlays page: reward_show carried the look {shown_look!r}")
        overlay.hide()
        # A theme preset and the custom colours, through the page.
        page.colours_box.setCurrentIndex(page.colours_box.findData("Orokin"))
        overlay.show(dict(args), located, None)
        if (fake.sent[-1].get("look") or {}) != _looks.presets()["Orokin"] \
                or 'colours = "Orokin"' not in settings_io.target_config_path().read_text():
            problems.append(f"overlays page: the Orokin preset reached the command as "
                            f"{fake.sent[-1].get('look')!r}")
        overlay.hide()
        page.colours_box.setCurrentIndex(page.colours_box.findData("custom"))
        if any(widget.isHidden() for widget in (*page.custom_edits.values(),
                                                *page.custom_swatches.values())):
            problems.append("overlays page: the custom colour fields are hidden for Custom colours")
        page.custom_edits["frame"].setText("#FFFFFF")
        page.custom_edits["frame"].editingFinished.emit()
        page.custom_edits["text"].setText("#ffd400")
        page.custom_edits["text"].editingFinished.emit()
        written = settings_io.target_config_path().read_text()
        if 'custom_frame = "#ffffff"' not in written or 'custom_text = "#ffd400"' not in written \
                or cfg.get("overlay.custom_frame") != "#ffffff" or page.custom_hint.text():
            problems.append(f"overlays page: custom colours not saved normalised "
                            f"({cfg.get('overlay.custom_frame')!r}, hint {page.custom_hint.text()!r})")
        overlay.show(dict(args), located, None)
        if (fake.sent[-1].get("look") or {}) != {"frame": "#ffffff", "text": "#ffd400"}:
            problems.append(f"overlays page: custom colours reached the command as "
                            f"{fake.sent[-1].get('look')!r}")
        overlay.hide()
        # An invalid value is not saved and the hint says so in red; the old value stays.
        page.custom_edits["frame"].setText("not a colour")
        page.custom_edits["frame"].editingFinished.emit()
        if cfg.get("overlay.custom_frame") != "#ffffff" \
                or "Use a colour like #ff2a2a." not in page.custom_hint.text() \
                or _theme.BAD not in page.custom_hint.styleSheet():
            problems.append(f"overlays page: an invalid colour was saved or not answered "
                            f"({cfg.get('overlay.custom_frame')!r}, {page.custom_hint.text()!r})")
        page.custom_edits["frame"].setText("#ffffff")
        page.custom_edits["frame"].editingFinished.emit()
        # A text colour that is hard to read is saved, with a warning.
        page.custom_edits["text"].setText("#222222")
        page.custom_edits["text"].editingFinished.emit()
        if cfg.get("overlay.custom_text") != "#222222" \
                or 'custom_text = "#222222"' not in settings_io.target_config_path().read_text() \
                or "Hard to read on a light picture." not in page.custom_hint.text():
            problems.append(f"overlays page: a low-contrast text colour: saved "
                            f"{cfg.get('overlay.custom_text')!r}, hint {page.custom_hint.text()!r}")
        page.custom_edits["text"].setText("#ffd400")
        page.custom_edits["text"].editingFinished.emit()
        # The checkbox that switches the drawing off switches the colour controls off.
        page.rewards_box.setChecked(False)
        controls = (page.colours_box, *page.custom_edits.values(), *page.custom_swatches.values())
        if any(widget.isEnabled() for widget in controls):
            problems.append("overlays page: the colour controls stay on with the overlay off")
        page.rewards_box.setChecked(True)
        if not all(widget.isEnabled() for widget in controls):
            problems.append("overlays page: the colour controls stay off with the overlay on")
        # The helper takes the colours; invalid ones fall back to red-green.
        helper_window = helper.RewardWindow()
        helper_window.show_rewards({"window": {"x": 0, "y": 0, "w": 800, "h": 450},
                                    "tiles": _OVERLAY_TILES,
                                    "look": {"frame": "#E69F00", "text": "#56b4e9"}})
        if helper_window.look != {"frame": "#e69f00", "text": "#56b4e9"}:
            problems.append(f"reward helper: look is {helper_window.look!r}")
        helper_window.show_rewards({"window": {"x": 0, "y": 0, "w": 800, "h": 450},
                                    "tiles": _OVERLAY_TILES,
                                    "look": {"frame": "oops", "text": "#56b4e9"}})
        if helper_window.look != {"frame": "#ff2a2a", "text": "#56b4e9"}:
            problems.append(f"reward helper: an invalid frame colour gave {helper_window.look!r}")
        helper_window.hide_rewards()
        page.colours_box.setCurrentIndex(page.colours_box.findData("red-green"))
        # A fresh page shows the saved custom colours even while a preset is chosen
        # (they are what "Custom colours" draws with), and warns about a saved
        # low-contrast text as soon as Custom is shown.
        again = OverlaysView(cfg, fake, rewards=overlay)
        shown = {key: edit.text() for key, edit in again.custom_edits.items()}
        if shown != _looks.custom_from_config(cfg) or shown != {"frame": "#ffffff", "text": "#ffd400"}:
            problems.append(f"overlays page: a fresh page shows the custom colours {shown!r}, "
                            f"saved are {_looks.custom_from_config(cfg)!r}")
        settings_io.save_value(cfg, "overlay", "custom_text", "#222222")
        settings_io.save_value(cfg, "overlay", "colours", "custom")
        again = OverlaysView(cfg, fake, rewards=overlay)
        if "Hard to read on a light picture." not in again.custom_hint.text() \
                or again.custom_hint.isHidden():
            problems.append("overlays page: a saved low-contrast text gives no warning on a fresh page")
        settings_io.save_value(cfg, "overlay", "custom_text", "#ffd400")
        settings_io.save_value(cfg, "overlay", "colours", "red-green")
        # Failure text.
        overlay.last_error = "The overlay helper stopped: boom"
        overlay.unavailable.emit(1)
        if "boom" not in page.reward_error.text() or page.reward_error.isHidden():
            problems.append("overlays page: a failure of the helper is not shown "
                            "for the reward overlay")

        # -- the window's position ------------------------------------------------------------
        if cap.Placement(7, 1, 2, 3, 4).as_rect() != {"x": 1, "y": 2, "w": 3, "h": 4}:
            problems.append("placement: as_rect is wrong")
        if len(cap._x11.XTranslateCoordinates.argtypes) != 8:
            problems.append("placement: XTranslateCoordinates is not declared")

        def no_display():
            raise cap.CaptureError("no display")

        patches.set(cap, "_open_display", no_display)
        try:
            cap.placement(cap.Window(7, 100, 100, "", ""))
            problems.append("placement: no X display did not raise")
        except cap.CaptureError:
            pass
    except Exception as exc:  # noqa: BLE001
        import traceback

        problems.append(f"reward overlay: raised {type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=3))
    finally:
        patches.restore()
        notify.set_sender(lambda _summary, _body="": None)
        last_screen.forget()
        for widget in pages:
            widget.close()
            widget.deleteLater()
    print("reward overlay    : frame (inner edge = card edge), TAKE THIS, lines in a dark box "
          "(under the names, 2/3/4 cards, clear of 4 name rows and the Endless line), colour "
          "presets, themes and custom colours, tick; header checked over other "
          "backgrounds and themes; lifetime; helper commands; notification gives way; "
          "page. Not tested: the X server's answer for the window position and a window "
          "on a real screen")


def _check_click_to_pick(app, problems: list) -> None:
    """The card clicked in the game is the pick.

    Pointer position to card at 2, 3, 4 cards and with the game window on the
    second monitor; the last click counts and every click moves the tick; one
    booking when the overlay ends, through the "I took this" function and no
    other; nothing without a click, in the test mode, after a booking by button;
    a replaced reading books the old choice first. The worker reads clicks only
    while armed and with the game focused, from fake devices (pipes), and
    the hotkey's focus gate is the one both use. No real device, no X server.
    """
    import subprocess
    import time
    from types import SimpleNamespace

    from ui.overlay import rewards as R
    from ui.views.overlays_view import OverlaysView
    from ui.views.relic_panel import RelicPanel, overlay_args
    from ui.workers import hotkey as hotkey_mod
    from ui.workers.pick_click import PickClickWorker
    from wft import config as config_mod
    from wft.capture import keys, window as cap
    from wft.data import db as dbq
    from wft.relic import regions

    patches = _Patches()
    widgets = []
    worker = None
    mouse = None
    try:
        # -- pointer position to card ---------------------------------------------------
        for count in (2, 3, 4):
            tiles = [regions.tile(i, count).as_tuple() for i in range(count)]
            for where in (cap.Placement(1, 0, 0, 3840, 2160),
                          cap.Placement(1, 3840, 0, 1920, 1080),
                          cap.Placement(1, -1920, 300, 1920, 1080)):
                for index, (tx, ty, tw, th) in enumerate(tiles):
                    px = where.x + (tx + tw / 2) * where.width
                    py = where.y + (ty + th / 2) * where.height
                    got = R.card_at(px, py, where, tiles)
                    if got != index:
                        problems.append(f"click: {count} cards, window at {where.x}: "
                                        f"centre of card {index} gave {got}")
                    # The same point on the desktop but as if the window were elsewhere.
                    if R.card_at(px + where.width, py, where, tiles) is not None \
                            and index == count - 1:
                        problems.append(f"click: {count} cards: right of the last card counts")
                above = R.card_at(where.x + 0.5 * where.width, where.y + 0.05 * where.height,
                                  where, tiles)
                below = R.card_at(where.x + 0.5 * where.width, where.y + 0.9 * where.height,
                                  where, tiles)
                left = R.card_at(where.x - 5, where.y + 0.3 * where.height, where, tiles)
                if above is not None or below is not None or left is not None:
                    problems.append(f"click: {count} cards: outside a card gave "
                                    f"{above}, {below}, {left}")
        spaced = [(0.1, 0.2, 0.1, 0.2), (0.4, 0.2, 0.1, 0.2)]
        flat = cap.Placement(1, 0, 0, 1000, 1000)
        if R.card_at(300, 300, flat, spaced) is not None or R.card_at(250, 300, flat, spaced) is not None:
            problems.append("click: a point between two cards counts")
        if R.card_at(150, 300, flat, spaced) != 0 or R.card_at(450, 300, flat, spaced) != 1:
            problems.append("click: a point on a spaced card is not found")
        if R.card_at(5, 5, cap.Placement(1, 0, 0, 0, 0), spaced) is not None:
            problems.append("click: a window with no size gave a card")

        # -- the overlay and the page, over a fake client --------------------------------
        cfg = _setup_cfg(data=True, key="")
        cfg.override("overlay.rewards", True)
        cfg.override("overlay.click_to_pick", True)
        fake = _FakeOverlayClient()
        overlay = R.RewardOverlay(cfg, fake, clock=lambda: 1.0)
        panel = RelicPanel(cfg)
        widgets.append(panel)
        panel.set_overlay(overlay)
        located = cap.Placement(7, 3840, 0, 3840, 2160)
        settled = []
        overlay.settling.connect(lambda token, index: settled.append((token, index)))
        booked: list = []
        real_confirm = panel._confirm_pick

        def recording(index):
            booked.append((index, panel._last_result))
            real_confirm(index)

        patches.set(panel, "_confirm_pick", recording)
        take4 = ["Gauss Prime Blueprint", "b", "c", "d"]

        def card_point(index):
            tx, ty, tw, th = _OVERLAY_TILES[index]
            return (located.x + (tx + tw / 2) * located.width,
                    located.y + (ty + th / 2) * located.height)

        screens = [1000.0]

        def reading(texts=take4, named=4):
            result = _trigger_result(texts, named)
            result.placement = located
            # Every call is another reward screen, because a second reading of
            # one screen is read as the same booking: 100 s apart.
            screens[0] += 100.0
            result.read_at = screens[0]
            panel._triggered_at = time.time()
            fake.sent.clear()
            panel._on_finished(result)
            token = fake.sent[-1]["token"]
            fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                    "token": token})
            return result

        def picks():
            return [c.get("picked") for c in fake.sent if c.get("cmd") == "reward_pick"]

        # The tools the reading uses are the ones the overlay's tiles come from.
        first = reading()
        if not overlay.arming.is_set():
            problems.append("click: the overlay is up and clicks are not armed")
        overlay.on_click(*card_point(1))
        overlay.on_click(*card_point(3))
        overlay.on_click(located.x + 0.5 * located.width, located.y + 0.8 * located.height)
        overlay.on_click(located.x - 100, located.y + 300)
        if picks() != [1, 3] or overlay.picked != 3:
            problems.append(f"click: ticks sent {picks()!r}, picked {overlay.picked!r} "
                            "(the last click on a card counts, a click elsewhere changes nothing)")
        if booked:
            problems.append("click: booked before the overlay ended")
        overlay.hide()
        if len(booked) != 1 or booked[0][0] != 3 or booked[0][1] is not first or settled != [(settled[0][0], 3)]:
            problems.append(f"click: ending the overlay booked {[b[0] for b in booked]!r}")
        if overlay.arming.is_set():
            problems.append("click: still armed after the overlay ended")
        connection = dbq.connect(cfg.db_path)
        try:
            counts = [dbq.owned_count(connection, f"/Test/{i}") for i in range(4)]
        finally:
            connection.close()
        if counts != [0, 0, 0, 1] or panel._recorded is None:
            problems.append(f"click: the booking left counts {counts!r} (recorded {panel._recorded!r})")
        overlay.hide()
        overlay.settle_now()
        if len(booked) != 1:
            problems.append("click: booked twice for one reading")
        # Undo is the page's own: the same take-back as after the button.
        connection = dbq.connect(cfg.db_path)
        try:
            panel._take_back(connection)
            connection.commit()
            if dbq.owned_count(connection, "/Test/3") != 0:
                problems.append("click: the booking cannot be taken back like the button's")
        finally:
            connection.close()

        # Nothing without a click.
        booked.clear()
        reading()
        overlay.on_click(located.x + 5, located.y + 5)
        overlay.hide()
        if booked:
            problems.append("click: a click outside every card booked something")
        reading()
        overlay.hide()
        if booked:
            problems.append("click: no click booked something")

        # A booking by the button comes first: the end books nothing more.
        reading()
        overlay.on_click(*card_point(1))
        panel.cards[2].taken.emit(3)
        overlay.on_click(*card_point(0))
        overlay.hide()
        if [b[0] for b in booked] != [2]:
            problems.append(f"click: after the button the booking list is {[b[0] for b in booked]!r}")

        # A new reading books the open choice of the old one first.
        booked.clear()
        old = reading()
        overlay.on_click(*card_point(2))
        new = reading()
        if len(booked) != 1 or booked[0][0] != 2 or booked[0][1] is not old:
            problems.append(f"click: a replacing reading booked {[(b[0], b[1] is old) for b in booked]!r}")
        if overlay.picked is not None or panel._answered:
            problems.append("click: the new reading inherited the old choice or counts as answered")
        overlay.hide()
        if len(booked) != 1:
            problems.append("click: the new reading booked without a click")

        # A failed reading books the choice of the one before.
        booked.clear()
        reading()
        overlay.on_click(*card_point(0))
        panel._on_failed("boom")
        if [b[0] for b in booked] != [0]:
            problems.append(f"click: a failed reading booked {[b[0] for b in booked]!r}")

        # The lifetime end (15 s) books the same way.
        booked.clear()
        clock = [1.0]
        timed = R.RewardOverlay(cfg, fake, clock=lambda: clock[0])
        panel.set_overlay(timed)
        result = _trigger_result(take4, 4)
        result.placement = located
        panel._triggered_at = time.time()
        fake.sent.clear()
        panel._on_finished(result)
        fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                "token": fake.sent[-1]["token"]})
        timed.on_click(*card_point(1))
        clock[0] = 100.0
        timed.tick()
        if [b[0] for b in booked] != [1] or timed.active:
            problems.append(f"click: the 15 seconds ended it with {[b[0] for b in booked]!r}")
        panel.set_overlay(overlay)

        # The test mode ticks and never books.
        booked.clear()
        settled.clear()
        token = overlay.show(overlay_args(_trigger_result(take4, 4)), located, None, None,
                             seconds=R.TEST_SECONDS, test=True)
        fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show", "token": token})
        fake.sent.clear()
        overlay.on_click(*card_point(2))
        if picks() != [2]:
            problems.append(f"click: the test overlay did not tick, sent {picks()!r}")
        overlay.hide()
        if booked or settled:
            problems.append("click: the test overlay booked")

        # The switch: off means not armed, on again arms the running overlay.
        cfg.override("overlay.click_to_pick", False)
        reading()
        if overlay.arming.is_set():
            problems.append("click: armed with the switch off")
        cfg.override("overlay.click_to_pick", True)
        overlay.refresh_arming()
        if not overlay.arming.is_set():
            problems.append("click: switching on did not arm the running overlay")
        cfg.override("overlay.click_to_pick", False)
        overlay.refresh_arming()
        if overlay.arming.is_set():
            problems.append("click: switching off did not disarm")
        cfg.override("overlay.click_to_pick", True)
        overlay.hide()
        # Not armed between the show command and the helper's confirmation.
        token = overlay.show(overlay_args(_trigger_result(take4, 4)), located, None, None)
        if overlay.arming.is_set():
            problems.append("click: armed before the helper confirmed the overlay")
        overlay.hide()

        # The Overlays page checkbox.
        page = OverlaysView(cfg, fake, rewards=overlay)
        widgets.append(page)
        page.show()
        _settle(app)
        from ui import settings_io

        page.click_box.setChecked(False)
        written = settings_io.target_config_path().read_text()
        if "click_to_pick = false" not in written or cfg.get("overlay.click_to_pick") is not False:
            problems.append("click: the checkbox was not saved to the file and the config")
        page.click_box.setChecked(True)
        if cfg.get("overlay.click_to_pick") is not True:
            problems.append("click: the checkbox did not apply when switched on")
        page.rewards_box.setChecked(False)
        if page.click_box.isEnabled():
            problems.append("click: the checkbox is not greyed out with the overlay off")
        page.rewards_box.setChecked(True)
        if not page.click_box.isEnabled():
            problems.append("click: the checkbox stays grey with the overlay on")

        # -- the focus gate is shared ----------------------------------------------------
        calls = []

        def fake_run(cmd, **_kw):
            calls.append(cmd[0])
            if cmd[0] == "xdotool":
                return SimpleNamespace(stdout="1234\n")
            return SimpleNamespace(stdout='WM_CLASS(STRING) = "steam_app_230410", "Warframe"\n'
                                          'WM_NAME(STRING) = "Warframe"\n')

        patches.set(hotkey_mod.subprocess, "run", fake_run)
        if not hotkey_mod.FocusGate().matches("warframe"):
            problems.append("focus gate: the game window does not match")
        if hotkey_mod.HotkeyWorker(cfg)._focus_matches("warframe") is not True:
            problems.append("focus gate: the hotkey no longer uses the gate")
        if hotkey_mod.FocusGate().matches("firefox"):
            problems.append("focus gate: another program's name matches the game's window")
        patches.set(hotkey_mod.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=""))
        if hotkey_mod.FocusGate().matches("warframe"):
            problems.append("focus gate: no focused window counts as the game")

        # -- the worker ----------------------------------------------------------------------
        if keys.evdev is None:
            print("click to pick     : worker skipped, python-evdev is not installed")
            return
        ecodes = keys.ecodes

        class Mouse(_FakeKeyboard):
            closed = 0

            def close(self):
                Mouse.closed += 1

        mouse = Mouse("/fake/mouse0")
        listed = []

        def pointer_listing(skip=frozenset()):
            listed.append(1)
            return iter([mouse])

        patches.set(keys, "pointer_devices", pointer_listing)
        focused = [True]
        arming = R.Arming()
        worker_cfg = _setup_cfg(data=False, key="")
        worker = PickClickWorker(worker_cfg, arming, focus=lambda _cls: focused[0],
                                 pointer=lambda: (11, 22))
        clicks: list = []
        worker.clicked.connect(lambda x, y: clicks.append((x, y)))
        worker.start()
        _wait(app, lambda: False, 0.3)
        mouse.press(ecodes.BTN_LEFT)
        _wait(app, lambda: False, 0.4)
        if listed or clicks:
            problems.append(f"click worker: devices opened ({len(listed)}) or a click read "
                            f"({clicks}) while not armed")
        mouse.forget()
        arming.set()
        if not _wait(app, lambda: bool(listed)):
            problems.append("click worker: armed but no device opened")
        mouse.press(ecodes.BTN_LEFT)
        if not _wait(app, lambda: clicks == [(11, 22)]):
            problems.append(f"click worker: an armed click in the game gave {clicks!r}")
        clicks.clear()
        mouse.press(ecodes.BTN_LEFT, 0)
        mouse.press(ecodes.BTN_RIGHT)
        mouse.press(ecodes.KEY_A)
        _wait(app, lambda: False, 0.4)
        if clicks:
            problems.append(f"click worker: a release, a right button or a key counted: {clicks!r}")
        focused[0] = False
        mouse.press(ecodes.BTN_LEFT)
        _wait(app, lambda: False, 0.4)
        if clicks:
            problems.append("click worker: a click outside the game counted")
        focused[0] = True
        before_closed = Mouse.closed
        arming.clear()
        if not _wait(app, lambda: Mouse.closed > before_closed):
            problems.append("click worker: the device stayed open after disarming")
        mouse.forget()
        mouse.press(ecodes.BTN_LEFT)
        _wait(app, lambda: False, 0.4)
        if clicks:
            problems.append("click worker: a click after disarming counted")
        # Forget the click above while the device is closed, not after arming:
        # the worker reopens a device whose pipe still holds that click, reads
        # it at once, and a `forget` racing that read (it drains the pipe
        # non-blocking) can make the read raise, so the worker drops the
        # device and the next click is never seen. It failed once in a full run.
        mouse.forget()
        arming.set()
        _wait(app, lambda: len(listed) == 2)
        mouse.press(ecodes.BTN_LEFT)
        if not _wait(app, lambda: bool(clicks)):
            problems.append("click worker: not armed again for the next overlay")
        worker.stop()
        if worker.isRunning():
            problems.append("click worker: still running after stop")
        worker = None
    except Exception as exc:  # noqa: BLE001
        import traceback

        problems.append(f"click to pick: raised {type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=4))
    finally:
        if worker is not None:
            worker.stop()
        if mouse is not None:
            mouse.really_close()
        patches.restore()
        for widget in widgets:
            widget.close()
            widget.deleteLater()
    print("click to pick     : pointer to card (2-4 cards, offset window), last click "
          "counts, one booking by the button's own function, none without a click / in the "
          "test mode / after the button, replaced reading books first; worker armed-only, "
          "game focus only. Not tested: XQueryPointer on the real X server")


def _check_one_booking_per_screen(app, problems: list) -> None:
    """A reward screen read again is one booking, Forma spends its relic, and the
    log can be read in order.

    Three readings of one screen with clicks and the button book once; the last
    choice moves it. Other rewards, other relics or more than 20 seconds
    (`read_at` of the readings, no clock) are a new screen and add. Forma by
    click and by button takes only the relic off the shelf, moves cleanly to a
    part and back. The stderr line carries the time, the hotkey names its device,
    and a click says what became of it.
    """
    import io
    import re
    import time

    from ui import app as app_mod
    from ui.overlay import rewards as R
    from ui.views.relic_panel import FORMA_NAME, RelicPanel, same_screen
    from ui.workers.hotkey import HotkeyWorker
    from ui.workers.pick_click import PickClickWorker
    from ui.workers.relic_trigger import TriggerResult
    from wft.capture import keys, window as cap
    from wft.data import db as dbq
    from wft.relic import score as S

    patches = _Patches()
    widgets = []
    keyboard = None
    worker = None
    try:
        # -- the rule, alone -------------------------------------------------------------
        located = cap.Placement(7, 0, 0, 3840, 2160)
        relic_a, relic_b = "/Test/RelicA", "/Test/RelicB"
        forma_part = "/Test/Forma"
        kuva = "/Test/Kuva"

        def result_of(layout, at, relics=(relic_a,)):
            rewards = []
            for index, kind in enumerate(layout.split()):
                if kind == "F":
                    rewards.append(S.Reward(position=index + 1, raw_text="Forma Blueprint"))
                elif kind == "M":       # Forma read as the catalogue's Forma Blueprint
                    rewards.append(S.Reward(position=index + 1, raw_text="2 X Forma Blueprint",
                                            part_unique=forma_part, name=FORMA_NAME,
                                            item_name=FORMA_NAME))
                elif kind == "K":       # Kuva: its own item and part, never counted
                    rewards.append(S.Reward(position=index + 1, raw_text="Kuva",
                                            part_unique=kuva, name="Kuva",
                                            item_name="Kuva", category="reward_other"))
                elif kind == "?":
                    rewards.append(S.Reward(position=index + 1, raw_text="zzz"))
                else:
                    rewards.append(S.Reward(position=index + 1, raw_text=f"Part {kind}",
                                            part_unique=f"/Test/{kind}", name=f"Part {kind}"))
            result = TriggerResult(S.rank(rewards), None, len(rewards),
                                   opened_relics=tuple(relics))
            result.read_at = at
            result.placement = located
            return result

        base = result_of("0 F 2 3", 1000.0)
        rule = {
            "same": same_screen(base, result_of("0 F 2 3", 1004.0)),
            "edge 20 s": same_screen(base, result_of("0 F 2 3", 1020.0)),
            "21 s": same_screen(base, result_of("0 F 2 3", 1021.0)),
            "earlier": same_screen(base, result_of("0 F 2 3", 999.0)),
            "other reward": same_screen(base, result_of("0 F 2 4", 1004.0)),
            "other order": same_screen(base, result_of("F 0 2 3", 1004.0)),
            "fewer cards": same_screen(base, result_of("0 F 2", 1004.0)),
            "other relics": same_screen(base, result_of("0 F 2 3", 1004.0, (relic_b,))),
            "relics, other order": same_screen(
                result_of("0 F 2 3", 1000.0, (relic_a, relic_b)),
                result_of("0 F 2 3", 1004.0, (relic_b, relic_a))),
            "nothing before": same_screen(None, base),
            "an error": same_screen(base, TriggerResult(None, "boom", 0)),
            "nothing named": same_screen(result_of("? ? ? ?", 1000.0),
                                         result_of("? ? ? ?", 1004.0)),
        }
        want = {"same": True, "edge 20 s": True, "21 s": False, "earlier": False,
                "other reward": False, "other order": False, "fewer cards": False,
                "other relics": False, "relics, other order": True,
                "nothing before": False, "an error": False, "nothing named": False}
        if rule != want:
            problems.append("one booking: same_screen is wrong for "
                            + ", ".join(k for k in want if rule[k] != want[k]))

        # -- the page over a fake client ---------------------------------------------------
        cfg = _setup_cfg(data=True, key="")
        cfg.override("overlay.rewards", True)
        cfg.override("overlay.click_to_pick", True)
        connection = dbq.connect(cfg.db_path)
        try:
            for unique, name in ((relic_a, "Test Relic A"), (relic_b, "Test Relic B")):
                connection.execute("INSERT INTO relics(unique_name, name, tier) VALUES(?,?,?)",
                                   (unique, name, "Meso"))
                connection.commit()
                dbq.set_relic_count(connection, unique, 5)
        finally:
            connection.close()
        fake = _FakeOverlayClient()
        overlay = R.RewardOverlay(cfg, fake, clock=lambda: 1.0)
        panel = RelicPanel(cfg)
        widgets.append(panel)
        panel.set_overlay(overlay)
        booked: list = []
        real_confirm = panel._confirm_pick

        def recording(index):
            booked.append(index)
            real_confirm(index)

        patches.set(panel, "_confirm_pick", recording)

        def card_point(index):
            tx, ty, tw, th = _OVERLAY_TILES[index]
            return (located.x + (tx + tw / 2) * located.width,
                    located.y + (ty + th / 2) * located.height)

        def step(layout, at, relics=(relic_a,)):
            result = result_of(layout, at, relics)
            panel._triggered_at = time.time()
            fake.sent.clear()
            panel._on_finished(result)
            shown = [c for c in fake.sent if c.get("cmd") == "reward_show"]
            if shown:
                fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                        "token": shown[-1]["token"]})
            return result, (shown[-1] if shown else None)

        def counts():
            connection = dbq.connect(cfg.db_path)
            try:
                got = {f"P{i}": dbq.owned_count(connection, f"/Test/{i}") for i in range(5)}
                got["A"] = dbq.relic_count(connection, relic_a)
                got["B"] = dbq.relic_count(connection, relic_b)
            finally:
                connection.close()
            return got

        def delta(before, **want):
            now = counts()
            return {k: now[k] - before[k] for k in now if now[k] != before[k]} == want

        def feedback():
            return panel.pick_feedback.text()

        start = counts()
        step("0 F 2 3", 1000.0)
        overlay.on_click(*card_point(3))
        _, shown = step("0 F 2 3", 1004.0)
        if booked or not delta(start):
            problems.append(f"one booking: the 2nd reading of a screen booked {booked!r}")
        if shown is None or shown.get("picked") != 3 or overlay.picked != 3:
            problems.append(f"one booking: the tick was not carried onto the 2nd reading "
                            f"({shown and shown.get('picked')!r}, {overlay.picked!r})")
        step("0 F 2 3", 1009.0)
        overlay.on_click(*card_point(3))
        if booked:
            problems.append(f"one booking: the 3rd reading booked {booked!r}")
        overlay.hide()
        if booked != [3] or not delta(start, P3=1, A=-1):
            problems.append(f"one booking: 3 readings and clicks left {booked!r} / {counts()!r}")
        text_after_click = feedback()
        if not text_after_click.startswith("Recorded: Part 3 (+1)") or "Test Relic A" not in text_after_click:
            problems.append(f"one booking: no 'Recorded' line after the click: {text_after_click!r}")

        _, shown = step("0 F 2 3", 1015.0)      # the screen read again after the booking
        if feedback() != text_after_click or panel.pick_feedback.isHidden():
            problems.append(f"one booking: the 'Recorded' line is gone after the next reading "
                            f"({feedback()!r})")
        if panel._recorded is None or shown is None or shown.get("picked") != 3:
            problems.append("one booking: the next reading lost the booking or the tick")
        overlay.hide()
        if booked != [3] or not delta(start, P3=1, A=-1):
            problems.append(f"one booking: the carried tick booked again ({booked!r}, {counts()!r})")

        panel.cards[3].taken.emit(4)           # "I took this (4)" on the same card
        if not delta(start, P3=1, A=-1):
            problems.append(f"one booking: the button after the click added: {counts()!r}")
        panel.cards[2].taken.emit(3)           # changed mind: a part
        if not delta(start, P2=1, A=-1):
            problems.append(f"one booking: changing the card did not move it: {counts()!r}")
        panel.cards[1].taken.emit(2)           # Forma
        forma_text = feedback()
        if not delta(start, A=-1) or not forma_text.startswith(f"Recorded: {FORMA_NAME}") \
                or "Test Relic A 5 \u2192 4" not in forma_text:
            problems.append(f"one booking: Forma by the button: {counts()!r} {forma_text!r}")
        panel.cards[0].taken.emit(1)           # Forma -> part
        if not delta(start, P0=1, A=-1) or FORMA_NAME in feedback():
            problems.append(f"one booking: Forma -> part: {counts()!r} {feedback()!r}")
        panel.cards[1].taken.emit(2)           # part -> Forma
        if not delta(start, A=-1):
            problems.append(f"one booking: part -> Forma: {counts()!r}")
        panel.cards[1].taken.emit(2)           # Forma again
        if not delta(start, A=-1):
            problems.append(f"one booking: Forma twice: {counts()!r}")
        panel.cards[0].taken.emit(1)
        panel.cards[0].taken.emit(1)
        if not delta(start, P0=1, A=-1):
            problems.append(f"one booking: the same part twice: {counts()!r}")

        # A new screen adds, whatever the old one did: other rewards within 20 s,
        # the same rewards after more than 20 s, the same rewards with other relics.
        before = counts()
        step("0 F 2 4", 1016.0)
        overlay.on_click(*card_point(3))
        overlay.hide()
        if not delta(before, P4=1, A=-1):
            problems.append(f"one booking: other rewards within 20 s: {counts()!r}")
        before = counts()
        step("0 F 2 4", 1040.0)
        overlay.on_click(*card_point(3))
        overlay.hide()
        if not delta(before, P4=1, A=-1):
            problems.append(f"one booking: the same rewards 24 s later: {counts()!r}")
        before = counts()
        step("0 F 2 4", 1045.0, (relic_b,))
        overlay.on_click(*card_point(3))
        overlay.hide()
        if not delta(before, P4=1, B=-1):
            problems.append(f"one booking: the same rewards with other relics: {counts()!r}")

        # Forma by click: only the relic; a later click on a part of the same
        # screen moves it; the same card again changes nothing.
        before = counts()
        step("0 F 2 3", 2000.0)
        overlay.on_click(*card_point(1))
        overlay.hide()
        if not delta(before, A=-1) or not feedback().startswith(f"Recorded: {FORMA_NAME}"):
            problems.append(f"forma by click: {counts()!r} {feedback()!r}")
        step("0 F 2 3", 2005.0)
        overlay.on_click(*card_point(2))
        overlay.hide()
        if not delta(before, P2=1, A=-1):
            problems.append(f"forma by click, then a part by click: {counts()!r}")
        step("0 F 2 3", 2008.0)
        overlay.on_click(*card_point(2))
        overlay.hide()
        if not delta(before, P2=1, A=-1):
            problems.append(f"the same card clicked on the next reading: {counts()!r}")
        step("0 F 2 3", 2010.0)
        overlay.on_click(*card_point(1))
        overlay.hide()
        if not delta(before, A=-1):
            problems.append(f"a part, then Forma by click on the same screen: {counts()!r}")

        # Something not recognised books nothing, by click or by button.
        before = counts()
        step("0 ? 2 3", 3000.0)
        overlay.on_click(*card_point(1))
        overlay.hide()
        panel.cards[1].taken.emit(2)
        if not delta(before) or "Not recognised" not in feedback():
            problems.append(f"a card not recognised booked or stayed silent: {counts()!r} {feedback()!r}")

        # Forma read as the catalogue's Forma Blueprint, which it is whenever a
        # relic that drops Forma is in play: still only the relic, by
        # click, by button and typed - never a Forma Blueprint part.
        connection = dbq.connect(cfg.db_path)
        try:
            connection.execute("INSERT INTO parts(unique_name, name) VALUES(?,?)",
                               (forma_part, FORMA_NAME))
            connection.commit()
            dbq.set_relic_count(connection, relic_a, 5)
        finally:
            connection.close()

        def forma_parts():
            connection = dbq.connect(cfg.db_path)
            try:
                return connection.execute(
                    "SELECT COALESCE(SUM(o.count), 0) FROM owned_parts o "
                    "JOIN parts p ON p.unique_name = o.part_unique WHERE p.name = ?",
                    (FORMA_NAME,)).fetchone()[0]
            finally:
                connection.close()

        before = counts()
        step("0 M 2 3", 3100.0)
        overlay.on_click(*card_point(1))
        overlay.hide()
        if not delta(before, A=-1) or forma_parts() \
                or not feedback().startswith(f"Recorded: {FORMA_NAME}"):
            problems.append(f"forma read as a part, by click: {counts()!r} "
                            f"{forma_parts()} {feedback()!r}")
        before = counts()
        step("0 M 2 4", 3200.0)
        panel.cards[1].taken.emit(2)
        if not delta(before, A=-1) or forma_parts():
            problems.append(f"forma read as a part, by button: {counts()!r} {forma_parts()}")
        before = counts()
        step("0 ? 2 4", 3300.0)
        panel.pick_manual.setText("forma blueprint")
        panel._confirm_typed()
        if not delta(before, A=-1) or forma_parts() \
                or not feedback().startswith(f"Recorded: {FORMA_NAME}"):
            problems.append(f"forma typed: {counts()!r} {forma_parts()} {feedback()!r}")

        # Kuva and the rest of Forma's kind book as Forma does, under their own
        # name, by click and typed - the writer would refuse them.
        connection = dbq.connect(cfg.db_path)
        try:
            connection.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                               "VALUES(?, 'Kuva', 'reward_other', 0)", (kuva,))
            connection.execute("INSERT INTO parts(unique_name, name, item_unique) "
                               "VALUES(?, 'Kuva', ?)", (kuva, kuva))
            connection.commit()
        finally:
            connection.close()
        panel._typed_part_names = None

        def kuva_count():
            connection = dbq.connect(cfg.db_path)
            try:
                return dbq.owned_count(connection, kuva)
            finally:
                connection.close()

        before = counts()
        step("0 K 2 3", 3400.0)
        overlay.on_click(*card_point(1))
        overlay.hide()
        if not delta(before, A=-1) or kuva_count() \
                or not feedback().startswith("Recorded: Kuva"):
            problems.append(f"kuva by click: {counts()!r} {kuva_count()} {feedback()!r}")
        before = counts()
        step("0 ? 2 3", 3500.0)
        panel.pick_manual.setText("kuva")
        panel._confirm_typed()
        if not delta(before, A=-1) or kuva_count() \
                or not feedback().startswith("Recorded: Kuva"):
            problems.append(f"kuva typed: {counts()!r} {kuva_count()} {feedback()!r}")

        # -- the log with the time ---------------------------------------------------------
        sink = io.StringIO()
        stamp = lambda t: time.strftime("%H:%M:%S", time.localtime(t))  # noqa: E731
        app_mod.wire_stderr_log(panel, stream=sink, clock=lambda: 1_800_000_000.0)
        panel.log_line.emit("picked in the game: card 4")
        panel.log_line.emit("hotkey: F23 from Some Mouse Keyboard")
        step("0 F 2 3", 4000.0)
        overlay.on_click(*card_point(3))
        overlay.on_click(located.x + 5, located.y + 5)
        lines = sink.getvalue().splitlines()
        t = stamp(1_800_000_000.0)
        want_lines = [f"{t} rewards: picked in the game: card 4",
                      f"{t} hotkey: F23 from Some Mouse Keyboard",
                      f"{t} click: card 4", f"{t} click: outside the cards"]
        for line in want_lines:
            if line not in lines:
                problems.append(f"log: missing the stderr line {line!r} in {lines!r}")
        if not all(re.match(r"^\d\d:\d\d:\d\d \S", line) for line in lines):
            problems.append(f"log: a stderr line without the time: {lines!r}")
        overlay.hide()

        # The click worker says why a click did not count.
        said: list = []
        arming = R.Arming()
        arming.set()
        unfocused = PickClickWorker(cfg, arming, focus=lambda _cls: False,
                                    pointer=lambda: (1, 1))
        unfocused.ignored.connect(said.append)
        unfocused._on_left_down()
        if said != ["click: game not focused"]:
            problems.append(f"log: an unfocused click said {said!r}")

        # The hotkey names the device it came from.
        if keys.evdev is None:
            print("one booking       : hotkey device name skipped, python-evdev is not installed")
        else:
            keyboard = _FakeKeyboard("/fake/event7")
            keyboard.name = "Fake Gaming Mouse Keyboard"
            _fake_devices(patches, keyboard)
            hot_cfg = _setup_cfg(data=False, key="F8")
            hot_cfg.override("game.window_class", "")
            worker = HotkeyWorker(hot_cfg)
            heard: list = []
            worker.fired.connect(heard.append)
            worker.start()
            _wait(app, lambda: False, 0.6)
            keyboard.forget()
            end = time.monotonic() + 5
            while time.monotonic() < end and not heard:
                keyboard.press(keys.ecodes.KEY_F8)
                _wait(app, lambda: bool(heard), 0.25)
            if heard[:1] != ["hotkey: F8 from Fake Gaming Mouse Keyboard"]:
                problems.append(f"log: the hotkey said {heard[:2]!r}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        problems.append(f"one booking: raised {type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=4))
    finally:
        if worker is not None:
            worker.requestInterruption()
            worker.wait(3000)
        if keyboard is not None:
            keyboard.really_close()
        patches.restore()
        for widget in widgets:
            widget.close()
            widget.deleteLater()
    print("one booking/screen: 3 readings + clicks + button = 1 booking, the last choice "
          "moves it; other rewards / relics / more than 20 s add; Forma (click and button) "
          "spends only the relic and moves cleanly, also read as the Forma Blueprint part "
          "and typed, Kuva the same by click and typed; stderr lines with "
          "the time, hotkey with its device, clicks named")


def _check_the_palette_grid(app, problems: list) -> None:
    """The palette grid overlay: the drawing, the helper's window, the
    Overlays page and the client that starts the helper.

    Nothing here opens a real window on the xcb platform: the drawing goes to a
    QImage, the helper's window and controller are driven directly on the
    offscreen platform, the page talks to a fake client, and the client runs
    against fake helpers (a program that fails, a plain-Python one that answers
    on the socket).
    """
    import json
    import time

    from PySide6.QtCore import QEvent, QLockFile, QRect, QSettings
    from PySide6.QtGui import QKeyEvent

    from ui import geometry, settings_io
    from ui.overlay import client as client_mod, drawing, helper
    from ui.views.overlays_view import OverlaysView

    where = QSettings(geometry.ORGANISATION, geometry.APPLICATION).fileName()
    scratch = os.environ.get("XDG_CONFIG_HOME", "")
    if not scratch or not where.startswith(scratch) \
            or not str(settings_io.target_config_path()).startswith(scratch):
        problems.append(f"palette grid: the settings file is {where}, outside the "
                        f"test's config folder; not checked")
        return

    # -- the drawing -------------------------------------------------------
    style = drawing.merge_style(None)
    margin = drawing.margin_px(style)
    cell = 84
    image = _palette_pixels(style, (425 + 2 * margin, 18 * cell + 2 * margin))
    if image.pixelColor(margin, margin + 40).alpha() == 0:
        problems.append("palette grid: the grid line at the left edge is missing")
    if image.pixelColor(margin + 42, margin + 42).alpha() != 0:
        problems.append("palette grid: the middle of a cell is not transparent")
    # Clear of the grid lines, whose width reaches a little into the margin.
    if not _anything_drawn(image, margin + 15, 0, margin + 70, margin - 6):
        problems.append("palette grid: no letter above column A")
    if not _anything_drawn(image, 0, margin + 17 * cell + 15, margin - 6, margin + 18 * cell - 15):
        problems.append("palette grid: no number beside row 18")
    inner = (margin + 4, margin + 4, margin + 80, margin + 4 + int(cell * 0.45))
    if _anything_drawn(image, *inner):
        problems.append("palette grid: a cell carries its name without cell_names")
    named = _palette_pixels({**style, "cell_names": True},
                            (425 + 2 * margin, 18 * cell + 2 * margin))
    if not _anything_drawn(named, *inner):
        problems.append("palette grid: cell_names writes no name into the cell")
    if drawing.cell_name(1, 6) != "B7" or drawing.column_name(4) != "E":
        problems.append("palette grid: the cell names are not column letter + row number")
    clamped = drawing.merge_style({"font_pt": "big", "rows": 5000, "line_opacity": -3,
                                   "colours": {"line": "not a colour"}})
    if (clamped["font_pt"] != 15 or clamped["rows"] != 99
            or clamped["line_opacity"] != 0.05 or clamped["colours"]["line"] != "#ffffff"):
        problems.append(f"palette grid: a bad style value was not held back: {clamped}")

    # -- the helper's window and controller ----------------------------------
    sent: list[dict] = []
    window = helper.PaletteWindow()
    controller = helper.Controller(window, sent.append)
    try:
        saved = {"x": 100, "y": 200, "w": 300, "h": 900}
        controller.handle({"cmd": "palette_show", "position": saved, "style": {}})
        last = sent[-1]
        if (last["mode"] != "locked" or last["position"] != saved
                or last["reason"] != "show" or not last["shown"]):
            problems.append(f"palette helper: show with a position gave {last}")
        if not (window.windowFlags() & Qt.WindowTransparentForInput):
            problems.append("palette helper: a locked grid takes mouse clicks")
        m = drawing.margin_px(window.style_)
        if window.geometry() != QRect(100 - m, 200 - m, 300 + 2 * m, 900 + 2 * m):
            problems.append(f"palette helper: the window is {window.geometry()}, "
                            f"not the grid plus its margin")

        controller.handle({"cmd": "palette_unlock"})
        if sent[-1]["mode"] != "aligning" or sent[-1]["reason"] != "unlock":
            problems.append(f"palette helper: unlock gave {sent[-1]}")
        if window.windowFlags() & Qt.WindowTransparentForInput:
            problems.append("palette helper: an aligning grid ignores the mouse")

        def press(key, modifiers=Qt.NoModifier):
            window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers))

        press(Qt.Key_Right, Qt.ControlModifier)
        press(Qt.Key_Down)
        press(Qt.Key_Right, Qt.ShiftModifier)
        press(Qt.Key_Return)
        last = sent[-1]
        want = {"x": 110, "y": 201, "w": 301, "h": 900}
        if last["mode"] != "locked" or last["reason"] != "lock" or last["position"] != want:
            problems.append(f"palette helper: Enter after moving gave {last}, "
                            f"wanted a locked {want}")
        if window.saved is None or window.saved.x() != 110:
            problems.append("palette helper: the locked position is not the one Esc returns to")

        controller.handle({"cmd": "palette_unlock"})
        press(Qt.Key_Left, Qt.ControlModifier)
        press(Qt.Key_Escape)
        last = sent[-1]
        if last["mode"] != "locked" or last["position"] != want or last["reason"] != "cancel":
            problems.append(f"palette helper: Esc gave {last}, wanted back {want}, locked")

        controller.handle({"cmd": "palette_unlock"})
        window.mouseDoubleClickEvent(None)
        if sent[-1]["reason"] != "lock" or sent[-1]["mode"] != "locked":
            problems.append(f"palette helper: a double click gave {sent[-1]}")

        controller.handle({"cmd": "palette_style",
                           "style": {"font_pt": 30, "cell_names": True}})
        last = sent[-1]
        if last["position"] != want or drawing.margin_px(window.style_) <= m:
            problems.append(f"palette helper: a bigger font moved the grid or kept "
                            f"the margin: {last}")

        controller.handle({"cmd": "palette_show", "position": None, "style": {}})
        last = sent[-1]
        screen = window.screen().geometry()
        position = last["position"]
        if (last["mode"] != "aligning" or position["h"] != int(screen.height() * 0.7)
                or position["w"] != int(position["h"] * 5 / 18)):
            problems.append(f"palette helper: show without a position gave {last} "
                            f"on a screen of {screen.width()}x{screen.height()}")
        press(Qt.Key_Escape)
        last = sent[-1]
        if last["shown"] or last["mode"] != "hidden" or last["position"] is not None:
            problems.append(f"palette helper: Esc without a stored position gave {last}")

        controller.handle({"cmd": "palette_show", "position": {"x": 1}, "style": {}})
        if sent[-1]["mode"] != "aligning":
            problems.append("palette helper: a broken position was not treated as none")
        controller.handle({"cmd": "palette_hide"})
        if sent[-1]["shown"] or sent[-1]["reason"] != "hide":
            problems.append(f"palette helper: hide gave {sent[-1]}")
        count = len(sent)
        controller.handle({"cmd": "palette_unlock"})
        if len(sent) != count or window.mode != "hidden" or window.isVisible():
            problems.append("palette helper: unlock brought a hidden grid back")
        controller.handle({"cmd": "nonsense"})
        if sent[-1].get("type") != "error":
            problems.append("palette helper: an unknown command was not answered "
                            "with an error")
        asked = []
        controller.quit_requested.connect(lambda: asked.append(True))
        controller.handle({"cmd": "quit"})
        if not asked:
            problems.append("palette helper: quit did not ask the helper to end")

        folder = tempfile.mkdtemp()
        first, second = helper.lock_file(folder), helper.lock_file(folder)
        if not first.tryLock(0) or second.tryLock(0):
            problems.append("palette helper: a second helper could take the lock")
        first.unlock()
        if not second.tryLock(0):
            problems.append("palette helper: the lock was not given back")
        second.unlock()
        shutil.rmtree(folder, ignore_errors=True)
    finally:
        window.close()
        window.deleteLater()

    # -- the Overlays page ----------------------------------------------------------
    patches = _Patches()
    page = None
    home = tempfile.mkdtemp()
    # The old script's file lives under $HOME; never read the user's own.
    patches.env("HOME", home)
    try:
        from wft import config as config_mod

        cfg = config_mod.load()
        fake = _FakeOverlayClient()
        page = OverlaysView(cfg, fake)
        page.resize(900, 700)
        page.show()
        _settle(app)
        geometry.clear_palette()
        settings = QSettings(geometry.ORGANISATION, geometry.APPLICATION)
        settings.remove("palette")
        settings.sync()

        def buttons():
            return {name: getattr(page, name + "_btn").isEnabled()
                    for name in ("start", "stop", "unlock", "lock", "reset")}

        if page.status.text() != "Not running" or buttons() != {
                "start": True, "stop": False, "unlock": False, "lock": False,
                "reset": False}:
            problems.append(f"overlays page: idle state wrong: {page.status.text()!r} "
                            f"{buttons()}")

        page.start_btn.click()
        command = fake.sent[-1] if fake.sent else {}
        if (command.get("cmd") != "palette_show" or command.get("position") is not None
                or command.get("style", {}).get("cell_names") is not False
                or command.get("style", {}).get("columns") != 5):
            problems.append(f"overlays page: Start with no stored position sent {command}")
        fake.state.emit({"type": "state", "shown": True, "mode": "aligning",
                         "position": {"x": 5, "y": 6, "w": 200, "h": 720}, "reason": "show"})
        if (page.status.text() != "Aligning at x 5, y 6, 200 \u00d7 720"
                or buttons() != {"start": False, "stop": True, "unlock": False,
                                 "lock": True, "reset": True}):
            problems.append(f"overlays page: aligning state wrong: {page.status.text()!r} "
                            f"{buttons()}")
        page.lock_btn.click()
        if fake.sent[-1] != {"cmd": "palette_lock"}:
            problems.append(f"overlays page: Lock sent {fake.sent[-1]}")
        if geometry.stored_palette() is not None:
            problems.append("overlays page: a position was stored before the user locked")
        locked = {"x": 7, "y": 8, "w": 210, "h": 756}
        fake.state.emit({"type": "state", "shown": True, "mode": "locked",
                         "position": locked, "reason": "lock"})
        if geometry.stored_palette() != locked:
            problems.append(f"overlays page: a locked position was stored as "
                            f"{geometry.stored_palette()}")
        if (page.status.text() != "Shown, locked at x 7, y 8, 210 \u00d7 756"
                or buttons() != {"start": False, "stop": True, "unlock": True,
                                 "lock": False, "reset": True}):
            problems.append(f"overlays page: locked state wrong: {page.status.text()!r} "
                            f"{buttons()}")
        page.unlock_btn.click()
        if fake.sent[-1] != {"cmd": "palette_unlock"}:
            problems.append(f"overlays page: Unlock sent {fake.sent[-1]}")
        fake.state.emit({"type": "state", "shown": True, "mode": "locked",
                         "position": {"x": 1, "y": 1, "w": 99, "h": 99}, "reason": "cancel"})
        if geometry.stored_palette() != locked:
            problems.append("overlays page: a cancelled alignment replaced the stored position")

        page.names_box.setChecked(True)
        if fake.sent[-1].get("cmd") != "palette_style" \
                or fake.sent[-1]["style"]["cell_names"] is not True:
            problems.append(f"overlays page: Cell names sent {fake.sent[-1]}")
        page.font_spin.setValue(20)
        page.opacity_spin.setValue(80)
        style_sent = fake.sent[-1]["style"]
        written = settings_io.target_config_path().read_text()
        if (style_sent["font_pt"] != 20 or style_sent["line_opacity"] != 0.8
                or cfg.get("palette_grid.cell_names") is not True
                or "[palette_grid]" not in written or "line_opacity = 0.8" not in written
                or "font_pt = 20" not in written or "cell_names = true" not in written):
            problems.append(f"overlays page: the look was not applied and saved "
                            f"({style_sent}, file: {written!r})")

        fake.running = False
        count = len(fake.attempts)
        page.names_box.setChecked(False)
        if len(fake.attempts) != count:
            problems.append("overlays page: a look change started the helper")

        page.start_btn.setEnabled(True)
        page.start_btn.click()
        if fake.sent[-1].get("position") != locked:
            problems.append(f"overlays page: Start sent {fake.sent[-1].get('position')}, "
                            f"not the stored {locked}")
        page.reset_btn.click()
        if geometry.stored_palette() is not None or fake.sent[-1].get("position") is not None \
                or fake.sent[-1].get("cmd") != "palette_show":
            problems.append(f"overlays page: Reset left {geometry.stored_palette()} "
                            f"and sent {fake.sent[-1]}")

        fake.failed.emit("The overlay helper stopped: boom")
        if not page.error.text().endswith("boom") or page.error.isHidden():
            problems.append("overlays page: a failure is not shown")
        fake.running = False
        fake.stopped.emit()
        if page.status.text() != "Not running":
            problems.append("overlays page: a stopped helper still shows as running")
        page.start_btn.click()
        if not page.error.isHidden():
            problems.append("overlays page: the failure stayed after Start")
        page.stop_btn.click()
        if fake.stops != 1:
            problems.append("overlays page: Stop did not stop the client")

        # The old script's position, taken over once.
        geometry.clear_palette()
        settings.remove("palette")
        settings.sync()
        old = pathlib.Path(home) / ".config" / "warframe"
        old.mkdir(parents=True)
        (old / "paletten-overlay.json").write_text(
            json.dumps({"x": 30, "y": 40, "b": 425, "h": 1515}))
        fake.running = False
        page.start_btn.setEnabled(True)
        page.start_btn.click()
        mapped = {"x": 30, "y": 40, "w": 425, "h": 1515}
        if fake.sent[-1].get("position") != mapped or geometry.stored_palette() != mapped:
            problems.append(f"overlays page: the old script's position became "
                            f"{fake.sent[-1].get('position')}")
        page.reset_btn.click()
        fake.running = False
        page.start_btn.setEnabled(True)
        page.start_btn.click()
        if fake.sent[-1].get("position") is not None:
            problems.append("overlays page: Reset was undone by the old script's file")
        # Reset before the old file was ever looked at: it must not come back.
        settings.remove("palette")
        settings.sync()
        geometry.clear_palette()
        if geometry.palette_position() is not None:
            problems.append("overlays page: Reset before the old script's file was "
                            "looked at was undone by it")
        (old / "paletten-overlay.json").write_text("not json")
        settings.remove("palette")
        settings.sync()
        if geometry.palette_position() is not None:
            problems.append("overlays page: a broken old file gave a position")
    finally:
        patches.restore()
        settings = QSettings(geometry.ORGANISATION, geometry.APPLICATION)
        settings.remove("palette")
        settings.sync()
        if page is not None:
            page.close()
            page.deleteLater()
        shutil.rmtree(home, ignore_errors=True)
        _settle(app)

    # -- the client ---------------------------------------------------------------------
    spawned = []
    answering = (
        "import json, os, socket, sys, tempfile\n"
        "path = os.path.join(tempfile.gettempdir(), sys.argv[1])\n"
        "link = socket.socket(socket.AF_UNIX)\n"
        "link.connect(path)\n"
        "stream = link.makefile('rw')\n"
        "stream.write(json.dumps({'type': 'state', 'shown': False, 'mode': 'hidden',\n"
        "                         'position': None, 'reason': 'hello'}) + '\\n')\n"
        "stream.flush()\n"
        "for line in stream:\n"
        "    command = json.loads(line)\n"
        "    if command['cmd'] == 'quit':\n"
        "        break\n"
        "    stream.write(json.dumps({'type': 'state', 'shown': True, 'mode': 'locked',\n"
        "                             'position': command['position'], 'reason': 'show'}) + '\\n')\n"
        "    stream.flush()\n")
    failing = "import sys; sys.stderr.write('qt.qpa.xcb: could not connect to display\\n'); sys.exit(1)"
    original = client_mod.helper_command
    program = {"code": failing}

    def fake_command(socket_name):
        spawned.append(socket_name)
        return sys.executable, ["-c", program["code"], socket_name]

    client_mod.helper_command = fake_command
    client = None
    try:
        client = client_mod.OverlayClient()
        failures, states, stops = [], [], []
        client.failed.connect(failures.append)
        client.state.connect(states.append)
        client.stopped.connect(lambda: stops.append(True))
        client.send({"cmd": "palette_hide"})
        if spawned or client.running:
            problems.append("overlay client: a command other than palette_show "
                            "started the helper")
        client.send({"cmd": "palette_show", "position": None, "style": {}})
        if not _wait(app, lambda: failures and stops, 10.0):
            problems.append("overlay client: a helper that fails at once gave no "
                            "message")
        elif ("qt.qpa.xcb: could not connect to display" not in failures[0]
              or client.running):
            problems.append(f"overlay client: the failure said {failures[0]!r}")

        program["code"] = answering
        failures.clear()
        stops.clear()
        client.send({"cmd": "palette_show", "position": {"x": 1, "y": 2, "w": 3, "h": 4},
                     "style": {}})
        client.send({"cmd": "palette_show", "position": {"x": 9, "y": 9, "w": 9, "h": 9},
                     "style": {}})
        if not _wait(app, lambda: any(s.get("reason") == "show" and s["position"]["x"] == 9
                                      for s in states), 10.0):
            problems.append(f"overlay client: no answer from a helper on the socket "
                            f"(states {states}, failures {failures})")
        if len(spawned) != 2 or not client.running:
            problems.append(f"overlay client: {len(spawned)} helper(s) started for "
                            f"2 commands (1 failing + 1 answering expected)")
        client.stop()
        if not _wait(app, lambda: stops, 10.0) or failures or client.running:
            problems.append(f"overlay client: Stop did not end the helper cleanly "
                            f"(failures {failures})")

        program["code"] = "import sys; sys.exit(0)"
        client_mod.helper_command = lambda _name: ("/nonexistent/overlay-helper", [])
        failures.clear()
        client.send({"cmd": "palette_show", "position": None, "style": {}})
        if not _wait(app, lambda: failures, 5.0) or "could not be started" not in failures[0]:
            problems.append(f"overlay client: a missing program said {failures}")
    finally:
        client_mod.helper_command = original
        if client is not None:
            client.shutdown()
            client.deleteLater()
        _settle(app)
    print("palette grid       : the grid is drawn (lines, clear cells, letters, numbers, "
          "cell names), the helper's window locks, aligns and cancels, the page "
          "follows it and stores the locked position, the client reports a helper "
          "that fails")


#: Exceptions raised in a slot or a timer callback while the suite ran. Qt does
#: not stop for them: PySide hands them to `sys.excepthook`, which only prints,
#: so a failing callback used to be a traceback in the log and a green run.
_SLOT_EXCEPTIONS: list[BaseException] = []


def _watch_slot_exceptions() -> None:
    previous = sys.excepthook

    def hook(kind, value, traceback):
        _SLOT_EXCEPTIONS.append(value)
        previous(kind, value, traceback)

    sys.excepthook = hook


def _unexpected_slot_exceptions() -> list[str]:
    """What went wrong in a callback, minus the network refusals `_offline` causes."""
    from wft.market import client as market_mod
    from wft.riven import prices as prices_mod
    from wft.world import fissures as fissures_mod

    expected = (prices_mod.RivenError, fissures_mod.FissureError,
                market_mod.MarketError)
    return [f"{type(e).__name__}: {e}" for e in _SLOT_EXCEPTIONS
            if not (isinstance(e, expected) and str(e) == "offline in tests")]


def _check_own_relic_from_the_log(app, problems: list) -> None:
    """The relic the player opened comes from the equip dialog in EE.log.

    The six reward screens of one real session (anonymised, `tests/fixtures/eelog/
    session.txt`) give the relic that was equipped, the Braton screen Meso D8 and
    not Meso V13; an endless round has the dialog only; a dialog of an earlier
    screen does not count; no dialog is None; the local loader's refinement wins
    over another player's. On the page: a known, held relic is taken off without
    a question; an unknown refinement asks among the held refinements of that
    relic; no dialog narrows by the leftmost card as before. A relic refined in
    the mission is read with its refinement and the one it came from,
    and the page takes the one it came from without a question.
    """
    import re

    from ui.views.relic_panel import RelicPanel
    from ui.workers.relic_trigger import TriggerResult
    from wft.data import db as dbq
    from wft.relic import eelog, score as S

    folder = pathlib.Path(__file__).parent / "fixtures" / "eelog"
    patches = _Patches()
    widgets = []
    try:
        session = (folder / "session.txt").read_text().splitlines(keepends=True)

        def cut(at):
            kept = []
            for line in session:
                if float(line.split(" ", 1)[0]) > at:
                    break
                kept.append(line)
            return "".join(kept)

        def got(own):
            return None if own is None else (own.name, own.refinement)

        truth = {2536: ("Meso Y2 Relic", "Bronze"), 2781: ("Meso D8 Relic", "Bronze"),
                 3530: ("Neo C7 Relic", "Bronze"), 3775: ("Neo C7 Relic", None),
                 3990: ("Neo C7 Relic", None), 5220: ("Lith S18 Relic", "Bronze"),
                 2296: ("Meso Y2 Relic", "Bronze")}
        wrong = [f"{at}: {got(eelog._own_from_text(cut(at)))!r}"
                 for at, want in truth.items() if got(eelog._own_from_text(cut(at))) != want]
        if wrong:
            problems.append("own relic: the real session reads wrong at " + "; ".join(wrong))

        files = {"normal": ("Meso D8 Relic", "Bronze"), "silver": ("Meso D8 Relic", "Silver"),
                 "endless": ("Neo C7 Relic", None), "earlier-round": None, "no-dialog": None,
                 "changed-mind": ("Meso D8 Relic", None),
                 "lobby-loader": ("Meso D8 Relic", None), "late-join": (None, "Platinum")}
        wrong = []
        for name, want in files.items():
            if got(eelog.own_relic(str(folder / f"{name}.txt"))) != want:
                wrong.append(name)
        if wrong:
            problems.append("own relic: wrong for the fixtures " + ", ".join(wrong))
        if eelog.own_relic(str(folder / "missing.txt")) is not None:
            problems.append("own relic: a missing file is not None")

        # Refined in the relic choice of an endless run: the equip dialog
        # prints the refinement, the refine dialog before it the one refined from;
        # a refine dialog of another relic, or of an earlier round, does not count.
        refined = (folder / "refined.txt").read_text().splitlines(keepends=True)
        want = {100.2: ("Lith G14 Relic", "Platinum", "Bronze"),
                300.2: ("Lith S19 Relic", "Platinum", "Silver"),
                500.2: ("Lith S19 Relic", "Platinum", None)}
        wrong = []
        for at, expected in want.items():
            own = eelog._own_from_text("".join(
                line for line in refined if float(line.split(" ", 1)[0]) <= at))
            if own is None or (own.name, own.refinement, own.refined_from) != expected:
                wrong.append(f"{at}: {own!r}")
        if wrong:
            problems.append("own relic: refined in the mission reads " + "; ".join(wrong))

        # No dialog, the loader line alone: a live screen
        # (late-join) names its identifier, and with the dialogs taken out the real
        # session gives the relic the dialog named wherever a loader line exists.
        projections = "/Lotus/Types/Game/Projections/"
        a22 = projections + "T4VoidProjectionStyanaxPrimeCPlatinum"
        late = eelog.own_relic(str(folder / "late-join.txt"))
        if late is None or (late.unique, late.source) != (a22, "local loader"):
            problems.append(f"own relic: the loader line alone gave {late!r}")
        alone = {2296: "T2VoidProjectionStyanaxPrimeBBronze",
                 2536: "T2VoidProjectionStyanaxPrimeBBronze",
                 2781: "T2VoidProjectionVorunaPrimeCBronze",
                 3530: "T3VoidProjectionGyrePrimeCBronze", 3775: None, 3990: None,
                 5220: "T1VoidProjectionStyanaxPrimeEBronze"}
        wrong = []
        for at, want in alone.items():
            text = "".join(line for line in cut(at).splitlines(keepends=True)
                           if "Dialog::CreateOkCancel" not in line)
            own = eelog._own_from_text(text)
            if (own.unique if own else None) != (want and projections + want):
                wrong.append(f"{at}: {own!r}")
        if wrong:
            problems.append("own relic: without the dialogs the session reads " + "; ".join(wrong))

        # The relics of a screen are everything since the screen before, not
        # 45 s back from the newest line: the squad loads in over a minute or more.
        screens = {
            2296: ("T2VoidProjectionCitrinePrimeAPlatinum", "T2VoidProjectionStyanaxPrimeDPlatinum",
                   "T2VoidProjectionStyanaxPrimeBBronze", "T2VoidProjectionCitrinePrimeDPlatinum"),
            2536: ("T2VoidProjectionVorunaPrimeCPlatinum", "T2VoidProjectionXakuPrimeAPlatinum",
                   "T2VoidProjectionStyanaxPrimeBBronze"),
            5220: ("T1VoidProjectionCitrinePrimeASilver", "T1VoidProjectionCitrinePrimeDBronze",
                   "T1VoidProjectionStyanaxPrimeEBronze")}
        cut_log = pathlib.Path(tempfile.mkdtemp()) / "EE.log"
        wrong = []
        for at, want in screens.items():
            cut_log.write_text(cut(at))
            read = eelog.read(str(cut_log)).unique_names
            if sorted(read) != sorted(projections + unique for unique in want):
                wrong.append(f"{at}: {[u.rsplit('/', 1)[1] for u in read]}")
        read = eelog.read(str(folder / "late-join.txt")).unique_names
        if read != [a22] + [projections + u for u in (
                "T4VoidProjectionCitrinePrimeAPlatinum", "T4VoidProjectionProteaPrimeBPlatinum",
                "T4VoidProjectionYareliPrimeCBronze")]:
            wrong.append(f"late-join: {[u.rsplit('/', 1)[1] for u in read]}")
        if wrong:
            problems.append("relics in play: " + "; ".join(wrong))

        # A long mission: 5 MB of other lines between the dialog and the screen.
        long_log = pathlib.Path(tempfile.mkdtemp()) / "EE.log"
        lines = (folder / "normal.txt").read_text().splitlines(keepends=True)
        filler = "".join(f"{150 + i / 1000:.3f} Sys [Info]: nothing to see here, "
                         f"a line of the kind the game writes all the time {i}\n"
                         for i in range(40000))
        long_log.write_text("".join(lines[:5]) + filler * 3 + "".join(lines[5:]))
        if long_log.stat().st_size < 5_000_000 or got(eelog.own_relic(str(long_log))) != (
                "Meso D8 Relic", "Bronze"):
            problems.append("own relic: the dialog 5 MB back was not found "
                            f"({long_log.stat().st_size} bytes, {eelog.own_relic(str(long_log))!r})")
        # Not a word of the log in what was kept: no names, no ids.
        text = "".join((folder / f"{n}.txt").read_text()
                       for n in (*files, "session", "refined"))
        if re.search(r"[0-9a-f]{24}", text.replace("0" * 24, "")) or "@" in text:
            problems.append("own relic: a fixture holds an id or an address")
        # Player names: the first session.txt carried a player's own name and
        # 21 squad mates' in its /Temp/ paths. Only the placeholders may stand
        # there, and no loader line may name anyone.
        named = [m.group(0) for m in re.finditer(
            r"/Temp/(?:Local|Other)Player\d+_(?!(?:Player|Other\d+|X)_\d+\))[^)]*\)", text)]
        named += [line for line in text.splitlines()
                  if "loadout loader finished" in line
                  and not re.search(r"Game \[Info\]: (?:Player|Someone) loadout loader "
                                    r"finished\.\s*$", line)]
        if named:
            problems.append(f"own relic: a fixture names a player: {named[:2]!r}")

        # -- the page ---------------------------------------------------------------------
        cfg = _setup_cfg(data=True, key="")
        d8, d8s = "/Test/D8Bronze", "/Test/D8Silver"
        v13, braton = "/Test/V13Bronze", "/Test/BratonPart"
        connection = dbq.connect(cfg.db_path)
        try:
            for unique, name in ((d8, "Meso D8 Relic"), (d8s, "Meso D8 Relic"),
                                 (v13, "Meso V13 Relic")):
                connection.execute("INSERT INTO relics(unique_name, name, tier) VALUES(?,?,?)",
                                   (unique, name, "Meso"))
            connection.execute("INSERT INTO parts(unique_name, name) VALUES(?,?)",
                               (braton, "Braton Prime Blueprint"))
            for relic in (d8, d8s, v13):
                connection.execute("INSERT INTO relic_rewards(relic_unique, part_unique, rarity) "
                                   "VALUES(?,?,'COMMON')", (relic, braton))
            connection.commit()
        except Exception:  # noqa: BLE001 - the schema decides; say what it was
            connection.close()
            raise
        connection.close()
        panel = RelicPanel(cfg)
        widgets.append(panel)
        lines_logged: list = []
        panel.log_line.connect(lines_logged.append)

        def play(own, opened, held, left=braton):
            """Spend a relic for one reading; what changed and what was asked."""
            connection = dbq.connect(cfg.db_path)
            try:
                for relic in (d8, d8s, v13):
                    dbq.set_relic_count(connection, relic, held.get(relic, 0))
                reward = S.Reward(position=1, raw_text="x", part_unique=left, name="x")
                result = TriggerResult(S.rank([reward]), None, 1, opened_relics=tuple(opened))
                result.own_relic = own
                panel._last_result = result
                panel._spent = None
                lines_logged.clear()
                panel._spend_relic(connection)
                now = {r: dbq.relic_count(connection, r) for r in (d8, d8s, v13)}
            finally:
                connection.close()
            changed = {r: now[r] - held.get(r, 0) for r in now if now[r] != held.get(r, 0)}
            asked = [b.text() for b in panel.relic_buttons]
            return changed, asked, list(lines_logged), panel.pick_feedback.text()

        own_d8 = eelog.OwnRelic("Meso D8 Relic", "Bronze")
        # The Braton screen: Meso V13 and Meso D8 both open, both held, the part drops from both.
        changed, asked, log, _ = play(own_d8, [v13, d8], {d8: 3, v13: 2})
        if changed != {d8: -1} or asked:
            problems.append(f"own relic: the Braton screen took {changed!r} / asked {asked!r}")
        if log != ["relic: yours is Meso D8 Relic [Intact] (equip dialog)"]:
            problems.append(f"own relic: the log line is {log!r}")
        # No dialog: the leftmost card narrows as before (V13 is the only one that drops it).
        connection = dbq.connect(cfg.db_path)
        connection.execute("DELETE FROM relic_rewards WHERE relic_unique = ?", (d8,))
        connection.commit()
        connection.close()
        changed, asked, log, _ = play(None, [v13, d8], {d8: 3, v13: 2})
        if changed != {v13: -1} or asked or log != [
                "relic: the log names no own relic, narrowing by the leftmost card"]:
            problems.append(f"own relic: the fallback is {changed!r} / {asked!r} / {log!r}")
        connection = dbq.connect(cfg.db_path)
        connection.execute("INSERT INTO relic_rewards(relic_unique, part_unique, rarity) "
                           "VALUES(?,?,'COMMON')", (d8, braton))
        connection.commit()
        connection.close()
        # Known, held, taken off without a question - also when the others are held too.
        changed, asked, _, _ = play(eelog.OwnRelic("Meso D8 Relic", "Silver"), [],
                                    {d8: 3, d8s: 1, v13: 1})
        if changed != {d8s: -1} or asked:
            problems.append(f"own relic: Silver held took {changed!r} / asked {asked!r}")
        # Name known, refinement not: ask among the held refinements, even for one.
        unknown = eelog.OwnRelic("Meso D8 Relic", None)
        changed, asked, _, _ = play(unknown, [], {d8: 3, d8s: 1, v13: 1})
        if changed or sorted(asked) != ["Meso D8 Relic [Exceptional]", "Meso D8 Relic [Intact]"]:
            problems.append(f"own relic: unknown refinement: {changed!r} / {asked!r}")
        changed, asked, _, _ = play(unknown, [], {d8: 3, v13: 1})
        if changed or asked != ["Meso D8 Relic [Intact]"]:
            problems.append(f"own relic: one held refinement: {changed!r} / {asked!r}")
        # The lines of this screen name the refinement once: the same as the loader's.
        changed, asked, _, _ = play(unknown, [v13, d8s], {d8: 3, d8s: 1, v13: 1})
        if changed != {d8s: -1} or asked:
            problems.append(f"own relic: refinement from the screen's lines: {changed!r} / {asked!r}")
        # Refinement known, not held: ask among what is held.
        changed, asked, _, _ = play(eelog.OwnRelic("Meso D8 Relic", "Silver"), [], {d8: 3})
        if changed or asked != ["Meso D8 Relic [Intact]"]:
            problems.append(f"own relic: Silver not held: {changed!r} / {asked!r}")
        # Refined from Intact in the mission: the Intact goes unasked. Held
        # at the log's own refinement, that one goes as before.
        refined_own = eelog.OwnRelic("Meso D8 Relic", "Silver", refined_from="Bronze")
        changed, asked, log, _ = play(refined_own, [], {d8: 3})
        if changed != {d8: -1} or asked or log != [
                "relic: yours is Meso D8 Relic [Exceptional] "
                "(equip dialog, refined from Intact)"]:
            problems.append(f"own relic: refined from Intact: {changed!r} / {asked!r} / {log!r}")
        changed, asked, _, _ = play(refined_own, [], {d8: 3, d8s: 1})
        if changed != {d8s: -1} or asked:
            problems.append(f"own relic: refined, target held: {changed!r} / {asked!r}")
        # Nothing of it held: no relic is taken, and the page says so.
        changed, asked, _, feedback = play(own_d8, [], {v13: 2})
        if changed or asked or "no relic taken off the shelf" not in feedback:
            problems.append(f"own relic: none held: {changed!r} / {asked!r} / {feedback!r}")
        # The loader line alone names the identifier, so only that relic's
        # refinements count, not another relic printed with the same name. Its own
        # refinement held: taken. Not held: asked, as for the dialog.
        connection = dbq.connect(cfg.db_path)
        other_d8 = "/Test/OtherD8Bronze"
        connection.execute("INSERT INTO relics(unique_name, name, tier) VALUES(?,?,?)",
                           (other_d8, "Meso D8 Relic", "Meso"))
        dbq.set_relic_count(connection, other_d8, 2)
        connection.commit()
        connection.close()
        loader = eelog.OwnRelic(None, "Silver", unique=d8s, source="local loader")
        changed, asked, log, _ = play(loader, [], {d8: 3, d8s: 1})
        if changed != {d8s: -1} or asked or log != [
                "relic: yours is Meso D8 Relic [Exceptional] (local loader)"]:
            problems.append(f"own relic: loader, held: {changed!r} / {asked!r} / {log!r}")
        changed, asked, _, _ = play(loader, [], {d8: 3})
        if changed or asked != ["Meso D8 Relic [Intact]"]:
            problems.append(f"own relic: loader, not held: {changed!r} / {asked!r}")
        changed, asked, _, feedback = play(loader, [], {v13: 2})
        if changed or asked or "no Meso D8 Relic is recorded as held" not in feedback:
            problems.append(f"own relic: loader, none held: {changed!r} / {asked!r} / {feedback!r}")
    except Exception as exc:  # noqa: BLE001 - one failed check must not stop the run
        import traceback
        problems.append(f"own relic: raised {type(exc).__name__}: {exc}\n"
                        + traceback.format_exc(limit=4))
    finally:
        patches.restore()
        for widget in widgets:
            widget.close()
            widget.deleteLater()
    print("own relic: the 6 real screens + 1 (Meso D8, not Meso V13, at Braton, endless rounds by the "
          "dialog, no dialog = None, Silver over another player's Bronze, 5 MB back); on the "
          "page a held relic is taken without a question, an unknown refinement asks, no "
          "dialog narrows as before; no dialog takes the loader line, and the relics in "
          "play are all since the screen before")


def _check_the_log_files(app, problems: list) -> None:
    """The tracker's own log, one file per ISO week."""
    import datetime
    import io
    import subprocess
    import time

    from PySide6.QtCore import QObject, Signal

    from ui import app as app_mod, reset as reset_mod
    from ui.views.settings_view import SettingsView
    from wft import logfile

    def bad(text):
        problems.append(f"log files: {text}")

    def at(*date):
        return datetime.datetime(*date, 12, 0, 0).timestamp()

    # The name follows the ISO week, also over the turn of the year.
    for stamp, want in ((at(2026, 10, 21), "2026-W43.log"),
                        (at(2026, 12, 31), "2026-W53.log"),
                        (at(2027, 1, 1), "2026-W53.log"),
                        (at(2027, 1, 4), "2027-W01.log"),
                        (at(2024, 12, 30), "2025-W01.log")):
        if logfile.week_file_name(stamp) != want:
            bad(f"{stamp} named {logfile.week_file_name(stamp)!r}, expected {want!r}")

    class Panel(QObject):
        log_line = Signal(str)

    saved = logfile._cfg
    try:
        cfg = _setup_cfg(data=False, key="F8")
        folder = logfile.log_dir(cfg)
        if folder != cfg.data_dir_path() / "logs":
            bad(f"the folder is {folder}")

        # Nothing is written until the run enables it (the self-test never does).
        logfile._cfg = None
        panel = Panel()
        sink = io.StringIO()
        app_mod.wire_stderr_log(panel, stream=sink, clock=lambda: at(2026, 10, 21))
        panel.log_line.emit("before enable")
        if folder.exists():
            bad("a line was written before the log was enabled")

        # The same text as stderr, with the date; the week changes in the run.
        logfile._cfg = cfg
        panel.log_line.disconnect(panel._stderr_writer)
        times = iter([at(2026, 10, 21), at(2026, 10, 21), at(2026, 10, 26)])
        sink = io.StringIO()
        app_mod.wire_stderr_log(panel, stream=sink, clock=lambda: next(times, at(2026, 10, 26)))
        panel.log_line.emit("read 3 cards")
        panel.log_line.emit("hotkey: F23 from Some Mouse")
        panel.log_line.emit("click: card 2")
        first = (folder / "2026-W43.log").read_text().splitlines() \
            if (folder / "2026-W43.log").exists() else []
        second = (folder / "2026-W44.log").read_text().splitlines() \
            if (folder / "2026-W44.log").exists() else []
        if first != ["2026-10-21 12:00:00 rewards: read 3 cards",
                     "2026-10-21 12:00:00 hotkey: F23 from Some Mouse"]:
            bad(f"week 43 holds {first!r}")
        if second != ["2026-10-26 12:00:00 click: card 2"]:
            bad(f"week 44 holds {second!r}")
        for line in first + second:
            if line[11:] not in sink.getvalue().splitlines():
                bad(f"{line!r} is not the stderr line with a date in front")
        print("log files         : sample " + " | ".join(first + second))

        # Tidy: the 4 newest weeks stay; a foreign file and notes.log stay too.
        for name in ("2025-W50.log", "2025-W51.log", "2026-W01.log",
                     "2026-W20.log", "2026-W30.log"):
            (folder / name).write_text("old\n")
        (folder / "notes.log").write_text("mine\n")
        (folder / "2026-W5.log").write_text("not ours\n")
        (folder / "2020-W01.log.bak").write_text("not ours\n")
        logfile.tidy(cfg)
        left = sorted(p.name for p in folder.iterdir())
        want = sorted(["2026-W20.log", "2026-W30.log", "2026-W43.log",
                       "2026-W44.log", "notes.log", "2026-W5.log",
                       "2020-W01.log.bak"])
        if left != want:
            bad(f"after tidying: {left!r}")

        # A folder that cannot be written costs nothing.
        blocked = _setup_cfg(data=False, key="F8")
        (blocked.data_dir_path() / "logs").write_text("a file, not a folder")
        logfile._cfg = blocked
        logfile._complained = False
        quiet = io.StringIO()
        old_err, sys.stderr = sys.stderr, quiet
        try:
            panel.log_line.emit("still reads")
            logfile.tidy(blocked)
            logfile.write("more")
        finally:
            sys.stderr = old_err
        if quiet.getvalue().count("log file not written") != 1:
            bad(f"a failing folder said {quiet.getvalue()!r}")
        logfile._cfg = cfg

        # Settings names the folder; Delete everything takes it along.
        page = SettingsView(cfg)
        if str(folder) not in page.log_files_note.text() \
                or "last 4 are kept" not in page.log_files_note.text() \
                or page.log_folder_btn.text() != "Open folder":
            bad(f"Settings says {page.log_files_note.text()!r}")
        if folder not in reset_mod.tracker_files(cfg):
            bad("Delete everything leaves the log folder")

        # The status lines of main() go the same way as the others.
        from ui.app import emit_log
        sink = io.StringIO()
        emit_log("hotkey: listening for F23", at(2026, 10, 21), sink)
        emit_log("click reading: no readable mouse", at(2026, 10, 21), sink)
        got = (folder / "2026-W43.log").read_text().splitlines()[-2:]
        if got != ["2026-10-21 12:00:00 hotkey: listening for F23",
                   "2026-10-21 12:00:00 click reading: no readable mouse"] \
                or sink.getvalue().splitlines()[0] != "12:00:00 hotkey: listening for F23":
            bad(f"status lines: {got!r} {sink.getvalue()!r}")

        import inspect
        main_source = inspect.getsource(app_mod.main)
        if 'emit_log(f"hotkey: {msg}"' not in main_source \
                or 'emit_log(f"click reading: {msg}"' not in main_source:
            bad("main() does not send the hotkey and click status lines through emit_log")

        # The real self-test never calls enable(): a stand-in for it leaves a
        # marker in a folder of the test's own, outside what the self-test deletes.
        home = tempfile.mkdtemp(prefix="wft-home-")
        marker = os.path.join(home, "enable-called")
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, "c"),
                   XDG_DATA_HOME=os.path.join(home, "d"), QT_QPA_PLATFORM="offscreen",
                   WFT_TEST_MARKER=marker)
        root = pathlib.Path(__file__).resolve().parent.parent
        code = ("import os, sys; sys.path.insert(0, %r); from wft import logfile; "
                "logfile.enable = lambda cfg: open(os.environ['WFT_TEST_MARKER'], 'w')"
                ".close(); from ui import app; sys.exit(app.main(['--self-test']))"
                % str(root))
        done = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, timeout=240)
        found = list(pathlib.Path(home).rglob("logs"))
        if "SELF-TEST OK" not in done.stdout or os.path.exists(marker) or found:
            # Exit code and stderr too: once the self-test printed
            # nothing in a full run and passed alone, and this line said no more.
            bad(f"the self-test enabled the log or wrote {found} "
                f"({done.stdout.strip()[-60:]!r}, exit {done.returncode}, "
                f"stderr {done.stderr.strip()[-300:]!r})")
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(cfg.data_dir_path(), ignore_errors=True)
    finally:
        logfile._cfg = saved
        logfile._complained = False
    print("log files         : weekly names, dated stderr lines, week change, "
          "tidy to 4, failures swallowed, status lines, Settings row, reset, self-test never enables it")


# -- The gap between the cards, the calibration by keys, the overlay's end -


def _row_errors(regions_mod, tolerance=2.0) -> list[str]:
    """Where `regions` puts the cards against the measured edges, in pixels at
    3840x2160, for 2, 3 and 4 rewards. Empty when every card is within
    `tolerance`."""
    wrong = []
    for count, edges in _MEASURED_LEFT_EDGES.items():
        for index, left in enumerate(edges):
            tile = regions_mod.tile(index, count)
            got_left, got_right = tile.x * 3840, (tile.x + tile.width) * 3840
            if abs(got_left - left) > tolerance:
                wrong.append(f"{count} cards, card {index + 1}: left edge {got_left:.1f}, "
                             f"measured {left}")
            if abs(got_right - (left + 480)) > tolerance:
                wrong.append(f"{count} cards, card {index + 1}: right edge {got_right:.1f}, "
                             f"measured {left + 480}")
            band = regions_mod.name_region(index, count)
            if abs(band.x - tile.x) > 1e-9 or abs(band.width - tile.width) > 1e-9:
                wrong.append(f"{count} cards, card {index + 1}: the name band is not over the card")
    return wrong


def _check_the_row_has_a_gap_between_the_cards(problems: list) -> None:
    """Cards are 0.125 wide and 0.00122 apart, the row is centred on 0.5.

    Against the left edges read off the coloured bars of the corpus, not against
    the module's own numbers. Old config files still load.
    """
    import importlib

    from wft import config as config_mod
    from wft.relic import regions as regions_mod

    importlib.reload(regions_mod)
    try:
        for line in _row_errors(regions_mod):
            problems.append(f"row geometry: {line}")
        tile = regions_mod.tile(0, 4)
        if abs(tile.y * 2160 - 446) > 2 or abs((tile.y + tile.height) * 2160 - 919) > 2:
            problems.append(f"row geometry: tile rows {tile.y * 2160:.1f} to "
                            f"{(tile.y + tile.height) * 2160:.1f}, measured 446 to 919")

        def loaded(values):
            importlib.reload(regions_mod)
            cfg = config_mod.Config({"regions": {"reward": values}}, config_mod._EXAMPLE)
            regions_mod.load_overrides(cfg)
            return regions_mod

        # A file with only a width (what an old hand-edited file has): the gap stays.
        r = loaded({"tile_width": 0.1249})
        if r.TILE_WIDTH != 0.1249 or r.TILE_GAP != 0.00122:
            problems.append(f"row geometry: an old file with only tile_width gave "
                            f"width {r.TILE_WIDTH}, gap {r.TILE_GAP}")
        # What the first calibration wrote: a pitch equal to the width, no gap.
        r = loaded({"tile_width": 0.127, "tile_pitch": 0.127, "row_centre_x": 0.474,
                    "tile_top": 0.3, "tile_height": 0.2})
        if r.TILE_GAP != 0.0 or abs(r.tile(1, 4).x - r.tile(0, 4).x - 0.127) > 1e-9 \
                or r.ROW_CENTRE_X != 0.474:
            problems.append(f"row geometry: an old calibration (pitch = width) gave gap "
                            f"{r.TILE_GAP}, ROW_CENTRE_X {r.ROW_CENTRE_X}")
        # An old pitch wider than the width is read as a gap; a new gap wins over it.
        r = loaded({"tile_width": 0.125, "tile_pitch": 0.1262})
        if abs(r.TILE_GAP - 0.0012) > 1e-9:
            problems.append(f"row geometry: tile_pitch 0.1262 gave gap {r.TILE_GAP}")
        r = loaded({"tile_width": 0.125, "tile_pitch": 0.1262, "tile_gap": 0.003})
        if r.TILE_GAP != 0.003:
            problems.append(f"row geometry: tile_gap did not win over tile_pitch ({r.TILE_GAP})")
        # Nothing in the file: the shipped values.
        r = loaded({})
        if (r.TILE_WIDTH, r.TILE_GAP, r.ROW_CENTRE_X) != (0.125, 0.00122, 0.5):
            problems.append(f"row geometry: an empty section changed the defaults: "
                            f"{(r.TILE_WIDTH, r.TILE_GAP, r.ROW_CENTRE_X)}")

        # Counter-check: the geometry from before the gap (edge to edge, centre 0.498) is
        # what the check above must call wrong - 13 px at the third of 3, 15 at the fourth of 4.
        importlib.reload(regions_mod)
        regions_mod.TILE_WIDTH, regions_mod.TILE_GAP, regions_mod.ROW_CENTRE_X = 0.1249, 0.0, 0.498
        old = _row_errors(regions_mod)
        third = [line for line in old if line.startswith("3 cards, card 3: left")]
        if not third or "measured 2165" not in third[0]:
            problems.append("row geometry: the old edge-to-edge geometry was not caught")
        else:
            error = float(third[0].split("left edge ")[1].split(",")[0]) - 2165
            if not 10 < abs(error) < 16:
                problems.append(f"row geometry: the old geometry misses the third of three "
                                f"by {error:.1f} px, not about 13")
    finally:
        importlib.reload(regions_mod)
    print("row geometry       : cards 0.125 wide, 0.00122 apart, centred on 0.5, "
          "within 2 px of the measured edges for 2, 3 and 4 cards; old config "
          "files load; the edge-to-edge row is 13 px off at the third of three")


def _check_an_old_calibration_is_seen_and_can_be_removed(app, problems: list) -> None:
    """A calibration from before the gap between the cards is shown, and one button
    takes it out.

    Seven old keys in `[regions.reward]` overrode the measured values with
    nothing in the program saying so; the overlay frame stood 0/9/18/27 px too
    far left at 4K. A temp XDG_CONFIG_HOME holds the file; a real one is never read.
    """
    import importlib

    from ui import settings_io
    from ui.views import calibrate_dialog as dialog_mod
    from ui.views.settings_view import SettingsView
    from wft import config as config_mod, doctor
    from wft.relic import last_screen, regions as regions_mod

    old = {"tile_top": "0.2050", "tile_height": "0.2208", "tile_width": "0.1239",
           "tile_pitch": "0.1239", "row_centre_x": "0.4959", "name_top": "0.3833",
           "name_height": "0.0391"}
    head = ("# my notes\n[notifications]\nenabled = true\n\n[regions.reward]\n"
            "# calibrated before the gap\n")
    tail = "# tile_gap = 0.00122\n\n[hotkey]\n# the key\nkey = \"F23\"\n"
    text = head + "".join(f'{k} = "{v}"\n' for k, v in old.items()) + tail
    expected = head + tail

    patches = _Patches()
    dialog = page = refused = None
    try:
        home = pathlib.Path(tempfile.mkdtemp())
        patches.env("XDG_CONFIG_HOME", str(home))
        path = home / "warframe-tracker" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text(text)
        importlib.reload(regions_mod)
        last_screen.forget()
        cfg = _setup_cfg(data=True, key="F8")

        # The report: every key with the configured and the built-in value.
        found = regions_mod.configured_overrides(cfg)
        got = {k: (v, b) for k, v, b in found}
        want = {"tile_top": (0.2050, 0.2065), "tile_height": (0.2208, 0.219),
                "tile_width": (0.1239, 0.125), "tile_pitch": (0.1239, 0.125 + 0.00122),
                "row_centre_x": (0.4959, 0.5), "name_top": (0.3833, 0.380),
                "name_height": (0.0391, 0.050)}
        if [k for k, _v, _b in found] != list(want) or any(
                abs(got[k][0] - want[k][0]) > 1e-9 or abs(got[k][1] - want[k][1]) > 1e-9
                for k in want):
            problems.append(f"old calibration: the report gave {found}")
        regions_mod.load_overrides(cfg)
        if round(regions_mod.tile(3, 4).x * 3840) != 2380:
            problems.append(f"old calibration: the old values put card 4 at "
                            f"{regions_mod.tile(3, 4).x * 3840:.0f} px, not 2380")

        page = SettingsView(cfg)
        if page.regions_status.text() != \
                "Reward regions: your own calibration in config.toml (7 values)":
            problems.append(f"old calibration: Settings says {page.regions_status.text()!r}")
        lines = [detail for _l, _n, detail in doctor.check_regions(cfg)]
        if len(lines) != 1 or not all(f"{k}={float(v):g}" in lines[0] for k, v in old.items()):
            problems.append(f"old calibration: doctor says {lines}")
        ran = [d for _l, n, d in doctor.run() if n == "reward regions"]
        if len(ran) != 1 or "your own calibration" not in ran[0]:
            problems.append(f"old calibration: `wf doctor` prints {ran}")

        dialog = dialog_mod.CalibrateDialog(cfg)
        dialog.show()
        _settle(app)
        note = dialog.overrides_note.text()
        if not dialog.overrides_note.isVisibleTo(dialog) \
                or not dialog.measured_btn.isVisibleTo(dialog) \
                or "tile_top: yours 0.2050, measured 0.2065" not in note \
                or "tile_pitch: yours 0.1239, measured 0.1262" not in note:
            problems.append(f"old calibration: the dialog's note is {note!r}")
        if dialog.measured_btn.text() != "Use the measured values":
            problems.append(f"old calibration: the button reads {dialog.measured_btn.text()!r}")

        # No: nothing changes, and the question names the lines and the file.
        asked: list = []

        def answer(reply):
            def question(parent, title, body, *rest):
                asked.append(body)
                return reply
            return staticmethod(question)

        patches.set(QMessageBox, "question", answer(QMessageBox.No))
        dialog.measured_btn.click()
        if path.read_text() != text or len(regions_mod.configured_overrides(cfg)) != 7 \
                or not dialog.overrides_note.isVisibleTo(dialog):
            problems.append("old calibration: answering No changed something")
        if not asked or str(path) not in asked[0] or any(
                f'{k} = "{v}"' not in asked[0] for k, v in old.items()):
            problems.append(f"old calibration: the question was {asked!r}")

        # Yes: exactly the seven lines go, memory follows, no restart needed.
        patches.set(QMessageBox, "question", answer(QMessageBox.Yes))
        dialog.measured_btn.click()
        _settle(app)
        if path.read_text() != expected:
            problems.append(f"old calibration: the file is now {path.read_text()!r}")
        if cfg.get("regions.reward") or regions_mod.configured_overrides(cfg):
            problems.append("old calibration: the running config still has the keys")
        if round(regions_mod.tile(3, 4).x * 3840) != 2407:
            problems.append(f"old calibration: card 4 at "
                            f"{regions_mod.tile(3, 4).x * 3840:.0f} px, not 2407, "
                            f"after removing the keys")
        if dialog.overrides_note.isVisibleTo(dialog) or dialog.measured_btn.isVisibleTo(dialog):
            problems.append("old calibration: note or button stay after the removal")
        if [d for _l, _n, d in doctor.check_regions(cfg)] != ["measured values"]:
            problems.append(f"old calibration: doctor after: {doctor.check_regions(cfg)}")
        _drop(app, dialog)
        dialog = None
        page._refresh_regions_status()
        if page.regions_status.text() != "Reward regions: measured values":
            problems.append(f"old calibration: Settings after: {page.regions_status.text()!r}")

        # A dialog without overrides has neither note nor button; removing keys
        # that are not there (or from a missing file) is no error.
        dialog = dialog_mod.CalibrateDialog(cfg)
        dialog.show()
        _settle(app)
        if dialog.overrides_note.isVisibleTo(dialog) or dialog.measured_btn.isVisibleTo(dialog):
            problems.append("old calibration: note or button shown without overrides")
        settings_io.remove_keys(cfg, "regions.reward", ["tile_top"])
        path.unlink()
        settings_io.remove_keys(cfg, "regions.reward", ["tile_top"])
    except Exception as exc:  # noqa: BLE001 - a crash is a finding
        import traceback
        problems.append(f"old calibration: raised {type(exc).__name__}: {exc} "
                        f"{traceback.format_exc().splitlines()[-3:]}")
    finally:
        patches.restore()
        last_screen.forget()
        importlib.reload(regions_mod)
        if dialog is not None:
            _drop(app, dialog)
    print("old calibration    : the 7 old keys are listed with the measured values in "
          "the dialog, Settings and doctor; Use the measured values asks, removes "
          "exactly those lines and takes effect without a restart")


def _check_the_calibration_fits_the_row_by_keys(app, problems: list) -> None:
    """Draw the first card, fit it with the keys, the gap and the name band, save."""
    import importlib

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtTest import QTest

    from ui.views import calibrate_dialog as dialog_mod
    from ui.workers import relic_trigger as RT
    from wft.relic import last_screen, regions as regions_mod

    patches = _Patches()
    saved: list = []
    dialog = None
    try:
        importlib.reload(regions_mod)
        cfg = _setup_cfg(data=True, key="F8")
        last_screen.forget()
        patches.set(dialog_mod.settings_io, "set_value",
                    lambda section, key, value: saved.append((section, key, value)))
        patches.set(QMessageBox, "information", staticmethod(lambda *a, **k: None))
        names = ["Braton Prime Barrel", "Braton Prime Stock", "Braton Prime Blueprint",
                 "Braton Prime Receiver"]
        picture = _reward_screen(names)
        dialog = dialog_mod.CalibrateDialog(cfg)
        dialog.resize(1500, 1000)
        dialog.show()
        _settle(app)
        picker, marks = dialog.picker, dialog.picker.marks
        dialog._set_image(dialog._pixmap_of(picture), picture, "test picture")
        _settle(app)
        if dialog.save_btn.isEnabled() or marks.card is not None:
            problems.append("calibrate keys: Save is open before a card is drawn")
        # No card, no gap in pixels: a dash, not "0 px", and the box is off.
        if dialog.gap_spin.text() != "–" or dialog.gap_spin.isEnabled():
            problems.append(f"calibrate keys: before a card the gap shows "
                            f"{dialog.gap_spin.text()!r}, enabled {dialog.gap_spin.isEnabled()}")
        if marks.width != 3840 or dialog.count_box.currentData() != 4:
            problems.append(f"calibrate keys: picture {marks.width}, count "
                            f"{dialog.count_box.currentData()}")

        # Drag the first card open on the picture (measured edges 953..1433, 446..919).
        shown = picker._shown()
        scale = shown.width() / 3840

        def at(x, y):
            now = picker._shown()
            return QPoint(round(now.x() + x * now.width() / 3840),
                          round(now.y() + y * now.width() / 3840))

        QTest.mousePress(picker, Qt.LeftButton, Qt.NoModifier, at(953, 446))
        QTest.mouseMove(picker, at(1200, 700))
        QTest.mouseMove(picker, at(1433, 919))
        QTest.mouseRelease(picker, Qt.LeftButton, Qt.NoModifier, at(1433, 919))
        _settle(app)
        x, y, w, h = marks.card or (0, 0, 0, 0)
        tolerance = 2 / scale
        if (abs(x - 953) > tolerance or abs(y - 446) > tolerance
                or abs(w - 480) > 2 * tolerance or abs(h - 473) > 2 * tolerance):
            problems.append(f"calibrate keys: the drag gave {marks.card}, wanted about "
                            f"(953, 446, 480, 473)")
        if not dialog.gap_spin.isEnabled() or dialog.gap_spin.value() != marks.gap \
                or marks.gap != round(regions_mod.TILE_GAP * 3840):
            problems.append(f"calibrate keys: after the card the gap box shows "
                            f"{dialog.gap_spin.text()!r} for gap {marks.gap}")
        if marks.gap != round(regions_mod.TILE_GAP * 3840) or marks.count != 4:
            problems.append(f"calibrate keys: gap {marks.gap}, count {marks.count} after the drag")
        if marks.band_dy != round(0.75 * h) or marks.mode != "card":
            problems.append(f"calibrate keys: the band starts at {marks.band_dy}, mode {marks.mode}")
        if not (dialog.save_btn.isEnabled() and dialog.test_btn.isEnabled()):
            problems.append("calibrate keys: Save or Read the names stayed shut after a card")
        rects = marks.card_rects()
        if len(rects) != 4 or any(r[0] != x + k * (w + marks.gap) for k, r in enumerate(rects)):
            problems.append(f"calibrate keys: the other cards are at {rects}")
        band = marks.band_rect()
        if band != (x, y + marks.band_dy, w, marks.band_height):
            problems.append(f"calibrate keys: the band is {band}")

        # The picture shows it: red on the first card, pale orange on the others,
        # green for the band.
        def shot():
            return picker.grab().toImage()

        def coloured(image, area, predicate):
            left, top, right, bottom = area
            return any(predicate(image.pixelColor(px, py))
                       for px in range(max(0, left), min(image.width(), right))
                       for py in range(max(0, top), min(image.height(), bottom)))

        def red(c): return c.red() > 200 and c.green() < 110 and c.blue() < 110
        def orange(c): return c.red() > 200 and 130 < c.green() < 200 and c.blue() < 140
        def green(c): return c.green() > 170 and c.red() < 110 and c.blue() < 170

        # Drawing the card changes the texts above the picture, so the picture
        # may have moved: where it is now.
        shown = picker._shown()
        scale = shown.width() / 3840
        image = shot()
        sx, sy = shown.x(), shown.y()
        first_left, first_top = sx + x * scale, sy + y * scale
        if not coloured(image, (int(first_left) - 3, int(first_top) + 40, int(first_left) + 4,
                                int(first_top) + 80), red):
            problems.append("calibrate keys: no red frame at the left edge of the first card")
        second_top = sy + y * scale
        second_left = sx + rects[1][0] * scale
        if not coloured(image, (int(second_left) + 10, int(second_top) - 3, int(second_left) + 40,
                                int(second_top) + 4), orange):
            problems.append("calibrate keys: no orange frame on the second card")
        band_y = sy + band[1] * scale
        if not coloured(image, (int(first_left) + 10, int(band_y) - 3, int(first_left) + 60,
                                int(band_y) + 4), green):
            problems.append("calibrate keys: no green band on the first card")

        def key(code, mods=Qt.NoModifier):
            QTest.keyClick(picker, code, mods)

        # Arrow keys move by 1 pixel, with Ctrl by 10.
        start = marks.card
        key(Qt.Key_Right)
        key(Qt.Key_Down)
        if marks.card[:2] != (start[0] + 1, start[1] + 1):
            problems.append(f"calibrate keys: Right and Down moved to {marks.card[:2]} from {start[:2]}")
        key(Qt.Key_Left, Qt.ControlModifier)
        key(Qt.Key_Up, Qt.ControlModifier)
        if marks.card[:2] != (start[0] - 9, start[1] - 9):
            problems.append(f"calibrate keys: Ctrl+Left and Ctrl+Up moved to {marks.card[:2]}")
        # The band follows the card.
        if marks.band_rect()[:2] != (marks.card[0], marks.card[1] + marks.band_dy):
            problems.append("calibrate keys: the band did not follow the card")
        # Shift changes the size.
        before = marks.card
        key(Qt.Key_Right, Qt.ShiftModifier)
        key(Qt.Key_Down, Qt.ShiftModifier | Qt.ControlModifier)
        if marks.card[2:] != (before[2] + 1, before[3] + 10) or marks.card[:2] != before[:2]:
            problems.append(f"calibrate keys: Shift+Right and Shift+Ctrl+Down gave {marks.card} "
                            f"from {before}")
        key(Qt.Key_Left, Qt.ShiftModifier)
        key(Qt.Key_Up, Qt.ShiftModifier)
        if marks.card[2:] != (before[2], before[3] + 9):
            problems.append(f"calibrate keys: Shift+Left, Shift+Up gave {marks.card}")
        # The gap: Alt+Left/Right, with Ctrl by 10, never below 0; the spin box follows.
        gap = marks.gap
        key(Qt.Key_Right, Qt.AltModifier)
        if marks.gap != gap + 1 or dialog.gap_spin.value() != gap + 1:
            problems.append(f"calibrate keys: Alt+Right gave gap {marks.gap}, spin "
                            f"{dialog.gap_spin.value()}")
        key(Qt.Key_Left, Qt.AltModifier | Qt.ControlModifier)
        if marks.gap != 0:
            problems.append(f"calibrate keys: Alt+Ctrl+Left gave gap {marks.gap}, wanted 0")
        dialog.gap_spin.setValue(9)
        if marks.gap != 9 or marks.card_rects()[1][0] != marks.card[0] + marks.card[2] + 9:
            problems.append(f"calibrate keys: the spin box gave gap {marks.gap}")
        key(Qt.Key_Right, Qt.AltModifier)
        key(Qt.Key_Left, Qt.AltModifier)
        # Alt without a card key moves the card? No: Up and Down with Alt do nothing.
        card = marks.card
        key(Qt.Key_Up, Qt.AltModifier)
        if marks.card != card or marks.gap != 9:
            problems.append("calibrate keys: Alt+Up changed something")

        # Tab switches to the band: Up and Down move it, Shift changes its height,
        # Left and Right do nothing, the card stays.
        key(Qt.Key_Tab)
        if marks.mode != "band" or not dialog.adjust_band.isChecked():
            problems.append(f"calibrate keys: Tab left the mode at {marks.mode}")
        card, dy, bh = marks.card, marks.band_dy, marks.band_height
        key(Qt.Key_Down)
        key(Qt.Key_Down, Qt.ControlModifier)
        key(Qt.Key_Right)
        if marks.band_dy != dy + 11 or marks.card != card:
            problems.append(f"calibrate keys: Down in band mode gave band_dy {marks.band_dy} "
                            f"from {dy}, card {marks.card}")
        key(Qt.Key_Down, Qt.ShiftModifier)
        key(Qt.Key_Up, Qt.ShiftModifier | Qt.ControlModifier)
        if marks.band_height != max(4, bh + 1 - 10) or marks.band_dy != dy + 11:
            problems.append(f"calibrate keys: Shift+Down in band mode gave height "
                            f"{marks.band_height} from {bh}")
        key(Qt.Key_Tab)
        if marks.mode != "card" or not dialog.adjust_card.isChecked():
            problems.append("calibrate keys: a second Tab did not come back to the card")
        # The radio buttons do the same; a click on the band or a card chooses.
        dialog.adjust_band.setChecked(True)
        if marks.mode != "band":
            problems.append("calibrate keys: the radio button did not choose the band")
        inside_card = at(marks.card[0] + marks.card[2] // 2, marks.card[1] + 40)
        QTest.mouseClick(picker, Qt.LeftButton, Qt.NoModifier, inside_card)
        if marks.mode != "card":
            problems.append("calibrate keys: a click on the card did not choose the card")
        b = marks.band_rect()
        QTest.mouseClick(picker, Qt.LeftButton, Qt.NoModifier, at(b[0] + b[2] // 2, b[1] + b[3] // 2))
        if marks.mode != "band":
            problems.append("calibrate keys: a click on the band did not choose the band")
        dialog.adjust_card.setChecked(True)

        # Two or three cards: the count follows the box.
        dialog.count_box.setCurrentIndex(0)
        if marks.count != 2 or len(marks.card_rects()) != 2:
            problems.append(f"calibrate keys: 2 cards gave {len(marks.card_rects())} rects")
        dialog.count_box.setCurrentIndex(2)

        # The numbers: from the marks, centred on the row.
        values = dialog._values()
        x, y, w, h = marks.card
        span = 4 * w + 3 * marks.gap
        expected = {"tile_top": y / 2160, "tile_height": h / 2160, "tile_width": w / 3840,
                    "tile_gap": marks.gap / 3840, "row_centre_x": (x + span / 2) / 3840,
                    "name_top": (y + marks.band_dy) / 2160,
                    "name_height": marks.band_height / 2160}
        if set(values) != set(expected) or any(abs(values[k] - expected[k]) > 1e-9 for k in expected):
            problems.append(f"calibrate keys: the values are {values}")

        # Read the names: the regions go to the reading as they are marked, tile_gap included.
        seen: list = []
        patches.set(RT, "run_on_image",
                    lambda cfg_, frame, report=None, regions=None, use_log=True:
                    seen.append(regions) or _trigger_result(names, 4))
        got: list = []
        dialog.tested.connect(got.append)
        dialog.test_btn.click()
        if not seen or "tile_gap" not in seen[0] or abs(seen[0]["tile_gap"] - values["tile_gap"]) > 1e-9 \
                or len(got) != 1:
            problems.append(f"calibrate keys: Read the names passed {seen!r}")
        if regions_mod.TILE_GAP != 0.00122:
            problems.append("calibrate keys: Read the names left its regions behind")

        # Save: every key, into regions.reward, 5 decimals.
        dialog.save_btn.click()
        keys = {key for _section, key, _value in saved}
        wanted = {"tile_top", "tile_height", "tile_width", "tile_gap", "row_centre_x",
                  "name_top", "name_height"}
        if keys != wanted or any(section != "regions.reward" for section, _k, _v in saved):
            problems.append(f"calibrate keys: Save wrote {sorted(keys)}")
        written = {key: value for _s, key, value in saved}
        if any(abs(float(written[k]) - values[k]) > 6e-6 for k in wanted if k in written):
            problems.append(f"calibrate keys: Save wrote {written}")

        # Start from the current regions: the marks land on the measured card.
        saved.clear()
        importlib.reload(regions_mod)
        dialog2 = dialog_mod.CalibrateDialog(cfg)
        dialog2.resize(1500, 1000)
        dialog2.show()
        dialog2._set_image(dialog2._pixmap_of(picture), picture, "again")
        _settle(app)
        dialog2.current_btn.click()
        m2 = dialog2.picker.marks
        if m2.card is None or any(abs(a - b) > 2 for a, b in zip(
                m2.card, (953, 446, 480, 473))) or m2.gap != 5:
            problems.append(f"calibrate keys: Start from the current regions gave {m2.card}, "
                            f"gap {m2.gap}")
        round_trip = dialog2._values()
        if abs(round_trip["row_centre_x"] - 0.5) > 3e-4 or abs(round_trip["name_top"] - regions_mod.NAME_TOP) > 6e-4:
            problems.append(f"calibrate keys: the round trip gave {round_trip}")
        _drop(app, dialog2)

        # A reading that named 3 rewards starts the row with 3 cards.
        last_screen.remember(picture, _trigger_result(["a", "b", "c"], 3))
        dialog3 = dialog_mod.CalibrateDialog(cfg)
        dialog3.show()
        _settle(app)
        if dialog3.count_box.currentData() != 3 or dialog3.picker.marks.count != 3:
            problems.append(f"calibrate keys: the last screen's 3 rewards gave count "
                            f"{dialog3.count_box.currentData()}")
        _drop(app, dialog3)
    except Exception as exc:  # noqa: BLE001 - a crash is a finding
        import traceback
        problems.append(f"calibrate keys: raised {type(exc).__name__}: {exc} "
                        f"{traceback.format_exc().splitlines()[-3:]}")
    finally:
        patches.restore()
        last_screen.forget()
        importlib.reload(regions_mod)
        if dialog is not None:
            _drop(app, dialog)
    print("calibrate keys     : a dragged card shows red, the others orange, the band "
          "green; arrows 1 px and Ctrl 10 px, Shift the size, Alt+Left/Right and the "
          "spin box the gap, Tab and a click switch card and band; Read the names "
          "and Save carry tile_gap")


def _check_a_click_after_the_screen_closed_is_no_pick(app, problems: list) -> None:
    """A click that arrives once the header is missing is not a pick.

    A live log: the real pick, clicks outside the cards, the
    first miss, then a click on the next screen where card 1 had been, then the
    end. The held click is dropped when the overlay ends and counts only when
    the header comes back (one missed frame).
    """
    from ui.overlay import rewards as R
    from wft import config as config_mod
    from wft.capture import window as cap
    from wft.relic import header

    patches = _Patches()
    try:
        cfg = config_mod.load()
        cfg.override("overlay.click_to_pick", True)
        fake = _FakeOverlayClient()
        clock = [100.0]
        answers: list = []
        patches.set(header, "is_present", lambda _mask, strip: strip)
        overlay = R.RewardOverlay(cfg, fake, grab=lambda _p: answers.pop(0),
                                  clock=lambda: clock[0])
        where = cap.Placement(7, 100, 50, 1920, 1080)
        args = {"tiles": _OVERLAY_TILES, "best": 1, "headline": None, "lines": [["a"]] * 4}
        notes: list = []
        settled: list = []
        overlay.note.connect(notes.append)
        overlay.settling.connect(lambda _t, index: settled.append(index))

        def point(index):
            tx, ty, tw, th = _OVERLAY_TILES[index]
            return (where.x + (tx + tw / 2) * where.width,
                    where.y + (ty + th / 2) * where.height)

        def run(steps):
            """`steps`: ("click", card) / ("outside",) / (seconds, present)."""
            notes.clear()
            settled.clear()
            clock[0] += 1000.0
            start = clock[0]
            token = overlay.show(args, where, object(), started=start)
            fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                    "token": token})
            for step in steps:
                if step[0] == "click":
                    overlay.on_click(*point(step[1]))
                elif step[0] == "outside":
                    overlay.on_click(where.x - 50, where.y + 5)
                else:
                    clock[0] = start + step[0]
                    answers[:] = [step[1]]
                    overlay.tick()
            overlay.hide()
            return list(notes), list(settled)

        # The two cases of the log: header gone, the click of the next screen.
        got, booked = run([("click", 1), ("outside",), (14.3, False), ("click", 0),
                           (14.8, False)])
        if booked != [1] or "click: after the screen closed" not in got \
                or "click: card 1" in got:
            problems.append(f"click after the close: card 2 case booked {booked!r}, log {got!r}")
        got, booked = run([("click", 2), ("outside",), ("outside",), (14.5, False),
                           ("click", 0), (15.0, True)])
        if booked != [2] or "click: card 1" in got:
            problems.append(f"click after the close: card 3 case at the limit booked "
                            f"{booked!r}, log {got!r}")
        # A single missed frame: the held click counts when the header is back.
        got, booked = run([("click", 1), (5.0, False), ("click", 2), (5.5, True),
                           (6.0, True)])
        if booked != [2] or "click: card 3" not in got:
            problems.append(f"click after the close: a flicker booked {booked!r}, log {got!r}")
        # While the header is seen a click counts at once, as before.
        got, booked = run([("click", 1), (5.0, True), ("click", 3), (5.5, True)])
        if booked != [3] or "click: card 4" not in got:
            problems.append(f"click after the close: a click with the header seen: {got!r}")
        got, booked = run([("click", 1), (5.0, True), ("click", 3), (5.5, True), (14.0, False),
                           (14.5, False)])
        if booked != [3]:
            problems.append(f"click after the close: a normal click booked {booked!r}")
    finally:
        patches.restore()


def _check_the_overlay_says_why_it_ended(app, problems: list) -> None:
    """Each end of a reward overlay is one line in the log, with the time."""
    from ui import app as app_mod
    from ui.overlay import rewards as R
    from wft import config as config_mod
    from wft.capture import window as cap
    from wft.relic import header

    patches = _Patches()
    try:
        cfg = config_mod.load()
        fake = _FakeOverlayClient()
        clock = [100.0]
        answers: list = []
        patches.set(header, "is_present", lambda _mask, strip: strip)
        overlay = R.RewardOverlay(cfg, fake, grab=lambda _p: answers.pop(0),
                                  clock=lambda: clock[0])
        where = cap.Placement(7, 100, 50, 1920, 1080)
        args = {"tiles": _OVERLAY_TILES, "best": 1, "headline": None, "lines": [["a"]] * 4}
        notes: list = []
        overlay.note.connect(notes.append)
        mask = object()

        def run(label, steps, mask_=mask, seconds=R.MAX_SECONDS, confirm=True, hide=False):
            """One overlay: `steps` are (seconds since the start, header present)."""
            notes.clear()
            clock[0] += 1000.0
            start = clock[0]
            token = overlay.show(args, where, mask_, started=start, seconds=seconds)
            if confirm:
                fake.reward_state.emit({"type": "reward", "shown": True, "reason": "show",
                                        "token": token})
            for at, present in steps:
                clock[0] = start + at
                answers[:] = [present]
                overlay.tick()
            if hide:
                clock[0] = start + hide
                overlay.hide()
            return list(notes)

        def ends(lines):
            return [line for line in lines if "ended" in line or "hidden" in line
                    or "replaced" in line]

        # The header goes: the first miss at 11.5 s, gone 0.5 s later.
        got = run("header", [(10.0, True), (11.5, False), (12.0, False)])
        if got != ["overlay: header not seen for the first time, 11.5 s after the reading",
                   "overlay: ended, header gone 0.5 s after the first miss (12.0 s after the reading)"]:
            problems.append(f"overlay log: header end: {got!r}")
        # A miss that recovers, then gone: the first miss is named once, the end
        # counts from it.
        got = run("recovers", [(5.0, False), (5.5, True), (9.0, False), (9.5, False)])
        first = [line for line in got if "for the first time" in line]
        if first != ["overlay: header not seen for the first time, 5.0 s after the reading"] \
                or got[-1] != "overlay: ended, header gone 4.5 s after the first miss (9.5 s after the reading)":
            problems.append(f"overlay log: miss, recovery, gone: {got!r}")
        # The 15 s limit with the header still there.
        got = run("limit", [(14.5, True), (15.0, True)])
        if got != ["overlay: ended at the 15 s limit (header still seen)"]:
            problems.append(f"overlay log: the limit: {got!r}")
        # The limit after a single miss that came back.
        got = run("limit after a miss", [(3.0, False), (3.5, True), (15.0, True)])
        if got[-1] != "overlay: ended at the 15 s limit (header missed at 3.0 s, not twice in a row)":
            problems.append(f"overlay log: limit after one miss: {got!r}")
        # Without a header to compare, the test button's 5 s.
        got = run("no header", [(5.0, None)], mask_=None, seconds=R.TEST_SECONDS)
        if got != ["overlay: ended at the 5 s limit (no header check)"]:
            problems.append(f"overlay log: no header: {got!r}")
        # Never confirmed by the helper.
        got = run("unconfirmed", [(15.0, None)], confirm=False)
        if got != ["overlay: ended at the 15 s limit (the helper had not confirmed)"]:
            problems.append(f"overlay log: unconfirmed: {got!r}")
        # Replaced by a new reading, and hidden.
        notes.clear()
        clock[0] += 1000.0
        first_token = overlay.show(args, where, mask, started=clock[0])
        clock[0] += 4.2
        overlay.show(args, where, mask, started=clock[0])
        if notes != ["overlay: replaced by a new reading (4.2 s after the reading)"]:
            problems.append(f"overlay log: replaced: {notes!r}")
        notes.clear()
        clock[0] += 2.0
        overlay.hide()
        if notes != ["overlay: hidden (2.0 s after the reading)"]:
            problems.append(f"overlay log: hidden: {notes!r}")
        if first_token is None:
            problems.append("overlay log: no token")
        # Once: ending a finished overlay says nothing more.
        notes.clear()
        overlay.hide()
        if notes:
            problems.append(f"overlay log: a second hide said {notes!r}")
        # Every line is tagged `overlay:` and goes to the log as it is.
        if any(app_mod.tagged(line) != line for line in (
                "overlay: hidden (2.0 s after the reading)",
                "overlay: ended at the 15 s limit (header still seen)")):
            problems.append("overlay log: the tag `overlay:` is not kept in the log")
        if app_mod.tagged("overlay not shown: x") != "rewards: overlay not shown: x":
            problems.append("overlay log: an old line lost its tag")
        overlay.hide()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"overlay log: raised {type(exc).__name__}: {exc}")
    finally:
        patches.restore()
    print("overlay log        : the end of an overlay is one tagged line: header gone "
          "(after the first miss, with the time of that miss), the limit (header seen, "
          "missed once, no check, helper silent), replaced, hidden")



# -- the trading pages ------------------------------------------------

def _trading_fixture(home) -> None:
    """A small Prime shelf in `home`, the shapes the two pages must tell apart.

    Ash Prime: open, 2 short (Blueprint, Systems), 2 Chassis held so 1 is spare
    (price 5, 3 days old). Zed Prime: open, 1 short (Barrel, vaulted). Frost
    Prime: 3 short, and a spare Zed Receiver in a second category. Mesa Prime: mastered, 1 short. Volt Prime: mastered, every
    piece spare, with a dear price, a missing one, a never-asked one and a
    cheap fresh one. Cedo: not Prime, spare barrels that must never show.
    """
    from wft.data import db as dbq

    conn = dbq.connect(home / "warframe.sqlite")
    schema = pathlib.Path(dbq.__file__).resolve().parent / "schema.sql"
    conn.executescript(schema.read_text())

    def item(unique, name, category, prime=1):
        conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                     "VALUES(?,?,?,?)", (unique, name, category, prime))

    def part(unique, name, owner, ducats=None):
        conn.execute("INSERT INTO parts(unique_name, name, item_unique, ducats, "
                     "count_needed) VALUES(?,?,?,?,1)", (unique, name, owner, ducats))

    def hold(unique, count):
        conn.execute("INSERT INTO owned_parts(part_unique, count, source) "
                     "VALUES(?,?,'test')", (unique, count))

    def four(key, name):
        item(f"W:{key}", name, "warframe")
        for suffix, label, value in (("bp", "Blueprint", 100), ("n", "Neuroptics", 45),
                                     ("c", "Chassis", 15), ("s", "Systems", 65)):
            part(f"P:{key}-{suffix}", f"{name} {label}", f"W:{key}", value)

    four("ash", "Ash Prime")
    hold("P:ash-n", 1)
    hold("P:ash-c", 2)
    four("frost", "Frost Prime")
    hold("P:frost-n", 1)
    four("mesa", "Mesa Prime")
    conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                 "VALUES('W:mesa', 0, 1, 'test')")
    for suffix in ("bp", "n", "c"):
        hold(f"P:mesa-{suffix}", 1)
    four("volt", "Volt Prime")
    conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                 "VALUES('W:volt', 0, 1, 'test')")
    for suffix in ("bp", "n", "c"):
        hold(f"P:volt-{suffix}", 1)
    hold("P:volt-s", 3)
    item("W:zed", "Zed Prime", "primary")
    for suffix, label in (("b", "Barrel"), ("r", "Receiver"), ("s", "Stock")):
        part(f"P:zed-{suffix}", f"Zed Prime {label}", "W:zed", 45)
    hold("P:zed-r", 2)      # one spare, in another category than the rest
    hold("P:zed-s", 1)
    conn.execute("INSERT INTO relics(unique_name, name, tier, vaulted) "
                 "VALUES('R:zed', 'Lith Z1 Relic', 'Lith', 1)")
    conn.execute("INSERT INTO relic_rewards VALUES('R:zed', 'P:zed-b', 'COMMON')")
    item("W:cedo", "Cedo", "primary", 0)
    part("P:cedo-b", "Cedo Barrel", "W:cedo")
    conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                 "VALUES('W:cedo', 0, 1, 'test')")
    hold("P:cedo-b", 2)

    def listing(unique, slug, sell=None, age_hours=None):
        conn.execute("INSERT INTO market_items(unique_name, slug, item_id) "
                     "VALUES(?,?,?)", (unique, slug, slug))
        if age_hours is not None:
            conn.execute("INSERT INTO market_prices(slug, sell_min, fetched_at) "
                         "VALUES(?,?,datetime('now', ?))",
                         (slug, sell, f"-{age_hours} hours"))

    listing("P:ash-c", "ash_chassis", 5, 72)      # cheap, three days old
    listing("P:volt-bp", "volt_bp", 30, 1)        # fresh, dear
    listing("P:volt-n", "volt_n")                 # never asked
    listing("P:volt-c", "volt_c", None, 1)        # asked, nobody sells it
    listing("P:volt-s", "volt_s", 12, 1)          # fresh, over a limit of 10
    listing("P:ash-bp", "ash_bp")                 # for the lookup count
    conn.execute("INSERT INTO imports(written_at, source, kind) "
                 "VALUES(datetime('now', '-3 days'), 'test', 'scan')")
    conn.commit()
    conn.close()


def _names(rows) -> set:
    return {(r.item, r.part) for r in rows}


def _trading_table(page):
    holder = page.table_holder.itemAt(0)
    widget = holder.widget() if holder is not None else None
    return widget if hasattr(widget, "rowCount") else None


def _shown(page) -> set:
    """What the table shows, as (item, part) read from its cells."""
    table = _trading_table(page)
    if table is None:
        return set()
    return {(table.item(r, 1).text(), table.item(r, 2).text())
            for r in range(table.rowCount())}


def _click_chip(page, key) -> None:
    page.chips._buttons[key].click()


def _check_the_trading_pages(app, problems: list) -> None:
    """Both pages: what they show, what they copy, what they book and keep.

    The lists are `wft/trading.py`'s; this checks that the page puts nothing of
    its own between them and the screen: the table and the preview are the same
    rows, every filter is the core's filter, the price lookup counts the list
    before the price limit, and the two buttons that write go through the
    functions the other pages write with.
    """
    import datetime
    import tomllib

    from PySide6.QtWidgets import QMessageBox as Box
    from ui import app as app_mod, settings_io
    from ui.views.inventory_view import InventoryView
    from ui.views.trading_view import GOT_ONE, TradingView
    from wft import config as config_mod, trading
    from wft.data import db as dbq
    from wft.market import client as market

    patches = _Patches()
    config_home = pathlib.Path(tempfile.mkdtemp(prefix="wft-trading-cfg-"))
    patches.env("XDG_CONFIG_HOME", str(config_home))
    home = pathlib.Path(tempfile.mkdtemp(prefix="wft-trading-"))
    originals = (dbq.set_part_count, dbq.trade_spare, Box.question)
    try:
        _trading_fixture(home)
        cfg = config_mod.load()
        cfg.override("paths.data_dir", str(home))
        conn = dbq.connect(cfg.db_path)
        failed = len(problems)

        def expect(ok, text):
            if not ok:
                problems.append(f"trading: {text}")

        # T1 the navigation lists the group and both pages, and they built.
        before = len(app_mod._BUILD_FAILURES)
        _app, window = app_mod.build_application(cfg)
        headings = [window.nav_list.item(i).text() for i in range(window.nav_list.count())]
        expect("TRADING" in headings and "I want" in window.NAV_ITEMS
               and "I offer" in window.NAV_ITEMS
               and headings.index("TRADING") > headings.index("COLLECTION"),
               f"the navigation has no Trading group after Collection: {headings}")
        expect(isinstance(window._pages.get("I want"), TradingView)
               and isinstance(window._pages.get("I offer"), TradingView)
               and len(app_mod._BUILD_FAILURES) == before,
               f"a trading page did not build: {app_mod._BUILD_FAILURES[before:]}")
        window.close()

        want = TradingView(cfg, "want")
        offer = TradingView(cfg, "offer")
        want.refresh()
        offer.refresh()
        target = config_home / "warframe-tracker" / "config.toml"
        expect(not target.exists(), "a trading page wrote the config just by being built")
        today = datetime.date.today()

        def markdown(page, rows):
            return trading.to_markdown(
                rows, page.kind, trade_for="ducats",
                max_missing=page.limit_spin.value() if page.kind == "want" else 2,
                as_of=today)

        # T2 the preview is the core's message for exactly these rows.
        expect(want.preview.toPlainText() == markdown(want, trading.wanted_rows(conn)),
               "I want: the preview is not to_markdown of the shown rows")
        expect(offer.preview.toPlainText() == markdown(offer, trading.offer_rows(conn)),
               "I offer: the preview is not to_markdown of the shown rows")
        expect(_shown(want) == _names(trading.wanted_rows(conn))
               and _shown(offer) == _names(trading.offer_rows(conn)),
               "a table does not show the rows the core returns")
        expect(("Ash Prime", "Blueprint") in _shown(want)
               and not any(i in ("Cedo", "Frost Prime", "Mesa Prime") for i, _ in _shown(want))
               and not any(i == "Cedo" for i, _ in _shown(offer)),
               f"I want shows {sorted(_shown(want))}")

        # T3 the I want filters, each against the core and against what it means.
        def want_set():
            expect(_shown(want) == _names(want.rows()),
                   f"I want: the table and rows() differ at {want.limit_spin.value()}")
            return {i for i, _ in _shown(want)}

        expect(want_set() == {"Ash Prime", "Zed Prime"}, f"I want default: {want_set()}")
        want.limit_spin.setValue(1)
        expect(want_set() == {"Zed Prime"}, f"I want, at most 1 part: {want_set()}")
        want.limit_spin.setValue(3)
        expect(want_set() == {"Ash Prime", "Zed Prime", "Frost Prime"},
               f"I want, at most 3 parts: {want_set()}")
        want.limit_spin.setValue(2)
        want.unmastered_box.setChecked(False)
        expect(want_set() == {"Ash Prime", "Zed Prime", "Mesa Prime"},
               f"I want, mastered sets too: {want_set()}")
        want.unmastered_box.setChecked(True)
        _click_chip(want, "primary")
        expect(want_set() == {"Zed Prime"}, f"I want, chip Primary: {want_set()}")
        _click_chip(want, "warframe")
        expect(want_set() == {"Ash Prime", "Zed Prime"},
               f"I want, chips Primary and Warframes: {want_set()}")
        _click_chip(want, "warframe")
        _click_chip(want, "primary")          # no chip left: All again
        want.vaulted_box.setChecked(True)
        expect(want_set() == {"Zed Prime"}, f"I want, vaulted only: {want_set()}")
        _click_chip(want, "warframe")
        expect(want_set() == set(),
               f"I want, vaulted and Warframes together should be empty: {want_set()}")
        want.vaulted_box.setChecked(False)
        _click_chip(want, "warframe")
        expect(want_set() == {"Ash Prime", "Zed Prime"}, f"I want, filters off: {want_set()}")

        # T4 the I offer filters: limit, old and missing prices, the default.
        def offer_set():
            expect(_shown(offer) == _names(offer.rows()), "I offer: table and rows() differ")
            return _shown(offer)

        def volt(*names):
            return {("Volt Prime", n) for n in names}

        mesa = {("Mesa Prime", n) for n in ("Blueprint", "Neuroptics", "Chassis")}  # no price
        zed = {("Zed Prime", "Receiver")}                                           # no price

        expect(offer_set() >= volt("Blueprint", "Neuroptics", "Chassis", "Systems")
               and ("Ash Prime", "Chassis") in offer_set(),
               f"I offer, no limit should list every part: {sorted(offer_set())}")
        expect(offer.unpriced_box.isChecked(),
               "I offer: without a limit the unpriced box should be on")
        offer.limit_spin.setValue(10)
        expect(not offer.unpriced_box.isChecked(),
               "I offer: with a limit the unpriced box should turn itself off")
        expect(offer_set() == {("Ash Prime", "Chassis")},        # 5p, 3 d old: stays
               f"I offer, at most 10p: {sorted(offer_set())}")
        offer.unpriced_box.click()
        expect(offer_set() == {("Ash Prime", "Chassis")} | volt("Neuroptics", "Chassis") | mesa | zed,
               f"I offer, at most 10p with unpriced parts: {sorted(offer_set())}")
        offer.limit_spin.setValue(0)           # touched: the box stays as the user left it
        expect(offer.unpriced_box.isChecked(),
               "I offer: a touched unpriced box changed by itself")
        offer.limit_spin.setValue(40)
        offer.unpriced_box.setChecked(False)
        expect(offer_set() == {("Ash Prime", "Chassis")} | volt("Blueprint", "Systems"),
               f"I offer, at most 40p: {sorted(offer_set())}")
        offer.limit_spin.setValue(0)
        offer.unpriced_box.setChecked(True)
        _click_chip(offer, "primary")
        expect(offer_set() == zed, f"I offer, chip Primary: {sorted(offer_set())}")
        _click_chip(offer, "primary")

        # T5 the price cell says the price and its age.
        table = _trading_table(offer)
        cells = {table.item(r, 1).text() + " " + table.item(r, 2).text():
                 table.item(r, 5).text() for r in range(table.rowCount())}
        expect(cells.get("Ash Prime Chassis") == "5p · 3 d ago"
               and cells.get("Volt Prime Neuroptics") == "—"
               and cells.get("Volt Prime Blueprint") == "30p · today",
               f"the Platinum column reads {cells}")

        # T6 the lookup counts the list before the price limit, and the button
        # hands exactly those parts to the shared fetch (no network here).
        offer.limit_spin.setValue(10)
        offer.unpriced_box.setChecked(False)
        limited = offer.rows()
        full = trading.offer_rows(conn, max_platinum=0, include_unpriced=True)
        counted = len(trading.parts_to_price(conn, full))
        button, _note = offer._market_widgets["list"]
        expect(button.text() == f"Look up platinum prices ({counted})",
               f"the lookup button says {button.text()!r}, not {counted}")
        expect(counted > len(trading.parts_to_price(conn, limited)),
               "the lookup count should come from the list before the price limit")
        asked = []
        offer._fetch_prices = lambda uniques, key: asked.append((list(uniques), key))
        button.click()
        expect(asked == [(market.unpriced(conn, [r.part_unique for r in full]), "list")],
               f"the lookup button asked for {asked}")
        del offer._fetch_prices
        offer.limit_spin.setValue(0)
        offer.unpriced_box.setChecked(True)

        # T7 Copy message: text/plain only, the same text as the preview.
        offer.copy_button.click()
        mime = QApplication.clipboard().mimeData()
        expect(mime is not None and list(mime.formats()) == ["text/plain"]
               and mime.text() == offer.preview.toPlainText(),
               f"the clipboard holds {list(mime.formats()) if mime else None}")
        expect(offer.copied_note.text().startswith(
            f"Copied ({len(offer.preview.toPlainText())} characters")
            and offer.copy_button.text() == "Copied",
            f"the copy note says {offer.copied_note.text()!r}, the button "
            f"{offer.copy_button.text()!r}")
        # The button reads "Copy message" again after a moment.
        from ui.views import trading_view as copied_mod
        _wait(app, lambda: offer.copy_button.text() == "Copy message",
              copied_mod.COPIED_MS / 1000 + 1.0)
        expect(offer.copy_button.text() == "Copy message",
               f"the button still reads {offer.copy_button.text()!r}")

        # T8 Trade for changes the message and its brackets, not the rows; the
        # table keeps both columns; the hint counts the parts without a price.
        rows_before = _shown(offer)
        expect(offer.trade_for() == "ducats" and not offer.price_hint.text(),
               "Trade for does not start at ducats without a hint")
        offer.trade_combo.setCurrentIndex(offer.trade_combo.findData("platinum"))
        plain_text = offer.preview.toPlainText()
        expect("Ducats per part" not in plain_text and "(5p)" in plain_text
               and plain_text.startswith("**Prime parts I sell**")
               and "checked " in plain_text and _shown(offer) == rows_before
               and plain_text == trading.to_markdown(
                   offer._rows, "offer", trade_for="platinum", as_of=today),
               f"Trade for = Platinum did not change the message as asked:\n{plain_text}")
        priceless = trading.missing_prices(offer._rows)
        expect(priceless and offer.price_hint.text()
               == f"{priceless} of {len(offer._rows)} parts have no price yet; "
                  "Look up platinum prices asks for them.",
               f"the price hint says {offer.price_hint.text()!r}")
        header_row = _trading_table(offer)
        heads = [header_row.horizontalHeaderItem(c).text()
                 for c in range(header_row.columnCount())]
        expect("Ducats" in heads and "Platinum" in heads,
               f"the table lost a price column: {heads}")
        offer.trade_combo.setCurrentIndex(offer.trade_combo.findData("both"))
        expect(offer.preview.toPlainText().startswith("**Prime parts I can give away or sell**")
               and ", 5p)" in offer.preview.toPlainText()
               and "Ducats and platinum per part in brackets" in offer.preview.toPlainText(),
               "Trade for = Parts or platinum did not change the message as asked")
        offer.trade_combo.setCurrentIndex(offer.trade_combo.findData("ducats"))
        expect(not offer.price_hint.text()
               and offer.preview.toPlainText() == markdown(offer, offer._rows),
               "back at ducats the hint or the message did not return")

        # T9 Got one books +1 through set_part_count, source manual, and the
        # Collection page's "Add and correct" writes through the same call.
        calls = []
        original = dbq.set_part_count

        def spy(connection, unique, count, source="manual"):
            calls.append((unique, count, source))
            return original(connection, unique, count, source)

        dbq.set_part_count = spy
        table = _trading_table(want)
        last = table.columnCount() - 1
        row = next(r for r in range(table.rowCount())
                   if (table.item(r, 1).text(), table.item(r, 2).text())
                   == ("Ash Prime", "Blueprint"))
        expect(table.item(row, last).text() == GOT_ONE,
               "the I want action column does not say Got one")
        table.cellClicked.emit(row, last)
        expect(calls == [("P:ash-bp", 1, "manual")],
               f"Got one wrote {calls}, not one set_part_count(+1, manual)")
        expect(dbq.owned_count(conn, "P:ash-bp") == 1, "Got one did not add the piece")
        expect(("Ash Prime", "Blueprint") not in _shown(want)
               and ("Ash Prime", "Systems") in _shown(want),
               "I want did not refresh after Got one")
        inventory = InventoryView(cfg)
        calls.clear()
        inventory.name_edit.setText("Ash Prime Systems")
        inventory.count_spin.setValue(1)
        inventory._add_entry()
        expect(calls == [("P:ash-s", 1, "manual")],
               f"Add and correct wrote {calls}, not a set_part_count(manual)")
        inventory.deleteLater()
        dbq.set_part_count = original

        # T10 It's gone: asks first; No changes nothing, Yes books -1 through
        # trade_spare, and a stale row can never take a piece a set still needs.
        def shelf(unique):
            return dbq.owned_count(conn, unique)

        def click_gone(item, part_name):
            table = _trading_table(offer)
            row = next(r for r in range(table.rowCount())
                       if (table.item(r, 1).text(), table.item(r, 2).text())
                       == (item, part_name))
            table.cellClicked.emit(row, table.columnCount() - 1)

        offer.refresh()
        asked, trades = [], []
        real_trade = dbq.trade_spare

        def trade_spy(connection, unique, copies=None, source="manual"):
            trades.append((unique, copies, source))
            return real_trade(connection, unique, copies, source)

        dbq.trade_spare = trade_spy
        Box.question = staticmethod(lambda *a, **k: (asked.append(a[1]), Box.No)[1])
        click_gone("Volt Prime", "Systems")
        expect(len(asked) == 1 and shelf("P:volt-s") == 3 and not trades,
               f"It's gone on No: asked {len(asked)}, held {shelf('P:volt-s')}, wrote {trades}")
        Box.question = staticmethod(lambda *a, **k: Box.Yes)
        click_gone("Volt Prime", "Systems")
        expect(shelf("P:volt-s") == 2 and trades == [("P:volt-s", 1, "manual")],
               f"It's gone on Yes: held {shelf('P:volt-s')}, wrote {trades}")
        retained = dbq.retained_counts(conn)["P:ash-c"]      # 2 held, 1 kept, 1 spare
        click_gone("Ash Prime", "Chassis")
        expect(shelf("P:ash-c") == retained == 1
               and ("Ash Prime", "Chassis") not in _shown(offer),
               f"the last spare Chassis: held {shelf('P:ash-c')}, kept {retained}")
        stale = trading.Row("warframe", "Ash Prime", "W:ash", "P:ash-c", "Chassis",
                            1, 15, None, None, None, 1, 4, False)
        offer._gone(stale)
        expect(shelf("P:ash-c") == retained,
               f"a stale row took a piece below the floor: {shelf('P:ash-c')}")
        dbq.trade_spare, Box.question = originals[1], originals[2]

        # T11 the age line: stock from the import row, mastery not yet recorded.
        expect("Mastery last imported: not recorded yet" in want.age_label.text()
               and "Stock last changed: " in want.age_label.text()
               and "(3 d ago)" in want.age_label.text(),
               f"the age line says {want.age_label.text()!r}")
        dbq.note_mastery_import(conn)
        want.refresh()
        expect("not recorded yet" not in want.age_label.text()
               and "(today)" in want.age_label.text(),
               f"the age line after a mastery import: {want.age_label.text()!r}")

        # T12 settings: written when a field is done, valid TOML, read back by
        # load(); the title follows the limit; a bad stored value is the default.
        def saved() -> dict:
            try:
                return tomllib.loads(target.read_text()).get("trading", {})
            except (OSError, tomllib.TOMLDecodeError) as exc:
                expect(False, f"the saved config is not valid TOML: {exc}")
                return {}

        want.trade_combo.setCurrentIndex(want.trade_combo.findData("both"))
        expect(saved().get("want_trade_for") == "both",
               f"want_trade_for in the file: {saved()}")
        want.limit_spin.setValue(3)
        offer.limit_spin.setValue(25)
        offer.trade_combo.setCurrentIndex(offer.trade_combo.findData("platinum"))
        written = saved()
        expect(written == {"want_trade_for": "both", "offer_trade_for": "platinum",
                           "max_missing": 3, "max_platinum": 25},
               f"[trading] in the file: {written}")
        expect(want.preview.toPlainText().splitlines()[0]
               .startswith("**Prime sets I still need 1-3 parts for**")
               and want.preview.toPlainText() == trading.to_markdown(
                   want._rows, "want", trade_for="both", max_missing=3, as_of=today),
               "the I want title does not follow the limit")
        want.limit_spin.setValue(1)
        expect(want.preview.toPlainText().startswith("**Prime sets I still need 1 part for**"),
               "the I want title at 1 part")
        # 0 is no limit: stored as 0, every open set listed, nothing hidden.
        want.limit_spin.setValue(0)
        expect(saved().get("max_missing") == 0
               and want.preview.toPlainText().splitlines()[0]
               == "**Prime sets I still need parts for** (as of "
                  f"{today:%Y-%m-%d})"
               and want.hidden_label.isHidden() and want._hidden_sets == 0
               and ("Frost Prime", "Blueprint") in _shown(want)
               and _shown(want) == _names(trading.wanted_rows(conn, max_missing=0)),
               f"I want with no limit: {sorted(_shown(want))}, saved {saved()}")
        want.limit_spin.setValue(2)
        hidden = trading.want_list(conn, max_missing=2)[1]
        expect(hidden >= 1 and not want.hidden_label.isHidden()
               and want.hidden_label.text()
               == f"{hidden} more set(s) miss more than 2 parts; 0 lists them all."
               and ("Frost Prime", "Blueprint") not in _shown(want),
               f"I want hidden-sets line: {want.hidden_label.text()!r}, {hidden}")
        for stored, wanted_value in ((0, 0), (99, 20), (-3, 0)):
            cfg.override("trading.max_missing", stored)
            probe = TradingView(cfg, "want")
            expect(probe.limit_spin.value() == wanted_value,
                   f"stored max_missing {stored} reads as {probe.limit_spin.value()}")
            probe.deleteLater()
        cfg.override("trading.max_missing", 2)
        want.limit_spin.setValue(3)
        try:
            reloaded = config_mod.load()
        except tomllib.TOMLDecodeError as exc:
            expect(False, f"config.load() cannot read the saved file: {exc}")
            reloaded = cfg
        expect(reloaded.get("trading.want_trade_for") == "both"
               and reloaded.get("trading.max_missing") == 3,
               "a reloaded config lost the trading settings")
        again = TradingView(reloaded, "want")
        expect(again.trade_for() == "both" and again.limit_spin.value() == 3,
               "a new page did not start from the saved settings")
        again.deleteLater()
        reloaded.override("trading.want_trade_for", "nonsense")
        bad = TradingView(reloaded, "want")
        expect(bad.trade_for() == "ducats", "a bad stored Trade for is not the default")
        bad.deleteLater()
        want.trade_combo.setCurrentIndex(want.trade_combo.findData("ducats"))
        offer.trade_combo.setCurrentIndex(offer.trade_combo.findData("ducats"))
        want.limit_spin.setValue(2)
        offer.limit_spin.setValue(0)
        quoted = "".join(chr(i) for i in range(0x20)) + '\x7f"\\ é'
        try:
            round_trip = tomllib.loads("k = " + settings_io._quote(quoted))["k"]
        except tomllib.TOMLDecodeError:
            round_trip = None
        expect(round_trip == quoted,
               "settings_io._quote does not escape control characters")

        # T13 the example file says what the core says.
        shipped = tomllib.loads((pathlib.Path(config_mod.__file__).resolve().parent.parent
                                 / "config.example.toml").read_text()).get("trading", {})
        expect(shipped.get("want_trade_for") == trading.DEFAULT_TRADE_FOR
               and shipped.get("offer_trade_for") == trading.DEFAULT_TRADE_FOR
               and trading.DEFAULT_TRADE_FOR in trading.TRADE_FOR
               and shipped.get("max_missing") == 2 and shipped.get("max_platinum") == 0,
               f"config.example.toml's [trading] differs from the core: {shipped}")

        # T14 the headings sort and the tables are tall enough.
        for page, label in ((want, "I want"), (offer, "I offer")):
            page.refresh()
            _settle(app)      # the tables of the earlier refreshes are gone by now
            _reordering_holds(page, label, problems)
            _tables_are_tall_enough(page, label, problems)


        # T15 Save as CSV / Save as HTML / Open in browser: the files are the
        # core's text for the rows on screen, written only where the user said
        # (the dialog) or into the data folder's trading folder, and the page
        # says where. No dialog and no browser here: both are replaced.
        from PySide6.QtWidgets import QLabel
        from ui.views import trading_view as tv_mod

        class _Browser:
            opened: list = []

            @staticmethod
            def openUrl(url):
                _Browser.opened.append(url.toLocalFile())
                return True

        picked, asked_for = [], []
        real_save, real_desktop = tv_mod.file_dialogs.save_file, tv_mod.QDesktopServices
        tv_mod.QDesktopServices = _Browser
        out = pathlib.Path(tempfile.mkdtemp(prefix="wft-trading-out-"))
        try:
            tv_mod.file_dialogs.save_file = lambda parent, caption, suggested="", filters="": (
                asked_for.append(suggested), picked.pop(0) if picked else "")[1]
            want.limit_spin.setValue(3)
            want.trade_combo.setCurrentIndex(want.trade_combo.findData("both"))
            offer.limit_spin.setValue(0)
            offer.unpriced_box.setChecked(True)
            for page, name in ((want, "i-want"), (offer, "i-offer")):
                page.refresh()
                page.trade_combo.setCurrentIndex(
                    page.trade_combo.findData("both" if page is want else "ducats"))
                shown_rows = page.rows()
                expect(shown_rows and shown_rows == page._rows
                       and all(b.isEnabled() for b in (page.csv_button, page.html_button,
                                                       page.browser_button)),
                       f"{name}: the buttons or the rows are not ready")
                # cancelled dialog: nothing written, nothing said
                note, written = page.copy_note.text(), sorted(out.iterdir())
                picked.append("")
                page.csv_button.click()
                expect(page.copy_note.text() == note and sorted(out.iterdir()) == written,
                       f"{name}: a cancelled save wrote or said something")
                # CSV
                target = out / f"{name}.csv"
                picked.append(str(target))
                page.csv_button.click()
                expect(asked_for[-1] == f"{name}-{datetime.date.today():%Y-%m-%d}.csv",
                       f"{name}: the suggested CSV name is {asked_for[-1]!r}")
                expect(target.read_bytes() == trading.to_csv(shown_rows, page.kind).encode("utf-8")
                       and not target.read_bytes().startswith(b"\xef\xbb\xbf"),
                       f"{name}: the CSV is not to_csv of the shown rows, UTF-8 without BOM")
                expect(page.copy_note.text() == f"Saved {len(shown_rows)} row(s) to {target}.",
                       f"{name}: the CSV note says {page.copy_note.text()!r}")
                # HTML
                target = out / f"{name}.html"
                picked.append(str(target))
                page.html_button.click()
                expect(asked_for[-1] == f"{name}-{datetime.date.today():%Y-%m-%d}.html",
                       f"{name}: the suggested HTML name is {asked_for[-1]!r}")
                html_now = target.read_text(encoding="utf-8")
                from ui import theme as theme_mod
                core_html = trading.to_html(
                    shown_rows, page.kind, trade_for=page.trade_for(),
                    max_missing=page._max_missing(), as_of=datetime.date.today(),
                    now=tv_mod._now(),
                    colours={"bg": theme_mod.BG, "card": theme_mod.BG_CARD,
                             "border": theme_mod.BORDER, "text": theme_mod.TEXT,
                             "dim": theme_mod.TEXT_DIM, "accent": theme_mod.ACCENT})
                expect(html_now == core_html and "<table>" in html_now
                       and all(r.item in html_now for r in shown_rows),
                       f"{name}: the saved HTML is not the page for the shown rows")
                expect(page.copy_note.text() == f"Saved the page to {target}.",
                       f"{name}: the HTML note says {page.copy_note.text()!r}")
                # the file follows a filter change
                if page is want:
                    page.limit_spin.setValue(1)
                else:
                    page.limit_spin.setValue(40)
                    page.unpriced_box.setChecked(False)
                changed = page.rows()
                expect(changed != shown_rows and changed == page._rows,
                       f"{name}: the filter change did not change the rows")
                picked.append(str(target))
                page.html_button.click()
                picked.append(str(out / f"{name}2.csv"))
                page.csv_button.click()
                expect(target.read_text(encoding="utf-8") == page.html_text(changed)
                       and (out / f"{name}2.csv").read_text(encoding="utf-8")
                       == trading.to_csv(changed, page.kind),
                       f"{name}: the files do not follow the filters")
                # Open in browser: the data folder's trading folder, overwritten
                browser_file = home / "trading" / f"{name}.html"
                expect(any(str(browser_file) in lab.text()
                           for lab in page.findChildren(QLabel)),
                       f"{name}: the page does not say where Open in browser writes")
                _Browser.opened.clear()
                page.browser_button.click()
                first = browser_file.read_text(encoding="utf-8")
                expect(first == page.html_text(changed)
                       and _Browser.opened == [str(browser_file)]
                       and page.copy_note.text() == f"Opened {browser_file}",
                       f"{name}: Open in browser wrote/opened/said "
                       f"{_Browser.opened} {page.copy_note.text()!r}")
                page.trade_combo.setCurrentIndex(page.trade_combo.findData("platinum"))
                page.browser_button.click()
                expect(browser_file.read_text(encoding="utf-8") != first
                       and "<h1>" in browser_file.read_text(encoding="utf-8")
                       and browser_file.read_text(encoding="utf-8") == page.html_text(changed),
                       f"{name}: Open in browser did not overwrite its file")
                page.trade_combo.setCurrentIndex(page.trade_combo.findData("ducats"))
            # an empty list: nothing to save
            want.limit_spin.setValue(1)
            want.vaulted_box.setChecked(True)
            _click_chip(want, "warframe")
            expect(not want._rows and not want.csv_button.isEnabled()
                   and not want.html_button.isEnabled()
                   and not want.browser_button.isEnabled(),
                   "I want: the save buttons stay on with nothing to save")
            want.vaulted_box.setChecked(False)
            _click_chip(want, "warframe")
            # the files the pages wrote are the reset's to remove
            from ui import reset as reset_mod
            expect((home / "trading") in reset_mod.tracker_files(cfg),
                   "Delete everything does not cover the trading folder")
            # nothing leaked into the user's own folders
            expect(not (pathlib.Path.home() / "i-want.csv").exists(), "a file in the home folder")
        finally:
            tv_mod.file_dialogs.save_file, tv_mod.QDesktopServices = real_save, real_desktop

        for page in (want, offer):
            page.deleteLater()
        conn.close()
        print(f"trading pages      : both built, filters and writes agree with the "
              f"core ({len(problems) - failed} problem(s))")
    finally:
        dbq.set_part_count, dbq.trade_spare, Box.question = originals
        patches.restore()


def _check_the_status_line_says_when_to_import_again(app, problems: list) -> None:
    """The status line carries the ages of both imports; the ticker scrolls only when it must.

    The ages come from `trading.data_age` (the Trading pages' function) and
    `dbq.days_since`; here a scratch database gets an import 6 days old and a
    mastery stamp 2 days old, then none, then today. The ticker is a
    label that stands still while the text fits and runs only while it
    overflows and is shown.
    """
    from wft.data import db as dbq
    from ui.widgets.ticker import TickerLabel

    cfg = _setup_cfg(data=True, key="F8")
    window = _app_window(cfg)
    window.remember_geometry = False
    _settle(app)

    def status(import_days, mastery_days) -> str:
        connection = dbq.connect(cfg.db_path)
        connection.execute("DELETE FROM imports")
        connection.execute("DELETE FROM meta WHERE key = 'mastery_imported_at'")
        if import_days is not None:
            connection.execute(
                "INSERT INTO imports(written_at, source) VALUES("
                "datetime('now', ?), 'smoke')", (f"-{import_days} days",))
        if mastery_days is not None:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('mastery_imported_at', "
                "datetime('now', ?))", (f"-{mastery_days} days",))
        connection.commit()
        connection.close()
        window._refresh_status()
        return window.ticker.fullText()

    for args, wanted in (
            ((6, 2), ("inventory import 6 day(s) old", "mastery import 2 day(s) old")),
            ((None, None), ("inventory never imported", "mastery import not recorded yet")),
            ((0, 0), ("inventory import from today", "mastery import from today"))):
        line = status(*args)
        for phrase in wanted:
            if phrase not in line:
                problems.append(f"status line {args}: {phrase!r} missing in {line!r}")
    line = status(6, 2)
    if line.index("inventory import") > line.index("mastery import") \
            or line.index("mastery import") > line.index("vault status"):
        problems.append(f"status line: order is not inventory, mastery, vault: {line!r}")
    # An import on the Import page refreshes the line itself, without a page change:
    # a mastery import (the profile widget's signal) and an undo (the history's).
    page = window._pages.get("Import")
    if page is None:
        problems.append("status line: no Import page in the window")
    else:
        for days, signal in ((3, page.profile_import.imported), (1, page.history.changed)):
            connection = dbq.connect(cfg.db_path)
            connection.execute("UPDATE meta SET value = datetime('now', ?) "
                               "WHERE key = 'mastery_imported_at'", (f"-{days} days",))
            connection.commit()
            connection.close()
            signal.emit()
            if f"mastery import {days} day(s) old" not in window.ticker.fullText():
                problems.append(f"status line: not refreshed after an import "
                                f"({days} days): {window.ticker.fullText()!r}")
    print(f"status line        : {line}")
    window.close()
    window.deleteLater()

    # -- the ticker ------------------------------------------------------------
    text = "a status line that is long enough to run past a narrow bar " * 2
    ticker = TickerLabel(text)
    ticker.resize(2000, 20)
    ticker.show()
    _settle(app)
    if ticker.is_scrolling() or ticker.offset() != 0:
        problems.append("ticker: scrolls although the text fits")
    ticker.resize(150, 20)
    _settle(app)
    if not ticker.is_scrolling():
        problems.append("ticker: does not scroll although the text overflows")
    # The offscreen cursor can sit over the widget; the pause is tested on its own.
    ticker._hovered = True
    ticker._tick()
    if ticker.offset() != 0:
        problems.append("ticker: moves while the mouse is over it")
    ticker._hovered = False
    for _ in range(5):
        ticker._tick()
    if ticker.offset() <= 0:
        problems.append("ticker: the offset does not advance")
    ticker.setText(text + "!")
    if ticker.offset() != 0:
        problems.append("ticker: new text did not restart at the beginning")
    if ticker.toolTip() != text + "!":
        problems.append("ticker: the tooltip is not the full text")
    ticker.hide()
    if ticker.is_scrolling():
        problems.append("ticker: still running while hidden")
    ticker.show()
    _settle(app)
    if not ticker.is_scrolling():
        problems.append("ticker: did not start again when shown")
    ticker.resize(2000, 20)
    _settle(app)
    if ticker.is_scrolling():
        problems.append("ticker: kept running after it grew wide enough")
    ticker.close()
    ticker.deleteLater()


def _check_add_and_correct_is_one_field(app, problems: list) -> None:
    """Add and correct: one name field, and the name says what it is.

    The kind used to be picked with radio buttons before typing. Now the typed
    name is resolved by `dbq.find_entry`, and each kind must still be written
    through exactly the setter it used before, with the same value. The three
    setters are spied on; Got one on the I want page keeps its own check (T9).
    """
    from PySide6.QtWidgets import QCompleter, QTableWidget
    from ui.views.inventory_view import InventoryView
    from wft import config as config_mod
    from wft.data import db as dbq

    patches = _Patches()
    config_home = pathlib.Path(tempfile.mkdtemp(prefix="wft-addcorrect-cfg-"))
    patches.env("XDG_CONFIG_HOME", str(config_home))
    home = pathlib.Path(tempfile.mkdtemp(prefix="wft-addcorrect-"))
    calls: list = []
    try:
        _trading_fixture(home)
        conn = dbq.connect(home / "warframe.sqlite")
        conn.execute("INSERT INTO items VALUES('W:bronco','Bronco Prime','secondary',1)")
        conn.execute("INSERT INTO items VALUES('W:akbronco','Akbronco Prime','secondary',1)")
        conn.execute("INSERT INTO items VALUES('W:acceltra','Acceltra','primary',0)")
        conn.execute("INSERT INTO items VALUES('W:acceltrap','Acceltra Prime','primary',1)")
        # A component weapon: the part row and the item row are one identifier.
        conn.execute("INSERT INTO parts(unique_name, name, item_unique, count_needed) "
                     "VALUES('W:bronco','Bronco Prime','W:akbronco',2)")
        # A name that is a part of one thing and, separately, a built item.
        conn.execute("INSERT INTO items VALUES('W:twin','Twin Name','other',0)")
        conn.execute("INSERT INTO parts(unique_name, name, item_unique, count_needed) "
                     "VALUES('P:twin','Twin Name','W:ash',1)")
        for suffix in ("Bronze", "Silver", "Gold", "Platinum"):
            conn.execute("INSERT INTO relics(unique_name, name, tier, vaulted) "
                         "VALUES(?,'Meso B9 Relic','Meso',0)", (f"R:meso{suffix}",))
        conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                     "VALUES('W:bronco', 3, 0, 'test')")
        # Forma: its own item and its own part, as in the export.
        conn.execute("INSERT INTO items VALUES('W:forma','Forma Blueprint','reward_other',0)")
        conn.execute("INSERT INTO parts(unique_name, name, item_unique, count_needed) "
                     "VALUES('W:forma','Forma Blueprint','W:forma',1)")
        conn.commit()
        conn.close()
        cfg = config_mod.load()
        cfg.override("paths.data_dir", str(home))

        def expect(ok, text):
            if not ok:
                problems.append(f"add and correct: {text}")

        for name in ("set_part_count", "set_item_count", "set_relic_count"):
            real = getattr(dbq, name)

            def spy(connection, unique, count, source="manual", _n=name, _real=real):
                calls.append((_n, unique, count, source))
                return _real(connection, unique, count, source)

            patches.set(dbq, name, spy)

        view = InventoryView(cfg)
        expect(not hasattr(view, "mode_part") and not hasattr(view, "mode_item")
               and not hasattr(view, "mode_relic"),
               "the kind radio buttons are still there")

        def save(text, count, choose=None):
            calls.clear()
            view.name_edit.setText(text)
            if choose is not None:
                view.kind_combo.setCurrentIndex(
                    view.kind_combo.findData(choose))
            view.count_spin.setValue(count)
            view._add_entry()
            return list(calls)

        # the core: contained names first, then close ones, no duplicates, a limit
        pool = ["Acceltra", "Acceltra Prime", "Bronco Prime Barrel", "Bronco Prime",
                "Meso B9 Relic [Radiant]", "Meso A9 Relic [Radiant]", "Rhino Prime"]
        suggest = dbq.name_suggestions
        # a misspelt name brings its parts too, by the beginning of their names
        expect(suggest("Accelatra", pool)[:2] == ["Acceltra", "Acceltra Prime"]
               and suggest("accelat", pool)[:2] == ["Acceltra", "Acceltra Prime"],
               f"suggest Accelatra: {suggest('Accelatra', pool)}, "
               f"accelat: {suggest('accelat', pool)}")
        expect(suggest("bronco prime barel", pool)[:1] == ["Bronco Prime Barrel"],
               f"suggest barel: {suggest('bronco prime barel', pool)}")
        expect(suggest("meso b9 radiant", pool)[:1] == ["Meso B9 Relic [Radiant]"],
               f"suggest radiant: {suggest('meso b9 radiant', pool)}")
        expect(suggest("xyzzy", pool) == [] and suggest("", pool) == [],
               "suggest: nonsense or nothing")
        expect(suggest("prime", pool)[:4] == ["Acceltra Prime", "Bronco Prime Barrel",
                                              "Bronco Prime", "Rhino Prime"]
               and suggest("prime", pool, limit=2) == ["Acceltra Prime", "Bronco Prime Barrel"]
               and len(set(suggest("rhino prim", pool))) == len(suggest("rhino prim", pool)),
               f"suggest contains/limit: {suggest('prime', pool)}")
        expect(suggest("ac", pool) == ["Acceltra", "Acceltra Prime"],
               f"suggest: a 2-letter text only gets containing names: {suggest('ac', pool)}")

        # One score and one filter for every search box. Word by word, so
        # a misspelt word of a longer name still finds it, in any order
        score, matches = dbq.name_score, dbq.name_matches
        expect(score("prime barel", "Bronco Prime Barrel") >= 90
               and score("rino", "Rhino Prime") >= 80
               and score("barrel prime", "Bronco Prime Barrel") >= 100
               and score("meso b9 radiant", "Meso B9 Relic [Radiant]") == 100
               and score("xyzzy", "Bronco Prime Barrel") < dbq.NAME_SUGGEST_CUTOFF
               and score("Accelatra", "ACCELTRA") > 90,
               "name_score: word rule or case")
        expect(matches("", ["x"]) and matches("  ", ["x"])
               and matches("acce", ["Acceltra"]) and matches("Accelatra", ["Acceltra"])
               and matches("prime barel", ["Bronco Prime Barrel"])
               and matches("rino", ["Rhino Prime"])
               and matches("sys", ["Ash", "Prime Systems"])
               and matches("Accelatra", ["Blueprint", "Acceltra"])
               and not matches("xyzzy", ["Acceltra", "Rhino Prime"])
               and not matches("ac", ["Rhino Prime"])
               and not matches("bronco", ["Rhino Prime", "Ash Prime Systems"])
               and not matches("rhino", []),
               "name_matches: empty, contained, typo, short text or nothing")

        # typing fills the popup with names that contain the text, then the
        # closest ones for a misspelling; only the user's typing refills it
        def typed(edit, model, text):
            edit.blockSignals(True)
            edit.setText(text)
            edit.blockSignals(False)
            edit.textEdited.emit(text)
            return model.stringList()

        for prefix, wanted in (("Ash Prime Sys", "Ash Prime Systems"),
                               ("Volt Pri", "Volt Prime"),
                               ("Meso B9", "Meso B9 Relic [Radiant]"),
                               ("meso b9 radiant", "Meso B9 Relic [Radiant]"),
                               ("Accelatra", "Acceltra")):
            shown = typed(view.name_edit, view._name_model, prefix)
            expect(wanted in shown, f"the popup lacks {wanted!r} for {prefix!r}: {shown}")
        expect(typed(view.name_edit, view._name_model, "Accelatra")[:1] == ["Acceltra"],
               "Accelatra does not offer Acceltra first")
        expect(typed(view.name_edit, view._name_model, "xyzzy") == [],
               "nonsense got a suggestion")
        expect(typed(view.name_edit, view._name_model, "") == [], "an empty field got names")
        view.name_edit.setText("Ash Prime Systems")       # taking a suggestion refills nothing
        expect(view._name_model.stringList() == [], "setText refilled the popup")
        expect(view.name_edit.completer().completionMode()
               == QCompleter.UnfilteredPopupCompletion,
               "the popup is filtered a second time")
        # a misspelling is offered, never written
        expect(save("Accelatra", 1) == [] and "Did you mean" in view.add_feedback.text()
               and "Acceltra" in view.add_feedback.text(),
               f"a misspelt name wrote {calls}, said {view.add_feedback.text()!r}")
        shown = typed(view.mastery_edit, view._mastery_model, "Accelatra")
        expect(shown[:1] == ["Acceltra"], f"the mastery field offers {shown}")
        view.mastery_edit.setText("Accelatra")
        view._set_mastery(True)
        expect("No item called" in view.mastery_feedback.text()
               and "Acceltra" in view.mastery_feedback.text()
               and not dbq.is_mastered(view.conn, "W:acceltra"),
               f"the mastery field did not refuse: {view.mastery_feedback.text()!r}")
        view.mastery_edit.clear()

        # the holding line names the kind and fills the count once
        view.name_edit.setText("ash prime chassis")
        expect("(part)" in view.holding_label.text()
               and "— 2." in view.holding_label.text() and view.count_spin.value() == 2,
               f"part: {view.holding_label.text()!r} {view.count_spin.value()}")
        view.name_edit.setText("Volt Prime")
        expect("(built item)" in view.holding_label.text(),
               f"item: {view.holding_label.text()!r}")
        view.name_edit.setText("Meso B9 Relic [Radiant]")
        expect("(relic)" in view.holding_label.text()
               and "[Radiant]" in view.holding_label.text(),
               f"relic: {view.holding_label.text()!r}")
        view.name_edit.setText("Bronco Prime")
        expect("(built item)" in view.holding_label.text()
               and "— 3." in view.holding_label.text() and view.count_spin.value() == 3,
               f"component weapon: {view.holding_label.text()!r}")
        view.count_spin.setValue(5)
        view.name_edit.setText("Bronco Prim")
        view.name_edit.setText("Bronco Prime")
        expect(view.count_spin.value() == 3, "the count is filled again for the same entry")

        expect(save("Ash Prime Systems", 3) == [("set_part_count", "P:ash-s", 3, "manual")],
               f"part wrote {calls}")
        expect(save("Volt Prime", 2) == [("set_item_count", "W:volt", 2, "manual")],
               f"item wrote {calls}")
        expect(save("Meso B9 Relic [Radiant]", 4)
               == [("set_relic_count", "R:mesoPlatinum", 4, "manual")],
               f"relic with refinement wrote {calls}")
        expect(save("Meso B9 Relic", 6) == [("set_relic_count", "R:mesoBronze", 6, "manual")],
               f"relic without refinement wrote {calls}")
        expect(save("Bronco Prime", 4) == [("set_item_count", "W:bronco", 4, "manual")],
               f"component weapon wrote {calls}")
        expect(save("Prime Sys", 1) == [] and "Did you mean" in view.add_feedback.text()
               and "Ash Prime Systems" in view.add_feedback.text(),
               f"unknown name wrote {calls}, said {view.add_feedback.text()!r}")

        # a name that is both a part and a built item asks, and never guesses
        view.name_edit.setText("Twin Name")
        expect(not view.kind_row.isHidden() and view.holding_label.text() == "",
               "an ambiguous name shows no choice or a holding line")
        expect(save("Twin Name", 1) == [] and "Choose" in view.add_feedback.text(),
               f"an ambiguous name was written without a choice: {calls}")
        expect(save("Twin Name", 2, "part") == [("set_part_count", "P:twin", 2, "manual")],
               f"Twin Name as part wrote {calls}")
        expect(save("Twin Name", 7, "item") == [("set_item_count", "W:twin", 7, "manual")],
               f"Twin Name as item wrote {calls}")
        view.name_edit.setText("Twin Name")
        view.kind_combo.setCurrentIndex(view.kind_combo.findData("part"))
        view.name_edit.setText("Ash Prime Systems")
        expect(view.kind_row.isHidden(), "the choice stays up for an unambiguous name")
        view.name_edit.setText("Twin Name")
        expect(view.kind_combo.currentData() is None and view.holding_label.text() == "",
               "a choice made for one name carried over to the next")

        # Forma is never counted. Not offered, not written, and said why;
        # the writers, the typed review and a restored backup refuse it as well
        from wft.data import backup as backup_mod

        expect("Forma Blueprint" not in view._entry_names, "the field offers Forma")
        view.name_edit.setText("forma blueprint")
        expect("is not counted" in view.holding_label.text(),
               f"forma holding line: {view.holding_label.text()!r}")
        expect(save("Forma Blueprint", 3) == []
               and "is not counted" in view.add_feedback.text(),
               f"forma wrote {calls}, said {view.add_feedback.text()!r}")
        for writer in (dbq.set_part_count, dbq.set_item_count):
            try:
                writer(view.conn, "W:forma", 1, "manual")
                problems.append(f"add and correct: {writer.__name__} wrote Forma")
            except ValueError:
                pass
        view.conn.execute("INSERT INTO owned_parts(part_unique, count, source) "
                          "VALUES('W:forma', 1, 'pick')")
        dbq.set_part_count(view.conn, "W:forma", 0, "manual")
        expect(dbq.owned_count(view.conn, "W:forma") == 0, "Forma cannot be set to 0")
        expect(dbq.entry_candidates(view.conn, "Forma Blueprint") == []
               and dbq.resolve_typed_name(view.conn, "Forma Blueprint",
                                          ["Forma Blueprint"]) is None,
               "a typed Forma resolves to a holding")
        written = backup_mod.restore(view.conn, {
            "parts": [{"part_unique": "W:forma", "count": 2},
                      {"part_unique": "P:ash-s", "count": 3}],
            "items": [{"item_unique": "W:forma", "count": 1}]}, replace=False)
        expect(written["parts"] == 1 and written["items"] == 0
               and dbq.owned_count(view.conn, "W:forma") == 0
               and dbq.item_built(view.conn, "W:forma") == 0,
               f"a backup restored Forma: {written}")
        calls.clear()

        # The other search and name fields forgive typos the same way
        from PySide6.QtWidgets import QLineEdit
        from ui.views.library_view import LibraryView
        from ui.views.plan_view import PlanView
        from ui.views.relic_panel import RelicPanel

        library = LibraryView(cfg)

        def listed(widget):
            return [widget.item(i).text() for i in range(widget.count())]

        library.item_search.setText("Accelatra")
        library._reload_items()
        names = listed(library.item_list)
        expect(any("Acceltra" in n for n in names) and "No matches." not in names,
               f"Collection > Items 'Accelatra': {names}")
        library.item_search.setText("xyzzy")
        library._reload_items()
        expect(listed(library.item_list) == ["No matches."],
               f"Collection > Items 'xyzzy': {listed(library.item_list)}")
        library.relic_search.setText("meso b9 relik")
        library._reload_relics()
        names = listed(library.relic_list)
        expect(any(n.startswith("Meso B9 Relic") for n in names),
               f"Collection > Relics typo: {names}")
        library.relic_search.setText("xyzzy")
        library._reload_relics()
        expect(listed(library.relic_list) == ["No matches."],
               f"Collection > Relics 'xyzzy': {listed(library.relic_list)}")
        library.deleteLater()

        plan = PlanView(cfg)
        plan.refresh()
        box = next((w for w in plan.findChildren(QLineEdit)
                    if (w.placeholderText() or "").startswith("Filter by item")), None)
        if box is None:
            expect(False, "Plan: no filter box was built for the fixture")
        else:
            table = next(t for t in plan.findChildren(QTableWidget)
                         if t.columnCount() >= 2 and t.rowCount()
                         and t.horizontalHeaderItem(0).text() == "Part")

            def visible():
                return [table.item(r, 0).text() for r in range(table.rowCount())
                        if not table.isRowHidden(r)]

            all_rows = visible()
            word = next(r for r in all_rows)[:6]
            box.setText(word[:2] + word[3:])             # one letter dropped
            expect(visible() and len(visible()) <= len(all_rows),
                   f"Plan filter with a typo shows {visible()}")
            box.setText("xyzzy")
            expect(visible() == [], f"Plan filter 'xyzzy' shows {visible()}")
            box.setText("")
            expect(visible() == all_rows, "Plan filter did not clear")
        plan.deleteLater()

        panel = RelicPanel(cfg)
        panel.pick_manual.textEdited.emit("ash prim systms")
        shown = panel._pick_model.stringList()
        expect("Ash Prime Systems" in shown[:3],
               f"the 'or type what you took' popup offers {shown}")
        panel.pick_manual.textEdited.emit("xyzzy")
        expect(panel._pick_model.stringList() == [], "nonsense got a suggestion")
        panel.deleteLater()

        view.deleteLater()
        print("add and correct    : one field; part, item, component weapon, "
              "relics, unknown and ambiguous names; Forma never counted")
    finally:
        patches.restore()


_CHECK_SECONDS: list[tuple[float, str]] = []


class _ProfileServer:
    """urlopen for the profile address: scripted answers, every call counted.

    The suite's own stub of `fetch.request` is put aside while this is in place
    (it refuses everything), and `urllib.request.urlopen` is the only thing that
    could reach a server, so this is the only thing the request goes through.
    """

    ID = "0123456789abcdef01234567"

    def __init__(self):
        import threading

        self.calls = []
        self.script = []
        self.gate = None
        self._threading = threading

    def body(self, *pairs):
        import json

        xp = [{"ItemType": u, "XP": x} for u, x in pairs]
        return json.dumps(
            {"Results": [{"LoadOutInventory": {"XPInfo": xp}}]}).encode()

    def answer(self, body, when, age=0):
        import email.utils

        self.script.append(("ok", body, {
            # An HTTP date is English whatever the locale Qt set on the process.
            "Date": email.utils.format_datetime(when, usegmt=True), "Age": str(age)}))

    def refuse(self, code):
        self.script.append(("http", code))

    def __call__(self, req, timeout=None, **_kw):
        import urllib.error

        self.calls.append({"url": req.full_url, "timeout": timeout,
                           "ua": req.get_header("User-agent")})
        if self.gate is not None:
            self.gate.wait(10)
        step = self.script.pop(0)
        if step[0] == "http":
            raise urllib.error.HTTPError(req.full_url, step[1], "x", {}, None)

        server = self

        class Response:
            status = 200
            headers = step[2]

            def read(self):
                return step[1]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


def _profile_world(app):
    """A configuration with an account id, a database with 5 frames, the server."""
    import urllib.request
    from datetime import datetime, timezone

    from wft.profile import fetch as profile_fetch_mod

    cfg = _setup_cfg(True, "")
    cfg.override("profile.account_id", _ProfileServer.ID)
    cfg.override("profile.auto_fetch", True)
    from wft.data import db as dbq

    conn = dbq.connect(cfg.db_path)
    for unique, name in (("/F/Ash", "Ash"), ("/F/Bo", "Bo"), ("/F/Cy", "Cy"),
                         ("/F/Di", "Di"), ("/F/Ed", "Ed"), ("/F/Fy", "Fy")):
        conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                     "VALUES(?,?,'warframe',0)", (unique, name))
    conn.commit()
    server = _ProfileServer()
    saved = (urllib.request.urlopen, profile_fetch_mod.request)
    urllib.request.urlopen = server
    profile_fetch_mod.request = saved[1].replaced
    t0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    def restore():
        urllib.request.urlopen, profile_fetch_mod.request = saved

    return cfg, conn, server, t0, restore


def _wait_fetch(app, fetcher) -> None:
    import time

    end = time.monotonic() + 10
    while fetcher.busy and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    _settle(app)


def _check_the_profile_is_fetched_once_a_day(app, problems: list) -> None:
    """Due at 24 hours, counted from any request, a failure waits a day too.

    The clock is a list the check moves; nothing waits. Every case counts the
    calls the server saw: the rule this feature lives by is how often, not
    whether it works.
    """
    from datetime import timedelta

    from ui.scheduler import Scheduler
    from ui.workers.profile_fetch import ProfileFetcher

    cfg, conn, server, t0, restore = _profile_world(app)
    try:
        now = [t0]

        def clock():
            return now[0]

        fetcher = ProfileFetcher(cfg, clock=clock)
        scheduler = Scheduler(clock=clock, first_ms=10_000_000)
        scheduler.add(fetcher)

        def tick(at, label, want_calls, answer=True, code=None):
            now[0] = at
            if code:
                server.refuse(code)
            elif answer:
                server.answer(server.body(("/F/Ash", 900_000)), at)
            before = len(server.calls)
            started = scheduler.tick()
            _wait_fetch(app, fetcher)
            made = len(server.calls) - before
            if made != want_calls or (started is not None) != bool(want_calls):
                problems.append(f"daily fetch: {label}: {made} request(s), wanted {want_calls}")
            while server.script and want_calls == 0:
                server.script.pop()

        # No id, an id that is not 24 hex characters, the switch off: never.
        for label, account, auto in (("no id", "", True), ("short id", "abc", True),
                                     ("not hex", "g" * 24, True),
                                     ("switch off", _ProfileServer.ID, False)):
            cfg.override("profile.account_id", account)
            cfg.override("profile.auto_fetch", auto)
            tick(t0, label, 0)
        cfg.override("profile.account_id", _ProfileServer.ID)
        cfg.override("profile.auto_fetch", True)

        tick(t0, "never fetched: due at the first check", 1)
        tick(t0 + timedelta(hours=23), "23 h later", 0)
        tick(t0 + timedelta(hours=25), "25 h later", 1)
        # A press of the button is a request like the others for the schedule.
        now[0] = t0 + timedelta(hours=30)
        server.answer(server.body(("/F/Ash", 900_000)), now[0])
        fetcher.start("manual")
        _wait_fetch(app, fetcher)
        tick(t0 + timedelta(hours=52), "22 h after a reload", 0)
        tick(t0 + timedelta(hours=55), "25 h after a reload", 1)
        # A refusal: shown, not answered with another try; a day later again.
        tick(t0 + timedelta(hours=80), "25 h later, the server refuses", 1, code=409)
        tick(t0 + timedelta(hours=81), "1 h after a 409", 0)
        tick(t0 + timedelta(hours=103), "23 h after a 409", 0)
        tick(t0 + timedelta(hours=105), "25 h after a 409", 1)

        # A new fetcher (a restart) reads it all from the database.
        fresh = ProfileFetcher(cfg, clock=clock)
        if fresh.due(t0 + timedelta(hours=106)) or not fresh.due(t0 + timedelta(hours=130)):
            problems.append("daily fetch: a new fetcher does not know the last request")
        for call in server.calls:
            if call["timeout"] != 30 or not call["ua"] or "warframe-tracker" not in call["ua"]:
                problems.append(f"daily fetch: request not as specified: {call}")
                break
        if any("playerId=" + _ProfileServer.ID not in c["url"] for c in server.calls):
            problems.append("daily fetch: a request went to another address")
        rows = conn.execute("SELECT kind, status FROM fetch_log ORDER BY id").fetchall()
        if len(rows) != len(server.calls):
            problems.append(f"daily fetch: {len(server.calls)} request(s), {len(rows)} logged")
        if [r["status"] for r in rows if r["kind"] == "manual"] != [200]:
            problems.append("daily fetch: the reload was not logged as manual")
        if 409 not in [r["status"] for r in rows]:
            problems.append("daily fetch: the 409 was not logged")
        print(f"daily fetch        : {len(server.calls)} request(s) over 105 h, "
              f"{len(rows)} logged")
    finally:
        restore()
        conn.close()


def _check_the_profile_card_applies_what_it_fetched(app, problems: list) -> None:
    """The first profile is offered, later ones are written, old copies are not."""
    import threading
    from datetime import timedelta

    from PySide6.QtWidgets import QDialog
    from ui.widgets.profile_import import ProfileImport
    from ui.workers.profile_fetch import ProfileFetcher
    from wft.profile import fetch as profile_fetch_mod

    cfg, conn, server, t0, restore = _profile_world(app)
    exec_before = QDialog.exec
    try:
        now = [t0]
        fetcher = ProfileFetcher(cfg, clock=lambda: now[0])
        card = ProfileImport(cfg, conn, fetcher=fetcher)
        card.resize(900, 700)
        card.show()
        _settle(app)
        emitted = []
        card.imported.connect(lambda: emitted.append(1))

        def fetch_now(at, label):
            now[0] = at
            card.reload_btn.click()
            _wait_fetch(app, fetcher)
            if not card.reload_btn.isEnabled():
                problems.append(f"profile card: {label}: the button stayed locked")

        def owned():
            return {r["item_unique"]: r["source"] for r in conn.execute(
                "SELECT item_unique, source FROM owned_items WHERE mastered = 1")}

        # 1. The first profile this installation sees: nothing written, review offered.
        server.answer(server.body(("/F/Ash", 900_000), ("/F/Bo", 900_000),
                                  ("/F/Cy", 1000)), t0, age=120)
        fetch_now(t0, "first")
        if owned() or conn.execute(
                "SELECT 1 FROM meta WHERE key='mastery_imported_at'").fetchone():
            problems.append("profile card: the first profile was written without a review")
        if not card.review_btn.isVisible():
            problems.append("profile card: no Review button after the first profile")
        if "reached their rank cap" not in card.state_label.text() \
                or "2 item" not in card.state_label.text():
            problems.append(f"profile card: first result: {card.state_label.text()!r}")
        if emitted:
            problems.append("profile card: the first profile announced an import")
        # The stamp appears with the confirmed list, as with a file.
        QDialog.exec = lambda self: (self.write_btn.click(), 1)[1]
        card.review_btn.click()
        QDialog.exec = exec_before
        _settle(app)
        if set(owned()) != {"/F/Ash", "/F/Bo"} or set(owned().values()) != {"profile"}:
            problems.append(f"profile card: after the review {owned()}")
        if not emitted or card.review_btn.isVisible():
            problems.append("profile card: confirming did not announce the import")
        emitted.clear()

        # 2. A newer copy later: written without a dialog, announced, named.
        def no_dialog(self):
            problems.append("profile card: a dialog opened by itself")
            return 0
        QDialog.exec = no_dialog
        server.answer(server.body(("/F/Ash", 900_000), ("/F/Di", 900_000),
                                  ("/F/Ed", 900_000), ("/F/Fy", 900_000),
                                  ("/F/Cy", 900_000)), t0 + timedelta(hours=25), age=60)
        fetch_now(t0 + timedelta(hours=25), "second")
        QDialog.exec = exec_before
        if set(owned()) != {"/F/Ash", "/F/Bo", "/F/Di", "/F/Ed", "/F/Fy", "/F/Cy"}:
            problems.append(f"profile card: the second profile wrote {sorted(owned())}")
        text = card.state_label.text()
        if "4 newly mastered" not in text or "(+1 more)" not in text:
            problems.append(f"profile card: second result: {text!r}")
        if not emitted:
            problems.append("profile card: the second profile did not announce an import")
        stamp_row = conn.execute(
            "SELECT value FROM meta WHERE key='mastery_imported_at'").fetchone()
        if stamp_row is None:
            problems.append("profile card: no import stamp after the second profile")
        path = profile_fetch_mod.profile_path(cfg)
        kept = path.read_bytes()

        # 3. An older copy (the CDN cache) and an equal one: the file stays, nothing applied.
        for label, shift, age in (("older", 25 * 60 - 10, 0), ("equal", 25 * 60, 60)):
            emitted.clear()
            server.answer(server.body(("/F/Ash", 900_000)), t0 + timedelta(
                minutes=shift), age=age)
            fetch_now(t0 + timedelta(hours=26, minutes=len(label)), label)
            if path.read_bytes() != kept:
                problems.append(f"profile card: an {label} copy replaced the kept file")
            if emitted:
                problems.append(f"profile card: an {label} copy was applied")
            if "no newer" not in card.state_label.text():
                problems.append(f"profile card: {label}: {card.state_label.text()!r}")

        # 4. A refusal: shown with its code, one request, nothing written.
        before = len(server.calls)
        server.refuse(409)
        fetch_now(t0 + timedelta(hours=27), "409")
        if len(server.calls) - before != 1:
            problems.append("profile card: a 409 was answered with more requests")
        if "HTTP 409" not in card.state_label.text() or path.read_bytes() != kept:
            problems.append(f"profile card: 409 shown as {card.state_label.text()!r}")
        if fetcher.due(t0 + timedelta(hours=28)):
            problems.append("profile card: due again 1 h after a 409")

        # 5. Warning from 5 requests in 24 h; the button still works.
        before = len(server.calls)
        server.refuse(403)
        fetch_now(t0 + timedelta(hours=27, minutes=5), "403")
        recent = profile_fetch_mod.count_recent(conn, t0 + timedelta(hours=27, minutes=5))
        if recent < 5 or not card.warning_label.isVisible():
            problems.append(f"profile card: {recent} request(s) in 24 h, warning "
                            f"visible: {card.warning_label.isVisible()}")
        if not card.reload_btn.isEnabled():
            problems.append("profile card: the button is locked by the warning")
        server.refuse(403)
        fetch_now(t0 + timedelta(hours=27, minutes=6), "403 again")
        if len(server.calls) - before != 2:
            problems.append("profile card: the button did not fetch past the warning")

        # 6. A request in flight: the button is locked, a second press adds nothing.
        now[0] = t0 + timedelta(hours=40)
        server.gate = threading.Event()
        server.answer(server.body(("/F/Ash", 900_000)), t0 + timedelta(hours=40))
        before = len(server.calls)
        card.reload_btn.click()
        app.processEvents()
        if card.reload_btn.isEnabled() or not fetcher.busy:
            problems.append("profile card: the button is not locked during a request")
        if fetcher.start("manual") or fetcher.start("auto"):
            problems.append("profile card: a second request started during the first")
        server.gate.set()
        _wait_fetch(app, fetcher)
        server.gate = None
        if len(server.calls) - before != 1:
            problems.append(f"profile card: {len(server.calls) - before} request(s) "
                            "for one press")
        server.script.clear()

        # 7. A card built later (a restart) shows the log, not an empty state.
        again = ProfileImport(cfg, conn, fetcher=ProfileFetcher(cfg, clock=lambda: now[0]))
        shown = again.state_label.text()
        if "Last fetch" not in shown or "Next automatic fetch" not in shown:
            problems.append(f"profile card: a new card shows {shown!r}")
        if str(path) not in shown:
            problems.append("profile card: the kept file's path is not named")

        # 8. profile.json is in the data folder and the data folder has no other copy.
        found = sorted(p.name for p in cfg.data_dir.iterdir() if "profile" in p.name)
        if found != ["profile.json"]:
            problems.append(f"profile card: files named profile in the data folder: {found}")
        again.deleteLater()
        card.deleteLater()
        _settle(app)
        print(f"profile card       : {len(server.calls)} request(s), first reviewed, "
              f"later written, old copies kept out, {recent} in 24 h warned")
    finally:
        QDialog.exec = exec_before
        restore()
        conn.close()


def _check_the_profile_id_and_switch_are_saved(app, problems: list) -> None:
    """The id is stored when it is valid, the switch when it is flipped."""
    from ui.widgets.profile_import import ProfileImport
    from ui.workers.profile_fetch import ProfileFetcher
    from ui import settings_io

    cfg, conn, server, t0, restore = _profile_world(app)
    try:
        cfg.override("profile.account_id", "")
        card = ProfileImport(cfg, conn, fetcher=ProfileFetcher(cfg))
        card.show()
        _settle(app)
        target = settings_io.target_config_path()
        written = lambda: target.read_text() if target.is_file() else ""  # noqa: E731

        card.account_edit.setText("not-an-id")
        card.account_edit.editingFinished.emit()
        if "not-an-id" in written() or cfg.get("profile.account_id", ""):
            problems.append("profile id: an invalid id was stored")
        card.reload_btn.setEnabled(True)
        card.reload_btn.click()
        _settle(app)
        if server.calls:
            problems.append("profile id: the reload button fetched with an invalid id")
        card.account_edit.setText(_ProfileServer.ID.upper())
        card.account_edit.editingFinished.emit()
        if cfg.get("profile.account_id", "") != _ProfileServer.ID \
                or _ProfileServer.ID not in written():
            problems.append("profile id: a valid id was not stored on editingFinished")
        if server.calls:
            problems.append("profile id: storing the id made a request")
        card.auto_box.setChecked(False)
        if cfg.get("profile.auto_fetch", True) is not False \
                or "auto_fetch = false" not in written():
            problems.append("profile id: switching off was not stored")
        if "off" not in card.state_label.text():
            problems.append(f"profile id: switch off shown as {card.state_label.text()!r}")
        card.auto_box.setChecked(True)
        if "auto_fetch = true" not in written():
            problems.append("profile id: switching on was not stored")
        if card.reload_btn.toolTip() != "Fetch my profile now":
            problems.append("profile id: the button's tooltip changed")
        if card.reload_btn.icon().isNull():
            problems.append("profile id: the reload button has no icon")
        card.deleteLater()
        _settle(app)
        print("profile id         : stored on editingFinished, switch stored, "
              "no request from either")
    finally:
        restore()
        conn.close()


def _check_a_run_without_an_id_asks_nobody(app, problems: list) -> None:
    """The release's offline start: no id, no request, nothing waited for.

    `wf-gui --self-test` builds the window and never the scheduler; and a
    scheduler with no id (or built by mistake in that start) finds nothing due.
    """
    import urllib.request

    from ui.scheduler import Scheduler
    from ui.workers.profile_fetch import ProfileFetcher
    from wft import config as config_mod

    cfg = _setup_cfg(True, "")
    cfg.override("profile.account_id", "")
    from wft.profile import fetch as profile_fetch_mod

    server = _ProfileServer()
    saved = (urllib.request.urlopen, profile_fetch_mod.request)
    urllib.request.urlopen = server
    profile_fetch_mod.request = saved[1].replaced
    try:
        fetcher = ProfileFetcher(cfg)
        scheduler = Scheduler(first_ms=10_000_000)
        scheduler.add(fetcher)
        scheduler.tick()
        _wait_fetch(app, fetcher)
        if server.calls or fetcher.busy:
            problems.append("no id: a request was made or waited for")
        import ui.app as app_mod
        source = pathlib.Path(app_mod.__file__).read_text()
        guard = source.index("scheduler = build_scheduler(")
        if "if not args.self_test:" not in source[guard - 300:guard]:
            problems.append("no id: the scheduler is not behind the self-test guard")
        print("no id              : no request, nothing waited for")
    finally:
        urllib.request.urlopen, profile_fetch_mod.request = saved


def _picture_world(app, owned: int = 0, base=None):
    """A configuration with `owned` held items that have an icon and no picture, a counting host."""
    import io

    import cv2
    import numpy as np

    from wft.data import pictures as pictures_mod

    from wft.data import db as dbq

    if base is None:
        cfg = _setup_cfg(True, "")
        conn = dbq.connect(cfg.db_path)
    else:
        cfg, conn = base
    cfg.override("pictures.auto_fetch", True)
    png = cv2.imencode(".png", np.full((8, 8, 3), 200, np.uint8))[1].tobytes()
    host = type("Host", (), {})()
    host.calls, host.script, host.gate = [], {}, None

    def fake(url):
        import threading
        import urllib.error

        location = url[len(pictures_mod.CONTENT_BASE):]
        host.calls.append(location)
        if host.gate is not None:
            host.gate.wait(10)
        step = host.script.get(location, "ok")
        if step == "ok":
            return png
        raise urllib.error.HTTPError(url, int(step), "x", {}, None)

    host.add = lambda n: _add_held_icons(conn, n)
    host.add(owned)
    saved = (pictures_mod._fetch, pictures_mod.PAUSE)
    pictures_mod._fetch = fake
    pictures_mod.PAUSE = 0

    def restore():
        pictures_mod._fetch, pictures_mod.PAUSE = saved

    return cfg, conn, host, restore


_ICON_SERIAL = [0]


def _add_held_icons(conn, count: int) -> list:
    made = []
    for _ in range(count):
        _ICON_SERIAL[0] += 1
        unique = f"/I/pic{_ICON_SERIAL[0]}"
        conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                     "VALUES(?,?, 'warframe', 0)", (unique, unique))
        conn.execute("INSERT INTO owned_items(item_unique, count) VALUES(?,1)", (unique,))
        conn.execute("INSERT INTO textures(unique_name, location) VALUES(?,?)",
                     (unique, f"/img/{_ICON_SERIAL[0]}.png"))
        made.append(f"/img/{_ICON_SERIAL[0]}.png")
    conn.commit()
    return made


def _check_the_pictures_are_fetched_by_themselves(app, problems: list) -> None:
    """The clock starts the pictures once a day, only when missing, never beside the profile."""
    import threading
    from datetime import timedelta

    from ui.scheduler import Scheduler
    from ui.workers.picture_fetch import PictureFetcher
    from ui.workers.profile_fetch import ProfileFetcher

    cfg, conn, host, restore = _picture_world(app)
    try:
        now = [datetime_t0()]
        t0 = now[0]
        clock = lambda: now[0]  # noqa: E731
        fetcher = PictureFetcher(cfg, clock=clock)
        scheduler = Scheduler(clock=clock, first_ms=10_000_000)
        scheduler.add(fetcher)

        def tick(at, label, want_calls):
            now[0] = at
            before = len(host.calls)
            started = scheduler.tick()
            _wait_fetch(app, fetcher)
            made = len(host.calls) - before
            if made != want_calls or (started is not None) != bool(want_calls):
                problems.append(f"pictures by themselves: {label}: {made} request(s), "
                                f"wanted {want_calls}")

        def rows():
            return conn.execute("SELECT * FROM fetch_log WHERE job='pictures' "
                                "ORDER BY id").fetchall()

        # Nothing missing: no request, no row.
        tick(t0, "nothing missing", 0)
        if rows():
            problems.append("pictures by themselves: a run was logged with nothing missing")
        # Missing, but the switch is off.
        host.add(3)
        cfg.override("pictures.auto_fetch", False)
        tick(t0, "switch off", 0)
        cfg.override("pictures.auto_fetch", True)
        tick(t0, "missing and on: due at the first check", 3)
        if len(rows()) != 1 or rows()[0]["kind"] != "auto" or "3 fetched" not in rows()[0]["detail"]:
            problems.append(f"pictures by themselves: log {[dict(r) for r in rows()]}")
        host.add(2)
        tick(t0 + timedelta(hours=23), "23 h later", 0)
        tick(t0 + timedelta(hours=25), "25 h later", 2)
        # A refusal ends the run at the first picture and holds the next day off.
        made = host.add(4)
        host.script[made[1]] = "503"
        tick(t0 + timedelta(hours=50), "a 503 on the 2nd picture", 2)
        last = rows()[-1]
        if last["status"] != 503 or "stopped: HTTP 503" not in last["detail"]:
            problems.append(f"pictures by themselves: the refusal was logged as {dict(last)}")
        tick(t0 + timedelta(hours=51), "1 h after a refusal", 0)
        host.script.clear()
        tick(t0 + timedelta(hours=75), "25 h after a refusal", 3)
        # A 404 does not end the run.
        made = host.add(3)
        host.script[made[0]] = "404"
        tick(t0 + timedelta(hours=100), "a 404 on the 1st picture", 3)
        if "1 missing on the server" not in rows()[-1]["detail"]:
            problems.append(f"pictures by themselves: the 404 was logged as {rows()[-1]['detail']!r}")

        # A new fetcher (a restart) reads the schedule from the database.
        host.add(1)
        fresh = PictureFetcher(cfg, clock=clock)
        if fresh.due(t0 + timedelta(hours=101)) or not fresh.due(t0 + timedelta(hours=125)):
            problems.append("pictures by themselves: a new fetcher does not know the last run")
        # Two fetchers on one configuration would run twice; `shared` gives one.
        from ui.workers import picture_fetch as module
        if module.shared(cfg) is not module.shared(cfg):
            problems.append("pictures by themselves: shared() returns 2 objects")
        print(f"pictures by clock  : {len(host.calls)} request(s), {len(rows())} run(s) logged")
    finally:
        restore()
        conn.close()

    # Beside the profile: one job at a time, the profile first.
    cfg, conn, server, t0, profile_restore = _profile_world(app)
    cfg, conn, host, restore = _picture_world(app, 3, base=(cfg, conn))
    profile_conn = conn
    try:
        now = [t0]
        clock = lambda: now[0]  # noqa: E731
        profile = ProfileFetcher(cfg, clock=clock)
        pictures_job = PictureFetcher(cfg, clock=clock)
        scheduler = Scheduler(clock=clock, first_ms=10_000_000)
        scheduler.add(profile)
        scheduler.add(pictures_job)
        server.gate = threading.Event()
        server.answer(server.body(("/F/Ash", 900_000)), t0)
        started = scheduler.tick()
        if started is not profile:
            problems.append("pictures beside profile: the profile did not start first")
        again = scheduler.tick()
        if again is not None or host.calls:
            problems.append("pictures beside profile: a job started while the profile ran")
        server.gate.set()
        _wait_fetch(app, profile)
        server.gate = None
        if host.calls:
            problems.append("pictures beside profile: pictures were requested before the next tick")
        started = scheduler.tick()
        _wait_fetch(app, pictures_job)
        if started is not pictures_job or len(host.calls) != 3:
            problems.append(f"pictures beside profile: pictures after the profile: "
                            f"{started}, {len(host.calls)} request(s)")
        # And the other way round: nothing starts while the pictures run.
        host.add(1)
        now[0] = t0 + timedelta(hours=48)
        host.gate = threading.Event()
        server.answer(server.body(("/F/Ash", 900_000)), now[0])
        if not pictures_job.start("manual"):
            problems.append("pictures beside profile: the manual run did not start")
        before = len(server.calls)
        if scheduler.tick() is not None or len(server.calls) != before:
            problems.append("pictures beside profile: the profile started during a picture run")
        if pictures_job.start("auto") or pictures_job.start("import"):
            problems.append("pictures beside profile: a second picture run started")
        host.gate.set()
        _wait_fetch(app, pictures_job)
        host.gate = None
        server.script.clear()
        print("pictures + profile : one at a time, the profile first")
    finally:
        profile_restore()
        restore()
        profile_conn.close()
        conn.close()


def datetime_t0():
    from datetime import datetime, timezone

    return datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def _check_the_pictures_card_and_the_import(app, problems: list) -> None:
    """The card shows the result; an applied import starts a run, nothing else does."""
    from datetime import timedelta

    from ui.views.import_view import ImportView
    from ui.widgets.picture_fetch import PictureFetch
    from ui.workers.picture_fetch import PictureFetcher
    from ui import settings_io

    cfg, conn, host, restore = _picture_world(app, 3)
    try:
        now = [datetime_t0()]
        fetcher = PictureFetcher(cfg, clock=lambda: now[0])
        card = PictureFetch(cfg, conn, fetcher=fetcher)
        card.resize(900, 300)
        card.show()
        _settle(app)
        if "3 still to fetch" not in card.note.text() or not card.reload_btn.isEnabled():
            problems.append(f"pictures card: first state {card.note.text()!r}")
        if card.reload_btn.toolTip() != "Fetch missing pictures now" \
                or card.reload_btn.icon().isNull():
            problems.append("pictures card: the reload button has no icon or tooltip")
        if "Next automatic run" not in card.state_label.text():
            problems.append(f"pictures card: {card.state_label.text()!r}")
        # Reload: one run, the result on the card, the log row 'manual'.
        card.reload_btn.click()
        app.processEvents()
        if card.reload_btn.isEnabled() or not card.cancel_button.isVisibleTo(card):
            problems.append("pictures card: the button is not locked during a run")
        _wait_fetch(app, fetcher)
        if len(host.calls) != 3 or "3 fetched" not in card.state_label.text() \
                or "2026" not in "".join(r["at"] for r in conn.execute(
                    "SELECT at FROM fetch_log WHERE job='pictures'")):
            problems.append(f"pictures card: after the reload {len(host.calls)} request(s), "
                            f"{card.state_label.text()!r}")
        if card.reload_btn.isEnabled() or "nothing missing" not in card.note.text():
            problems.append(f"pictures card: nothing left, yet {card.note.text()!r}")
        # A card built later (a restart) shows the log.
        again = PictureFetch(cfg, conn, fetcher=PictureFetcher(cfg))
        if "Last run" not in again.state_label.text() or "3 fetched" not in again.state_label.text():
            problems.append(f"pictures card: a new card shows {again.state_label.text()!r}")
        # The switch is saved, and read back.
        target = settings_io.target_config_path()
        written = lambda: target.read_text() if target.is_file() else ""  # noqa: E731
        card.auto_box.setChecked(False)
        if cfg.get("pictures.auto_fetch", True) is not False or "auto_fetch = false" not in written():
            problems.append("pictures card: switching off was not stored")
        if "off" not in card.state_label.text():
            problems.append(f"pictures card: switch off shown as {card.state_label.text()!r}")
        # Off: an import starts nothing.
        host.add(2)
        before = len(host.calls)
        if card.after_import():
            problems.append("pictures card: after_import started with the switch off")
        card.auto_box.setChecked(True)
        if "auto_fetch = true" not in written():
            problems.append("pictures card: switching on was not stored")
        # On, but the server ended a run an hour ago: an import waits the day.
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        from wft.data import pictures as pictures_mod
        from wft.profile import fetch as fetch_mod
        fetch_mod.record(conn, _dt.now(_tz.utc) - _td(hours=1), "auto", 503,
                         f"0 fetched{pictures_mod.STOPPED}HTTP 503",
                         job=pictures_mod.JOB)
        if card.after_import():
            problems.append("pictures card: after_import started an hour after "
                            "the server ended a run")
        again.deleteLater()
        card.deleteLater()
        _settle(app)
        if len(host.calls) != before:
            problems.append("pictures card: a request was made with the switch off")
    finally:
        restore()
        conn.close()

    # The import page: an applied recording starts a run, other imports do not.
    cfg, conn, host, restore = _picture_world(app, 2)
    try:
        view = ImportView(cfg)
        view.resize(1100, 900)
        view.show()
        _settle(app)
        fetcher = view.pictures.fetcher
        view.apply_rows = []
        view.scan_result = None
        view.clusters = []
        view.video_path = None
        # The profile import and taking an import back announce a change too.
        view.profile_import.imported.emit()
        view.history.changed.emit()
        _wait_fetch(app, fetcher)
        _settle(app)
        if host.calls or fetcher.busy:
            problems.append(f"pictures import: {len(host.calls)} request(s) after a "
                            "profile import or an undo")
        view._write_apply_table()
        _wait_fetch(app, fetcher)
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM fetch_log WHERE job='pictures'")]
        if len(host.calls) != 2 or kinds != ["import"]:
            problems.append(f"pictures import: after an applied import {len(host.calls)} "
                            f"request(s), log {kinds}")
        # With the switch off an applied import fetches nothing.
        cfg.override("pictures.auto_fetch", False)
        host.add(2)
        before = len(host.calls)
        view.apply_rows = []
        view._write_apply_table()
        _wait_fetch(app, fetcher)
        if len(host.calls) != before:
            problems.append("pictures import: an applied import fetched with the switch off")
        # An import-triggered run is a run like the others: the day counts from it.
        cfg.override("pictures.auto_fetch", True)
        if fetcher.due(fetcher.clock() + timedelta(hours=1)):
            problems.append("pictures import: the run after an import did not count for the day")
        view.deleteLater()
        _settle(app)
        print(f"pictures import    : applied import ran {len(host.calls)} request(s); "
              "profile import and undo ran none")
    finally:
        restore()
        conn.close()


def _check_the_pictures_run_asks_nobody_at_the_release_start(app, problems: list) -> None:
    """--self-test and the guide's pictures build no clock, so no run starts."""
    import ui.app as app_mod

    source = pathlib.Path(app_mod.__file__).read_text()
    guard = source.index("scheduler = build_scheduler(")
    if "if not args.self_test:" not in source[guard - 300:guard]:
        problems.append("pictures release start: the scheduler is not behind the self-test guard")
    shots = pathlib.Path(app_mod.__file__).parent.parent / "packaging" / "guide_shots.py"
    text = shots.read_text()
    if "Scheduler" in text or "ui.scheduler" in text:
        problems.append("pictures release start: the guide's pictures build a scheduler")
    print("pictures release   : the clock is built only by the running application")


def _reference_card_world(app, *, built: bool = True):
    """A Settings page over scratch data with a built database; returns (cfg, page)."""
    from ui.views.settings_view import SettingsView
    from wft.data import db as dbq

    cfg = _setup_cfg(True, "")
    conn = dbq.connect(cfg.db_path)
    if built:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('counts', "
                     "'{\"items\": 1}')")
        conn.commit()
    conn.close()
    cfg.override("update.auto_fetch", True)
    page = SettingsView(cfg)
    page.resize(1200, 900)
    page.show()
    _settle(app)
    return cfg, page


def _check_the_reference_card_has_a_saved_switch_and_says_when(app, problems: list) -> None:
    """The Reference data card: the daily run's switch, when it comes, what failed."""
    from datetime import timedelta

    from ui import settings_io
    from wft.data import db as dbq
    from wft.data import reference
    from wft.profile import fetch as fetch_mod

    cfg, page = _reference_card_world(app)
    try:
        page.show_tab("Game data")   # the card is on that tab
        target = settings_io.target_config_path()
        written = lambda: target.read_text() if target.is_file() else ""  # noqa: E731
        if not page.auto_box.isChecked() or "once a day" not in page.auto_box.text():
            problems.append(f"reference card: the switch is {page.auto_box.text()!r}, "
                            f"checked={page.auto_box.isChecked()}")
        # On: the status line and the schedule say the data updates itself.
        if "updates it by itself once a day" not in page.status_label.text() \
                or "reminder after" in page.status_label.text():
            problems.append(f"reference card, on: {page.status_label.text()!r}")
        if "the next check" not in page.auto_label.text():
            problems.append(f"reference card, on: {page.auto_label.text()!r}")
        # A run in the log: the next check is named, the last run shown.
        conn = dbq.connect(cfg.db_path)
        row = reference.begin(conn, fetch_mod.utcnow(), "auto")
        reference.end(conn, row, None, reference.success_text({"downloaded": 2}))
        conn.close()
        page.refresh()
        shown = page.auto_label.text()
        if "the next check comes after" not in shown \
                or "(automatic): 2 manifests downloaded, vault status refreshed" not in shown:
            problems.append(f"reference card, after a run: {shown!r}")
        if page.fail_label.isVisibleTo(page):
            problems.append("reference card: a failure shown after a run that went through")
        # After a failed automatic run: what failed, when it tries again.
        conn = dbq.connect(cfg.db_path)
        row = reference.begin(conn, fetch_mod.utcnow(), "auto")
        reference.end(conn, row, 503, "HTTPError: HTTP Error 503: unavailable")
        conn.close()
        page.refresh()
        failed = page.fail_label.text()
        if not page.fail_label.isVisibleTo(page) or "503" not in failed \
                or "tries again after" not in failed \
                or theme_warn() not in page.fail_label.styleSheet():
            problems.append(f"reference card, after a failure: {failed!r}")
        # Buttons stay free, whatever the schedule says.
        if not (page.update_btn.isEnabled() and page.force_btn.isEnabled()):
            problems.append("reference card: a button is locked outside a run")
        # Off: stored, and the old reminder text exactly.
        page.auto_box.setChecked(False)
        if cfg.get("update.auto_fetch", True) is not False \
                or "auto_fetch = false" not in written():
            problems.append("reference card: switching off was not stored")
        text = page.status_label.text()
        if "The line turns into a reminder after 14 days." not in text \
                or "by itself" in text:
            problems.append(f"reference card, off: {text!r}")
        if "off" not in page.auto_label.text():
            problems.append(f"reference card, off: {page.auto_label.text()!r}")
        page.auto_box.setChecked(True)
        if "auto_fetch = true" not in written():
            problems.append("reference card: switching on was not stored")
        # A restart: a card built later reads the switch back from the config.
        cfg.override("update.auto_fetch", False)
        from ui.views.settings_view import SettingsView

        again = SettingsView(cfg)
        if again.auto_box.isChecked():
            problems.append("reference card: a new page ignores the stored switch")
        again.deleteLater()
        _settle(app)
        print("reference card     : switch stored, schedule, failure and reminder text")
    finally:
        page.close()


def theme_warn() -> str:
    from ui import theme

    return theme.WARN


def _check_the_reference_update_is_started_only_by_the_running_application(app, problems: list) -> None:
    """The clock gets the update in the right place, never in --self-test; nothing starts it unasked."""
    import ui.app as app_mod
    from ui.scheduler import Scheduler
    from ui.views.settings_view import UpdateJob
    from ui.workers.reference_update import ScheduledReference

    source = pathlib.Path(app_mod.__file__).read_text()
    guard = source.index("scheduler = build_scheduler(")
    if "if not args.self_test:" not in source[guard - 300:guard]:
        problems.append("reference start: the scheduler is not behind the self-test guard")
    # Built for real, the way `main` builds it, over a whole window: reading
    # the code as text passed while `main` named a variable that only existed
    # in `build_application`, and the running tool started with no clock.
    from PySide6.QtCore import SIGNAL

    from ui.workers.picture_fetch import PictureFetcher
    from ui.workers.profile_fetch import ProfileFetcher

    built_cfg = _setup_cfg(True, "")
    _app, built_window = app_mod.build_application(built_cfg, "Settings")
    try:
        before = app.receivers(SIGNAL("aboutToQuit()"))
        clock = app_mod.build_scheduler(built_cfg, app, built_window)
        kinds = [type(job).__name__ for job in clock.jobs]
        if kinds != ["ProfileFetcher", "ScheduledReference", "PictureFetcher"]:
            problems.append(f"reference start: the clock holds {kinds}, wanted "
                            "profile, reference, pictures")
        elif not (isinstance(clock.jobs[0], ProfileFetcher)
                  and isinstance(clock.jobs[2], PictureFetcher)
                  and clock.jobs[1].job is built_window.update_job):
            problems.append("reference start: the clock's update is not the "
                            "window's one shared update job")
        if clock._timer.isActive():
            problems.append("reference start: building the clock started it")
        if app.receivers(SIGNAL("aboutToQuit()")) - before != 3:
            problems.append("reference start: quitting does not wait for all "
                            "three jobs")
    finally:
        built_window.close()
    # A page, the clock built but not started, an update job: nothing runs.
    cfg, page = _reference_card_world(app)
    try:
        job = UpdateJob(cfg)
        scheduler = Scheduler(first_ms=10_000_000)
        scheduler.add(ScheduledReference(cfg, job))
        _settle(app)
        if job.running or page.update_job.running:
            problems.append("reference start: an update runs without the clock having ticked")
    finally:
        page.close()
    print("reference start    : behind the self-test guard, between profile and pictures")


def _timed(check, *args):
    """Run one `_check_*` and note how long it took (listed at the end of main)."""
    import time

    began = time.monotonic()
    try:
        return check(*args)
    finally:
        _CHECK_SECONDS.append((time.monotonic() - began, check.__name__))


def main() -> int:
    app = QApplication.instance() or QApplication([])
    _quiet()
    _offline()
    _no_real_notify_send()
    _no_real_overlay_helper()
    _watch_slot_exceptions()
    # Settings writes what is chosen on it (the theme and text size, on every
    # combo box and slider the walk below presses) through `settings_io`, which
    # writes to $XDG_CONFIG_HOME. Run as it was, the suite left the last item of
    # each in the user's own config file. Writes go to a scratch folder; reading
    # the user's file (a few checks load it for defaults) is unchanged.
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="wft-config-")
    from wft import config as config_mod
    from wft.data import db as dbq

    problems: list[str] = []
    # The database the data checks copy from, found before the data folder is
    # redirected. Nothing below opens it for anything but copying.
    source = testdb.source_database(config_mod)
    # A check that builds a page from a plain `config.load()` would otherwise
    # connect to the default data folder - which creates a database there, or
    # changes the one that is there. The default folder is a scratch one for
    # the whole run, so such a page meets an empty database, as on a first start.
    os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="wft-data-")

    # A database with the user's own shape, a database with nothing in it, and
    # a file that is not a database at all. The last one is the case that
    # matters: it is what a half-finished download leaves behind.
    cases = []

    full = pathlib.Path(tempfile.mkdtemp())
    has_data = testdb.has_game_data(source)
    if has_data:
        testdb.copy_database(source, full / "warframe.sqlite")
        cases.append(("a full database", full))

    empty = pathlib.Path(tempfile.mkdtemp())
    connection = dbq.connect(empty / "warframe.sqlite")
    schema = pathlib.Path(dbq.__file__).resolve().parent / "schema.sql"
    connection.executescript(schema.read_text())
    connection.commit()
    connection.close()
    cases.append(("an empty database", empty))

    broken = pathlib.Path(tempfile.mkdtemp())
    (broken / "warframe.sqlite").write_bytes(b"not a database" * 200)
    cases.append(("a corrupt database", broken))

    missing = pathlib.Path(tempfile.mkdtemp())
    cases.append(("no database at all", missing))

    for label, directory in cases:
        cfg = config_mod.load()
        cfg.override("paths.data_dir", str(directory))
        before = len(problems)
        pressed = _walk(app, cfg, problems)
        found = len(problems) - before
        print(f"{label:22s}: {pressed:4d} control(s) pressed, "
              f"{found} problem(s)")

    # Against a copy, never the real file. This check drives a review, and a
    # review exists to write to a database - an earlier version of it pointed
    # at a real one and left four imports in it that nobody had asked for.
    # Nothing in tests/ may open the live database for anything but copying.
    _timed(_check_manual_accepts_refinement, problems)
    _timed(_check_a_question_keeps_its_refinement, problems)
    _timed(_check_ocr_confusions_pick_the_right_numeral, problems)
    _timed(_check_two_tiles_for_one_relic_are_asked_about, problems)
    _timed(_check_a_bar_is_a_letter_inside_a_name, problems)
    _timed(_check_the_layout_with_no_empty_slots_wins, problems)
    _timed(_check_the_riven_page_shows_the_week, app, problems)
    _timed(_check_a_table_lights_the_whole_row, app, problems)
    _timed(_check_a_capped_table_settles, app, problems)
    _timed(_check_page_argument, problems)
    _timed(_check_focus_does_not_change_the_page, app, problems)
    _timed(_check_names_for_the_game_and_the_key, problems)
    _timed(_check_a_redirected_config_home_is_the_only_one, problems)
    _timed(_check_the_mastery_list_can_be_hit, app, problems)
    _timed(_check_a_cut_table_cell_shows_its_text, app, problems)
    _timed(_check_a_checked_radio_keeps_its_size, app, problems)
    _timed(_check_the_combo_arrow_follows_the_text, app, problems)
    _timed(_check_text_colours_can_be_read_in_every_theme, problems)
    _timed(_check_a_checked_box_shows_a_tick, app, problems)
    _timed(_check_numbers_are_aligned_to_the_right, app, problems)
    _timed(_check_a_long_path_is_shortened_with_its_whole_text, app, problems)
    _timed(_check_the_profile_is_fetched_once_a_day, app, problems)
    _timed(_check_the_profile_card_applies_what_it_fetched, app, problems)
    _timed(_check_the_profile_id_and_switch_are_saved, app, problems)
    _timed(_check_a_run_without_an_id_asks_nobody, app, problems)
    _timed(_check_the_pictures_are_fetched_by_themselves, app, problems)
    _timed(_check_the_pictures_card_and_the_import, app, problems)
    _timed(_check_the_pictures_run_asks_nobody_at_the_release_start, app, problems)
    _timed(_check_the_reference_card_has_a_saved_switch_and_says_when, app, problems)
    _timed(_check_the_reference_update_is_started_only_by_the_running_application, app, problems)
    _timed(_check_no_hotkey_is_assumed, problems)
    _timed(_check_the_setup_page_shows_each_state, app, problems)
    _timed(_check_the_download_button_runs_the_shared_update, app, problems)
    _timed(_check_a_key_is_chosen_by_pressing_it, app, problems)
    _timed(_check_the_listener_takes_a_new_key_without_a_restart, app, problems)
    _timed(_check_the_game_log_is_found_in_every_steam_layout, app, problems)
    _timed(_check_rewards_says_not_ready_until_setup_is_done, app, problems)
    _timed(_check_the_plan_points_at_the_page_that_fixes_it, app, problems)
    _timed(_check_no_text_uses_an_old_page_name, app, problems)
    _timed(_check_the_calibration_dialog_takes_the_last_screen, app, problems)
    _timed(_check_a_picture_that_is_not_the_game_window_is_warned_about, app, problems)
    _timed(_check_a_bad_reading_offers_to_calibrate_on_its_screen, app, problems)
    _timed(_check_the_setup_card_for_the_first_reward_screen, app, problems)
    _timed(_check_the_hotkey_reads_rewards_on_every_page_and_imports_come_from_files, app, problems)
    _timed(_check_the_window_remembers_its_size, app, problems)
    _timed(_check_a_reading_sends_one_notification, app, problems)
    _timed(_check_a_muted_kde_is_said, app, problems)
    _timed(_check_the_reset_buttons_delete_what_they_say, app, problems)
    _timed(_check_the_import_page_follows_a_theme_change, app, problems)
    _timed(_check_the_setup_card_for_the_inventory, app, problems)
    _timed(_check_the_guide_button_opens_what_there_is, app, problems)
    _timed(_check_the_private_word_check_finds_whole_words, app, problems)
    _timed(_check_the_installer_ends_with_the_start_question, problems)
    _timed(_check_the_installer_puts_the_new_folder_in_place_of_the_old, problems)
    _timed(_check_the_installer_reopens_itself_in_a_terminal, problems)
    _timed(_check_the_installer_and_doctor_speak_to_a_newcomer, problems)
    _timed(_check_the_guide_is_built_from_markdown, problems)
    _timed(_check_the_palette_grid, app, problems)
    _timed(_check_the_reward_overlay, app, problems)
    _timed(_check_click_to_pick, app, problems)
    _timed(_check_one_booking_per_screen, app, problems)
    _timed(_check_own_relic_from_the_log, app, problems)
    _timed(_check_the_row_has_a_gap_between_the_cards, problems)
    _timed(_check_the_calibration_fits_the_row_by_keys, app, problems)
    _timed(_check_an_old_calibration_is_seen_and_can_be_removed, app, problems)
    _timed(_check_the_overlay_says_why_it_ended, app, problems)
    _timed(_check_a_click_after_the_screen_closed_is_no_pick, app, problems)
    _timed(_check_the_log_files, app, problems)
    _timed(_check_versions_and_the_whats_new_window, app, problems)
    _timed(_check_the_ducat_page_can_be_reordered, config_mod.load(), problems)
    _timed(_check_the_trading_pages, app, problems)
    _timed(_check_add_and_correct_is_one_field, app, problems)
    _timed(_check_the_status_line_says_when_to_import_again, app, problems)

    # The checks below need the game's data (items, parts, relics), which `bin/wf
    # update` downloads. They read it from a copy of the source database; a
    # check that needs holdings makes its own in that copy.
    _timed(_check_the_official_feed_stands_in, problems)
    _timed(_check_a_board_behind_the_game_is_stale, problems)
    _timed(_check_mastered_sets_let_their_parts_go, problems)
    data_checks = [
        (_check_review_is_one_shape, ("app", "cfg")),
        (_check_review_keys_stay_on_the_list, ("app", "cfg")),
        (_check_review_one_handed_keys, ("app", "cfg")),
        (_check_requiem_relics_keep_their_keys, ("app", "cfg")),
        (_check_the_summary_puts_lowered_rows_first, ("app", "cfg")),
        (_check_gone_rows_come_from_every_source, ("app", "cfg")),
        (_check_absent_rows_are_judged_per_kind_and_category, ("app", "cfg")),
        (_check_a_relic_question_writes_the_stack_the_tile_shows, ("app", "cfg")),
        (_check_a_doubted_word_sends_its_names_last, ("app", "cfg")),
        (_check_relic_panel_reads_a_screen, ("cfg",)),
        (_check_read_at_reads_the_squad_screen, ("cfg",)),
        (_check_the_hotkey_keeps_the_last_reward_screen, ("app", "cfg")),
        (_check_kept_reward_pictures, ("app", "cfg")),
        (_check_1080p_reads_the_reward_row_with_the_default_regions, ("cfg",)),
        (_check_1080p_folder_import_reads_as_well_as_4k, ("cfg",)),
        (_check_a_build_consumes_its_pieces, ("app", "cfg")),
        (_check_a_built_weapon_that_is_a_component_goes_to_items, ("app", "cfg")),
        (_check_the_akimbo_primes_count_their_built_guns, ("app", "cfg")),
        (_check_a_sold_set_leaves_the_shelf, ("app", "cfg")),
        (_check_a_built_set_leaves_the_shelf, ("app", "cfg")),
        (_check_the_keep_one_mark_is_set_in_the_item_dialog, ("app", "cfg")),
        (_check_the_collection_marks_mastery, ("app", "cfg")),
        (_check_the_fissure_page_shows_a_board, ("app", "cfg")),
        (_check_settings_tabs_and_quick_missions, ("app", "cfg")),
        (_check_the_recommendation_order_in_settings, ("app", "cfg")),
        (_check_contested_tiles_get_their_own_question, ("app", "cfg")),
        (_check_the_apply_table_can_be_hit, ("app", "cfg")),
        (_check_every_page_tab_and_dialog_shows_its_text, ("app", "cfg")),
        (_check_the_ducat_page_can_empty_itself, ("app", "cfg")),
        (_check_every_page_can_be_reordered, ("app", "cfg")),
        (_check_the_survey_moved_unchanged, ("app", "cfg")),
        (_check_built_sets_free_their_parts_and_nothing_else, ("cfg",)),
        (_check_the_ducat_page_counts_a_piece_once, ("cfg",)),
    ]
    if has_data:
        sandbox = pathlib.Path(tempfile.mkdtemp())
        testdb.copy_database(source, sandbox / "warframe.sqlite")
        cfg = config_mod.load()
        cfg.override("paths.data_dir", str(sandbox))
        context = {"app": app, "cfg": cfg}
        for check, names in data_checks:
            _timed(check, *[context[name] for name in names], problems)
    else:
        print(f"SKIPPED: {len(data_checks)} checks need the game data. "
              f"{testdb.HOW_TO_GET_THE_DATA}")

    # Every window the checks left open is closed, so a close handler that
    # raises is still reported below. They are not destroyed: destroying the
    # leftovers crashed the process (exit 139 or 134) depending on the order -
    # first when the interpreter did it, and in 3 of 6 runs when
    # this suite did it itself with deleteLater, always after the last check.
    # The core dumps show a call through a freed pointer inside ~QObject while
    # a detached review card's children were deleted; the running application
    # left no such dump. Nothing checked here depends on destruction, so the
    # verdict is printed and the process ends without it (`os._exit`).
    for widget in QApplication.topLevelWidgets():
        widget.close()
    _settle(app)

    for text in sorted(set(_unexpected_slot_exceptions())):
        problems.append(f"callback: an exception in a slot or timer: {text}")

    print("slowest checks (wall seconds):")
    for seconds, name in sorted(_CHECK_SECONDS, reverse=True)[:15]:
        print(f"  {seconds:7.1f}  {name}")
    print(f"  {sum(s for s, _ in _CHECK_SECONDS):7.1f}  all {len(_CHECK_SECONDS)} checks")

    for problem in problems:
        print("   ", problem)
    if problems:
        print(f"FAILED — {len(problems)} problem(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    # Flushed and ended without the interpreter's teardown: see the end of
    # `main` for why the leftovers are not destroyed.
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
