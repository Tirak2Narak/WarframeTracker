#!/usr/bin/env python3
"""The trading lists (wft/trading.py) on a database made here.

Every case is a shape an earlier standalone script was tested
on - an open set two parts short, a mastered set to give away, a gun built out
of other guns, an item that is a component of another - plus the ones that
script never saw: non-Prime rows, unstarted sets, prices of every age, and a
built item whose mastery is still open.

Run: python3 tests/test_trading.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wft import trading  # noqa: E402
from wft.data import db as dbq  # noqa: E402
from wft.market import client as market  # noqa: E402

problems: list[str] = []


def check(ok, text):
    if not ok:
        problems.append(text)


home = pathlib.Path(tempfile.mkdtemp())
conn = dbq.connect(home / "warframe.sqlite")


def item(unique, name, category, prime=1):
    conn.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                 "VALUES(?,?,?,?)", (unique, name, category, prime))


def part(unique, name, owner, ducats=None, needed=1):
    conn.execute("INSERT INTO parts(unique_name, name, item_unique, ducats, "
                 "count_needed) VALUES(?,?,?,?,?)", (unique, name, owner, ducats, needed))


def hold(unique, count):
    conn.execute("INSERT INTO owned_parts(part_unique, count, source) "
                 "VALUES(?,?,'test')", (unique, count))


def own(unique, count=0, mastered=0):
    conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                 "VALUES(?,?,?,'test')", (unique, count, mastered))


def four(key, name, ducats=(100, 45, 15, 65), cat="warframe", prime=1):
    """A four-piece Warframe set: Blueprint, Neuroptics, Chassis, Systems."""
    item(f"W:{key}", name, cat, prime)
    for suffix, label, value in zip(("bp", "n", "c", "s"),
                                    ("Blueprint", "Neuroptics", "Chassis", "Systems"), ducats):
        part(f"P:{key}-{suffix}", f"{name} {label}", f"W:{key}", value)


# Ash Prime: open, holds Neuroptics and two Chassis -> missing Blueprint, Systems.
four("ash", "Ash Prime")
hold("P:ash-n", 1)
hold("P:ash-c", 2)
# Frost Prime: open, holds one piece -> three missing.
four("frost", "Frost Prime")
hold("P:frost-n", 1)
# Volt Prime: mastered, whole set and three Systems -> everything is spare.
four("volt", "Volt Prime")
own("W:volt", 0, 1)
for s in ("bp", "n", "c"):
    hold(f"P:volt-{s}", 1)
hold("P:volt-s", 3)
# Mesa Prime: mastered, one piece short (only listed when mastered sets are).
four("mesa", "Mesa Prime")
own("W:mesa", 0, 1)
for s in ("bp", "n", "c"):
    hold(f"P:mesa-{s}", 1)
# Neu Prime: nothing held at all.
four("neu", "Neu Prime")
# Ivara Prime: BUILT, mastery still open, every piece still on the shelf.
four("ivara", "Ivara Prime")
own("W:ivara", 1, 0)
for s in ("bp", "n", "c", "s"):
    hold(f"P:ivara-{s}", 1)
# Not Prime: a Sibear one piece short and a Cedo with spare barrels.
item("W:sibear", "Sibear", "primary", 0)
part("P:sibear-bp", "Sibear Blueprint", "W:sibear", None)
part("P:sibear-b", "Sibear Barrel", "W:sibear", None)
hold("P:sibear-b", 1)
item("W:cedo", "Cedo", "primary", 0)
part("P:cedo-b", "Cedo Barrel", "W:cedo", None)
own("W:cedo", 0, 1)
hold("P:cedo-b", 2)
# Prime, but a category nobody trades (Kavasa Prime is "other").
item("W:kavasa", "Kavasa Prime", "other")
part("P:kavasa-bp", "Kavasa Prime Blueprint", "W:kavasa", 45)

# Bronco Prime: mastered and built once, but Akbronco Prime (open) takes two.
item("W:bronco", "Bronco Prime", "secondary")
part("P:bronco-bp", "Bronco Prime Blueprint", "W:bronco", 15)
part("P:bronco-b", "Bronco Prime Barrel", "W:bronco", 25)
own("W:bronco", 1, 1)
hold("P:bronco-bp", 1)
hold("P:bronco-b", 2)
item("W:akbronco", "Akbronco Prime", "secondary")
part("P:akbronco-bp", "Akbronco Prime Blueprint", "W:akbronco", 45)
part("P:akbronco-l", "Akbronco Prime Link", "W:akbronco", 100)
part("W:bronco", "Bronco Prime", "W:akbronco", None, needed=2)
# Bravo Prime: built once, mastery open, and Akbravo Prime (not built) takes two:
# a component chain, so its pieces stay held although it is built.
item("W:bravo", "Bravo Prime", "secondary")
part("P:bravo-bp", "Bravo Prime Blueprint", "W:bravo", 15)
part("P:bravo-b", "Bravo Prime Barrel", "W:bravo", 25)
own("W:bravo", 1, 0)
hold("P:bravo-bp", 1)
hold("P:bravo-b", 1)
item("W:akbravo", "Akbravo Prime", "secondary")
part("P:akbravo-bp", "Akbravo Prime Blueprint", "W:akbravo", 45)
part("P:akbravo-l", "Akbravo Prime Link", "W:akbravo", 100)
part("W:bravo", "Bravo Prime", "W:akbravo", None, needed=2)
# Akmagnus Prime is two Magnus Primes, none built; Magnus Prime is open itself.
item("W:magnus", "Magnus Prime", "secondary")
part("P:magnus-bp", "Magnus Prime Blueprint", "W:magnus", 100)
part("P:magnus-b", "Magnus Prime Barrel", "W:magnus", 100)
part("P:magnus-r", "Magnus Prime Receiver", "W:magnus", 45)
hold("P:magnus-bp", 2)
hold("P:magnus-r", 2)
item("W:akmagnus", "Akmagnus Prime", "secondary")
part("P:akmagnus-bp", "Akmagnus Prime Blueprint", "W:akmagnus", 45)
part("P:akmagnus-l", "Akmagnus Prime Link", "W:akmagnus", 100)
part("W:magnus", "Magnus Prime", "W:akmagnus", None, needed=2)
hold("P:akmagnus-bp", 1)
hold("P:akmagnus-l", 1)


# Penta Prime: five pieces, nothing held - only an unlimited want list shows it.
item("W:penta", "Penta Prime", "primary")
for i in range(5):
    part(f"P:penta-{i}", f"Penta Prime Piece{i}", "W:penta", 15)
# Hexa Prime: six pieces, one held (five missing).
item("W:hexa", "Hexa Prime", "melee")
for i in range(6):
    part(f"P:hexa-{i}", f"Hexa Prime Piece{i}", "W:hexa", 15)
hold("P:hexa-0", 1)
# Untradable Prime (shaped like Sagek Prime): parts without ducats, in no relic,
# and no built item among them. Nothing held, and one spare piece in the offer.
item("W:untrade", "Untrade Prime", "melee")
for i in range(3):
    part(f"P:untrade-{i}", f"Untrade Prime Piece{i}", "W:untrade", None)
hold("P:untrade-0", 2)
# Akzeta Prime: a ducat-less piece that IS a built item (Zeta Prime) stays asked for.
item("W:zeta", "Zeta Prime", "secondary")
item("W:akzeta", "Akzeta Prime", "secondary")
part("P:akzeta-bp", "Akzeta Prime Blueprint", "W:akzeta", 45)
part("P:akzeta-l", "Akzeta Prime Link", "W:akzeta", 100)
part("W:zeta", "Zeta Prime", "W:akzeta", None, needed=2)
hold("P:akzeta-bp", 1)

# Prices (market slugs), of every age.
def listing(unique, slug, sell=None, age_hours=None):
    conn.execute("INSERT INTO market_items(unique_name, slug, item_id) VALUES(?,?,?)",
                 (unique, slug, slug))
    if age_hours is not None:
        conn.execute("INSERT INTO market_prices(slug, sell_min, fetched_at) "
                     "VALUES(?,?,datetime('now', ?))", (slug, sell, f"-{age_hours} hours"))


listing("P:ash-c", "ash_chassis", 5, 1)           # fresh
listing("P:ivara-bp", "ivara_bp", 8, 72)          # stale
listing("P:volt-bp", "volt_bp", 30, 1)            # fresh, dear
listing("P:volt-n", "volt_n")                     # known to the market, never asked
listing("P:volt-c", "volt_c", None, 1)            # asked, nobody sells it
listing("P:volt-s", "volt_s", 12, 1)              # fresh, over the limit of 10
# Ivara Chassis is not on the market at all (no listing).
conn.commit()


def lines(rows, kind="want", **kw):
    kw.setdefault("tz", dt.timezone.utc)
    return trading.to_markdown(rows, kind, as_of=dt.date(2026, 10, 21), **kw)


def item_lines(text):
    return [z for z in text.splitlines() if z.startswith("- ")]


# -- the want list -------------------------------------------------------------
want = trading.wanted_rows(conn)
by_item = {}
for r in want:
    by_item.setdefault(r.item, []).append(r)
names = set(by_item)
check("Ash Prime" in names, "want: Ash Prime (2 short) is missing")
check([(r.part, r.count) for r in by_item.get("Ash Prime", [])]
      == [("Blueprint", 1), ("Systems", 1)], f"want: Ash Prime parts {by_item.get('Ash Prime')}")
check("Frost Prime" not in names, "want: Frost Prime is 3 short and showed at max 2")
check("Mesa Prime" not in names, "want: a mastered set showed with only_unmastered")
check("Ivara Prime" not in names, "want: a built item showed")
check("Sibear" not in names, "want: a non-Prime set showed")
check("Kavasa Prime" not in names, "want: a category nobody trades showed")
check("Neu Prime" not in names, "want: an unstarted set showed at max 2")
check("Bronco Prime" not in names, "want: a built mastered item showed")
check(all(r.item_unique.startswith("W:") for r in want), "want: odd row")

# Component rule: Akmagnus once, with the need, no second Magnus line.
check("Magnus Prime" not in names, "want: Magnus Prime has its own line besides Akmagnus Prime")
check([(r.part, r.count) for r in by_item.get("Akmagnus Prime", [])]
      == [("Magnus Prime Barrel", 2)], f"want: Akmagnus parts {by_item.get('Akmagnus Prime')}")
check(by_item["Akmagnus Prime"][0].needs == ((2, "Magnus Prime"),),
      f"want: Akmagnus needs {by_item['Akmagnus Prime'][0].needs}")
check([(r.part, r.count) for r in by_item.get("Akbronco Prime", [])]
      == [("Blueprint", 1), ("Link", 1)], f"want: Akbronco parts {by_item.get('Akbronco Prime')}")
check(by_item["Akbronco Prime"][0].needs == ((2, "Bronco Prime"),), "want: Akbronco needs")
# Left alone, the component does get its line (the parent is 2 short, limit 1).
alone = {r.item for r in trading.wanted_rows(conn, max_missing=1)}
check("Magnus Prime" in alone and "Akmagnus Prime" not in alone,
      f"want: at max 1 the component should stand alone, got {sorted(alone)}")
# Hiding the parent's category does not bring the component back.
sec_only = {r.item for r in trading.wanted_rows(conn, categories=["warframe"])}
check("Magnus Prime" not in sec_only and "Akmagnus Prime" not in sec_only,
      "want: category filter let the component back in")

# max_missing 1/2/3/4, unstarted sets only from 4 missing, Frost at 3.
def missing_set(**kw):
    return {r.item for r in trading.wanted_rows(conn, **kw)}


check("Frost Prime" in missing_set(max_missing=3), "want: Frost missing at 3")
check("Neu Prime" not in missing_set(max_missing=3), "want: unstarted set at 3")
check("Neu Prime" in missing_set(max_missing=4), "want: unstarted set missing at 4")
one = missing_set(max_missing=1)
check("Ash Prime" not in one and "Cedo" not in one, f"want: max 1 {sorted(one)}")
# only_unmastered off brings the mastered, one-short Mesa Prime; built stay out.
check("Mesa Prime" in missing_set(only_unmastered=False), "want: only_unmastered off lost Mesa")
check("Ivara Prime" not in missing_set(only_unmastered=False, max_missing=4), "want: built showed")
# Categories.
check(missing_set(categories=["secondary"]) == {"Akbravo Prime", "Akbronco Prime", "Akmagnus Prime"},
      f"want: secondary only {sorted(missing_set(categories=['secondary']))}")
# No vault data in the fixture: nothing is known vaulted.
check(not trading.wanted_rows(conn, vaulted_only=True), "want: vaulted_only found vaulted parts")

# -- no limit, hidden sets, parts nobody can trade ------------------------
everything, no_hidden = trading.want_list(conn, max_missing=0)
every = {r.item for r in everything}
check({"Penta Prime", "Hexa Prime", "Neu Prime", "Frost Prime", "Ash Prime"} <= every,
      f"want, no limit: {sorted(every)}")
check(no_hidden == 0, f"want, no limit: hidden {no_hidden}")
check("Ivara Prime" not in every and "Mesa Prime" not in every and "Sibear" not in every,
      "want, no limit: a built, mastered or non-Prime set showed")
check(len([r for r in everything if r.item == "Penta Prime"]) == 5
      and all(r.count == 1 for r in everything if r.item == "Penta Prime"),
      "want, no limit: an unstarted set should list all its pieces")
limited_rows, hidden = trading.want_list(conn, max_missing=4)
limited = {r.item for r in limited_rows}
check("Penta Prime" not in limited and "Hexa Prime" not in limited
      and limited < every, f"want, limit 4: {sorted(limited)}")
check(hidden == len(every - limited) and hidden >= 2,
      f"want, limit 4: hidden {hidden}, expected {len(every - limited)}")
check(trading.want_list(conn, max_missing=4)[0] == trading.wanted_rows(conn, max_missing=4),
      "want_list and wanted_rows differ")
check(trading.want_list(conn, max_missing=2)[1] == len(every - missing_set(max_missing=2)),
      "want, limit 2: hidden count")
check(trading.title("want", "ducats", 0) == "Prime sets I still need parts for",
      "title: no limit")

untradable = ("Untrade Prime",)
check(not any(r.item in untradable for r in everything),
      "want: an untradable set was asked for")
check(not any(r.item in untradable for r in trading.offer_rows(conn)),
      "offer: an untradable part was offered")
check(not any(r.item in untradable for r in trading.offer_rows(conn, include_unpriced=True)),
      "offer: an untradable part was offered (unpriced)")
check(dbq.is_tradable_part(conn, "P:ash-c") and dbq.is_tradable_part(conn, "W:zeta")
      and not dbq.is_tradable_part(conn, "P:untrade-0")
      and not dbq.is_tradable_part(conn, "P:cedo-b"),
      "is_tradable_part: wrong answer")
zeta = [r for r in everything if r.item == "Akzeta Prime"]
check(len(zeta) == 2 and {r.part for r in zeta} == {"Zeta Prime", "Link"}
      and any(r.part == "Zeta Prime" and r.ducats is None for r in zeta),
      f"want: the built-item piece of an Ak set is gone: {zeta}")
# One piece of a set with ducats and one without: only the ducat piece is asked for.
conn.execute("UPDATE parts SET ducats = 15 WHERE unique_name = 'P:untrade-1'")
mixed = [r.part for r in trading.wanted_rows(conn, max_missing=0) if r.item == "Untrade Prime"]
check(mixed == ["Piece1"], f"want: mixed set {mixed}")
conn.execute("UPDATE parts SET ducats = NULL WHERE unique_name = 'P:untrade-1'")

everything_text = (
    lines(everything, max_missing=0)
    + lines(trading.offer_rows(conn), "offer")
    + trading.to_csv(everything, "want") + trading.to_csv(trading.offer_rows(conn), "offer")
    + trading.to_html(everything, "want", as_of=dt.date(2026, 10, 21),
                      colours={"bg": "#000", "card": "#111", "border": "#222",
                               "text": "#fff", "dim": "#999", "accent": "#d90"},
                      now=dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc), max_missing=0))
check("Untrade" not in everything_text and "Penta Prime" in everything_text,
      "message, CSV or HTML mention an untradable part")

# -- the offer list ------------------------------------------------------------
offer = trading.offer_rows(conn)
spare_names = {(r.item, r.part, r.count) for r in offer}
check(("Ash Prime", "Chassis", 1) in spare_names, "offer: Ash's second Chassis")
check(("Volt Prime", "Systems", 3) in spare_names, "offer: Volt's 3 Systems")
check(all(r.item != "Cedo" for r in offer), "offer: a non-Prime part (Cedo Barrel) was offered")
check(not any(r.item.startswith(("Frost", "Magnus", "Akmagnus")) for r in offer),
      "offer: a piece an open set needs was offered")
check(("Bronco Prime", "Barrel", 1) in spare_names and not any(
    r.item == "Bronco Prime" and r.part == "Blueprint" for r in offer),
    "offer: the Bronco chain is not held back as Akbronco needs two")
check(not any(r.item == "Bravo Prime" for r in offer),
      "offer: a built component of an open set gave up its pieces")
check({(r.part) for r in offer if r.item == "Ivara Prime"}
      == {"Blueprint", "Neuroptics", "Chassis", "Systems"},
      "offer: a built, not mastered item still holds its own set back")
check(len(offer) == 13, f"offer: {len(offer)} rows, wanted 13")
check(all(r.ducats for r in offer), "offer: a row lost its ducats")
check(trading.offer_rows(conn, categories=["melee"]) == [], "offer: category filter")

# At the source: spare_parts says why.
spares = {r["unique_name"]: r for r in dbq.spare_parts(conn)}
check(spares["P:ivara-bp"]["freed_by_build"] and spares["P:ivara-bp"]["freed_by_mastery"],
      "Ivara's blueprint is not marked as freed by the build")
check(not spares["P:volt-bp"]["freed_by_build"] and spares["P:volt-bp"]["freed_by_mastery"],
      "Volt's blueprint, mastered, is marked as freed by the build")
check("P:bravo-b" not in spares and "P:bravo-bp" not in spares, "Bravo's pieces are spare")
# Once Akbravo Prime is built too, nothing is made from Bravo any more.
conn.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
             "VALUES('W:akbravo', 1, 0, 'test')")
check("P:bravo-b" in {r["unique_name"] for r in dbq.spare_parts(conn)},
      "a built chain end still holds its component's pieces")
conn.execute("DELETE FROM owned_items WHERE item_unique = 'W:akbravo'")

# Price filter: the last stored ask of any age; unpriced follows the default.
def offer_names(**kw):
    return {(r.item, r.part) for r in trading.offer_rows(conn, **kw)}


limited = offer_names(max_platinum=10)
check(limited == {("Ash Prime", "Chassis"), ("Ivara Prime", "Blueprint")},
      f"price: limit 10 without unpriced gave {sorted(limited)}")
with_unpriced = offer_names(max_platinum=10, include_unpriced=True)
check(("Volt Prime", "Blueprint") not in with_unpriced
      and ("Volt Prime", "Systems") not in with_unpriced
      and ("Volt Prime", "Neuroptics") in with_unpriced
      and ("Volt Prime", "Chassis") in with_unpriced
      and ("Ivara Prime", "Chassis") in with_unpriced
      and limited < with_unpriced, f"price: include_unpriced gave {sorted(with_unpriced)}")
check(len(offer_names()) == 13, "price: no limit should keep every row")
check(("Volt Prime", "Neuroptics") not in offer_names(include_unpriced=False),
      "price: include_unpriced=False without a limit kept an unpriced part")
old = next(r for r in offer if (r.item, r.part) == ("Ivara Prime", "Blueprint"))
check(old.sell_min == 8 and old.price_at is not None, "price: an old price lost its value or age")
none_row = next(r for r in offer if (r.item, r.part) == ("Volt Prime", "Chassis"))
check(none_row.sell_min is None and none_row.price_at is None, "price: 'nobody sells' read as a price")

# parts_to_price counts the list before the filter and only missing/stale.
slugs = trading.parts_to_price(conn, offer)
check(sorted(slugs) == ["ivara_bp", "volt_n"], f"parts_to_price: {sorted(slugs)}")
filtered = trading.offer_rows(conn, max_platinum=10)
check(sorted(trading.parts_to_price(conn, filtered)) == ["ivara_bp"],
      "parts_to_price: the filtered list should miss the unpriced part")
check(market.unpriced(conn, [r.part_unique for r in offer])
      == [u for u in (r.part_unique for r in offer) if u in ("P:ivara-bp", "P:volt-n")],
      "market.unpriced: not the same answer as parts_to_price")

# -- data age ------------------------------------------------------------------
check(trading.data_age(conn) == trading.DataAge(None, None), f"age: {trading.data_age(conn)}")
conn.execute("INSERT INTO imports(written_at, source) VALUES('2026-10-18 10:03:56', 'a')")
conn.execute("INSERT INTO imports(written_at, source, undone_at) "
             "VALUES('2026-10-20 08:00:00', 'b', '2026-10-20 09:00:00')")
conn.commit()
check(trading.data_age(conn).stock_changed == "2026-10-18 10:03:56", "age: an undone import counted")
dbq.note_mastery_import(conn)
stamp = trading.data_age(conn).mastery_imported
check(stamp is not None and len(stamp) == 19, f"age: mastery import time {stamp}")

# -- the message ---------------------------------------------------------------
sec = trading.wanted_rows(conn, categories=["secondary"])
body = (
    "\n"
    "**Secondary**\n"
    "- Akbravo Prime (needs 2x Bravo Prime): Blueprint (45), Link (100)\n"
    "- Akbronco Prime (needs 2x Bronco Prime): Blueprint (45), Link (100)\n"
    "- Akmagnus Prime (needs 2x Magnus Prime): 2x Magnus Prime Barrel (100)\n")
want_terms = ("In exchange I give one of my spare parts (see my other list) or "
              "parts of the same ducat value.")
expected = ("**Prime sets I still need 1-2 parts for** (as of 2026-10-21)\n"
            + want_terms + " Ducats per part in brackets.\n" + body)
check(lines(sec) == expected, f"markdown want:\n{lines(sec)}")
check(lines(sec, trade_for="ducats") == expected, "markdown: ducats is the default")
check(lines(sec, max_missing=3).splitlines()[0]
      == "**Prime sets I still need 1-3 parts for** (as of 2026-10-21)", "title: 3 parts")
check(lines(sec, max_missing=1).splitlines()[0]
      == "**Prime sets I still need 1 part for** (as of 2026-10-21)", "title: 1 part")
check(lines(sec, trade_for="nonsense") == expected, "markdown: an unknown setting is the default")
# no stored price on these rows: platinum has nothing to say, both falls back to ducats
no_price = lines(sec, trade_for="platinum")
check(no_price.splitlines()[1] == "I pay platinum."
      and "(" not in no_price.split("**Secondary**")[1].replace("(needs", ""),
      f"markdown want platinum without prices:\n{no_price}")
check(lines(sec, trade_for="both").splitlines()[1]
      == "In exchange I give one of my spare parts (see my other list), parts of the "
         "same ducat value, or platinum. Ducats per part in brackets.",
      f"markdown want both without prices:\n{lines(sec, trade_for='both')}")


def hand(item_name, part_name, count, ducats, sell, category="warframe", at=None):
    return trading.Row(category, item_name, "U:" + item_name, "P:" + part_name, part_name,
                       count, ducats, sell, at, None, 0, 4, False)


day1, day2 = "2026-10-18T10:00:00+00:00", "2026-10-21 08:30:00"
rows = [hand("Volt Prime", "Chassis", 1, 15, 8, at=day2), hand("Volt Prime", "Systems", 3, 65, None),
        hand("Atlas Prime", "Blueprint", 1, None, 4, at=day2),
        hand("Braton Prime", "Stock", 2, 25, 3, "primary", at=day2)]
rows = trading._order(rows)
tail = ("**Warframes**\n{atlas}\n{volt}\n\n**Primary**\n{braton}\n")
offer_terms = ("In exchange I want either one of my missing parts or parts of the "
               "same ducat value.")
both_terms = ("In exchange I want one of my missing parts, parts of the same ducat "
              "value, or platinum.")
check(lines(rows, "offer", trade_for="both")
      == "**Prime parts I can give away or sell** (as of 2026-10-21)\n" + both_terms
         + " Ducats and platinum per part in brackets; platinum is the cheapest seller "
           "on warframe.market, checked 2026-10-21.\n\n"
         "**Warframes**\n- Atlas Prime: Blueprint (4p)\n"
         "- Volt Prime: Chassis (15, 8p), 3x Systems (65)\n\n"
         "**Primary**\n- Braton Prime: 2x Stock (25, 3p)\n",
      f"markdown offer both:\n{lines(rows, 'offer', trade_for='both')}")
check(lines(rows, "offer", trade_for="platinum")
      == "**Prime parts I sell** (as of 2026-10-21)\nFor platinum. Platinum per part in "
         "brackets: the cheapest seller on warframe.market, checked 2026-10-21.\n\n"
         "**Warframes**\n- Atlas Prime: Blueprint (4p)\n"
         "- Volt Prime: Chassis (8p), 3x Systems\n\n"
         "**Primary**\n- Braton Prime: 2x Stock (3p)\n",
      f"markdown offer platinum (a part without a price has no bracket):\n"
      f"{lines(rows, 'offer', trade_for='platinum')}")
check(lines(rows, "offer")
      == "**Prime parts I can give away** (as of 2026-10-21)\n" + offer_terms
         + " Ducats per part in brackets.\n\n"
         "**Warframes**\n- Atlas Prime: Blueprint\n"
         "- Volt Prime: Chassis (15), 3x Systems (65)\n\n"
         "**Primary**\n- Braton Prime: 2x Stock (25)\n",
      f"markdown offer ducats:\n{lines(rows, 'offer')}")
check(trading.terms("want", "platinum") == "I pay platinum."
      and trading.terms("offer", "platinum") == "For platinum."
      and trading.title("offer", "both") == "Prime parts I can give away or sell"
      and trading.title("offer", "platinum") == "Prime parts I sell"
      and trading.title("want", "platinum", 4) == "Prime sets I still need 1-4 parts for",
      "terms or title of the other modes")
check(trading.missing_prices(rows) == 1, "missing_prices counts rows without a price")

# The price date: one day, a span, none; local time, not UTC.
span = [hand("A Prime", "X", 1, 15, 8, at=day1), hand("B Prime", "Y", 1, 15, 8, at=day2),
        hand("C Prime", "Z", 1, 15, None)]
check(trading.price_dates(rows, dt.timezone.utc) == "2026-10-21", "price_dates: one day")
check(trading.price_dates(span, dt.timezone.utc) == "2026-10-18 to 2026-10-21",
      "price_dates: a span")
check(trading.price_dates([hand("C Prime", "Z", 1, 15, None)], dt.timezone.utc) == "",
      "price_dates: none")
late = [hand("A Prime", "X", 1, 15, 8, at="2026-10-21T23:30:00+00:00")]
check(trading.price_dates(late, dt.timezone.utc) == "2026-10-21"
      and trading.price_dates(late, dt.timezone(dt.timedelta(hours=2))) == "2026-10-22",
      "price_dates: the UTC time is not turned into the local day")
check("checked 2026-10-18 to 2026-10-21" in lines(span, "offer", trade_for="platinum"),
      "markdown: the span is named")
check(lines(span[2:], "offer", trade_for="platinum").splitlines()[1] == "For platinum.",
      "markdown platinum without any price has no bracket sentence")
check(lines(span[2:], "offer", trade_for="both").splitlines()[1] == both_terms
      + " Ducats per part in brackets.", "markdown both without any price: ducats only")
check(lines([], "offer") == "**Prime parts I can give away** (as of 2026-10-21)\n"
      + offer_terms + " Ducats per part in brackets.\n", "markdown: an empty list")
try:
    lines(rows, "both")
    check(False, "markdown: unknown kind accepted")
except ValueError:
    pass

# -- CSV and HTML -------------------------------------------------------------
import csv  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402

# The accents and markup characters in these names are test data for encoding and escaping.
odd = trading.Row("warframe", 'Odd, "Prime" & <b>é', "U:odd", "P:odd", 'Bläde <x> "y"',
                  2, 45, 12, "2026-10-18T10:00:00+00:00", True, 3, 4, False,
                  ((2, "Magnus Prime"),))
plain = trading.Row("primary", "Braton Prime", "U:b", "P:b", "Stock", 1, None, None,
                    None, None, 0, 4, True)
sample = trading._order([odd, plain])
for kind in ("want", "offer"):
    text = trading.to_csv(sample, kind)
    check("\r" not in text, f"csv {kind}: line ends are not plain newlines")
    back = list(csv.DictReader(io.StringIO(text, newline="")))
    check(len(back) == len(sample), f"csv {kind}: {len(back)} lines for {len(sample)} rows")
    check(list(back[0]) == trading.CSV_COLUMNS + (["needs"] if kind == "want" else []),
          f"csv {kind}: header {list(back[0])}")
    for r, line in zip(sample, back):
        check(line["item"] == r.item and line["part"] == r.part
              and line["list"] == kind and line["category"] == trading.CATEGORIES[r.category]
              and line["count"] == str(r.count)
              and line["ducats"] == ("" if r.ducats is None else str(r.ducats))
              and line["platinum"] == ("" if r.sell_min is None else str(r.sell_min))
              and line["platinum_checked"] == ("2026-10-18" if r.price_at else "")
              and line["vaulted"] == {True: "yes", None: ""}[r.vaulted]
              and line["set_held"] == str(r.held) and line["set_total"] == str(r.total)
              and line["mastered"] == ("yes" if r.mastered else "no"),
              f"csv {kind}: {line} differs from {r}")
    check(("needs" in back[0]) == (kind == "want"), f"csv {kind}: needs column")
    if kind == "want":
        check(back[0]["needs"] == "needs 2x Magnus Prime" and back[1]["needs"] == "",
              f"csv want: needs {[b['needs'] for b in back]}")
check(trading.to_csv([], "offer").count("\n") == 1, "csv: an empty list is the header only")
try:
    trading.to_csv(sample, "both")
    check(False, "csv: unknown kind accepted")
except ValueError:
    pass

COLOURS = {"bg": "#0c0f14", "card": "#1a2029", "border": "#262e3a", "text": "#e6e8ec",
           "dim": "#939aa8", "accent": "#d9a441"}


def page(kind="offer", rows=sample, **kw):
    args = dict(as_of=dt.date(2026, 10, 21), colours=COLOURS, tz=dt.timezone.utc,
                now=dt.datetime(2026, 10, 21, tzinfo=dt.timezone.utc))
    args.update(kw)
    return trading.to_html(rows, kind, **args)


doc = page(trade_for="both")
check(doc.startswith("<!doctype html>"), "html: no doctype")
check("Odd, &quot;Prime&quot; &amp; &lt;b&gt;é" in doc
      and "Bläde &lt;x&gt; &quot;y&quot;" in doc and "Braton Prime" in doc,
      "html: item and part names are not all there, escaped")
check("<b>" not in doc and "<x>" not in doc,
      "html: a text from outside is not escaped")
check("<h1>Prime parts I can give away or sell</h1>" in doc
      and "In exchange I want one of my missing parts, parts of the same ducat value, "
          "or platinum." in doc
      and "Platinum: the cheapest seller on warframe.market, checked 2026-10-18." in doc
      and "as of 2026-10-21" in doc, "html: title, terms, price line or date missing")
check(not re.search(r"(?i)(https?:)?//|<script|<link|<img|url\(|@import", doc),
      "html: an outside resource or script")
check("@media print" in doc and "background: #fff; color: #000" in doc, "html: print CSS")
check(doc.index("Warframes") < doc.index("Primary") and doc.count("<table>") == 2,
      "html: one table per category, in the message's order")
check("12p" in doc and "2 d ago" in doc and "&mdash;" in doc, "html: platinum and its age")
check("brackets" not in doc, "html: the bracket sentence belongs to the message only")
check("<th class=\"num\">Ducats</th>" in doc and "<th class=\"num\">Platinum</th>" in doc,
      "html: ducats and platinum headings")
dual = page()
body_of = lambda t: t.split("</style>")[1]
check("Platinum" not in body_of(dual) and "checked" not in dual and "<th class=\"num\">Ducats</th>" in dual
      and "45" in body_of(dual), "html: ducats mode has the ducat column only")
plat = page(trade_for="platinum")
check("Ducats" not in body_of(plat) and "45" not in body_of(plat)
      and "<th class=\"num\">Platinum</th>" in plat and "For platinum." in plat
      and "<h1>Prime parts I sell</h1>" in plat, "html: platinum mode has the platinum column only")
check("checked" not in page(trade_for="platinum", rows=[plain]),
      "html: no price line without a price")
check("<h1>Prime sets I still need 1-3 parts for</h1>" in page("want", max_missing=3),
      "html: the want title names the limit")
check("needs 2x Magnus Prime" in page("want") and "needs 2x" not in page("offer"),
      "html: the needs note is the want list's only")
check("No part matches" in page(rows=[]), "html: an empty list says so")
try:
    page(colours={"bg": "#000"})
    check(False, "html: missing colours accepted")
except ValueError:
    pass

# -- The chain arithmetic and the "keep one" mark -----------------------
# Each case on a database of its own, so nothing above is disturbed.
def chain_db(name):
    c = dbq.connect(home / f"{name}.sqlite")
    return c


def c_item(c, unique, name, category="secondary"):
    c.execute("INSERT INTO items(unique_name, name, category, is_prime) "
              "VALUES(?,?,?,1)", (unique, name, category))


def c_part(c, unique, name, owner, needed=1):
    c.execute("INSERT INTO parts(unique_name, name, item_unique, ducats, "
              "count_needed) VALUES(?,?,?,10,?)", (unique, name, owner, needed))


def c_own(c, unique, count=0, mastered=0):
    c.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
              "VALUES(?,?,?,'test')", (unique, count, mastered))


def c_hold(c, unique, count):
    c.execute("INSERT INTO owned_parts(part_unique, count, source) "
              "VALUES(?,?,'test')", (unique, count))


def gun_pair(c, key, inner_built, inner_mastered, outer_mastered=0, outer_built=0,
             held=(0, 0)):
    """An inner gun with a blueprint and a barrel, and an outer one of two."""
    c_item(c, f"W:{key}", f"{key.title()} Prime")
    c_part(c, f"P:{key}-bp", f"{key.title()} Prime Blueprint", f"W:{key}")
    c_part(c, f"P:{key}-b", f"{key.title()} Prime Barrel", f"W:{key}")
    c_item(c, f"W:ak{key}", f"Ak{key} Prime")
    c_part(c, f"P:ak{key}-bp", f"Ak{key} Prime Blueprint", f"W:ak{key}")
    c_part(c, f"W:{key}", f"{key.title()} Prime", f"W:ak{key}", needed=2)
    c_own(c, f"W:{key}", inner_built, inner_mastered)
    if outer_mastered or outer_built:
        c_own(c, f"W:ak{key}", outer_built, outer_mastered)
    c_hold(c, f"P:{key}-bp", held[0])
    c_hold(c, f"P:{key}-b", held[1])
    c.commit()


def spare_of(c):
    return {r["unique_name"]: r["spare"] for r in dbq.spare_parts(c)}


# Lex: 3 built and mastered, Aklex open (needs 2): spare without the mark,
# and with it (3 needed, 3 held) too.
lex = chain_db("lex")
gun_pair(lex, "lex", 3, 1, held=(1, 1))
check(dbq.retained_counts(lex)["P:lex-b"] == 0,
      f"Lex, 3 built, retained {dbq.retained_counts(lex)['P:lex-b']}, wanted 0")
check(spare_of(lex).get("P:lex-b") == 1 and spare_of(lex).get("P:lex-bp") == 1,
      "Lex pieces are spare when the built copies cover Aklex")
dbq.set_kept(lex, "W:lex", True)
check(dbq.is_kept(lex, "W:lex") and dbq.crafting_stock(lex, "W:lex") == 2
      and dbq.owned_count(lex, "W:lex") == 3,
      "crafting stock is the raw count less the kept copy, raw unchanged")
check(dbq._missing_copies(lex, dbq._consumers(lex), "W:lex", {}) == 0
      and spare_of(lex).get("P:lex-b") == 1,
      "kept, 3 built cover the 3 needed: still spare")
# Lex 2 built with the mark: one set is retained.
lex.execute("UPDATE owned_items SET count = 2 WHERE item_unique = 'W:lex'")
check(dbq.retained_counts(lex)["P:lex-b"] == 1 and "P:lex-b" not in spare_of(lex),
      "kept with 2 built, one set of pieces is retained")
dbq.set_kept(lex, "W:lex", False)
check(dbq.retained_counts(lex)["P:lex-b"] == 0,
      "clearing the mark releases the set again")

# Magnus: 0 built, Akmagnus open: pieces_short_for asks 3 with the mark, 2 without.
mag = chain_db("mag")
gun_pair(mag, "magnus", 0, 0)
edges = dbq._consumers(mag)
check(dbq._missing_copies(mag, edges, "W:magnus", {}) == 2, "Magnus multiplier without mark")
short = {r["name"]: n for r, n in dbq.pieces_short_for(mag, "W:akmagnus")}
check(short.get("Magnus Prime") == 2, f"Akmagnus asks {short} without the mark")
dbq.set_kept(mag, "W:magnus", True)
check(dbq._missing_copies(mag, edges, "W:magnus", {}) == 3, "Magnus multiplier with mark")
short = {r["name"]: n for r, n in dbq.pieces_short_for(mag, "W:akmagnus")}
check(short.get("Magnus Prime") == 3, f"Akmagnus asks {short} with the mark")
check(dbq.set_progress(mag, "W:akmagnus") == (0, 3), "set progress with a mark and no copy")
mag.execute("UPDATE owned_items SET count = 1 WHERE item_unique = 'W:magnus'")
check(dbq.set_progress(mag, "W:akmagnus") == (0, 3),
      "the only copy is the kept one: no progress for Akmagnus")

# Aklex mastered, Lex unmastered and unbuilt: one set, not two.
mst = chain_db("mst")
gun_pair(mst, "vasto", 0, 0, outer_mastered=1)
check(dbq.retained_counts(mst)["P:vasto-b"] == 1,
      f"mastered Aklex demands nothing, got {dbq.retained_counts(mst)['P:vasto-b']}")

# One item eaten by two wanted parents: the demands add up.
two = chain_db("two")
gun_pair(two, "bronco", 0, 1)
c_item(two, "W:other", "Other Prime")
c_part(two, "P:other-bp", "Other Prime Blueprint", "W:other")
two.commit()
# A part row has one owner (primary key), so the second edge is a curated one.
saved_edges = dbq._curated_edges
dbq._curated_edges = {"Other Prime": {"requires": [{"item": "Bronco Prime",
                                                    "quantity": 1}]}}
check(dbq._missing_copies(two, dbq._consumers(two), "W:bronco", {}) == 3,
      "an item eaten by two wanted parents needs 2 + 1 copies")
dbq._curated_edges = saved_edges

# A mark on an item nothing is built from changes nothing.
root = chain_db("root")
c_item(root, "W:solo", "Solo Prime")
c_part(root, "P:solo-bp", "Solo Prime Blueprint", "W:solo")
c_hold(root, "P:solo-bp", 1)
root.commit()
before = dbq.retained_counts(root)
dbq.set_kept(root, "W:solo", True)
check(dbq.retained_counts(root) == before and dbq.set_progress(root, "W:solo") == (1, 1),
      "a mark on a root item has an effect")

# A database from before the table gets it on connect.
old = sqlite3.connect(home / "old.sqlite")
old.executescript(
    "CREATE TABLE items(unique_name TEXT PRIMARY KEY, name TEXT, category TEXT, is_prime INTEGER);"
    "CREATE TABLE imports(id INTEGER PRIMARY KEY);")
old.close()
fresh = dbq.connect(home / "old.sqlite")
check(fresh.execute("SELECT name FROM sqlite_master WHERE name = 'kept_items'").fetchone(),
      "a database without kept_items did not get it on connect")
fresh.close()

# The backup carries the marks.
from wft.data import backup  # noqa: E402

dbq.set_kept(mag, "W:magnus", True)
data = backup.export_holdings(mag)
check(data["kept"] == ["W:magnus"], f"backup kept {data.get('kept')}")
dbq.set_kept(mag, "W:magnus", False)
dbq.set_kept(mag, "W:other", True)
backup.restore(mag, data)
check(dbq.is_kept(mag, "W:magnus") and not dbq.is_kept(mag, "W:other"),
      "restore with replace did not replace the marks")
del data["kept"]
backup.restore(mag, data)
check(not dbq.is_kept(mag, "W:magnus"), "restoring an old backup keeps the marks")
for c in (lex, mag, mst, two, root):
    c.close()

conn.close()
sqlite3.connect(":memory:").close()
if problems:
    print("FAILED")
    for p in problems:
        print(" -", p)
    sys.exit(1)
print("trading: want, offer, prices, data age and message OK")
