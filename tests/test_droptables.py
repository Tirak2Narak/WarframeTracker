"""Vault status from DE's drop tables, against a page shaped like theirs.

The fixture below is not the live page - that is 4.4 MB and changes with
every rotation - but a cut-down copy of its structure as served on
2026-09-25: the disclaimer and table of contents before the first section,
`<h3 id="...">` headings, mission tables with rotations, a refined relic
from Elite Sanctuary Onslaught, the relic contents section (which must never
count as a drop), a Kuva Siphon table with Requiem Eterna, a bounty table
with an empty first cell, the enemy table, and an event mission whose name
ends in "Relic". It carries every shape the parser has to get right, and a
few it has to refuse.

No network: `apply` is exercised with `_download` stubbed, because the one
thing a test must not do is spend a request on DE's CDN. Nothing here opens
the live database; the rebuild check copies the live manifests into a
temporary directory and skips when there are none.

Run: python3 tests/test_droptables.py
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wft import config as config_mod  # noqa: E402
from wft.data import vaulted  # noqa: E402
from wft.data.vaulted import DropTableError  # noqa: E402

SCHEMA = (pathlib.Path(__file__).resolve().parent.parent
          / "wft" / "data" / "schema.sql")

FIXTURE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Warframe PC Drops</title></head>
<body>
<p><b>Last Update:</b>
23 September, 2026
<h3>Disclaimer:</h3>
<p>Not a drop: <table><tr><td>Neo Z9 Relic</td></tr></table></p>
<h3>Table of Contents:</h3>
<ul><li><a href="#missionRewards">Missions</a></li><li><a href="#relicRewards">Relics</a></li></ul>
<h3 id="missionRewards">Missions:</h3>
<table>
<tr><th colspan="2">Void/Mot (Survival)</th></tr>
<tr><th colspan="2">Rotation A</th></tr>
<tr><td>Lith C15 Relic</td><td>Uncommon (7.14%)</td></tr>
<tr><th colspan="2">Rotation C</th></tr>
<tr><td>Axi C12 Relic</td><td>Rare (0.29%)</td></tr>
<tr class="blank-row"><td class="blank-row" colspan="2"></td></tr>
<tr><th colspan="2">Sanctuary/Elite Onslaught (Sanctuary Onslaught)</th></tr>
<tr><th colspan="2">Rotation A</th></tr>
<tr><td>Meso K9 Relic (Radiant)</td><td>Uncommon (7.04%)</td></tr>
<tr><th colspan="2">Event: Help Clem Retrieve The Relic</th></tr>
<tr><td>100 Endo</td><td>Common (50.00%)</td></tr>
</table>
<h3 id="relicRewards">Relics:</h3>
<table>
<tr><th colspan="2">Lith C15 Relic (Intact)</th></tr><tr><td>Corufell Prime Stock</td><td>Uncommon (25.33%)</td></tr>
<tr><th colspan="2">Lith C15 Relic (Radiant)</th></tr><tr><td>Corufell Prime Stock</td><td>Uncommon (16.67%)</td></tr>
<tr><th colspan="2">Axi C12 Relic (Intact)</th></tr><tr><td>Corufell Prime Handle</td><td>Uncommon (11.00%)</td></tr>
<tr><th colspan="2">Axi S20 Relic (Intact)</th></tr><tr><td>Forma Blueprint</td><td>Common (25.33%)</td></tr>
<tr><th colspan="2">Meso K9 Relic (Intact)</th></tr><tr><td>Forma Blueprint</td><td>Common (25.33%)</td></tr>
<tr><th colspan="2">Neo C10 Relic (Intact)</th></tr><tr><td>Corufell Prime Receiver</td><td>Rare (2.00%)</td></tr>
<tr><th colspan="2">Requiem Iii Relic (Intact)</th></tr><tr><td>Netra Requiem</td><td>Uncommon (25.33%)</td></tr>
<tr><th colspan="2">Requiem Eterna Relic (Intact)</th></tr><tr><td>Requiem Relic (Intact)</td><td>Common (100.00%)</td></tr>
</table>
<h3 id="transientRewards">Dynamic Location Rewards:</h3>
<table>
<tr id="kuvasiphon"><th colspan="2">Kuva Siphon</th></tr>
<tr><td>100 Endo</td><td>Common (50.00%)</td></tr>
<tr><td>Requiem Eterna Relic</td><td>Common (50.00%)</td></tr>
</table>
<h3 id="cetusRewards">Cetus Bounty Rewards:</h3>
<table>
<tr><th colspan="3">Level 5 - 15 Cetus Bounty</th></tr>
<tr><th colspan="3">Rotation A</th></tr>
<tr><td></td><th>Stage 1</th><td></td></tr>
<tr><td></td><td>Lith C15 Relic</td><td>Uncommon (11.06%)</td></tr>
</table>
<h3 id="relicByAvatar">Relic Drops by Source:</h3>
<table>
<tr><th>Corrupted Heavy Gunner</th><th colspan="2">Relic Drop Chance: 20.00%</th></tr>
<tr><td></td><td>Neo C10 Relic</td><td>Uncommon (8.33%)</td></tr>
</table>
</body></html>
"""

DROPPING = {"lith c15 relic", "axi c12 relic", "meso k9 relic",
            "requiem eterna relic", "neo c10 relic"}

# unique_name, name, tier, vaulted before the refresh. The befores are WFCD's
# answers as of 2026-09-25, which is the point: two of them are backwards.
RELICS = [
    *[(f"/T1CitrineD{r}", "Lith C15 Relic", "Lith", 1)
      for r in ("Bronze", "Silver", "Gold", "Platinum")],
    ("/T4CitrineBBronze", "Axi C12 Relic", "Axi", 1),
    ("/T4XakuBronze", "Axi S20 Relic", "Axi", 0),
    ("/T2MesoK9Bronze", "Meso K9 Relic", "Meso", 1),
    ("/T3CitrineBronze", "Neo C10 Relic", "Neo", 1),
    ("/T5ImmortalCBronze", "Requiem III Relic", "Requiem", 1),
    ("/T5ImmortalOmniA", "Requiem Eterna Relic", "Requiem", 1),
    ("/T3NotOnPage", "Neo Z9 Relic", "Neo", None),
]


def _db(path: pathlib.Path | str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.executemany("INSERT INTO relics(unique_name,name,tier,vaulted) VALUES(?,?,?,?)",
                     RELICS)
    conn.executemany(
        "INSERT INTO owned_relics(relic_unique,count,source,updated_at) VALUES(?,?,?,?)",
        [("/T1CitrineDBronze", 3, "video", "2026-09-25 10:00:00"),
         ("/T4XakuBronze", 7, "manual", "2026-09-20 10:00:00")])
    conn.execute("INSERT INTO meta(key,value) VALUES('vaulted_updated_at','2026-09-25 13:36:47')")
    conn.commit()
    return conn


def _vault(conn) -> dict[str, int | None]:
    return dict(conn.execute("SELECT unique_name, vaulted FROM relics"))


def _owned_digest(conn) -> str:
    rows = conn.execute("SELECT * FROM owned_relics ORDER BY relic_unique").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def _check_parse() -> list[str]:
    problems = []
    tables = vaulted.parse(FIXTURE)
    if tables.available != DROPPING:
        problems.append(f"parse: dropping {sorted(tables.available)}, "
                        f"wanted {sorted(DROPPING)}")
    if "axi s20 relic" in tables.available:
        problems.append("parse: a relic with only a contents table counted as dropping")
    if "neo z9 relic" in tables.available:
        problems.append("parse: a cell before the first section counted as a drop")
    if "requiem iii relic" not in tables.contents:
        problems.append("parse: 'Requiem Iii' in the contents section was not read")
    if tables.page_date != "23 September, 2026":
        problems.append(f"parse: page date {tables.page_date!r}")
    if tables.sections.get("relicByAvatar") != 1 or tables.sections.get("cetusRewards") != 1:
        problems.append(f"parse: per-section counts {tables.sections}")
    if vaulted.CONTENTS_SECTION in tables.sections:
        problems.append("parse: the contents section was counted as a reward table")
    return problems


def _refused(text: str, why: str) -> list[str]:
    try:
        vaulted.parse(text)
    except DropTableError:
        return []
    return [f"refuse: {why} was accepted"]


def _check_refusals() -> list[str]:
    problems = []
    problems += _refused(FIXTURE.replace('id="missionRewards"', 'id="missions"'),
                         "a page without the missions section")
    problems += _refused(FIXTURE.replace('id="relicRewards"', 'id="relics"'),
                         "a page without the contents section")
    problems += _refused("<html><body>Service Unavailable</body></html>",
                         "an error page")
    no_drops = FIXTURE
    for name in ("Lith C15 Relic</td>", "Axi C12 Relic</td>", "Meso K9 Relic (Radiant)</td>",
                 "Requiem Eterna Relic</td>", "Neo C10 Relic</td>"):
        no_drops = no_drops.replace(name, "Orokin Cell</td>")
    problems += _refused(no_drops, "a page whose reward tables name no relic")
    no_contents = FIXTURE.replace(" Relic (Intact)</th>", " (Intact)</th>").replace(
        " Relic (Radiant)</th>", " (Radiant)</th>")
    problems += _refused(no_contents, "a page whose contents section names no relic")
    return problems


def _check_apply() -> list[str]:
    problems = []
    conn = _db()
    owned = _owned_digest(conn)
    notes: list[str] = []
    counts = vaulted.apply_tables(conn, vaulted.parse(FIXTURE), notes, fetched_at="T")
    conn.commit()
    got = _vault(conn)
    want = {
        **{f"/T1CitrineD{r}": 0 for r in ("Bronze", "Silver", "Gold", "Platinum")},
        "/T4CitrineBBronze": 0,       # new Citrine relic, WFCD said vaulted
        "/T4XakuBronze": 1,           # rotated out, WFCD said available
        "/T2MesoK9Bronze": 0,         # only drops Radiant, still drops
        "/T3CitrineBronze": 0,        # only an enemy drops it
        "/T5ImmortalCBronze": 1,      # contents only, and spelled Iii there
        "/T5ImmortalOmniA": 0,        # Kuva Siphon
        "/T3NotOnPage": 1,            # nowhere on the page is no drop table
    }
    for unique, state in want.items():
        if got.get(unique) != state:
            problems.append(f"apply: {unique} is {got.get(unique)}, wanted {state}")
    if counts["available"] != 8 or counts["vaulted"] != 3:
        problems.append(f"apply: counts {counts}")
    if _owned_digest(conn) != owned:
        problems.append("apply: owned_relics changed")
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    if meta.get("vaulted_updated_at") == "2026-09-25 13:36:47":
        problems.append("apply: vaulted_updated_at was not restamped")
    if meta.get("droptables_fetched_at") != "T" \
            or meta.get("droptables_last_update") != "23 September, 2026" \
            or meta.get("vaulted_source") != "droptables":
        problems.append(f"apply: meta stamps {meta}")
    if not any("appear nowhere" in note for note in notes):
        problems.append(f"apply: the relic missing from the page was not reported: {notes}")
    conn.close()
    return problems


def _check_unknown_relic_is_named() -> list[str]:
    conn = _db()
    notes: list[str] = []
    page = FIXTURE.replace("<tr><td>100 Endo</td><td>Common (50.00%)</td></tr>\n</table>",
                           "<tr><td>Axi Q99 Relic</td><td>Rare (1.00%)</td></tr>\n</table>", 1)
    vaulted.apply_tables(conn, vaulted.parse(page), notes)
    conn.close()
    if not any("Axi Q99 Relic" in note and "not in the database" in note for note in notes):
        return [f"unknown: a dropping relic the database lacks went unmentioned: {notes}"]
    return []


def _check_join_guards() -> list[str]:
    problems = []
    tables = vaulted.parse(FIXTURE)
    # Nothing on the page matches: the page and the database disagree about
    # what a relic is called. Writing would vault everything.
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    conn.execute("INSERT INTO relics VALUES('/x','Lith Z1 Relic','Lith',0)")
    try:
        vaulted.apply_tables(conn, tables, [])
        problems.append("guard: a join that matched nothing was written")
    except DropTableError:
        if _vault(conn) != {"/x": 0}:
            problems.append("guard: the refused join still changed a row")
    conn.close()
    # Everything matches: every relic available is not a drop table.
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA.read_text())
    conn.execute("INSERT INTO relics VALUES('/y','Lith C15 Relic','Lith',1)")
    try:
        vaulted.apply_tables(conn, tables, [])
        problems.append("guard: all-available was written")
    except DropTableError:
        pass
    conn.close()
    return problems


def _sandbox(page: str | Exception, url: str = "https://example.invalid/drops.html"):
    """A config on a temporary data dir, with the download stubbed."""
    home = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(home))
    cfg.override("sources.droptables_url", url)
    _db(cfg.db_path).close()

    def fake_download(_url, target):
        if isinstance(page, Exception):
            raise page
        pathlib.Path(target).write_text(page, encoding="utf-8")
        return "2026-09-25T16:00:00+00:00"

    return home, cfg, fake_download


def _check_refresh_writes_page_and_status() -> list[str]:
    problems = []
    home, cfg, fake = _sandbox(FIXTURE)
    real, vaulted._download = vaulted._download, fake
    try:
        cfg.droptables_path.write_text("yesterday's page", encoding="utf-8")
        vaulted.apply(cfg, [])
    finally:
        vaulted._download = real
    if cfg.droptables_path.read_text(encoding="utf-8") != FIXTURE:
        problems.append("refresh: the accepted page did not replace the old one")
    if list(home.glob("*.part")) or list(home.glob("*.rejected.*")):
        problems.append("refresh: a temporary or rejected file was left behind")
    conn = sqlite3.connect(cfg.db_path)
    if _vault(conn).get("/T4CitrineBBronze") != 0:
        problems.append("refresh: the status was not committed")
    if dict(conn.execute("SELECT key, value FROM meta")).get(
            "droptables_fetched_at") != "2026-09-25T16:00:00+00:00":
        problems.append("refresh: the fetch time was not stamped")
    conn.close()
    shutil.rmtree(home)
    return problems


def _check_refresh_failure_keeps_everything() -> list[str]:
    problems = []
    cases = [
        ("a page in a new layout", "<html><body><h3>Missions:</h3></body></html>"),
        ("a download that fails", OSError("connection reset")),
    ]
    for label, page in cases:
        home, cfg, fake = _sandbox(page)
        cfg.droptables_path.write_text("yesterday's page", encoding="utf-8")
        conn = sqlite3.connect(cfg.db_path)
        before_vault, before_owned = _vault(conn), _owned_digest(conn)
        before_meta = dict(conn.execute("SELECT key, value FROM meta"))
        conn.close()
        real, vaulted._download = vaulted._download, fake
        try:
            vaulted.apply(cfg, [])
            problems.append(f"failure: {label} did not raise")
        except DropTableError as exc:
            if "2026-09-25 13:36:47" not in str(exc):
                problems.append(f"failure: {label} does not say how old the kept status is: {exc}")
        finally:
            vaulted._download = real
        conn = sqlite3.connect(cfg.db_path)
        if _vault(conn) != before_vault:
            problems.append(f"failure: {label} changed vault status")
        if _owned_digest(conn) != before_owned:
            problems.append(f"failure: {label} changed owned_relics")
        if dict(conn.execute("SELECT key, value FROM meta")) != before_meta:
            problems.append(f"failure: {label} restamped meta")
        conn.close()
        if cfg.droptables_path.read_text(encoding="utf-8") != "yesterday's page":
            problems.append(f"failure: {label} replaced the accepted page")
        if list(home.glob("*.part")):
            problems.append(f"failure: {label} left a .part file")
        if isinstance(page, str) and not list(home.glob("droptables.rejected.html")):
            problems.append(f"failure: {label} did not keep the refused page")
        shutil.rmtree(home)
    return problems


def _check_unconfigured_url() -> list[str]:
    home, cfg, _fake = _sandbox(FIXTURE, url="")
    try:
        vaulted.apply(cfg, [])
        return ["unconfigured: an empty droptables_url was not refused"]
    except DropTableError as exc:
        if "droptables_url" not in str(exc):
            return [f"unconfigured: the error does not name the setting: {exc}"]
        return []
    finally:
        shutil.rmtree(home)


def _check_missing_database() -> list[str]:
    home, cfg, fake = _sandbox(FIXTURE)
    cfg.db_path.unlink()
    real, vaulted._download = vaulted._download, fake
    try:
        vaulted.apply(cfg, [])
        return ["no database: a refresh without a database went through"]
    except DropTableError:
        if cfg.db_path.exists():
            return ["no database: the refusal left an empty database file behind"]
        return []
    finally:
        vaulted._download = real
        shutil.rmtree(home)


def _check_rebuild_keeps_vault_status() -> list[str]:
    """A rebuild must not blank vault status: a failed refresh keeps yesterday's."""
    live = config_mod.load().manifest_dir
    needed = ("ExportRelicArcane_en.json", "ExportRecipes_en.json")
    if not all((live / name).is_file() for name in needed):
        print('    SKIPPED: the rebuild check copies the downloaded manifests, and there are '
              'none. Run "bin/wf update" once (it downloads Digital Extremes\' public export).')
        return []
    from wft.data import build

    home = pathlib.Path(tempfile.mkdtemp())
    cfg = config_mod.load()
    cfg.override("paths.data_dir", str(home))
    shutil.copytree(live, cfg.manifest_dir, dirs_exist_ok=True)
    build.build(cfg, [])
    conn = sqlite3.connect(cfg.db_path)
    uniques = [u for (u,) in conn.execute("SELECT unique_name FROM relics ORDER BY unique_name")]
    if any(v is not None for v in _vault(conn).values()):
        conn.close()
        shutil.rmtree(home)
        return ["rebuild: a fresh build carried a vault status out of nowhere"]
    marked = {u: index % 2 for index, u in enumerate(uniques)}
    conn.executemany("UPDATE relics SET vaulted=? WHERE unique_name=?",
                     [(v, u) for u, v in marked.items()])
    conn.commit()
    conn.close()
    build.build(cfg, [])
    conn = sqlite3.connect(cfg.db_path)
    after = _vault(conn)
    conn.close()
    shutil.rmtree(home)
    lost = [u for u, v in marked.items() if after.get(u) != v]
    if lost:
        return [f"rebuild: {len(lost)} relic(s) lost their vault status, e.g. {lost[0]}"]
    return []


def main() -> int:
    checks = [
        ("the fixture parses", _check_parse),
        ("unrecognised pages are refused", _check_refusals),
        ("vault status is joined and written", _check_apply),
        ("an unknown dropping relic is named", _check_unknown_relic_is_named),
        ("a join that cannot be right is refused", _check_join_guards),
        ("a refresh keeps the page and the status", _check_refresh_writes_page_and_status),
        ("a failed refresh keeps everything", _check_refresh_failure_keeps_everything),
        ("a missing URL is named", _check_unconfigured_url),
        ("a missing database is named", _check_missing_database),
        ("a rebuild keeps vault status", _check_rebuild_keeps_vault_status),
    ]
    problems = []
    for label, check in checks:
        try:
            problems.extend(check())
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}: blew up: {exc!r}")

    for problem in problems:
        print("   ", problem)
    print(f"drop tables        : {len(checks)} checks, "
          f"{'all clean' if not problems else f'{len(problems)} problem(s)'}")
    if problems:
        print(f"FAILED — {len(problems)} problem(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
