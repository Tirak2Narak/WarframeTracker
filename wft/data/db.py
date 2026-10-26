"""Database access.

Every query about a part has to reckon with one fact: a single thing can carry
two unique names. Relics drop `GaussPrimeSystemsBlueprint`; the inventory holds
`GaussPrimeSystemsComponent`. They are one item to a player, and the loader
stores both so OCR can match either spelling, linked by `alias_of`.

Ask about the wrong one and you get zero rows and no error — which is exactly
what happened before this module existed: a vault lookup on the component found
none of the 32 relics that drop it and reported "vault status unknown".

So: never query `parts` by a bare `unique_name` outside this module. Use
`alias_group()` or the helpers built on it.
"""
from __future__ import annotations

import functools
import re
import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple


def connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    _ensure_schema(connection, db_path)
    return connection


#: Files already reported as unreadable, so the warning is one line and not
#: one line per view.
_unusable: set[str] = set()


def _ensure_schema(connection: sqlite3.Connection,
                   db_path: Path | None = None) -> None:
    """Add tables a newer version of the tool expects.

    Every statement in the schema is `IF NOT EXISTS`, so running it against an
    existing database adds what is missing and leaves the rest alone. Guarded
    on the newest table rather than run every time: this is on the path of
    every connection, and the file is read from disk.

    A database built before this version has an inventory and no journal. That
    is not an error - it is a database with no imports recorded yet.
    """
    schema = Path(__file__).resolve().parent / "schema.sql"
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        # Not a database at all - a truncated file, something else with the
        # right name. Said once per file rather than once per connection: this
        # runs on every connect, and five views opening the same broken file
        # printed the same line five times, which reads as five faults.
        if str(db_path) not in _unusable:
            _unusable.add(str(db_path))
            print(f"warning: {db_path} is not a usable database: {exc}",
                  file=sys.stderr)
        return
    # Every statement in the file is IF NOT EXISTS, so running it adds what is
    # missing and leaves the rest alone. Guarded on the whole set rather than
    # one table: guarding on the newest one meant the *next* table added would
    # silently never appear on an existing database.
    wanted = {"imports", "observations", "owned_relics", "learned_ignores",
              "thumbnails", "textures", "market_items", "market_prices",
              "riven_prices", "fetch_log", "kept_items"}
    if not wanted <= tables:
        try:
            connection.executescript(schema.read_text())
            connection.commit()
        except (OSError, sqlite3.Error) as exc:
            # A read-only copy, or a database this tool did not make. Said out
            # loud rather than swallowed: a caller that needs the journal will
            # otherwise find it missing with no idea why.
            print(f"warning: could not update the database schema: {exc}",
                  file=sys.stderr)
            return

    # Indexes added after databases were already in the field. The script
    # above only runs when a *table* is missing, so an index that arrives on
    # its own would silently never appear on an existing database; asked for
    # by name instead. On a database that has it this is one sqlite_master
    # lookup, which is what the whole guard above costs anyway.
    try:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_parts_alias ON parts(alias_of)")
        connection.commit()
    except sqlite3.Error:
        pass

    # Columns added to tables that already existed. `CREATE TABLE IF NOT
    # EXISTS` does nothing for those, so each one is its own step.
    for table, column, definition in (
            ("observations", "before_source", "TEXT"),):
        try:
            existing = {row[1] for row in connection.execute(
                f"PRAGMA table_info({table})")}
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                connection.commit()
        except sqlite3.Error:
            pass

    # Built weapons that older imports wrote into owned_parts. One
    # indexed query when there is nothing to move; it is the same for every
    # connection, including the tools that only read.
    try:
        if settle_built_components(connection):
            connection.commit()
    except sqlite3.Error:
        pass


def assert_usable(connection: sqlite3.Connection) -> None:
    """Raise now if this file is not actually a database.

    `connect` warns about a broken file but still hands the connection back -
    the status bar and every guarded page prefer a quiet failure to a dead
    application. A view constructor is the opposite case: it runs inside
    build_application's try, where raising is what turns the page into a
    placeholder instead of letting the first query blow up under a click.
    The views used to get this probing for free by filling themselves at
    construction; now that they fill on first select, they ask for it.
    """
    connection.execute("SELECT 1 FROM sqlite_master LIMIT 1")


def canonical(connection: sqlite3.Connection, part_unique: str) -> str:
    """The name a part is counted under: the component, not its blueprint."""
    row = connection.execute(
        "SELECT COALESCE(alias_of, unique_name) AS c FROM parts WHERE unique_name = ?",
        (part_unique,)).fetchone()
    return row["c"] if row else part_unique


def alias_group(connection: sqlite3.Connection, part_unique: str) -> list[str]:
    """Every unique_name meaning the same part, canonical one first.

    Two indexed lookups rather than `unique_name = ? OR alias_of = ?`: sqlite
    answers an OR across two columns with a full scan of parts, and this
    function is the inner loop of every per-part helper above and below it.
    UNION ALL keeps each half in index order, so the aliases come back in the
    same rowid order the scan produced; the duplicate a self-alias could
    introduce is already filtered by the `!= root` below, as it always was.
    """
    root = canonical(connection, part_unique)
    rows = connection.execute(
        "SELECT unique_name FROM parts WHERE unique_name = ? "
        "UNION ALL "
        "SELECT unique_name FROM parts WHERE alias_of = ?",
        (root, root)).fetchall()
    names = [r["unique_name"] for r in rows]
    return [root] + [n for n in names if n != root]


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def relics_dropping(connection: sqlite3.Connection, part_unique: str) -> list[sqlite3.Row]:
    """Relics that drop this part, under any of its names."""
    group = alias_group(connection, part_unique)
    return connection.execute(
        f"SELECT DISTINCT rl.name, rl.tier, rl.vaulted, rr.rarity "
        f"FROM relic_rewards rr JOIN relics rl ON rr.relic_unique = rl.unique_name "
        f"WHERE rr.part_unique IN ({_placeholders(len(group))}) ORDER BY rl.name",
        group).fetchall()


def vault_state(connection: sqlite3.Connection, part_unique: str) -> tuple[bool | None, float | None]:
    """How hard this part is to get, as (is_vaulted, vaulted_share).

    A part usually drops from several relics, some vaulted and some not — the
    wiki for Dual Zoren Prime shows its Handle in Axi A22 (available) and
    Axi P8 (vaulted) alike. So "vaulted" is not a property of the part:

    * share 1.0 — every relic dropping it is vaulted: genuinely hard to get
    * share 0.0 — freely farmable
    * in between — restricted, and worth showing as such

    Returns (None, None) when nothing is known. Unknown is reported as unknown;
    defaulting either way would quietly rank items wrong.
    """
    rows = relics_dropping(connection, part_unique)
    states = [r["vaulted"] for r in rows if r["vaulted"] is not None]
    if not states:
        return None, None
    share = sum(states) / len(states)
    return share == 1.0, share


#: Chance that one opened relic yields one specific reward, by refinement and
#: by the rarity of the slot that reward sits in. These are fixed game
#: constants, not data: a relic has three common slots, two uncommon and one
#: rare, and refining moves probability from the common slots to the rare one.
#:
#: The scan records the Intact form of a relic (it reads the printed name, and
#: the name is the same at every refinement), so Intact is what the estimate
#: below uses. That understates a refined collection, which is the safe
#: direction: it makes the tool say "take it now" rather than "you can get
#: this yourself" on a guess about void traces the user may not want to spend.
DROP_CHANCE = {
    "INTACT":      {"COMMON": 0.2533, "UNCOMMON": 0.11, "RARE": 0.02},
    "EXCEPTIONAL": {"COMMON": 0.2333, "UNCOMMON": 0.13, "RARE": 0.04},
    "FLAWLESS":    {"COMMON": 0.2000, "UNCOMMON": 0.17, "RARE": 0.06},
    "RADIANT":     {"COMMON": 0.1667, "UNCOMMON": 0.20, "RARE": 0.10},
}

#: The four refinement levels, and the suffix each carries in a relic's
#: `unique_name`. The name printed on a tile carries the word in brackets -
#: "Meso L3 Relic [Radiant]" - and nothing at all for Intact.
REFINEMENTS = {
    "INTACT": "Bronze",
    "EXCEPTIONAL": "Silver",
    "FLAWLESS": "Gold",
    "RADIANT": "Platinum",
}


#: Own-stock chance at or above which a part counts as secured — the user can
#: realistically get it without taking it now.
SECURE_CHANCE = 0.50

#: Below this, a stock of relics is not a plan. Two Intact relics on a rare
#: slot come to 4%, which is not "I can get this myself" by any reading.
FAINT_CHANCE = 0.05

#: More active relics than this and the part is plainly farmable.
#:
#: Zero, i.e. any active relic at all makes a part farmable. It was one, on the
#: reasoning that a single relic is one rotation away from none - which sounded
#: right and is contradicted by the data. Counted over the built database, of
#: the 101 parts with any active relic, 66 have exactly one and 27 have two.
#: One relic is what a Prime part in the current rotation normally has, so the
#: rule would have marked two thirds of everything currently farmable as
#: scarce, and a label that fits two thirds of the list says nothing.
#:
#: Scarcity is therefore the plain thing: nothing drops it any more. What
#: separates scarce from unreachable is then the relics already held.
PLENTIFUL_ACTIVE = 0


def relic_unique_for(connection: sqlite3.Connection, name: str,
                     refinement: str | None) -> str | None:
    """The identifier of one relic at one refinement.

    Every refinement of a relic is its own row with its own identifier, ending
    in Bronze, Silver, Gold or Platinum. They carry identical rewards and
    different odds, and the inventory holds them as separate stacks - so
    "Meso L3 Relic" and "Meso L3 Relic [Radiant]" are two entries, not one
    counted twice.
    """
    suffix = REFINEMENTS.get((refinement or "INTACT").upper())
    if suffix:
        # Ordered, because a printed relic name is not unique: "Lith G12 Relic"
        # is two different relics, eight rows across their refinements. An
        # unordered LIMIT 1 picks a different one run to run, so the same
        # screenshot would book to a different identifier each import.
        row = connection.execute(
            "SELECT unique_name FROM relics WHERE name = ? "
            "AND unique_name LIKE ? ORDER BY unique_name LIMIT 1",
            (name, f"%{suffix}")).fetchone()
        if row:
            return row["unique_name"]
    # An unknown refinement falls back to any form of the relic rather than to
    # nothing: the count is still worth recording, only the odds are a guess.
    row = connection.execute(
        "SELECT MIN(unique_name) AS unique_name FROM relics WHERE name = ?",
        (name,)).fetchone()
    return row["unique_name"] if row and row["unique_name"] else None


def refinement_of(unique_name: str) -> str:
    """Which refinement an identifier names, from its suffix."""
    for level, suffix in REFINEMENTS.items():
        if unique_name.endswith(suffix):
            return level
    return "INTACT"


def relic_label(name: str, refinement: str | None, always: bool = False) -> str:
    """A relic's printable name with its refinement: "Meso L3 Relic [Radiant]".

    Intact carries no suffix, because that is how the game prints it, unless
    `always` asks for it (a list that must tell the two apart on sight). The
    one place this is spelled; every table, card and label that names a relic
    by identifier goes through it, so two of them cannot disagree about which
    stack a row is.
    """
    if refinement and (always or refinement.upper() != "INTACT"):
        return f"{name} [{refinement.title()}]"
    return name


def own_stock_chance(connection: sqlite3.Connection, part_unique: str) -> float:
    """Chance of getting this part from the relics already in the inventory.

    `1 - Π(1 - p)` over every held relic that drops it, so ten Intact relics at
    2% come to 18% rather than 200%. Counting relics instead would say "you
    hold three, you are covered" about a rare slot where three Intact relics
    are 6%.

    Each relic is weighed at its own refinement, read off its identifier. The
    tile prints it - "Meso L3 Relic [Radiant]" - so this is measured rather
    than assumed, and it matters: on a rare slot Radiant is five times Intact.
    """
    group = alias_group(connection, part_unique)
    rows = connection.execute(
        f"SELECT rr.rarity, o.count, o.relic_unique FROM owned_relics o "
        f"JOIN relic_rewards rr ON rr.relic_unique = o.relic_unique "
        f"WHERE rr.part_unique IN ({_placeholders(len(group))})", group).fetchall()

    miss = 1.0
    for row in rows:
        table = DROP_CHANCE[refinement_of(str(row["relic_unique"]))]
        chance = table.get(str(row["rarity"]).upper(), 0.0)
        miss *= (1.0 - chance) ** max(0, int(row["count"] or 0))
    return 1.0 - miss


def reachability(connection: sqlite3.Connection, part_unique: str) -> tuple[str, int, float]:
    """How hard this part is to get without taking it now.

    Returns (state, active_relics, own_chance) where state is one of
    "farmable", "scarce" or "unreachable", or "unknown" when the vault status
    of every relic dropping it is NULL.

    This replaces asking whether a part is vaulted. Vaulting stops a relic
    dropping; it does not remove the relics already in the inventory, so the
    same vaulted part is a different proposition for someone holding ten of
    its relics than for someone holding none. That difference is exactly what
    the pick turns on.
    """
    rows = relics_dropping(connection, part_unique)
    states = [r["vaulted"] for r in rows if r["vaulted"] is not None]
    if not states:
        return "unknown", 0, 0.0

    active = sum(1 for state in states if not state)
    chance = own_stock_chance(connection, part_unique)

    if active > PLENTIFUL_ACTIVE or chance >= SECURE_CHANCE:
        return "farmable", active, chance
    if active or chance >= FAINT_CHANCE:
        return "scarce", active, chance
    return "unreachable", active, chance


def best_rarity(connection: sqlite3.Connection, part_unique: str) -> str:
    """The rarest slot this part occupies. Rarer means a worse drop chance."""
    order = {"RARE": 3, "UNCOMMON": 2, "COMMON": 1}
    rows = relics_dropping(connection, part_unique)
    if not rows:
        return "COMMON"
    return max((r["rarity"] for r in rows), key=lambda r: order.get(str(r).upper(), 0))


def owned_count(connection: sqlite3.Connection, part_unique: str) -> int:
    """How many of this piece the user holds.

    A finished weapon counts here too, because some pieces are finished
    weapons: Akbronco Prime is built out of two Bronco Primes, and a Bronco
    Prime that has been built lives in `owned_items`, never in `owned_parts`.
    Reading only the parts table said a player held none of something sitting
    in their arsenal.

    Ordinary components are unaffected - a barrel is not an item, so the second
    query finds nothing.
    """
    row = connection.execute(
        "SELECT count FROM owned_parts WHERE part_unique = ?",
        (canonical(connection, part_unique),)).fetchone()
    held = row["count"] if row else 0
    built = connection.execute(
        "SELECT count FROM owned_items WHERE item_unique = ?",
        (part_unique,)).fetchone()
    return held + (built["count"] if built else 0)


def is_kept(connection: sqlite3.Connection, item_unique: str) -> bool:
    """True if the player keeps one copy of this item for themselves."""
    return connection.execute(
        "SELECT 1 FROM kept_items WHERE item_unique = ?",
        (item_unique,)).fetchone() is not None


def set_kept(connection: sqlite3.Connection, item_unique: str,
             kept: bool) -> None:
    """Set or clear the mark. Commits, like the other single-row setters."""
    if kept:
        connection.execute(
            "INSERT INTO kept_items(item_unique, updated_at) "
            "VALUES(?, datetime('now')) ON CONFLICT(item_unique) DO NOTHING",
            (item_unique,))
    else:
        connection.execute(
            "DELETE FROM kept_items WHERE item_unique = ?", (item_unique,))
    connection.commit()


def crafting_stock(connection: sqlite3.Connection, piece_unique: str) -> int:
    """Copies of this piece that are there for crafting its parents.

    `owned_count` is the raw record - what import, picks and write checks
    compare against. This is the same number less the copy the player keeps for
    themselves, and it answers the other question: is this piece still short for
    its recipe. It can be -1: one copy is kept and none is built yet, which is
    what makes "keep one" ask for one more copy than the recipes need.
    """
    return owned_count(connection, piece_unique) - (
        1 if is_kept(connection, piece_unique) else 0)


#: Hand-kept crafting edges the export cannot express, from the same folder
#: `build.py` layers onto the manifests. `parts.unique_name` is a primary key,
#: so a weapon used by two recipes can only record one of them - Akvasto needs
#: two Vastos, but that row went to Redeemer and the Akvasto edge is simply
#: absent. Six of the thirteen curated edges are missing from `parts` for that
#: reason, which matters here and nowhere else: a missing edge is a set the
#: release rule cannot see, and pieces offered for ducats that a recipe still
#: wants.
_DEPENDENCIES = Path(__file__).resolve().parent / "curated" / "item-dependencies.json"

_curated_edges: dict | None = None


def _curated_dependencies() -> dict:
    """The curated file, read once. An unreadable file is no edges, not a crash.

    Failing soft is deliberate and it fails towards caution in only one
    direction - fewer edges means fewer items held back - so a missing file is
    reported by the tests rather than swallowed silently in production.
    """
    global _curated_edges
    if _curated_edges is None:
        import json

        try:
            _curated_edges = json.loads(_DEPENDENCIES.read_text())
        except (OSError, ValueError):
            _curated_edges = {}
    return _curated_edges


def _consumers(connection: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """item -> every item built from it, and how many copies each one eats.

    Two sources for one question. The database carries the edge whenever a
    finished weapon is itself a component: Bronco Prime is an item and also a
    row in `parts` whose `item_unique` is Akbronco Prime. The curated file
    carries the ones the primary key had no room for.

    A name that belongs to several items contributes an edge to all of them.
    Fifteen names in the database are shared, and guessing wrong in the
    other direction would release pieces a recipe still wants; an edge too many
    only keeps something on the shelf.

    The count travels with the edge because the floor depends on it: Akbronco
    Prime eats two Bronco Primes, so a shelf with none of them needs two of
    every Bronco piece and not one. Where both sources describe the same edge
    the larger count wins, for the same reason the extra edges do - erring
    upwards keeps metal on the shelf, erring downwards sells it.
    """
    edges: dict[str, dict[str, int]] = {}

    def note(child: str, parent: str, quantity: int) -> None:
        wants = edges.setdefault(child, {})
        wants[parent] = max(wants.get(parent, 0), max(1, quantity))

    for row in connection.execute(
            "SELECT p.unique_name AS child, p.item_unique AS parent, "
            "       p.count_needed AS quantity "
            "FROM parts p JOIN items i ON i.unique_name = p.unique_name "
            "WHERE p.item_unique IS NOT NULL "
            "AND p.item_unique <> p.unique_name"):
        note(row["child"], row["parent"], int(row["quantity"] or 1))

    by_name: dict[str, set[str]] = {}
    for row in connection.execute("SELECT name, unique_name FROM items"):
        by_name.setdefault(row["name"], set()).add(row["unique_name"])
    for parent, spec in _curated_dependencies().items():
        if parent.startswith("_") or not isinstance(spec, dict):
            continue
        for owner in by_name.get(parent, ()):
            for required in spec.get("requires") or []:
                for child in by_name.get(str(required.get("item", "")), ()):
                    if child != owner:
                        note(child, owner, int(required.get("quantity") or 1))
    return edges


def wanted_items(connection: sqlite3.Connection) -> set[str]:
    """Every item whose pieces are still worth keeping.

    Mastery is the question underneath: the points are banked at rank thirty
    and building the thing again earns nothing, so the recipe has no further
    claim on the shelf. Once a set is mastered, it can go and nobody needs
    to keep sets back.

    The chain is what makes this more than a flag. Bronco Prime is mastered and
    would never be built for its own sake, but Akbronco Prime is two Bronco
    Primes and is not mastered - so a Bronco Prime Barrel is still wanted. Read
    as a rule: an item is wanted while it is unmastered and not built, or while
    anything built from it is wanted.

    Wanted is not the same as "copies still to build": a wanted item whose
    built copies already cover the recipes above it is held back at 0 sets
    (`_missing_copies`). The pieces of such an item stay wanted-listed
    so that a copy that leaves its stock brings the demand back.

    Grown to a fixed point rather than walked recursively. The set only ever
    gains members, so it terminates whatever the graph does - and the graph
    does contain self-references: thirteen rows in the database name
    themselves as their own consumer, Forma Blueprint among them.
    """
    # A built copy has taken its pieces with it and a built item cannot be
    # traded, so its own recipe has nothing left to keep on the shelf.
    # Its mastery comes from levelling that copy, not from building another.
    # It is wanted again below when something not yet built is made from it.
    wanted = {row["unique_name"] for row in connection.execute(
        "SELECT i.unique_name FROM items i "
        "LEFT JOIN owned_items o ON o.item_unique = i.unique_name "
        "WHERE COALESCE(o.mastered, 0) = 0 AND COALESCE(o.count, 0) = 0")}
    edges = _consumers(connection)
    growing = True
    while growing:
        growing = False
        for child, parents in edges.items():
            if child not in wanted and not parents.keys().isdisjoint(wanted):
                wanted.add(child)
                growing = True
    return wanted


def _missing_copies(connection: sqlite3.Connection, edges: dict, item: str,
                    cache: dict, walking: frozenset = frozenset(),
                    wanted: set | None = None) -> int:
    """How many copies of this item still have to be built.

    The floor under a piece is its recipe's count, and for an ordinary
    component that is the whole story: "Dual Zoren Prime Blade" carries a
    count of two and holding two is a set, not a surplus. It stops being the
    whole story one link up the chain. Akbronco Prime is built from two Bronco
    Primes, and one Bronco Prime is built from one barrel - so a shelf with no
    Bronco Prime on it needs *two* barrels, while the recipe for a barrel says
    one and knows nothing about the gun above it.

    The chain demand is summed over every parent that is still wanted: an item
    two recipes eat needs copies for both. A parent that is mastered or built
    asks for nothing, so a mastered Aklex Prime no longer demands two Lex
    Primes. The copies on hand are `crafting_stock`, the built ones less the
    one the player keeps for themselves, if marked:

        missing = max(0, demand - stock)

    There is no floor of one. Where the built copies already cover the chain
    this answers 0 and the item's pieces are spare; the earlier floor held one
    set back for everybody because one player keeps a Lex Prime for
    themselves, which is now that player's mark and nobody else's.

    An item nothing wanted is built from asks for itself only: 1 while it is
    unmastered and unbuilt, else 0. The mark adds nothing there, since no
    recipe wants copies. A cycle cannot be asked how many copies are missing
    and answers 1, the conservative direction.
    """
    if item in cache:
        return cache[item]
    if item in walking:
        return 1
    if wanted is None:
        wanted = wanted_items(connection)
    demand = 0
    for parent, quantity in (edges.get(item) or {}).items():
        if parent in wanted:
            demand += quantity * _missing_copies(
                connection, edges, parent, cache, walking | {item}, wanted)
    if demand > 0:
        answer = max(0, demand - crafting_stock(connection, item))
    else:
        mastered = connection.execute(
            "SELECT 1 FROM owned_items WHERE item_unique = ? AND mastered = 1",
            (item,)).fetchone() is not None
        # The built copies, not `owned_count`: a part that is its own item
        # (Forma Blueprint) would read its pieces as copies.
        built = item_built(connection, item)
        answer = 0 if mastered or built > 0 else 1
    cache[item] = answer
    return answer


def copies_still_wanted(connection: sqlite3.Connection, item_unique: str) -> int:
    """How many more copies of this item the recipes above it still ask for.

    The public form of `_missing_copies`: a built item is "still needed
    for" its consumer only while this is above 0. Aklex Prime asks for two Lex
    Primes; with three built it asks for none, and a page that says the Lex is
    still needed contradicts the Ducats page that offers its parts.
    """
    return _missing_copies(connection, _consumers(connection), item_unique, {})


def copies_demanded(connection: sqlite3.Connection, item_unique: str) -> int:
    """Copies of this item the wanted recipes above it call for, stock aside.

    `copies_still_wanted` is this less the copies on hand. Selling a spare copy
    is safe while the demand stays covered afterwards, which needs the demand
    itself and not the shortfall (two built, two wanted: shortfall 0, and one
    sale makes it 1).
    """
    wanted = wanted_items(connection)
    edges = _consumers(connection)
    cache: dict = {}
    return sum(quantity * _missing_copies(connection, edges, parent, cache,
                                          frozenset({item_unique}), wanted)
               for parent, quantity in (edges.get(item_unique) or {}).items()
               if parent in wanted)


def sale_costs_copies(connection: sqlite3.Connection, item_unique: str) -> bool:
    """Whether handing over one built copy would leave a recipe short."""
    demand = copies_demanded(connection, item_unique)
    return demand > 0 and demand >= crafting_stock(connection, item_unique)


#: Categories mastery never releases. Forma, Kuva and the rest are consumables
#: that happen to have recipes; they are never mastered, so the rule would not
#: reach them anyway - but a mastery flag set by hand on the wrong row would,
#: and offering somebody their Forma blueprints for ducats is not a mistake worth
#: leaving one typo away.
_NEVER_RELEASED = ("reward_other", "component_only")


def retained_counts(connection: sqlite3.Connection) -> dict[str, int]:
    """part_unique -> how many copies are not on offer, and why they are not.

    The one place the floor is decided, with the chain arithmetic.
    `spare_parts` shows what is above it
    and `trade_spare` refuses to write below it, and when those were two
    expressions of the same idea they disagreed: the page offered a mastered
    set's last piece and the write silently kept it, so the kiosk had the part
    and the database still claimed it.

    Released only on positive proof - the part names an item, the item exists,
    its category allows release, and nothing wants it. A part whose owner is
    missing or unknown keeps its recipe's floor, because "I cannot tell" and
    "nobody wants it" are not the same answer and only one of them is safe.

    What is kept is the recipe's count once per copy still to be built, not
    once outright - see `_missing_copies`. Outside a chain that multiplier is
    one and this is the recipe's count, which is what it has always been. In a
    chain it can be 0 when the built copies already cover the recipes above,
    and it grows by one for an item the user marked as kept (`set_kept`).
    """
    wanted = wanted_items(connection)
    edges = _consumers(connection)
    copies: dict[str, int] = {}
    retained: dict[str, int] = {}
    for row in connection.execute(
            "SELECT p.unique_name, p.count_needed, p.item_unique, i.category "
            "FROM parts p LEFT JOIN items i ON i.unique_name = p.item_unique"):
        released = (row["item_unique"] is not None
                    and row["category"] is not None
                    and row["category"] not in _NEVER_RELEASED
                    and row["item_unique"] not in wanted)
        if released:
            retained[row["unique_name"]] = 0
            continue
        # What the recipe asks for, once per copy of its own item that is
        # still to be built. See `_missing_copies`: outside a chain that
        # multiplier is one and this is the recipe's count, exactly as before.
        # An owner that is not wanted and was not released (a never-released
        # category) keeps the recipe's count, as it always did.
        owner = row["item_unique"]
        multiplier = (1 if owner is None or owner not in wanted
                      else _missing_copies(connection, edges, owner, copies,
                                           wanted=wanted))
        retained[row["unique_name"]] = int(row["count_needed"]) * multiplier
    return retained


def spare_parts(connection: sqlite3.Connection) -> list[dict]:
    """Parts held beyond what their set still asks for, with the surplus.

    A recipe can want two of a component, so holding two is not a duplicate -
    it is a finished piece of the set. Counting everything past the first as
    spare offered the user a Gunsen Prime Handle for ducats while the set still
    needed it, which is the one mistake this page must not make.

    But a recipe only has a claim while its set is still worth building. Once
    the mastery is banked and nothing else is built from it, the whole holding
    is surplus - see `retained_counts`. Those rows carry `freed_by_mastery` so
    the page can say why they are on offer: they are shown marked
    rather than mixed in, because what goes to the kiosk does not come back.

    The surplus is not worthless, though it earns no mastery: a spare rare part
    is ducats, and a second full set sells. So it is reported rather than
    hidden - the capping belongs in set progress, not here.
    """
    retained = retained_counts(connection)
    # Built but not mastered: their pieces are free because of the build, not
    # because of mastery, and the pages say which.
    built = {r["item_unique"] for r in connection.execute(
        "SELECT item_unique FROM owned_items WHERE count > 0 AND mastered = 0")}
    spares = []
    for row in connection.execute(
            "SELECT p.unique_name, p.name, p.item_unique, p.count_needed, o.count "
            "FROM owned_parts o JOIN parts p ON p.unique_name = o.part_unique "
            "WHERE p.alias_of IS NULL ORDER BY p.name"):
        needed = int(row["count_needed"])
        keep = retained.get(row["unique_name"], needed)
        spare = int(row["count"]) - keep
        if spare > 0:
            spares.append({
                "unique_name": row["unique_name"], "name": row["name"],
                "count_needed": needed, "count": int(row["count"]),
                "spare": spare, "freed_by_mastery": keep == 0 and needed > 0,
                "freed_by_build": (keep == 0 and needed > 0
                                   and row["item_unique"] in built)})
    return spares


def set_progress(connection: sqlite3.Connection, item_unique: str) -> tuple[int, int]:
    """(pieces held, pieces the recipe needs).

    Pieces, not kinds of piece. A recipe can call for more than one of the
    same component: Gunsen Prime wants a blueprint, two handles and two
    blades, which is five pieces across three kinds. Counting kinds reported
    a player holding the blueprint and both handles as "1 of 3" when the
    honest answer is three of five — and it understated every set of that
    shape, in the inventory, in the plan and in the reward ranking alike.

    Held is capped at what the recipe needs, so a third handle does not read
    as progress towards a set it cannot advance.

    Counts real set pieces only — `alias_of IS NULL`. Counting aliases as well
    would make a four-piece Warframe set look like a seven-piece one.

    A finished weapon counts as the piece it is, the same way `owned_count`
    reads it: Akbronco Prime wants a blueprint, a link and two Bronco Primes -
    four pieces across three kinds - and a Bronco Prime that has been built
    lives in `owned_items`. Reading only `owned_parts` here called that set
    two of four while one of the two guns was in the arsenal.

    A copy the player keeps for themselves (`kept_items`) is not held for
    the recipe: the same arithmetic as `crafting_stock`, in SQL.
    """
    row = connection.execute(
        "SELECT COALESCE(SUM(p.count_needed), 0) AS total, "
        "       COALESCE(SUM(MIN(MAX(COALESCE(o.count, 0) + COALESCE(b.count, 0) "
        "                            - (k.item_unique IS NOT NULL), 0), "
        "                        p.count_needed)), 0) AS held "
        "FROM parts p "
        "LEFT JOIN owned_parts o ON o.part_unique = p.unique_name "
        "LEFT JOIN owned_items b ON b.item_unique = p.unique_name "
        "LEFT JOIN kept_items k ON k.item_unique = p.unique_name "
        "WHERE p.item_unique = ? AND p.alias_of IS NULL",
        (item_unique,)).fetchone()
    return int(row["held"]), int(row["total"])


def is_crafted(connection: sqlite3.Connection, unique_name: str) -> bool:
    """True if this is made in the foundry rather than found in a relic.

    Asked of the guns that are components of other guns. A Bronco Prime is a
    piece of Akbronco Prime and has a recipe of its own; no relic drops one and
    no player can trade one, so a copy that was not there before came out of
    the foundry. That is what makes its arrival usable as evidence.
    """
    return connection.execute(
        "SELECT 1 FROM parts WHERE item_unique = ? AND alias_of IS NULL LIMIT 1",
        (unique_name,)).fetchone() is not None


def is_built_component(connection: sqlite3.Connection, unique_name: str) -> bool:
    """True if this is a finished weapon that serves as a piece of another item.

    Lex Prime inside Aklex Prime, Bronco Prime inside Akbronco Prime, Magistar
    inside Sibear. One name, two tables in the export: `items` has it as a
    weapon and `parts` has it as a component. The model (see `owned_count`) is
    that the built copies live in `owned_items` and never in `owned_parts`, so
    a reading of one of these goes to `owned_items`.

    Two things that are in both tables are not one of these and stay parts:
    an item that is a part of *itself* (Forma Blueprint: `parts.item_unique`
    equals its own unique name), and the `component_only` items (Chroma
    Neuroptics, Equinox Night Aspect), which exist as items only because the
    export lists their recipe and can never be built as a thing of their own.
    """
    return connection.execute(
        "SELECT 1 FROM parts p JOIN items i ON i.unique_name = p.unique_name "
        "WHERE p.unique_name = ? AND p.item_unique <> p.unique_name "
        "AND i.category <> 'component_only' "
        "AND p.alias_of IS NULL LIMIT 1", (unique_name,)).fetchone() is not None


def settle_built_components(connection: sqlite3.Connection
                            ) -> list[tuple[str, int, int | None, int]]:
    """Move built weapons out of `owned_parts` into `owned_items`.

    Databases written by earlier versions hold a built component weapon (see
    `is_built_component`) in `owned_parts`, put there by the video import,
    next to or instead of its `owned_items` row, so `owned_count` adds them
    twice. Rule, per such `owned_parts` row:

    - The `owned_parts` row is the more recent reading of the two when its
      `updated_at` is not older than the `owned_items` row's, or there is no
      `owned_items` row. Then its count becomes the `owned_items` count (the
      last number read is the number held, never the sum), with the source
      and date of that row.
    - Otherwise the `owned_items` row is the newer reading and keeps its count.
    - Either way the `owned_parts` row is deleted. Mastery on the
      `owned_items` row is untouched.

    Idempotent: after one run there is no such row left. Returns
    (unique_name, parts_count, items_count_before or None, items_count_after).
    Does not commit; the caller does.
    """
    done = []
    for row in connection.execute(
            "SELECT o.part_unique, o.count, o.source, o.updated_at FROM owned_parts o "
            "WHERE EXISTS (SELECT 1 FROM parts p JOIN items i "
            "              ON i.unique_name = p.unique_name "
            "              WHERE p.unique_name = o.part_unique "
            "              AND p.item_unique <> p.unique_name "
            "              AND i.category <> 'component_only' "
            "              AND p.alias_of IS NULL)").fetchall():
        unique = row["part_unique"]
        item = connection.execute(
            "SELECT count, updated_at FROM owned_items WHERE item_unique = ?",
            (unique,)).fetchone()
        before = int(item["count"]) if item else None
        if item is None or (row["updated_at"] or "") >= (item["updated_at"] or ""):
            after = int(row["count"])
            connection.execute(
                "INSERT INTO owned_items(item_unique, count, source, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(item_unique) DO UPDATE SET "
                "count=?, source=?, updated_at=?",
                (unique, after, row["source"], row["updated_at"],
                 after, row["source"], row["updated_at"]))
        else:
            after = before
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?", (unique,))
        done.append((unique, int(row["count"]), before, after))
    return done


def pieces_of_one_set(connection: sqlite3.Connection, item_unique: str,
                      copies: int = 1) -> list[tuple[str, str, str, int, int]]:
    """(kind, unique_name, name, held now, held after) when a set leaves.

    Two things take a set off a shelf and a scan is neither of them. The
    foundry trades a blueprint and its components for one finished item; the
    trade window hands the whole set to another player. Both leave the same
    hole, and in both cases the recipe is what says how big it is.

    A capture can only photograph what is still there - the new weapon in the
    arsenal, or nothing at all - and the pieces that left simply stop
    appearing, which looks exactly like a shelf the recording missed.

    So the answer comes from the recipe and from the fact that a copy appeared,
    never from what a capture failed to show. That difference is the whole
    point: a player built Vadarya Prime, levelled it, imported, and the tool
    went on offering its four pieces for ducats.

    A piece that is itself a finished weapon - Bronco Prime inside Akbronco
    Prime - lives in `owned_items`, so where each piece is counted is decided
    per piece rather than assumed. Pieces that are not recorded at all are left
    out: there is nothing to take off a shelf that never held them.

    This only works it out. Writing it down is `remove_one_set`, because the
    import wants the arithmetic to show somebody before anything is written.
    """
    rows: list[tuple[str, str, str, int, int]] = []
    for piece in connection.execute(
            "SELECT unique_name, name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL ORDER BY name",
            (item_unique,)):
        taken = int(piece["count_needed"]) * max(1, copies)

        root = canonical(connection, piece["unique_name"])
        row = connection.execute(
            "SELECT count FROM owned_parts WHERE part_unique = ?",
            (root,)).fetchone()
        held = int(row["count"]) if row else 0
        if held > 0:
            rows.append(("part", piece["unique_name"], piece["name"],
                         held, max(0, held - taken)))
            taken -= min(taken, held)
        if taken <= 0:
            continue

        row = connection.execute(
            "SELECT count FROM owned_items WHERE item_unique = ?",
            (piece["unique_name"],)).fetchone()
        built = int(row["count"]) if row else 0
        if built > 0:
            rows.append(("item", piece["unique_name"], piece["name"],
                         built, max(0, built - taken)))
    return rows


def remove_one_set(connection: sqlite3.Connection, item_unique: str,
                   copies: int = 1, source: str = "manual"
                   ) -> list[tuple[str, str, str, int, int]]:
    """Take one set's worth of pieces off the shelf. Returns what went.

    For the sets that leave without being built - traded away, gifted, sold to
    another player. The pieces are four rows in two tables and finding them by
    hand is the kind of chore that stops a person from keeping the tool honest,
    so the set is the unit here, exactly as it is the unit on the page.

    Mastery is untouched. The points were banked the moment the item hit rank
    thirty and no trade takes them back - which is the whole reason these sets
    are worth giving away.
    """
    gone = pieces_of_one_set(connection, item_unique, copies)
    for kind, unique_name, _name, _now, after in gone:
        if kind == "part":
            set_part_count(connection, unique_name, after, source)
        else:
            set_item_count(connection, unique_name, after, source)
    return gone


def build_one_set(connection: sqlite3.Connection, item_unique: str,
                  copies: int = 1, source: str = "manual"
                  ) -> list[tuple[str, str, str, int, int]]:
    """Put one set in the foundry: pieces off the shelf, a copy in the arsenal.

    The other half of `remove_one_set`, and the reason both exist. A build is a
    trade and the tool has to be able to record it the moment it happens, not
    only when a later recording notices the result - the import can see a
    finished weapon, but only after it is finished, levelled and photographed,
    which is days.

    The pieces go the instant the build is started, so that is what this
    writes. Mastery is not touched: a fresh build is rank 0 and earns its
    points by being played, which the profile import is what notices.
    """
    gone = pieces_of_one_set(connection, item_unique, copies)
    for kind, unique_name, _name, _now, after in gone:
        if kind == "part":
            set_part_count(connection, unique_name, after, source)
        else:
            set_item_count(connection, unique_name, after, source)
    set_item_count(connection, item_unique,
                   item_built(connection, item_unique) + copies, source)
    return gone


def trade_spare(connection: sqlite3.Connection, part_unique: str,
                copies: int | None = None, source: str = "manual"
                ) -> tuple[int, int]:
    """Hand spare copies of a part to the kiosk. Returns (before, after).

    Never below what the set still asks for. That floor is the whole reason
    this is a function rather than a subtraction at the call site: the page
    that offers it lists surpluses, and a surplus is only a surplus against a
    number that has to be looked up. Selling a Gunsen Prime Handle while the
    set still wants two is the one mistake this page must not cause.

    The floor comes from `retained_counts`, the same call the page used to
    decide what to show. It was a separate expression here until the two
    disagreed: once mastery began releasing a set's last pieces, the page
    offered them and this refused to write, so a part could be handed to the
    kiosk while the tool went on insisting it was still held.

    `copies` defaults to every spare, because that is what a person does at
    the kiosk - they hand over the pile, not one of it.
    """
    root = canonical(connection, part_unique)
    row = connection.execute(
        "SELECT p.count_needed, o.count FROM parts p "
        "JOIN owned_parts o ON o.part_unique = p.unique_name "
        "WHERE p.unique_name = ?", (root,)).fetchone()
    if row is None:
        return 0, 0
    held = int(row["count"])
    needed = retained_counts(connection).get(root, max(1, int(row["count_needed"])))
    spare = max(0, held - needed)
    going = spare if copies is None else max(0, min(int(copies), spare))
    after = held - going
    if going:
        set_part_count(connection, part_unique, after, source)
    return held, after


def trade_built(connection: sqlite3.Connection, item_unique: str,
                copies: int = 1, source: str = "manual") -> tuple[int, int]:
    """Hand finished copies of an item to the kiosk. Returns (before, after).

    Mastery survives, as it does everywhere else: the points were banked at
    rank thirty and selling the thing does not take them back. That is
    `set_item_count`'s doing and it is why zeroing a row there keeps it.
    """
    held = item_built(connection, item_unique)
    after = max(0, held - max(1, int(copies)))
    if after != held:
        set_item_count(connection, item_unique, after, source)
    return held, after


def pieces_short_for(connection: sqlite3.Connection, item_unique: str,
                     copies: int = 1) -> list[tuple[sqlite3.Row, int]]:
    """(piece, how many short) for building `copies` of this item.

    For the guns built out of other guns. "Bronco Prime x1 of 2" is a true
    statement and a useless one: a Bronco Prime is not a thing that drops, it
    is a thing you build, and what the user needs to go and find is its
    blueprint and its receiver. One level deep, deliberately - the pieces of a
    piece are parts, and parts are what relics drop.

    Returns nothing for an ordinary component, which has no pieces of its own.
    The copies on hand are `crafting_stock`: a copy the player keeps for themselves
    does not count towards the recipe, so with the mark one more is asked.
    """
    short = []
    for row in connection.execute(
            "SELECT unique_name, name, count_needed FROM parts "
            "WHERE item_unique = ? AND alias_of IS NULL ORDER BY name",
            (item_unique,)):
        lacking = (int(row["count_needed"]) * copies
                   - crafting_stock(connection, row["unique_name"]))
        if lacking > 0:
            short.append((row, lacking))
    return short


#: Hand-typed names are matched without regard to case. Nothing in the
#: catalogue depends on it - across parts, items and relics there is not one
#: name that differs from another only in capitalisation, checked - and the
#: strict comparison cost a whole import answer: a typed "Neo c9
#: relic [radiant]" showed in green on the review card as a match, and the
#: write refused it because sqlite compares TEXT case-sensitively. The card
#: and the writer now share `resolve_typed_name`, and these three lookups are
#: the floor underneath it.
_BY_NAME = "name = ? COLLATE NOCASE"


def find_part_by_name(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT * FROM parts WHERE {_BY_NAME} "
        f"ORDER BY alias_of IS NOT NULL LIMIT 1",
        (name,)).fetchone()


def part_row(connection: sqlite3.Connection, part_unique: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT p.*, i.name AS item_name, i.category FROM parts p "
        "LEFT JOIN items i ON p.item_unique = i.unique_name WHERE p.unique_name = ?",
        (part_unique,)).fetchone()


def ducats(connection: sqlite3.Connection, part_unique: str) -> int | None:
    """Ducat value, taken from whichever alias carries one.

    The value sits on the component in some cases and on the blueprint in
    others, so asking only one of the two loses it.
    """
    group = alias_group(connection, part_unique)
    row = connection.execute(
        f"SELECT ducats FROM parts WHERE unique_name IN ({_placeholders(len(group))}) "
        f"AND ducats IS NOT NULL LIMIT 1", group).fetchone()
    return row["ducats"] if row else None


def is_tradable_part(connection: sqlite3.Connection, part_unique: str) -> bool:
    """Whether players can trade this part.

    Digital Extremes gives every tradable part a ducat value. A part without one
    is tradable only when it is itself a built item (Bronco Prime inside
    Akbronco Prime). Galariak Prime and Sagek Prime have neither: their parts
    cannot be traded and drop from no relic.
    """
    if ducats(connection, part_unique) is not None:
        return True
    return connection.execute(
        "SELECT 1 FROM items WHERE unique_name = ?", (part_unique,)).fetchone() is not None


def item_built(connection: sqlite3.Connection, item_unique: str) -> int:
    """How many finished copies of this item the user has.

    This is the fact the game itself cannot show. Warframe's "Owned" badge on a
    reward reflects `owned_parts` only: once a part is consumed to build the
    item, the badge disappears and the reward looks exactly like something never
    obtained. Distinguishing the two is the reason this tool exists.
    """
    row = connection.execute(
        "SELECT count FROM owned_items WHERE item_unique = ?", (item_unique,)).fetchone()
    return row["count"] if row else 0


def needed_as_ingredient(connection: sqlite3.Connection, item_unique: str) -> list[sqlite3.Row]:
    """Other items that consume this one as a component.

    Aklex Prime consumes two Lex Prime, so a finished Lex Prime is not
    automatically spare. Selling the second one is a mistake the tool has to
    warn about rather than cause.
    """
    return connection.execute(
        "SELECT DISTINCT i.unique_name, i.name, p.count_needed "
        "FROM parts p JOIN items i ON p.item_unique = i.unique_name "
        "WHERE p.unique_name = ? AND p.item_unique != ?",
        (item_unique, item_unique)).fetchall()


# --- writing the user's holdings ----------------------------------------
#
# Every write records where the number came from. A video import can then be
# rolled back without disturbing hand-entered data, and any recommendation can
# be traced to its evidence — which matters because a wrong holding produces a
# confidently wrong answer rather than an obvious failure.

#: Categories held as currency or consumables, never as inventory: Forma, Kuva,
#: Riven Slivers, Ayatan sculptures and the Immortal mods. They have to exist as
#: relic rewards, but no count of them is ever written. A reward screen
#: once wrote "Forma Blueprint 1" because the catalogue lists it as a
#: part of itself and every writer took it for one.
UNCOUNTED_CATEGORIES = ("reward_other",)


def is_uncounted(connection: sqlite3.Connection, unique_name: str) -> bool:
    """Whether this part or item is one the tool never counts.

    The one question every writer and every name field asks. Each of these
    is its own item and its own part, so either table answers it."""
    marks = ",".join("?" * len(UNCOUNTED_CATEGORIES))
    return connection.execute(
        f"SELECT 1 FROM items i WHERE i.category IN ({marks}) "
        f"AND (i.unique_name = ? OR i.unique_name IN "
        f"(SELECT item_unique FROM parts WHERE unique_name = ?)) LIMIT 1",
        (*UNCOUNTED_CATEGORIES, unique_name, unique_name)).fetchone() is not None


def uncounted_names(connection: sqlite3.Connection) -> set[str]:
    """The names of everything `is_uncounted`, for the name fields to leave out."""
    marks = ",".join("?" * len(UNCOUNTED_CATEGORIES))
    return {row["name"] for row in connection.execute(
        f"SELECT p.name FROM parts p JOIN items i ON i.unique_name = p.item_unique "
        f"WHERE i.category IN ({marks}) "
        f"UNION SELECT name FROM items WHERE category IN ({marks})",
        UNCOUNTED_CATEGORIES * 2)}


def _refuse_uncounted(connection: sqlite3.Connection, unique_name: str) -> None:
    """Stop a count of Forma and its kind before it is written.

    Every path is meant to ask `is_uncounted` first and say so on screen; this
    is the floor under all of them, so one that forgets fails loudly rather
    than booking a Forma the user never asked to count."""
    if is_uncounted(connection, unique_name):
        raise ValueError(f"{unique_name} is not counted")


def set_part_count(connection: sqlite3.Connection, part_unique: str, count: int,
                   source: str = "manual") -> None:
    """Record how many of a part the user holds. Counted under the canonical name."""
    from datetime import datetime, timezone

    root = canonical(connection, part_unique)
    if count > 0:
        _refuse_uncounted(connection, root)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if count <= 0:
        connection.execute("DELETE FROM owned_parts WHERE part_unique = ?", (root,))
    else:
        connection.execute(
            "INSERT INTO owned_parts(part_unique, count, source, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(part_unique) DO UPDATE SET count=?, source=?, updated_at=?",
            (root, count, source, now, count, source, now))
    connection.commit()


def set_item_count(connection: sqlite3.Connection, item_unique: str, count: int,
                   source: str = "manual") -> None:
    """Record finished copies of an item — the fact the game will not show."""
    from datetime import datetime, timezone

    if count > 0:
        _refuse_uncounted(connection, item_unique)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if count <= 0:
        # Zero the count, but keep a row that carries mastery. Selling a Prime
        # after levelling it is the normal thing to do, and deleting the row
        # threw the mastery away with the ownership - the two are independent,
        # which is the whole reason mastery has a column of its own.
        connection.execute(
            "DELETE FROM owned_items WHERE item_unique = ? AND mastered = 0",
            (item_unique,))
        connection.execute(
            "UPDATE owned_items SET count = 0, source = ?, updated_at = ? "
            "WHERE item_unique = ?", (source, now, item_unique))
    else:
        connection.execute(
            "INSERT INTO owned_items(item_unique, count, source, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(item_unique) DO UPDATE SET count=?, source=?, updated_at=?",
            (item_unique, count, source, now, count, source, now))
    connection.commit()


def clear_source(connection: sqlite3.Connection, source: str) -> tuple[int, int]:
    """Drop everything that came from one source. Returns (parts, items) removed."""
    parts = connection.execute(
        "DELETE FROM owned_parts WHERE source = ?", (source,)).rowcount
    # Mastery survives clearing an import: it is a fact about the account, not
    # about what a recording happened to show.
    items = connection.execute(
        "DELETE FROM owned_items WHERE source = ? AND mastered = 0",
        (source,)).rowcount
    connection.execute(
        "UPDATE owned_items SET count = 0 WHERE source = ?", (source,))
    connection.execute("DELETE FROM owned_relics WHERE source = ?", (source,))
    connection.commit()
    return parts, items


#: What the player put in, as opposed to what the game data and the markets
#: provide: the holdings a backup carries (`wft.data.backup`), the history of
#: the imports that wrote them, the readings the reviews dismissed and the
#: copies the player keeps for themselves. The observations go first:
#: they belong to an import.
HOLDINGS_TABLES = ("observations", "imports", "owned_parts", "owned_items",
                   "owned_relics", "learned_ignores", "kept_items")


def delete_holdings(connection: sqlite3.Connection) -> dict[str, int]:
    """Empty the user's own tables; returns the rows removed from each.

    Unlike `clear_source`, mastery goes too: this is the way back to an empty
    account, and a latch that survives it would be the one thing left behind.
    The scan revision goes with the scans it described, or an empty inventory
    is reported as recorded by an old reader. Kept: the game data, the prices,
    the riven history, the pictures (they show a thing, not who owns it) and
    the measured tile grid, which belongs to the screen.
    """
    removed = {table: connection.execute(f"DELETE FROM {table}").rowcount
               for table in HOLDINGS_TABLES}
    connection.execute("DELETE FROM meta WHERE key = 'scan_revision'")
    connection.commit()
    return removed


#: The rank at which an item grants its mastery. Every item this tool tracks is
#: relic-dropped and caps at 30; Kuva and Tenet weapons cap at 40 but are not
#: in the tracked set, so there is no per-item maximum to carry.
MASTERY_RANK = 30


def set_item_mastered(connection: sqlite3.Connection, item_unique: str,
                      source: str = "video") -> None:
    """Record that an item has been levelled to mastery, once and for good.

    Mastery is independent of ownership: the points are granted at rank 30 and
    kept when the item is sold, which is what most players do with a levelled
    Prime. So this latches - it can be set by a scan and never cleared by one.
    A row that does not exist yet is created with count 0, because "mastered,
    not owned" is a real and common state rather than a contradiction.

    Count 0 is also what keeps the two sources from interfering. The profile
    knows what has been levelled and nothing about how many are held; a scan
    knows how many are held. This writes only the flag, so a row already
    counted keeps its count and a row created here waits for a scan to give it
    one.

    The source is named rather than assumed: a row that came from the profile
    used to claim it came from a video, which made the by-source breakdown on
    the Inventory page wrong about where its own numbers came from.
    """
    connection.execute(
        "INSERT INTO owned_items(item_unique, count, mastered, source) "
        "VALUES(?, 0, 1, ?) "
        "ON CONFLICT(item_unique) DO UPDATE SET mastered = 1",
        (item_unique, source))
    connection.commit()


def set_item_mastered_manually(connection: sqlite3.Connection, item_unique: str,
                               mastered: bool) -> None:
    """Set or clear mastery by hand.

    Clearing exists because the latch has to be correctable: a misread tile
    could mark the wrong item forever, and only a person can know that. The
    scan never clears; this does.
    """
    connection.execute(
        "INSERT INTO owned_items(item_unique, count, mastered, source) "
        "VALUES(?, 0, ?, 'manual') "
        "ON CONFLICT(item_unique) DO UPDATE SET mastered = excluded.mastered",
        (item_unique, int(mastered)))
    connection.commit()


def is_mastered(connection: sqlite3.Connection, item_unique: str) -> bool:
    row = connection.execute(
        "SELECT mastered FROM owned_items WHERE item_unique = ?",
        (item_unique,)).fetchone()
    return bool(row["mastered"]) if row else False


#: Categories that do not count towards mastery and are not a gap in a
#: collection. Named once so the numerator and the denominator cannot drift
#: apart. Beasts are not in it: they count like every tracked item.
NOT_FOR_MASTERY_CATEGORIES = (
    "reward_other", "other", "amp", "special", "component_only")

#: Categories a recording can never show. The game's inventory lists a
#: beast under the pet's own name, so the scan cannot recognise one and keeps
#: ignoring it on purpose. This is about reading a scan only; whether an item
#: counts for mastery is `NOT_FOR_MASTERY_CATEGORIES`. The import never expects or
#: offers one as "gone?" either.
NOT_IN_RECORDINGS = ("beast",)

#: What the scan's name lookup leaves out: built items, and the parts of the
#: items listed in `SCAN_IGNORED_PART_CATEGORIES`.
SCAN_IGNORED_CATEGORIES = NOT_IN_RECORDINGS + NOT_FOR_MASTERY_CATEGORIES
SCAN_IGNORED_PART_CATEGORIES = NOT_IN_RECORDINGS + (
    "reward_other", "other", "amp", "special")


def mastered_count(connection: sqlite3.Connection) -> tuple[int, int]:
    """(mastered, trackable) — the ratio the user actually plays for.

    Both halves filtered the same way. The numerator used to count every
    mastered row whatever its category, so marking one untracked item by hand
    would have pushed the ratio past its own denominator.
    """
    placeholders = _placeholders(len(NOT_FOR_MASTERY_CATEGORIES))
    mastered = connection.execute(
        f"SELECT COUNT(*) AS n FROM owned_items o "
        f"JOIN items i ON i.unique_name = o.item_unique "
        f"WHERE o.mastered = 1 AND i.category NOT IN ({placeholders})",
        NOT_FOR_MASTERY_CATEGORIES).fetchone()["n"]
    total = connection.execute(
        f"SELECT COUNT(*) AS n FROM items WHERE category NOT IN ({placeholders})",
        NOT_FOR_MASTERY_CATEGORIES).fetchone()["n"]
    return int(mastered), int(total)


def note_mastery_import(connection: sqlite3.Connection) -> None:
    """Remember that the profile's mastery was imported just now.

    The mastery rows carry no time of their own (`updated_at` stays empty) and
    the profile import writes no `imports` row, so without this the age of the
    mastery data is unknown. Written whether or not anything new was found:
    "nothing new" is also a fresh look. UTC, like the other meta times.
    """
    connection.execute(
        "INSERT OR REPLACE INTO meta(key, value) "
        "VALUES('mastery_imported_at', datetime('now'))")
    connection.commit()


def days_since(connection: sqlite3.Connection, stamp: str | None) -> int | None:
    """Whole days from a stored UTC time (`datetime('now')`) to now, None without one."""
    if not stamp:
        return None
    row = connection.execute(
        "SELECT CAST(julianday('now') - julianday(?) AS INTEGER) AS days",
        (stamp,)).fetchone()
    return int(row["days"]) if row and row["days"] is not None else None


def vault_data_age(connection: sqlite3.Connection) -> int | None:
    """Days since the vault status was last refreshed, or None if never.

    Vaulting rotates, so this number decides whether a "vaulted" in the
    ranking is a fact or a memory.
    """
    row = connection.execute(
        "SELECT value FROM meta WHERE key = 'vaulted_updated_at'").fetchone()
    return days_since(connection, row["value"] if row else None)


def set_relic_count(connection: sqlite3.Connection, relic_unique: str,
                    count: int, source: str = "manual") -> None:
    """Record how many of a relic are held. Zero removes the row."""
    if count <= 0:
        connection.execute("DELETE FROM owned_relics WHERE relic_unique = ?",
                           (relic_unique,))
    else:
        connection.execute(
            "INSERT INTO owned_relics(relic_unique, count, source, updated_at) "
            "VALUES(?,?,?,datetime('now')) "
            "ON CONFLICT(relic_unique) DO UPDATE SET "
            "count=excluded.count, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (relic_unique, count, source))
    connection.commit()


def relic_count(connection: sqlite3.Connection, relic_unique: str) -> int:
    row = connection.execute(
        "SELECT count FROM owned_relics WHERE relic_unique = ?",
        (relic_unique,)).fetchone()
    return int(row["count"]) if row else 0


def owned_relics(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Held relics, most held first, summed across refinements.

    Grouped by the relic itself, not by its printed name. Four refinements are
    four unique names under one printed name, so they have to be summed - a
    shelf of 12 Intact and 3 Radiant is 15 of that relic, and grouping without
    summing showed whichever row SQLite reached first.

    But one printed name can also cover two genuinely different relics: "Lith
    G12 Relic" is two Sevagoth Prime projections with different rewards.
    Grouping by name would add those together and call the result one relic.
    The identifier minus its refinement suffix is the thing itself, so that is
    what this groups by, and `variants` says when a name is shared so a caller
    can avoid presenting two different relics as one.

    The suffix is cut by name, not by character class. `RTRIM(x, 'Bronze...')`
    reads like a suffix strip and is not one - it removes *any* trailing
    character from the set, so "...YareliPrimeBBronze" comes back as
    "...Yareli" and two unrelated relics collapse into one row.
    """
    return connection.execute(
        "WITH base AS ("
        "  SELECT o.count AS count, r.name AS name, r.tier AS tier, "
        "         r.vaulted AS vaulted, r.unique_name AS unique_name, "
        "         CASE "
        "           WHEN r.unique_name LIKE '%Bronze' "
        "             THEN substr(r.unique_name, 1, length(r.unique_name) - 6) "
        "           WHEN r.unique_name LIKE '%Silver' "
        "             THEN substr(r.unique_name, 1, length(r.unique_name) - 6) "
        "           WHEN r.unique_name LIKE '%Gold' "
        "             THEN substr(r.unique_name, 1, length(r.unique_name) - 4) "
        "           WHEN r.unique_name LIKE '%Platinum' "
        "             THEN substr(r.unique_name, 1, length(r.unique_name) - 8) "
        "           ELSE r.unique_name END AS stem "
        "  FROM owned_relics o JOIN relics r ON r.unique_name = o.relic_unique "
        "  WHERE o.count > 0) "
        "SELECT MIN(unique_name) AS unique_name, name, tier, "
        "       MAX(vaulted) AS vaulted, SUM(count) AS count, "
        "       (SELECT COUNT(DISTINCT stem) FROM base b2 WHERE b2.name = base.name) "
        "         AS variants "
        "FROM base GROUP BY stem, name "
        "ORDER BY SUM(count) DESC, name").fetchall()


def find_relic_by_name(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """A relic by its printed name, for manual entry during a review."""
    # Ordered for the same reason as `relic_unique_for`, and pinned to Intact:
    # a hand-typed "Lith G12 Relic" with no refinement written is the unrefined
    # one, not whichever row the database happened to return first.
    return connection.execute(
        f"SELECT * FROM relics WHERE {_BY_NAME} "
        f"ORDER BY (unique_name LIKE '%Bronze') DESC, unique_name LIMIT 1",
        (name,)).fetchone()


def find_item_by_name(connection: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return connection.execute(
        f"SELECT * FROM items WHERE {_BY_NAME} LIMIT 1", (name,)).fetchone()


def find_relic_typed(connection: sqlite3.Connection, typed: str) -> sqlite3.Row | None:
    """The relic row for a name that may carry its refinement in brackets.

    "Meso B9 Relic [Radiant]" and "Meso B9 Relic" are the same relic at two
    refinements, and the refinement is part of the identifier. Without
    brackets it is Intact, which is what the game prints nothing for.
    """
    from wft.inventory import quantity as quantity_mod

    bare, refinement = quantity_mod.strip_refinement(typed)
    wanted = (refinement or "INTACT").upper()
    for row in connection.execute(
            f"SELECT name, unique_name FROM relics WHERE {_BY_NAME}",
            (bare.strip(),)):
        if refinement_of(row["unique_name"]).upper() == wanted:
            return row
    return None


def entry_candidates(connection: sqlite3.Connection,
                     text: str) -> list[tuple[str, sqlite3.Row]]:
    """Every holding a hand-typed name can mean, as (kind, row) pairs.

    One list for the form's holding line, its Save and its choice box, so the
    three cannot disagree. A text with "Relic" in it is a relic. Otherwise the
    part and the item of that name are looked up; a part row that is itself
    an item (Bronco Prime, a piece of Akbronco Prime) is that item, because
    built copies are what count there. A part and a different item sharing a
    name are both returned, and the caller has to ask which.
    """
    text = (text or "").strip()
    if not text:
        return []
    if re.search(r"\brelic\b", text, re.IGNORECASE):
        row = find_relic_typed(connection, text)
        return [("relic", row)] if row else []
    found: list[tuple[str, sqlite3.Row]] = []
    part = find_part_by_name(connection, text)
    item = find_item_by_name(connection, text)
    if part is not None:
        as_item = connection.execute(
            "SELECT * FROM items WHERE unique_name = ?",
            (part["unique_name"],)).fetchone()
        found.append(("item", as_item) if as_item is not None
                     else ("part", part))
    if item is not None and not any(k == "item" and r["unique_name"]
                                    == item["unique_name"] for k, r in found):
        found.append(("item", item))
    # Forma and its kind are no holding of either kind.
    return [(k, r) for k, r in found
            if not is_uncounted(connection, r["unique_name"])]


def find_entry(connection: sqlite3.Connection, text: str,
               kind: str | None = None) -> tuple[str, sqlite3.Row] | None:
    """The one holding a typed name means, or None when there is none or two.

    `kind` picks between candidates when the name is ambiguous (the form's
    "Save as" box); without it an ambiguous name resolves to nothing rather
    than to a guess. `entry_candidates` tells the two cases apart.
    """
    found = entry_candidates(connection, text)
    if kind is not None:
        found = [f for f in found if f[0] == kind]
    return found[0] if len(found) == 1 else None


def entry_count(connection: sqlite3.Connection, kind: str, row) -> int:
    """What is on record for a resolved entry, by the counter of its kind."""
    if kind == "relic":
        return relic_count(connection, row["unique_name"])
    if kind == "item":
        return item_built(connection, row["unique_name"])
    return owned_count(connection, row["unique_name"])


def set_entry_count(connection: sqlite3.Connection, kind: str, row,
                    count: int, source: str = "manual") -> None:
    """Write a resolved entry through the setter of its kind."""
    if kind == "relic":
        set_relic_count(connection, row["unique_name"], count, source)
    elif kind == "item":
        set_item_count(connection, row["unique_name"], count, source)
    else:
        set_part_count(connection, row["unique_name"], count, source)


#: Below this the widest scorer's best match is not an answer, it is the least
#: bad of four thousand. Measured: "xyzzy nonsense" peaks at 67.5 and "qqq" at
#: 45, while every name a person half-typed in testing - "neo c9", "gunsen
#: prime handl", "bronco prime barel" - sits at 90 or above.
TYPED_NAME_FLOOR = 88.0


class TypedName(NamedTuple):
    """What a hand-typed name would actually be written as."""

    kind: str            # "part" | "item" | "relic"
    unique_name: str
    name: str            # the label, refinement included
    matched: str         # the catalogue name the text resolved to
    refinement: str | None
    exact: bool          # the text named it outright, case aside


def _typed_exact(text: str, names: list[str]) -> str | None:
    folded = text.casefold()
    for name in names:
        if name.casefold() == folded:
            return name
    return None


def _typed_fuzzy(text: str, names: list[str]) -> str | None:
    """The one name a partial text points at, or nothing.

    Two scorers, and they have to agree. Neither is sufficient alone, which is
    the whole reason both are here: `WRatio` finds "Neo C9 Relic" from "neo c9"
    at 90 but leads its runner-up by only 4.5, because partial matching
    flattens the difference between the relic eras; plain `ratio` keeps the
    lead but scores the same query 66.7, below any floor that would also reject
    nonsense. Where they agree the answer has survived both a partial reading
    and a whole-string one.

    Measured against the catalogue, this refuses exactly what it should:
    "prime", "neo", "relic", "c9", "blade" and "axi a" all name too many things
    and are turned down, while "neo c9", "meso b9", "lex prime barrel" and a
    misspelt "bronco prime barel" resolve. Refusing is the safe direction: an
    unresolved name is reported at the end of the review, an
    invented one is written into the inventory.
    """
    from rapidfuzz import fuzz, process

    wide = process.extract(text, names, scorer=fuzz.WRatio,
                           processor=str.casefold, limit=2)
    if not wide or wide[0][1] < TYPED_NAME_FLOOR:
        return None
    if len(wide) > 1 and wide[1][1] == wide[0][1]:
        # Two names fit equally well. "c9" is every era's C9 relic at once.
        return None
    tight = process.extractOne(text, names, scorer=fuzz.ratio,
                               processor=str.casefold)
    if tight is None or tight[0] != wide[0][0]:
        return None
    return wide[0][0]


#: Below this score a name is not suggested for a typo.
NAME_SUGGEST_CUTOFF = 70
#: A name's beginning counts only when it is at least this close: a text being
#: typed is the start of a name, but three letters are near the start of many.
NAME_SUGGEST_PREFIX_FLOOR = 80
#: Shorter texts only get names that contain them: two letters are close to
#: everything.
NAME_SUGGEST_MIN_LENGTH = 3


#: A typed word of at least this many characters may be a misspelling of a
#: word in a name; shorter ones must be contained in it.
NAME_WORD_FUZZY_MIN_LENGTH = 4
#: How close a typed word must be to a word of a name.
NAME_WORD_CUTOFF = 80
_WORD_PUNCTUATION = "[]()&,"


@functools.lru_cache(maxsize=None)
def _name_words(folded: str) -> tuple[str, ...]:
    """The words of a casefolded name, brackets and punctuation stripped."""
    words = (w.strip(_WORD_PUNCTUATION) for w in folded.split())
    return tuple(w for w in words if w)


def _word_score(text: str, folded: str) -> float:
    """Every typed word against some word of the name; 0 if one fits nowhere.

    A typed word that is contained in a name word counts 100; one of 4 or more
    characters that is merely close (`fuzz.ratio` of 80 or more) counts that
    ratio. The result is the weakest of the typed words.
    """
    from rapidfuzz import fuzz

    words = _name_words(folded)
    if not words:
        return 0.0
    worst = 100.0
    for typed in text.split():
        best = 0.0
        for word in words:
            if typed in word:
                best = 100.0
                break
            if len(typed) >= NAME_WORD_FUZZY_MIN_LENGTH:
                ratio = fuzz.ratio(typed, word)
                if ratio >= NAME_WORD_CUTOFF and ratio > best:
                    best = ratio
        if not best:
            return 0.0
        worst = min(worst, best)
    return worst


def _score_folded(text: str, folded: str, **_kwargs) -> float:
    from rapidfuzz import fuzz

    start = fuzz.ratio(text, folded[:len(text)])
    return max(fuzz.ratio(text, folded),
               start if start >= NAME_SUGGEST_PREFIX_FLOOR else 0,
               _word_score(text, folded))


def name_score(text: str, name: str) -> float:
    """How close a typed text is to a name (case ignored), 0 to 100.

    The best of three readings:
    - the whole name (`fuzz.ratio`);
    - the name's beginning, as long as the text, counted from
      `NAME_SUGGEST_PREFIX_FLOOR` up: a name half typed ("accelat") is close
      to the start of "Acceltra Prime Barrel" and nowhere near all of it;
    - word by word: each typed word is contained in a word of the name (100)
      or, from 4 characters, within 80 of one; the weakest word counts. This is
      what lets "prime barel" find every "... Prime Barrel" and "rino" find
      Rhino, in whatever order the words were typed.
    """
    return _score_folded(text.strip().casefold(), name.casefold())


def name_matches(text: str, names) -> bool:
    """Whether a filter text fits any of `names` (strings, not joined).

    An empty text fits everything. Otherwise: the text (case ignored) is
    contained in one of the names, or, from `NAME_SUGGEST_MIN_LENGTH`
    characters, `name_score` reaches `NAME_SUGGEST_CUTOFF` for one. One
    function for every search box, so they forgive the same typos.
    """
    folded = text.strip().casefold()
    if not folded:
        return True
    names = [n.casefold() for n in names if n]
    if any(folded in n for n in names):
        return True
    if len(folded) < NAME_SUGGEST_MIN_LENGTH:
        return False
    return any(_score_folded(folded, n) >= NAME_SUGGEST_CUTOFF for n in names)


def name_suggestions(text: str, names: list[str], limit: int = 10) -> list[str]:
    """Names to offer for what is being typed, best first, at most `limit`.

    First the names that contain the text (case ignored, in the order given),
    then, for a misspelt text, the closest names by `name_score` (case
    ignored) at `NAME_SUGGEST_CUTOFF` or better. A suggestion is only ever
    offered; it is never resolved to a name here (`resolve_typed_name` and
    `entry_candidates` decide what gets written).

    Whole-string `ratio` plus the name's beginning, not `WRatio` or
    `partial_ratio`: those rate a short name hidden in a long query as perfect
    ("bronco prime barel" gives "Bronco" and "Bronco Prime" 90 to 100). Whole
    names alone missed the parts of a misspelt item ("accelatra" offered only
    Acceltra, none of its Prime parts). Measured on the 6,033 names of a
    catalogue (parts, items, relics once per refinement):
    - "accelatra": Acceltra 94.1, its Prime parts and blueprint 88.9
    - "accelat": Acceltra and all its parts 85.7
    - "bronco prime barel": Bronco Prime Barrel 97.3, then 86.5
    - "volt prime systms": Volt Prime Systems 97.1, other Systems 80 to 83.3
    - "meso b9 radiant": Meso B9 Relic [Radiant] 78.9, the other Meso relics 73.7
    - "rino prim": Rhino Prime 90.0, its parts 88.9, then 76.2 and below
    - "xyzzy": nothing reaches the cutoff
    About 5 ms per keystroke for 6,000 names.
    """
    from rapidfuzz import process

    text = text.strip()
    if not text or limit <= 0:
        return []
    folded = text.casefold()
    found = [n for n in names if folded in n.casefold()][:limit]
    if len(text) >= NAME_SUGGEST_MIN_LENGTH and len(found) < limit:
        taken = set(found)
        for name, _score, _index in process.extract(
                folded, names, scorer=_score_folded, processor=str.casefold,
                score_cutoff=NAME_SUGGEST_CUTOFF, limit=limit + len(found)):
            if name not in taken:
                found.append(name)
                taken.add(name)
                if len(found) >= limit:
                    break
    return found


def resolve_typed_name(connection: sqlite3.Connection, text: str,
                       names: list[str],
                       default_refinement: str | None = None) -> TypedName | None:
    """What a name typed during a review resolves to, or nothing.

    The one place that question is answered. It used to be two: the review
    card compared with `casefold` and said "matches Neo c9 relic - recorded as
    Radiant" in green, while applying the review looked the same text up with
    SQL equality, which is case-sensitive on TEXT. Typing
    "Neo c9 relic [radiant]", exactly as the field invited, showed it
    approved, and the player was told at the end that it matched nothing and could not be
    written. Same class of fault as the ducat page's floor, and the same cure:
    both sides call this.

    `names` is passed in rather than queried because the caller already holds
    it - the completer is built from the same list, and rebuilding it on every
    keystroke would query four thousand rows to answer a question that does
    not change.

    `default_refinement` is what a relic named without one is taken to be: the
    refinement the tiles behind a review question showed. A refinement typed
    in the text, "Intact" included, wins over it.
    """
    from wft.inventory import quantity as quantity_mod

    text = (text or "").strip()
    if not text or not names:
        return None

    # The text as typed, and the text with a refinement split off it. Both are
    # tried, whole first: a name that stands on its own is never a name plus a
    # word that happens to begin "Intact".
    readings = [(text, None)]
    bare, refinement = quantity_mod.split_typed_refinement(text)
    if refinement and bare:
        readings.append((bare, refinement))

    for query, level in readings:
        matched = _typed_exact(query, names)
        if matched is not None:
            return _typed_row(connection, matched, level or default_refinement,
                              exact=True)
    # Nothing was named outright. Now the refinement-stripped form leads: a
    # trailing "radi" is not part of any relic's name, so leaving it in only
    # makes the match worse.
    for query, level in reversed(readings):
        matched = _typed_fuzzy(query, names)
        if matched is not None:
            return _typed_row(connection, matched, level or default_refinement,
                              exact=False)
    return None


def _typed_row(connection: sqlite3.Connection, matched: str,
               refinement: str | None, exact: bool) -> TypedName | None:
    """One catalogue name, as the row the review would write.

    Parts before items before relics, which is the order the review has always
    resolved in - a finished weapon that is also a piece of another weapon is
    recorded as the piece. Forma and its kind are neither.
    """
    row = find_part_by_name(connection, matched)
    if row is not None and not is_uncounted(connection, row["unique_name"]):
        return TypedName("part", row["unique_name"], row["name"], matched,
                         None, exact)
    row = find_item_by_name(connection, matched)
    if row is not None and not is_uncounted(connection, row["unique_name"]):
        return TypedName("item", row["unique_name"], row["name"], matched,
                         None, exact)
    row = find_relic_by_name(connection, matched)
    if row is not None:
        # Refinement decides the odds and each one is its own stack, so it is
        # part of the identity here rather than a label on it.
        unique = relic_unique_for(connection, row["name"],
                                  refinement) or row["unique_name"]
        label = relic_label(row["name"], refinement)
        return TypedName("relic", unique, label, matched, refinement, exact)
    return None


def learned_ignores(connection: sqlite3.Connection) -> list[str]:
    """Readings the user has dismissed before."""
    return [row["reading"] for row in
            connection.execute("SELECT reading FROM learned_ignores")]


def learn_ignore(connection: sqlite3.Connection, reading: str) -> None:
    """Remember that this reading was not worth asking about."""
    text = (reading or "").strip()
    # Below four characters a reading is too generic to key on: it would match
    # half the queue on the next run.
    if len(text) < 4:
        return
    connection.execute(
        "INSERT OR IGNORE INTO learned_ignores(reading) VALUES(?)", (text,))
    connection.commit()


def forget_ignores(connection: sqlite3.Connection) -> int:
    """Drop every learned dismissal. Returns how many were removed."""
    removed = connection.execute("DELETE FROM learned_ignores").rowcount
    connection.commit()
    return removed


def scan_revision(connection: sqlite3.Connection) -> int | None:
    """Which version of the reader wrote the current video import."""
    row = connection.execute(
        "SELECT value FROM meta WHERE key='scan_revision'").fetchone()
    if row is None:
        return None
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return None


def set_scan_revision(connection: sqlite3.Connection, revision: int) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('scan_revision',?)",
        (str(revision),))
    connection.commit()



def set_tile_grid(connection: sqlite3.Connection, rows: float, columns: float) -> None:
    """Remember the tile grid as fractions of the frame, not as pixels.

    The user asked the right question: why measure this over and over when it
    only depends on the screen? It does not change between captures, and a
    capture too short or too empty to measure it well should not have to -
    the companions recording keeps seven frames, measured 436 pixels where the
    tiles are 444, and cut every crop two percent short.

    Stored as a fraction so it survives a change of resolution: 0.206 of the
    frame height on this monitor is 444 pixels at 4K and 296 at 1440p.
    """
    connection.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES('tile_grid',?)",
        (f"{rows:.6f},{columns:.6f}",))
    connection.commit()


def tile_grid(connection: sqlite3.Connection) -> tuple[float, float] | None:
    """The remembered tile grid, as fractions of frame height and width."""
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='tile_grid'").fetchone()
    except Exception:  # noqa: BLE001 - a scan must not die of a missing table
        return None
    if not row or not row[0]:
        return None
    try:
        rows, columns = (float(part) for part in str(row[0]).split(","))
    except ValueError:
        return None
    return rows, columns


def stale_scan(connection: sqlite3.Connection, current: int) -> tuple[bool, int | None]:
    """Whether the stored numbers predate the reader now installed.

    Answers the question the tool could ask and never did: the database holds
    whatever the last import wrote, and a correction to the reader does not
    reach back into it. Someone has to be told to scan again, and until now
    nobody was.
    """
    stored = scan_revision(connection)
    if stored is None:
        # Nothing recorded from a scan yet, or a database from before revisions
        # were tracked. Either way there is nothing to be stale.
        holdings = connection.execute(
            "SELECT COUNT(*) FROM owned_parts WHERE source='video'").fetchone()[0]
        return bool(holdings), None
    return stored < current, stored


def recorded_from(connection: sqlite3.Connection, source: str) -> dict:
    """Everything currently held from one source, as {kind: {unique: count}}.

    Needed to answer what a new scan did *not* see. A capture is a photo of the
    whole inventory, so a row it has nothing to say about is a row that is no
    longer there - but only the previous numbers can tell which rows those are.
    """
    return {
        "part": {r["part_unique"]: r["count"] for r in connection.execute(
            "SELECT part_unique, count FROM owned_parts "
            "WHERE source = ? AND count > 0", (source,))},
        "item": {r["item_unique"]: r["count"] for r in connection.execute(
            "SELECT item_unique, count FROM owned_items "
            "WHERE source = ? AND count > 0", (source,))},
        "relic": {r["relic_unique"]: r["count"] for r in connection.execute(
            "SELECT relic_unique, count FROM owned_relics "
            "WHERE source = ? AND count > 0", (source,))},
    }



def recorded_with_source(connection: sqlite3.Connection) -> dict:
    """Everything currently held above 0, from any source, as
    {kind: {unique: (count, source)}}.

    What `recorded_from` does for one source, for all of them: a capture
    shows the current state of the categories it covers, whoever wrote the
    number before. Rows at 0 (mastery only) are not in it.
    """
    return {
        "part": {r["part_unique"]: (r["count"], r["source"])
                 for r in connection.execute(
                     "SELECT part_unique, count, source FROM owned_parts "
                     "WHERE count > 0")},
        "item": {r["item_unique"]: (r["count"], r["source"])
                 for r in connection.execute(
                     "SELECT item_unique, count, source FROM owned_items "
                     "WHERE count > 0")},
        "relic": {r["relic_unique"]: (r["count"], r["source"])
                  for r in connection.execute(
                      "SELECT relic_unique, count, source FROM owned_relics "
                      "WHERE count > 0")},
    }


def categories_of(connection: sqlite3.Connection, entries) -> dict:
    """Map (kind, unique_name) to the inventory category it is filed under.

    The category is what a capture actually covers. Warframe files its
    inventory by tab - Warframes, Primary, Melee, Relics - and a recording is
    of one tab or a few, never of everything. Asking "did this capture see it"
    at the level of part-versus-item cannot tell a Warframes recording from a
    weapons one, so a Warframes capture looked like evidence about every weapon
    the user owns.

    Relics are their own category. They are neither a component nor a built
    thing and they live in their own tab.
    """
    wanted_parts = [u for kind, u in entries if kind == "part"]
    wanted_items = [u for kind, u in entries if kind == "item"]
    found: dict[tuple[str, str], str] = {}

    def chunks(values):
        for start in range(0, len(values), 400):
            yield values[start:start + 400]

    for batch in chunks(wanted_parts):
        marks = ",".join("?" * len(batch))
        for row in connection.execute(
                f"SELECT p.unique_name, i.category FROM parts p "
                f"LEFT JOIN items i ON i.unique_name = p.item_unique "
                f"WHERE p.unique_name IN ({marks})", batch):
            found[("part", row["unique_name"])] = row["category"] or "unknown"
    for batch in chunks(wanted_items):
        marks = ",".join("?" * len(batch))
        for row in connection.execute(
                f"SELECT unique_name, category FROM items "
                f"WHERE unique_name IN ({marks})", batch):
            found[("item", row["unique_name"])] = row["category"] or "unknown"
    for kind, unique in entries:
        if kind == "relic":
            found[(kind, unique)] = "relic"
        found.setdefault((kind, unique), "unknown")
    return found


def name_for(connection: sqlite3.Connection, kind: str, unique_name: str) -> str:
    """A printable name for one identifier, or the identifier if it is unknown."""
    table = {"part": "parts", "item": "items", "relic": "relics"}.get(kind)
    if not table:
        return unique_name
    row = connection.execute(
        f"SELECT name FROM {table} WHERE unique_name = ?", (unique_name,)).fetchone()
    if not row:
        return unique_name
    if kind == "relic":
        # Each refinement is its own stack, so the name says which one.
        return relic_label(row["name"], refinement_of(unique_name))
    return row["name"]


def bare_relic_name(connection: sqlite3.Connection, unique_name: str) -> str:
    """A relic's printed name without its refinement, or the identifier."""
    row = connection.execute(
        "SELECT name FROM relics WHERE unique_name = ?", (unique_name,)).fetchone()
    return row["name"] if row else unique_name


def inventory_summary(connection: sqlite3.Connection) -> dict:
    """Counts by source, for showing what the holdings rest on."""
    summary: dict = {"parts": {}, "items": {}, "part_total": 0, "item_total": 0}
    for row in connection.execute(
            "SELECT source, COUNT(*) c, SUM(count) n FROM owned_parts GROUP BY source"):
        summary["parts"][row["source"]] = (row["c"], row["n"])
        summary["part_total"] += row["n"] or 0
    for row in connection.execute(
            "SELECT source, COUNT(*) c, SUM(count) n FROM owned_items GROUP BY source"):
        summary["items"][row["source"]] = (row["c"], row["n"])
        summary["item_total"] += row["n"] or 0
    return summary

# -- the import journal ---------------------------------------------------
#
# The inventory tables hold one number per thing: the latest. That is enough to
# answer "what do I have" and nothing else. It could not answer "what did that
# import change", "was that number always wrong", or "take that back" - and the
# one wrong number this tool has produced, a stack of 12 stored as 2, was
# invisible for exactly that reason. There was no 12 anywhere to compare against.
#
# An import now opens a row, writes what it changed along with what stood there
# before, and closes. Undo is replaying `before`.


def start_import(connection: sqlite3.Connection, source: str, kind: str = "scan",
                 scan_revision: int | None = None, tiles: int | None = None,
                 questions: int | None = None, note: str = "") -> int:
    """Open a journal entry and return its id."""
    cursor = connection.execute(
        "INSERT INTO imports (source, kind, scan_revision, tiles, questions, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, kind, scan_revision, tiles, questions, note))
    connection.commit()
    return int(cursor.lastrowid)


def record_observation(connection: sqlite3.Connection, import_id: int, kind: str,
                       unique_name: str, before, after,
                       reading: str = "", score: float | None = None,
                       before_source: str | None = None) -> None:
    """Note one thing an import wrote, and what it replaced.

    Replaces rather than adds on conflict: an import writes a given thing once,
    and a second write for the same thing within one import is a correction of
    the first - keeping both would make the undo ambiguous about which `before`
    is the real one.
    """
    connection.execute(
        "INSERT INTO observations "
        "(import_id, kind, unique_name, before, after, reading, score, "
        " before_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(import_id, kind, unique_name) DO UPDATE SET "
        "  after = excluded.after, reading = excluded.reading, "
        "  score = excluded.score",
        (import_id, kind, unique_name, before, after, reading, score,
         before_source))



def source_of(connection: sqlite3.Connection, kind: str, unique_name: str) -> str | None:
    """Which source a stored row carries, or None if there is no row.

    Read before an import overwrites it, so taking that import back can put the
    stamp as well as the number back. The stamp is not decoration: the next
    import reads it to work out which categories a previous recording covered.
    """
    table, column = {
        "part": ("owned_parts", "part_unique"),
        "item": ("owned_items", "item_unique"),
        "relic": ("owned_relics", "relic_unique"),
    }.get(kind, (None, None))
    if table is None:
        return None
    name = canonical(connection, unique_name) if kind == "part" else unique_name
    row = connection.execute(
        f"SELECT source FROM {table} WHERE {column} = ?", (name,)).fetchone()
    return row["source"] if row else None



# -- pictures --------------------------------------------------------------

#: How wide a stored thumbnail is. Big enough to recognise across a list at the
#: user's 20pt text, small enough that two hundred of them are a couple of
#: megabytes rather than fifty.
THUMBNAIL_WIDTH = 96


def set_thumbnail(connection: sqlite3.Connection, kind: str, unique_name: str,
                  image: bytes, source: str = "video") -> None:
    """Keep a picture of one thing, replacing any older one."""
    name = canonical(connection, unique_name) if kind == "part" else unique_name
    connection.execute(
        "INSERT INTO thumbnails (unique_name, kind, image, source, updated_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(unique_name) DO UPDATE SET "
        "  image = excluded.image, source = excluded.source, "
        "  updated_at = excluded.updated_at",
        (name, kind, image, source))


def thumbnail(connection: sqlite3.Connection, kind: str,
              unique_name: str) -> bytes | None:
    """The picture for one thing, or None.

    Falls back from a part to its item: a set's blueprint and its chassis are
    different tiles with different artwork, but a list of *sets* wants the one
    picture that stands for the set, and the first part scanned is as good a
    stand-in as any.
    """
    name = canonical(connection, unique_name) if kind == "part" else unique_name
    row = connection.execute(
        "SELECT image FROM thumbnails WHERE unique_name = ?", (name,)).fetchone()
    if row:
        return row["image"]
    if kind == "item":
        row = connection.execute(
            "SELECT t.image FROM thumbnails t JOIN parts p "
            "ON p.unique_name = t.unique_name "
            "WHERE p.item_unique = ? ORDER BY p.name LIMIT 1",
            (unique_name,)).fetchone()
        if row:
            return row["image"]
    return None



def set_texture(connection: sqlite3.Connection, unique_name: str,
                location: str) -> None:
    connection.execute(
        "INSERT INTO textures (unique_name, location) VALUES (?, ?) "
        "ON CONFLICT(unique_name) DO UPDATE SET location = excluded.location",
        (unique_name, location))


def texture(connection: sqlite3.Connection, unique_name: str) -> str | None:
    row = connection.execute(
        "SELECT location FROM textures WHERE unique_name = ?",
        (unique_name,)).fetchone()
    return row["location"] if row else None


def missing_pictures(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Things that have an icon on record and no picture stored yet.

    Only things the user actually holds or has mastered. Nineteen thousand
    icons exist; a few hundred are of any use here, and asking DE's host for
    the rest would be nineteen thousand requests for nothing.
    """
    return connection.execute(
        "SELECT t.unique_name, t.location, 'item' AS kind FROM textures t "
        "WHERE t.unique_name IN (SELECT item_unique FROM owned_items) "
        "  AND t.unique_name NOT IN (SELECT unique_name FROM thumbnails) "
        "UNION ALL "
        "SELECT t.unique_name, t.location, 'part' AS kind FROM textures t "
        "WHERE t.unique_name IN (SELECT part_unique FROM owned_parts) "
        "  AND t.unique_name NOT IN (SELECT unique_name FROM thumbnails) "
        "UNION ALL "
        "SELECT t.unique_name, t.location, 'relic' AS kind FROM textures t "
        "WHERE t.unique_name IN (SELECT relic_unique FROM owned_relics) "
        "  AND t.unique_name NOT IN (SELECT unique_name FROM thumbnails)"
    ).fetchall()


def thumbnail_count(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT COUNT(*) FROM thumbnails").fetchone()[0]


def recent_imports(connection: sqlite3.Connection, limit: int = 12) -> list[sqlite3.Row]:
    """The journal, newest first, with how much each one moved."""
    return connection.execute(
        "SELECT i.*, "
        "  (SELECT COUNT(*) FROM observations o WHERE o.import_id = i.id) AS rows, "
        "  (SELECT COUNT(*) FROM observations o WHERE o.import_id = i.id "
        "     AND COALESCE(o.before, 0) <> COALESCE(o.after, 0)) AS changed "
        "FROM imports i ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()


def import_rows(connection: sqlite3.Connection, import_id: int) -> list[sqlite3.Row]:
    """What one import wrote, biggest change first.

    Biggest first because that is the order a mistake is found in: a count that
    moved by ten is worth a second look, one that moved by one is the tool
    working.
    """
    return connection.execute(
        "SELECT * FROM observations WHERE import_id = ? "
        "ORDER BY ABS(COALESCE(after, 0) - COALESCE(before, 0)) DESC, unique_name",
        (import_id,)).fetchall()


def undo_import(connection: sqlite3.Connection, import_id: int) -> int:
    """Put back what an import replaced, and say how many rows moved.

    Only where the current value is still what the import wrote. Anything
    changed since - by hand, by a relic pick, by a later import - is left
    alone: undoing an import should not quietly undo the work that came after
    it. That is also why this does not delete the journal entry; it marks it
    undone, so the history stays readable.
    """
    already = connection.execute(
        "SELECT undone_at FROM imports WHERE id = ?", (import_id,)).fetchone()
    if already is None or already["undone_at"]:
        # Taking one back twice is not a second undo, it is a chance to undo
        # somebody else's work. Counts are protected by the value check below;
        # mastery is not, and clearing a flag the user has since set by hand is
        # exactly the kind of quiet damage this whole journal exists to stop.
        return 0

    restored = 0
    for row in connection.execute(
            "SELECT kind, unique_name, before, after, before_source "
            "FROM observations WHERE import_id = ?", (import_id,)).fetchall():
        kind, unique_name = row["kind"], row["unique_name"]
        before, after = row["before"], row["after"]
        if before == after:
            # The import wrote what was already there. Writing it again would
            # change nothing except the row's source, and that stamp is what
            # the next import reads to work out which categories a previous
            # recording covered - so restamping every untouched row as "undo"
            # blinded it completely.
            continue
        if kind == "mastery":
            if before:
                continue
            # Mastery is granted by any scan that saw rank 30 and never
            # withdrawn, so a later import can be independent evidence for the
            # same flag. Clearing it because *this* import is being taken back
            # would drop a fact something else also established.
            elsewhere = connection.execute(
                "SELECT 1 FROM observations o JOIN imports i ON i.id = o.import_id "
                "WHERE o.kind = 'mastery' AND o.unique_name = ? "
                "AND o.import_id <> ? AND o.after = 1 AND i.undone_at IS NULL "
                "LIMIT 1", (unique_name, import_id)).fetchone()
            if elsewhere:
                continue
            connection.execute(
                "UPDATE owned_items SET mastered = 0 WHERE item_unique = ?",
                (unique_name,))
            restored += 1
            continue
        current = (relic_count(connection, unique_name) if kind == "relic"
                   else owned_count(connection, unique_name) if kind == "part"
                   else item_built(connection, unique_name))
        if current != after:
            continue
        # Back to the source it carried before, not to "undo". The source is
        # bookkeeping the next import depends on.
        source = row["before_source"] or "manual"
        if kind == "relic":
            set_relic_count(connection, unique_name, before or 0, source)
        elif kind == "part":
            set_part_count(connection, unique_name, before or 0, source)
        else:
            set_item_count(connection, unique_name, before or 0, source)
        restored += 1
    connection.execute(
        "UPDATE imports SET undone_at = datetime('now') WHERE id = ?", (import_id,))
    connection.commit()
    return restored

