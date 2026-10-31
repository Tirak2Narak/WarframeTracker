"""Application entry point.

Builds one QApplication, the main window with its views (the Rewards page
among them) — all in this one process. Run via `bin/wf-gui`.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget  # noqa: E402

from wft import config as config_mod  # noqa: E402


def _placeholder(text: str) -> QWidget:
    """Stand-in for a view not built yet in this development pass."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel(text)
    label.setObjectName("ViewTitle")
    layout.addWidget(label)
    layout.addStretch(1)
    return widget


_TAGGED = re.compile(r"^(rewards|hotkey|click reading|click|relic|overlay): ")


def tagged(message: str) -> str:
    """The line with its source tag: `rewards:` unless the line names its own,
    like `hotkey:` and `click:`."""
    return message if _TAGGED.match(message) else f"rewards: {message}"


def log_line_for_stderr(message: str, now: float | None = None) -> str:
    """One line of the Rewards page's log as it goes to stderr: the time of day
    first, and a source tag, so a log of one session can be read in order.
    The log file gets the same tagged text with the date."""
    stamp = time.strftime("%H:%M:%S", time.localtime(time.time() if now is None else now))
    return f"{stamp} {tagged(message)}"


def emit_log(message: str, now: float, stream=None) -> None:
    """The one place a log line is written: stderr with the time, and the week's
    log file with the date, both from the same tagged text."""
    from wft import logfile

    print(log_line_for_stderr(message, now), file=stream or sys.stderr, flush=True)
    logfile.write(tagged(message), now)


def wire_stderr_log(panel, stream=None, clock=time.time) -> None:
    """Every line the Rewards page logs also goes to stderr, with the time, and
    to the week's log file once `wft.logfile.enable` was called. The one place
    that does it."""
    def write(message: str) -> None:
        emit_log(message, clock(), stream)

    panel.log_line.connect(write)
    # Kept on the panel, or the closure would be collected with this frame.
    panel._stderr_writer = write


def build_application(cfg, page: str = "Plan") -> tuple[QApplication, "MainWindow"]:
    from ui import theme
    from ui.main_window import MainWindow
    from ui.views.settings_view import SettingsView, UpdateJob
    from ui.views.setup_view import SetupView

    app = QApplication.instance() or QApplication(sys.argv)
    # The Wayland app id, named after the desktop entry. Without it the id is
    # the interpreter's, "python3", and KWin cannot pair the window with the
    # entry KRunner launched it from.
    app.setDesktopFileName("warframe-tracker")
    app.setStyle("Fusion")

    # Theme and text size come from the config, which is where Settings writes
    # them. An unknown name falls back to the default rather than failing: a
    # config edited by hand should not be able to leave the window unpainted.
    chosen = str(cfg.get("display.theme", theme.DEFAULT_THEME) or theme.DEFAULT_THEME)
    try:
        size = int(cfg.get("display.font_size", theme.DEFAULT_FONT_PT)
                   or theme.DEFAULT_FONT_PT)
    except (TypeError, ValueError):
        size = theme.DEFAULT_FONT_PT
    app.setStyleSheet(theme.apply(chosen, size))
    app.setFont(theme.base_font(size))

    relic_panel = None
    try:
        from ui.views.relic_panel import RelicPanel
        relic_panel = RelicPanel(cfg)
        wire_stderr_log(relic_panel)
    except Exception as exc:  # noqa: BLE001 - the rest of the app must not die with it
        print(f"warning: relic panel unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"relic panel: {exc}")

    window = MainWindow(cfg, relic_panel=relic_panel)
    # Settings ("Update now") and Setup ("Download game data") are two buttons
    # for one job; one object, so they cannot run it twice at once.
    update_job = UpdateJob(cfg, window)
    window.update_job = update_job

    try:
        from ui.views.import_view import ImportView
        import_page = ImportView(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: import view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"import view: {exc}")
        import_page = _placeholder("Import — under construction")
    window.register_page("Import", import_page)

    try:
        from ui.views.collection_view import CollectionView
        collection_page = CollectionView(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: collection view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"collection view: {exc}")
        collection_page = _placeholder("Collection — under construction")
    window.register_page("Collection", collection_page)

    try:
        from ui.views import fissure_view as fissure_view_mod
        fissure_page = fissure_view_mod.FissureView(cfg)
        # A board fetch caught mid-flight by quit aborts the process (see
        # wait_for_fetches). aboutToQuit covers the normal exit; the
        # self-test, which never enters the event loop, waits on its own.
        app.aboutToQuit.connect(fissure_view_mod.wait_for_fetches)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: fissure view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"fissure view: {exc}")
        fissure_page = _placeholder("Fissures — under construction")
    window.register_page("Fissures", fissure_page)

    try:
        from ui.views.plan_view import PlanView
        plan_page = PlanView(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: plan view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"plan view: {exc}")
        plan_page = _placeholder("Plan — under construction")
    window.register_page("Plan", plan_page)

    try:
        from ui.views.ducat_view import DucatView
        ducat_page = DucatView(cfg)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: ducat view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"ducat view: {exc}")
        ducat_page = _placeholder("Ducats — under construction")
    window.register_page("Ducats", ducat_page)

    # Two pages from one class: the message for the trading chat, from the
    # same lists the Ducats page's numbers come from.
    from ui.views.trading_view import PAGES as trading_pages
    for page_name, kind in trading_pages.items():
        try:
            from ui.views.trading_view import TradingView
            trading_page = TradingView(cfg, kind)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: {page_name} view unavailable: {exc}", file=sys.stderr)
            _BUILD_FAILURES.append(f"{page_name} view: {exc}")
            trading_page = _placeholder(f"{page_name} — under construction")
        window.register_page(page_name, trading_page)

    try:
        from ui.views import riven_view as riven_view_mod
        riven_page = riven_view_mod.RivenView(cfg)
        # Same reasoning as the fissure board: a fetch caught mid-flight by
        # quit aborts the process, and the self-test waits on its own.
        app.aboutToQuit.connect(riven_view_mod.wait_for_fetches)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: riven view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"riven view: {exc}")
        riven_page = _placeholder("Rivens — under construction")
    window.register_page("Rivens", riven_page)

    # Guarded like every other page. It was the one construction without a
    # net, on the grounds that Settings always works - and then a database
    # that was not a database took the whole application down through it.
    try:
        settings_page = SettingsView(cfg, relic_panel=relic_panel,
                                     update_job=update_job)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: settings view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"settings view: {exc}")
        settings_page = _placeholder("Settings — under construction")
    window.register_page("Settings", settings_page)

    try:
        setup_page = SetupView(cfg, update_job=update_job)
        setup_page.import_requested.connect(lambda: window.select("Import"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: setup view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"setup view: {exc}")
        setup_page = _placeholder("Setup — under construction")
    window.register_page("Setup", setup_page)

    # The overlay helper starts only when a grid or a reward overlay is asked for;
    # building the client starts nothing. It is ended with the application.
    try:
        from ui.overlay.client import OverlayClient
        from ui.views.overlays_view import OverlaysView
        overlay_client = OverlayClient(window)
        app.aboutToQuit.connect(overlay_client.shutdown)
        window.overlay_client = overlay_client
        from ui.overlay.rewards import RewardOverlay
        reward_overlay = RewardOverlay(cfg, overlay_client, parent=window)
        if relic_panel is not None:
            relic_panel.set_overlay(reward_overlay)
        overlays_page = OverlaysView(cfg, overlay_client, rewards=reward_overlay)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: overlays view unavailable: {exc}", file=sys.stderr)
        _BUILD_FAILURES.append(f"overlays view: {exc}")
        overlays_page = _placeholder("Overlays — under construction")
    window.register_page("Overlays", overlays_page)

    # The rewards page holds the panel itself. Registered after the others so
    # the panel exists for every page that wants to talk to it, and placed in
    # the navigation where the hotkey will switch to it.
    if relic_panel is not None:
        window.register_page("Rewards", relic_panel)

    window.select(page)
    return app, window


#: Pages that failed to build. A page is wrapped in a try so one broken view
#: cannot take out the whole application, which is right at runtime and wrong
#: in a test - a self-test that prints OK while a page is missing is worse than
#: none, because it is believed.
_BUILD_FAILURES: list[str] = []


def _scratch_data_dir(cfg):
    """Point the config at a disposable copy of the database, and stock it.

    An empty inventory sends every data-driven view down its "nothing yet"
    branch, so the self-test was walking six pages without rendering a single
    table. It passed while the Plan page was missing an import and raised on
    the first row it tried to draw.

    A copy rather than the real database because the seeding below writes, and
    a test that can damage the user's inventory is worse than no test.
    """
    import shutil
    import tempfile

    from wft.data import db as dbq

    scratch = tempfile.mkdtemp(prefix="wft-selftest-")
    source = cfg.db_path
    if not source.exists():
        # Nothing to copy and nothing to seed. The pages still get walked
        # against an empty database, which is the weaker test but a real one -
        # better than a "no such table" traceback that reads like a code fault.
        print("note: no database built yet — the self-test runs against an "
              "empty one. Run `wf update` for the fuller check.",
              file=sys.stderr)
        cfg.override("paths.data_dir", scratch)
        # The schema, so the pages meet empty tables rather than missing ones.
        # A view asking an absent table raises where it would otherwise take
        # its "nothing yet" branch, which is a fault in the test, not the code.
        schema = Path(dbq.__file__).resolve().parent / "schema.sql"
        connection = dbq.connect(cfg.db_path)
        connection.executescript(schema.read_text())
        connection.commit()
        connection.close()
        return scratch
    shutil.copy2(source, Path(scratch) / source.name)
    cfg.override("paths.data_dir", scratch)

    connection = dbq.connect(cfg.db_path)
    for table in ("owned_parts", "owned_items", "owned_relics"):
        connection.execute(f"DELETE FROM {table}")

    # Enough shapes that every branch has something to draw: a set one piece
    # short, one further off, a built item, a mastered one, and relics that
    # actually drop something missing.
    items = connection.execute(
        "SELECT unique_name FROM items WHERE unique_name IN "
        "(SELECT item_unique FROM parts) ORDER BY name LIMIT 8").fetchall()
    for index, item in enumerate(items):
        parts = connection.execute(
            "SELECT unique_name FROM parts WHERE item_unique=? AND alias_of IS NULL",
            (item["unique_name"],)).fetchall()
        for part in parts[:max(0, len(parts) - 1 - index % 3)]:
            dbq.set_part_count(connection, part["unique_name"], 1, "test")
        if index % 4 == 0:
            dbq.set_item_count(connection, item["unique_name"], 1, "test")
        if index % 3 == 0:
            dbq.set_item_mastered(connection, item["unique_name"])
        for part in parts:
            for relic in dbq.relics_dropping(connection, part["unique_name"])[:1]:
                row = connection.execute(
                    "SELECT unique_name FROM relics WHERE name=? LIMIT 1",
                    (relic["name"],)).fetchone()
                if row:
                    dbq.set_relic_count(connection, row["unique_name"], 2, "test")
    connection.commit()
    connection.close()
    return scratch


def _stop_hotkey(worker) -> None:
    worker.requestInterruption()
    worker.wait(2000)


def _check_pick_is_one(cfg, panel) -> list[str]:
    """A reward screen is one choice, however many times it is clicked.

    Every click used to add one, so a mis-click corrected by clicking the
    right card left both parts counted. Four evenings of that is how a stock
    reached five when one had been picked. Driven here rather than reasoned
    about because the counting happens in the database, not in the widget.
    """
    from wft.data import db as dbq
    from wft.relic import score as sc
    from ui.workers.relic_trigger import TriggerResult

    if panel is None:
        return []
    connection = dbq.connect(cfg.db_path)
    try:
        names = [row["unique_name"] for row in connection.execute(
            "SELECT unique_name FROM parts WHERE alias_of IS NULL LIMIT 3")]
        if len(names) < 3:
            return []
        before = {name: dbq.owned_count(connection, name) for name in names}
    finally:
        connection.close()

    def rewards():
        return [sc.Reward(position=index + 1, raw_text=name, part_unique=name,
                          name=name, item_name=name, set_total=4,
                          category="warframe")
                for index, name in enumerate(names)]

    def verdict():
        found = rewards()
        return TriggerResult(
            verdict=sc.Verdict(rewards=found, best=found[0], alternative=None,
                               headline="self-test"),
            error=None, reward_count=len(found))

    def held():
        connection = dbq.connect(cfg.db_path)
        try:
            return {name: dbq.owned_count(connection, name) for name in names}
        finally:
            connection.close()

    errors = []
    panel.show_result(verdict())
    panel._confirm_pick(0)
    panel._confirm_pick(1)
    panel._confirm_pick(2)
    moved = {name: held()[name] - before[name] for name in names}
    if sum(moved.values()) != 1 or moved[names[2]] != 1:
        errors.append(f"three clicks on one screen recorded {moved}, expected "
                      f"one copy of the last card")

    panel.show_result(verdict())
    panel._confirm_pick(0)
    total = sum(held().values()) - sum(before.values())
    if total != 2:
        errors.append(f"a second screen recorded {total} parts in total, expected 2")
    return errors


def resolve_page(name: str) -> str | None:
    """The page a `--page` argument names, matched case-insensitively.

    Read from the class, not from a built window: the names are known before
    any Qt object exists, so a typo is refused without opening anything.
    """
    from ui.main_window import MainWindow

    for known in MainWindow.NAV_ITEMS:
        if known.lower() == name.strip().lower():
            return known
    return None


def start_page_for(cfg, requested: str | None = None) -> str:
    """The page the application opens on.

    An explicit `--page` always wins. Otherwise Setup while a required step is
    open (game data, keyboard access, a hotkey): a newcomer's first start used
    to land on an empty Plan page that pointed at pages by their old names. Once
    nothing required is open it starts as it always did.
    """
    from wft import readiness

    if requested:
        return requested
    try:
        return "Plan" if readiness.ready(cfg) else "Setup"
    except Exception as exc:  # noqa: BLE001 - a start page must not stop the start
        print(f"warning: could not tell whether setup is complete: {exc}",
              file=sys.stderr)
        return "Plan"


def _tidy_kept_pictures(cfg) -> None:
    """Move expired day folders of the kept reward pictures to the trash.

    On a thread of its own so a big folder cannot hold up the window; the
    work and its safety rules are `wft.relic.kept_pictures.tidy`.
    """
    import threading

    from ui import reset
    from wft.relic import kept_pictures

    def run():
        kept_pictures.tidy(
            cfg, reset.to_trash,
            report=lambda msg: print(f"kept pictures: {msg}", file=sys.stderr))

    threading.Thread(target=run, name="kept-pictures-tidy", daemon=True).start()


def build_scheduler(cfg, app, window):
    """The clock with its jobs, in order: profile, reference data, pictures, update check.

    Not started here: `main` starts it, and only outside `--self-test`. A
    function of its own so a test can build exactly what `main` builds; the
    checks used to read this code as text, and a name that existed only in
    `build_application` (`update_job`) passed them and left the running
    tool with no clock at all.
    """
    from ui.scheduler import Scheduler
    from ui.workers.picture_fetch import shared as picture_fetcher
    from ui.workers.profile_fetch import shared as profile_fetcher
    from ui.workers.reference_update import ScheduledReference
    from ui.workers.update_check import shared as update_checker

    scheduler = Scheduler(window)
    fetcher = profile_fetcher(cfg)
    scheduler.add(fetcher)
    app.aboutToQuit.connect(fetcher.wait)
    reference_job = ScheduledReference(cfg, window.update_job)
    scheduler.add(reference_job)
    app.aboutToQuit.connect(reference_job.wait)
    pictures_job = picture_fetcher(cfg)
    scheduler.add(pictures_job)
    app.aboutToQuit.connect(pictures_job.wait)
    update_job = update_checker(cfg)
    scheduler.add(update_job)
    app.aboutToQuit.connect(update_job.wait)
    return scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wf-gui", description="warframe-tracker GUI")
    parser.add_argument("--self-test", action="store_true",
                        help="build the interface, verify it, and exit without an event loop")
    parser.add_argument("--page", metavar="NAME",
                        help="open on this page instead of Plan, or Setup while "
                             "a required step is open (case-insensitive; used by "
                             "the desktop entry's quick actions)")
    args = parser.parse_args(argv)

    requested = None
    if args.page is not None:
        from ui.main_window import MainWindow
        requested = resolve_page(args.page)
        if requested is None:
            parser.error(f"unknown page {args.page!r}; valid pages: "
                         + ", ".join(MainWindow.NAV_ITEMS))

    cfg = config_mod.load()
    scratch = None
    if args.self_test:
        scratch = _scratch_data_dir(cfg)
    else:
        from wft import logfile
        logfile.enable(cfg)
    start_page = start_page_for(cfg, requested)

    # Asked before the window exists: building it creates the database and the
    # settings file, and an update is told from a new install by those two.
    fresh_install = False
    if not args.self_test:
        from ui.views import whatsnew_dialog
        fresh_install = whatsnew_dialog.is_fresh_install(cfg)

    app, window = build_application(cfg, start_page)
    if not args.self_test:
        _tidy_kept_pictures(cfg)
    if args.self_test:
        # Offscreen and sized by nobody: storing that would replace the size
        # the user gave the real window.
        window.remember_geometry = False

    hotkey_worker = None
    if not args.self_test and window.relic_panel is not None:
        try:
            from ui.workers.hotkey import HotkeyWorker

            hotkey_worker = HotkeyWorker(cfg)
            # Switch to the page and read. In that order, so the user is
            # looking at the result while it is still being read rather than
            # after — the fifteen seconds are already running.
            hotkey_worker.fired.connect(window.relic_panel.log_line)
            hotkey_worker.triggered.connect(window.show_rewards)
            hotkey_worker.triggered.connect(window.relic_panel.trigger)
            hotkey_worker.status.connect(
                lambda msg: emit_log(f"hotkey: {msg}", time.time()))
            app.aboutToQuit.connect(lambda: _stop_hotkey(hotkey_worker))
            hotkey_worker.start()
        except Exception as exc:  # noqa: BLE001 - the app must run without it
            print(f"warning: hotkey listener unavailable: {exc}", file=sys.stderr)

    click_worker = None
    reward_overlay = getattr(window.relic_panel, "overlay", None) if window.relic_panel else None
    if not args.self_test and reward_overlay is not None:
        try:
            from ui.workers.pick_click import PickClickWorker

            click_worker = PickClickWorker(cfg, reward_overlay.arming)
            click_worker.clicked.connect(reward_overlay.on_click)
            click_worker.status.connect(
                lambda msg: emit_log(f"click reading: {msg}", time.time()))
            click_worker.ignored.connect(reward_overlay.note)
            app.aboutToQuit.connect(click_worker.stop)
            click_worker.start()
        except Exception as exc:  # noqa: BLE001 - the app must run without it
            print(f"warning: click reading unavailable: {exc}", file=sys.stderr)

    # Everything the tool fetches by itself runs from this one clock. Not
    # built for the self-test: that run has no event loop to wait in and must
    # never reach for the network. With no account id, or the switch off, the
    # job is never due and nothing happens.
    if not args.self_test:
        try:
            scheduler = build_scheduler(cfg, app, window)
            scheduler.start()
            window.scheduler = scheduler
        except Exception as exc:  # noqa: BLE001 - the app must run without it
            print(f"warning: the scheduled fetch is unavailable: {exc}",
                  file=sys.stderr)

    if args.self_test:
        window.show()
        app.processEvents()
        errors = list(_BUILD_FAILURES)
        if window.stack.count() != len(window.NAV_ITEMS):
            errors.append(f"expected {len(window.NAV_ITEMS)} pages, got {window.stack.count()}")
        if window.stack.currentWidget() is not window._pages[start_page]:
            errors.append(f"asked to open on {start_page}, the window shows another page")
        for name in window.NAV_ITEMS:
            window.select(name)
            app.processEvents()
        errors += _check_pick_is_one(cfg, window.relic_panel)
        # A widget that lost its layout but is still switched visible becomes a
        # window of its own: empty, titled with the program name, and opened
        # by the hotkey in the middle of a mission.
        app.processEvents()
        stray = [w for w in app.topLevelWidgets() if w.isVisible() and w is not window]
        if stray:
            errors.append("visible without a parent window: "
                          + ", ".join(type(w).__name__ for w in stray))
        window.close()
        app.processEvents()
        # The walk above visited Fissures, whose board fetch may still be in
        # flight - and this path never enters the event loop, so aboutToQuit
        # will not fire to wait for it.
        try:
            from ui.views.fissure_view import wait_for_fetches
            wait_for_fetches()
        except Exception:  # noqa: BLE001 - the page may not have built at all
            pass
        # And Rivens, which fetches its week the same way on first visit.
        try:
            from ui.views.riven_view import wait_for_fetches as wait_riven
            wait_riven()
        except Exception:  # noqa: BLE001 - the page may not have built at all
            pass
        if scratch:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)
        if errors:
            print("SELF-TEST FAILED: " + "; ".join(errors))
            return 1
        print("SELF-TEST OK")
        return 0

    window.show()
    # Once after an update. Not in the self-test, which returned above.
    whatsnew_dialog.start(window, fresh_install)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
