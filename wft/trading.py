"""The two trading lists: Prime parts I still need, and Prime parts I can offer.

One source for everything the lists become - the table on the page, the chat
message, and the CSV and HTML later. Each is a view of the same `Row` list, so
a number in the message cannot differ from the number in the table. No Qt here.

Prime only: the offer list reads `db.spare_parts`, which also reports plain
parts (a Cedo Barrel has no ducat value and nobody trades it), and the survey
lists every tracked set (a Sibear Blueprint). Both are cut to `items.is_prime`.

The want list follows the format an earlier standalone script used for this
message: a set that is a component of another open set is counted once,
on the parent's line ("needs 2x Magnus Prime"), never twice.
"""
from __future__ import annotations

import csv
import datetime as dt
import html
import io
import sqlite3
from dataclasses import dataclass
from typing import NamedTuple

from wft.data import db as dbq
from wft.data.survey import set_survey
from wft.market import client as market

#: Tracker category -> heading, in the order the message lists them. Anything
#: else (Exalted weapons without parts of their own, Kavasa Prime) is not traded.
CATEGORIES = {
    "warframe": "Warframes", "primary": "Primary", "secondary": "Secondary",
    "melee": "Melee", "companion": "Companions",
    "companion_weapon": "Companion Weapons", "archwing": "Archwing",
    "archgun": "Arch-Gun",
}

#: What the list is traded for. Value -> the label the page shows. "ducats":
#: other parts of the same ducat value; "platinum"; "both".
TRADE_FOR = {
    "ducats": "Parts (same ducat value)",
    "platinum": "Platinum",
    "both": "Parts or platinum",
}
DEFAULT_TRADE_FOR = "ducats"

#: Message texts that do not depend on the setting (English: the message goes
#: to other players). Title, terms and the bracket sentence are made by
#: `title`, `terms` and `bracket_sentence` from the setting.
TEXTS = {
    "as_of": "as of",
    "needs": "needs {n}x {item}",
}

_TITLES = {
    ("offer", "ducats"): "Prime parts I can give away",
    ("offer", "platinum"): "Prime parts I sell",
    ("offer", "both"): "Prime parts I can give away or sell",
}
_TERMS = {
    ("want", "ducats"): ("In exchange I give one of my spare parts (see my other "
                         "list) or parts of the same ducat value."),
    ("want", "platinum"): "I pay platinum.",
    ("want", "both"): ("In exchange I give one of my spare parts (see my other "
                       "list), parts of the same ducat value, or platinum."),
    ("offer", "ducats"): ("In exchange I want either one of my missing parts or "
                          "parts of the same ducat value."),
    ("offer", "platinum"): "For platinum.",
    ("offer", "both"): ("In exchange I want one of my missing parts, parts of "
                        "the same ducat value, or platinum."),
}


@dataclass(frozen=True)
class Row:
    """One part on one list."""
    category: str
    item: str
    item_unique: str
    part_unique: str
    part: str                 # name without the item's name: "Blueprint"
    count: int                # missing (want) or spare (offer)
    ducats: int | None        # per piece; None when the kiosk takes none
    sell_min: int | None      # cheapest seller's ask, last stored, any age
    price_at: str | None      # when that was stored (UTC), None without a price
    vaulted: bool | None
    held: int                 # pieces of the set held ...
    total: int                # ... of pieces it takes
    mastered: bool
    needs: tuple = ()         # ((n, "Magnus Prime"), ...) for component items


class DataAge(NamedTuple):
    stock_changed: str | None      # last change to the stock, UTC
    mastery_imported: str | None   # last mastery import from the profile, UTC


# -- shared helpers ----------------------------------------------------------
def _short(part_name: str, item_name: str) -> str:
    prefix = item_name + " "
    return part_name[len(prefix):] if part_name.startswith(prefix) else part_name


def _prime_items(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    return {r["unique_name"]: (r["name"], r["category"]) for r in conn.execute(
        "SELECT unique_name, name, category FROM items WHERE is_prime = 1")}


def _row(conn: sqlite3.Connection, item_unique: str, item: str, category: str,
         part_unique: str, part_name: str, count: int, held: int, total: int,
         mastered: bool, needs: tuple = ()) -> Row:
    slug = market.slug_for(conn, part_unique)
    price = market.stored_price(conn, slug) if slug else None
    return Row(
        category=category, item=item, item_unique=item_unique,
        part_unique=part_unique, part=_short(part_name, item), count=count,
        ducats=dbq.ducats(conn, part_unique) or None,
        sell_min=price["sell_min"] if price else None,
        price_at=price["fetched_at"] if price and price["sell_min"] is not None else None,
        vaulted=dbq.vault_state(conn, part_unique)[0],
        held=held, total=total, mastered=mastered, needs=needs)


def _order(rows: list[Row]) -> list[Row]:
    rank = {k: i for i, k in enumerate(CATEGORIES)}
    return sorted(rows, key=lambda r: (rank[r.category], r.item, r.item_unique, r.part))


def _wanted_category(categories) -> set:
    return set(CATEGORIES) if categories is None else set(categories) & set(CATEGORIES)


# -- the want list -----------------------------------------------------------
def wanted_rows(conn: sqlite3.Connection, *, max_missing: int = 2,
                only_unmastered: bool = True, categories=None,
                vaulted_only: bool = False) -> list[Row]:
    """The rows of `want_list`, without the count of hidden sets."""
    return want_list(conn, max_missing=max_missing, only_unmastered=only_unmastered,
                     categories=categories, vaulted_only=vaulted_only)[0]


def want_list(conn: sqlite3.Connection, *, max_missing: int = 2,
              only_unmastered: bool = True, categories=None,
              vaulted_only: bool = False) -> tuple[list[Row], int]:
    """(rows, hidden): the want list, and how many sets `max_missing` hides.

    The hidden sets are those the same call lists with no limit and not with
    this one, so the count and the rows cannot disagree.
    """
    kept = _want_rows(conn, max_missing, only_unmastered, categories, vaulted_only)
    if not max_missing:
        return kept, 0
    everything = _want_rows(conn, 0, only_unmastered, categories, vaulted_only)
    shown = {r.item_unique for r in kept}
    return kept, len({r.item_unique for r in everything} - shown)


def _want_rows(conn: sqlite3.Connection, max_missing: int, only_unmastered: bool,
               categories, vaulted_only: bool) -> list[Row]:
    """Parts of Prime sets that are 1 to `max_missing` pieces short.

    `max_missing` 0 is no limit: every open set with a piece missing, including
    sets of which nothing is held. Parts that cannot be traded
    (`db.is_tradable_part`) are not asked for; a set with no tradable piece
    missing has no row.

    Built items never appear: a built item owes nothing. With `only_unmastered`
    mastered sets are left out too. A piece that is itself a built item
    (Magnus Prime inside Akmagnus Prime) is not asked for; the pieces it is
    built from are, for the copies still to be built, and the line says what it
    needs. The component's own line is dropped when an open set above it already
    covers it - decided before the category and vaulted filters, so hiding the
    parent's category does not bring the component back as a second line.
    """
    prime = _prime_items(conn)
    has_parts = {r[0] for r in conn.execute(
        "SELECT DISTINCT item_unique FROM parts WHERE alias_of IS NULL")}
    lines = []                       # (entry, [(part row, count)], needs)
    covered = set()
    for entry in set_survey(conn, include_unstarted=True):
        unique = entry["unique"]
        if (unique not in prime or entry["category"] not in CATEGORIES
                or entry["built"] or (only_unmastered and entry["mastered"])):
            continue
        gaps, needs = [], []
        for part, lacking in dbq.pieces_short_for(conn, unique):
            if part["unique_name"] in prime and part["unique_name"] in has_parts:
                needs.append((int(part["count_needed"]), part["unique_name"]))
                gaps.extend(dbq.pieces_short_for(conn, part["unique_name"], lacking))
            else:
                gaps.append((part, lacking))
        gaps = [(p, n) for p, n in gaps if dbq.is_tradable_part(conn, p["unique_name"])]
        total = sum(n for _, n in gaps)
        if total >= 1 and (not max_missing or total <= max_missing):
            lines.append((entry, gaps, needs))
            covered.update(u for _, u in needs)

    wanted = _wanted_category(categories)
    rows = []
    for entry, gaps, needs in lines:
        if entry["unique"] in covered or entry["category"] not in wanted:
            continue
        note = tuple((n, prime[u][0]) for n, u in needs)
        item_rows = [
            _row(conn, entry["unique"], entry["name"], entry["category"],
                 part["unique_name"], part["name"], n, entry["held"],
                 entry["total"], entry["mastered"], note)
            for part, n in gaps]
        if vaulted_only:
            item_rows = [r for r in item_rows if r.vaulted is True]
        rows.extend(item_rows)
    return _order(rows)


# -- the offer list ----------------------------------------------------------
def offer_rows(conn: sqlite3.Connection, *, max_platinum: int = 0,
               include_unpriced: bool | None = None, categories=None) -> list[Row]:
    """Prime parts above what any open set still needs.

    The source is `db.spare_parts`, the one the Ducats page and `trade_spare`
    use, so the list never offers a piece the write would refuse. Parts the
    kiosk takes nothing for stay (they trade between players), without a
    bracket in the message.

    `max_platinum` > 0 keeps parts whose last stored cheapest ask is at most
    that, whatever its age; parts without a stored ask follow `include_unpriced`
    (default: kept without a limit, dropped with one).
    """
    if include_unpriced is None:
        include_unpriced = not max_platinum
    prime = _prime_items(conn)
    item_uniques = {r[0] for r in conn.execute("SELECT unique_name FROM items")}
    wanted = _wanted_category(categories)
    owner = dict(conn.execute("SELECT unique_name, item_unique FROM parts"))
    rows = []
    for spare in dbq.spare_parts(conn):
        unique = spare["unique_name"]
        item_unique = owner.get(unique)
        if item_unique not in prime or unique in item_uniques:
            continue                 # not Prime, or a built item (not tradeable)
        if not dbq.is_tradable_part(conn, unique):
            continue                 # no ducat value, not built: players cannot trade it
        name, category = prime[item_unique]
        if category not in wanted:
            continue
        held, total = dbq.set_progress(conn, item_unique)
        row = _row(conn, item_unique, name, category, unique, spare["name"],
                   int(spare["spare"]), held, total,
                   dbq.is_mastered(conn, item_unique))
        if row.sell_min is None:
            if not include_unpriced:
                continue
        elif max_platinum and row.sell_min > max_platinum:
            continue
        rows.append(row)
    return _order(rows)


def parts_to_price(conn: sqlite3.Connection, rows: list[Row]) -> list[str]:
    """Market slugs a price lookup for these rows would ask about.

    Pass the list before the price filter: the filter can only see prices that
    exist, so counting after it would never ask about the parts that have none.
    Missing and older than `FRESH_HOURS` are asked, one request per slug; the
    rule is `market.unpriced`, the same the Ducats page counts with.
    """
    uniques = market.unpriced(conn, [r.part_unique for r in rows])
    return [market.slug_for(conn, u) for u in uniques]


def data_age(conn: sqlite3.Connection) -> DataAge:
    """When the stock last changed and when mastery was last imported."""
    stock = conn.execute(
        "SELECT MAX(written_at) FROM imports WHERE undone_at IS NULL").fetchone()[0]
    mastery = conn.execute(
        "SELECT value FROM meta WHERE key = 'mastery_imported_at'").fetchone()
    return DataAge(stock, mastery[0] if mastery else None)


# -- the message -------------------------------------------------------------
def trade_for_or_default(value) -> str:
    """`value` when it is one of `TRADE_FOR`, else the default."""
    return value if isinstance(value, str) and value in TRADE_FOR else DEFAULT_TRADE_FOR


def _check_kind(kind: str) -> None:
    if kind not in ("want", "offer"):
        raise ValueError(f"kind is 'want' or 'offer', not {kind!r}")


def title(kind: str, trade_for: str, max_missing: int = 2) -> str:
    """The message's title. The want list names the limit it was cut at."""
    _check_kind(kind)
    trade_for = trade_for_or_default(trade_for)
    if kind == "want":
        if not max_missing:
            return "Prime sets I still need parts for"
        if max_missing == 1:
            return "Prime sets I still need 1 part for"
        return f"Prime sets I still need 1-{max_missing} parts for"
    return _TITLES[(kind, trade_for)]


def terms(kind: str, trade_for: str) -> str:
    """The one sentence saying what the other player gets or gives."""
    _check_kind(kind)
    return _TERMS[(kind, trade_for_or_default(trade_for))]


def _local_date(stamp: str | None, tz: dt.tzinfo | None = None) -> dt.date | None:
    when = _stamp(stamp)
    return when.astimezone(tz).date() if when else None


def price_dates(rows: list[Row], tz: dt.tzinfo | None = None) -> str:
    """The day the shown prices were checked: "2026-10-21", or the span
    "2026-10-18 to 2026-10-21"; "" when no row has a price. Local dates (`tz`
    None is the machine's time zone) of the stored UTC times."""
    days = sorted({d for r in rows if r.sell_min is not None
                   if (d := _local_date(r.price_at, tz)) is not None})
    if not days:
        return ""
    if days[0] == days[-1]:
        return f"{days[0]:%Y-%m-%d}"
    return f"{days[0]:%Y-%m-%d} to {days[-1]:%Y-%m-%d}"


def missing_prices(rows: list[Row]) -> int:
    """How many rows have no stored price."""
    return sum(1 for r in rows if r.sell_min is None)


def bracket_sentence(rows: list[Row], trade_for: str,
                     tz: dt.tzinfo | None = None) -> str:
    """What the brackets in the message hold. Without a price on any shown row
    there is nothing to say about platinum."""
    trade_for = trade_for_or_default(trade_for)
    dates = price_dates(rows, tz)
    ducats = "Ducats per part in brackets."
    if trade_for == "ducats" or (trade_for == "both" and not dates):
        return ducats
    if not dates:
        return ""
    if trade_for == "platinum":
        return ("Platinum per part in brackets: the cheapest seller on "
                f"warframe.market, checked {dates}.")
    return ("Ducats and platinum per part in brackets; platinum is the cheapest "
            f"seller on warframe.market, checked {dates}.")


def _bracket(row: Row, trade_for: str) -> str:
    bits = []
    if trade_for in ("ducats", "both") and row.ducats is not None:
        bits.append(str(row.ducats))
    if trade_for in ("platinum", "both") and row.sell_min is not None:
        bits.append(f"{row.sell_min}p")
    return f" ({', '.join(bits)})" if bits else ""


def to_markdown(rows: list[Row], kind: str, *, trade_for: str = DEFAULT_TRADE_FOR,
                max_missing: int = 2, as_of: dt.date, texts: dict | None = None,
                tz: dt.tzinfo | None = None) -> str:
    """The chat message, as Markdown, in one piece.

    `kind` is "want" or "offer"; only the want list says what a component item
    needs. Bold title with the date, the terms and the bracket sentence under
    it, then a bold heading per category and one line per set:
    `- Item: Part (45), 2x Part (100)`. Title, terms and brackets follow
    `trade_for`; `max_missing` is the want list's limit, named in its title.
    """
    _check_kind(kind)
    trade_for = trade_for_or_default(trade_for)
    texts = {**TEXTS, **(texts or {})}
    head = (f"**{title(kind, trade_for, max_missing)}** "
            f"({texts['as_of']} {as_of:%Y-%m-%d})")
    line = " ".join(x for x in (terms(kind, trade_for),
                                bracket_sentence(rows, trade_for, tz)) if x)
    head += "\n" + line
    blocks = [head]
    for category, heading in CATEGORIES.items():
        lines, items = [], {}
        for row in rows:
            if row.category == category:
                items.setdefault(row.item_unique, []).append(row)
        if not items:
            continue
        for item_rows in items.values():
            first = item_rows[0]
            note = ""
            if kind == "want":
                note = "".join(" (" + texts["needs"].format(n=n, item=item) + ")"
                               for n, item in first.needs)
            parts = ", ".join(
                (f"{r.count}x {r.part}" if r.count > 1 else r.part)
                + _bracket(r, trade_for) for r in item_rows)
            lines.append(f"- {first.item}{note}: {parts}")
        blocks.append("\n".join([f"**{heading}**"] + lines))
    return "\n\n".join(blocks) + "\n"


# -- the table file and the web page -----------------------------------------
def _stamp(stamp: str | None) -> dt.datetime | None:
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def age_words(stamp: str | None, now: dt.datetime | None = None) -> str:
    """"today", "1 d ago", "3 d ago" for a stored UTC time; "" without one."""
    when = _stamp(stamp)
    if when is None:
        return ""
    now = now or dt.datetime.now(dt.timezone.utc)
    days = max(0, (now - when).days)
    return "today" if days == 0 else f"{days} d ago"


def need_text(row: Row, texts: dict | None = None) -> str:
    """"needs 2x Magnus Prime" for a component item, "" otherwise."""
    template = {**TEXTS, **(texts or {})}["needs"]
    return ", ".join(template.format(n=n, item=item) for n, item in row.needs)


def _yes_no(value: bool | None) -> str:
    return "" if value is None else ("yes" if value else "no")


CSV_COLUMNS = ["list", "category", "item", "part", "count", "ducats", "platinum",
               "platinum_checked", "vaulted", "set_held", "set_total", "mastered"]


def to_csv(rows: list[Row], kind: str) -> str:
    """The rows as CSV for a spreadsheet: one line per part, numbers plain.

    Comma-separated, `\n` line ends, a header line. Empty cells for what is not
    known (no ducat value, no stored price). `platinum_checked` is the date the
    stored price was fetched. The want list adds a `needs` column. Save it as
    UTF-8 without a byte-order mark, with `newline=""`.
    """
    _check_kind(kind)
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS + (["needs"] if kind == "want" else []))
    for r in rows:
        when = _stamp(r.price_at)
        line = [kind, CATEGORIES[r.category], r.item, r.part, r.count,
                "" if r.ducats is None else r.ducats,
                "" if r.sell_min is None else r.sell_min,
                f"{when:%Y-%m-%d}" if when else "",
                _yes_no(r.vaulted), r.held, r.total, _yes_no(r.mastered)]
        if kind == "want":
            line.append(need_text(r))
        writer.writerow(line)
    return out.getvalue()


#: The colours `to_html` takes, as `{name: "#rrggbb"}`; the page passes the
#: active theme's. Printing ignores them.
HTML_COLOURS = ("bg", "card", "border", "text", "dim", "accent")


def to_html(rows: list[Row], kind: str, *, trade_for: str = DEFAULT_TRADE_FOR,
            max_missing: int = 2, as_of: dt.date, colours: dict,
            texts: dict | None = None, now: dt.datetime | None = None,
            tz: dt.tzinfo | None = None) -> str:
    """The same rows as one standalone web page.

    Nothing outside the file: CSS is inline, no script, no font or image from
    anywhere. A heading and a small table per category, in the order of the
    message; every text from outside is escaped. Printing gives dark text on
    white whatever the colours say. Title, terms and the columns follow
    `trade_for`, as in the message.
    """
    _check_kind(kind)
    trade_for = trade_for_or_default(trade_for)
    show_ducats = trade_for in ("ducats", "both")
    show_platinum = trade_for in ("platinum", "both")
    page_title = title(kind, trade_for, max_missing)
    texts = {**TEXTS, **(texts or {})}
    esc = html.escape
    missing = [k for k in HTML_COLOURS if k not in colours]
    if missing:
        raise ValueError(f"colours lacks {missing}")
    c = {k: esc(str(v), quote=True) for k, v in colours.items()}
    css = f"""
body {{ margin: 0; background: {c['bg']}; color: {c['text']};
  font: 16px/1.45 "Noto Sans", "DejaVu Sans", sans-serif; }}
main {{ max-width: 52rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }}
h1 {{ margin: 0 0 .2rem; font-size: 1.5rem; color: {c['accent']}; }}
.asof {{ color: {c['dim']}; margin: 0 0 .6rem; }}
.terms {{ margin: 0 0 .4rem; }}
h2 {{ margin: 1.6rem 0 .4rem; font-size: 1.1rem; color: {c['accent']}; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed;
  background: {c['card']}; }}
col.item {{ width: 36%; }} col.part {{ width: 24%; }}
th, td {{ text-align: left; padding: .35rem .6rem;
  border-bottom: 1px solid {c['border']}; vertical-align: top; }}
th {{ color: {c['dim']}; font-weight: 600; font-size: .85rem; }}
td.num, th.num {{ text-align: right; white-space: nowrap; }}
.note {{ display: block; color: {c['dim']}; font-size: .85rem; }}
.empty {{ color: {c['dim']}; }}
@media print {{
  body {{ background: #fff; color: #000; font-size: 11pt; }}
  main {{ max-width: none; padding: 0; }}
  h1, h2 {{ color: #000; }}
  .asof, .note, .empty, th {{ color: #333; }}
  table {{ background: #fff; }}
  th, td {{ border-bottom: 1px solid #999; }}
  h2 {{ break-after: avoid; }}
  tr {{ break-inside: avoid; }}
}}
"""
    head_cells = ["Item", "Part", "Count"] + (["Ducats"] if show_ducats else []) \
        + (["Platinum"] if show_platinum else [])
    sections = []
    for category, heading in CATEGORIES.items():
        mine = [r for r in rows if r.category == category]
        if not mine:
            continue
        body = []
        for r in mine:
            note = need_text(r, texts) if kind == "want" else ""
            item = esc(r.item) + (f'<span class="note">{esc(note)}</span>' if note else "")
            cells = [f"<td>{item}</td>", f"<td>{esc(r.part)}</td>",
                     f'<td class="num">x{r.count}</td>']
            if show_ducats:
                cells.append(f'<td class="num">{r.ducats if r.ducats is not None else "&mdash;"}</td>')
            if show_platinum:
                if r.sell_min is None:
                    plat = "&mdash;"
                else:
                    age = age_words(r.price_at, now)
                    plat = f"{r.sell_min}p" + (
                        f' <span class="note">{esc(age)}</span>' if age else "")
                cells.append(f'<td class="num">{plat}</td>')
            body.append("<tr>" + "".join(cells) + "</tr>")
        head = "".join(
            '<th class="num">' + h + "</th>" if h in ("Count", "Ducats", "Platinum")
            else "<th>" + h + "</th>" for h in head_cells)
        cols = ('<colgroup><col class="item"><col class="part">'
                + "<col>" * (len(head_cells) - 2) + "</colgroup>")
        sections.append(f"<section><h2>{esc(heading)}</h2>\n<table>"
                        f"{cols}<thead><tr>{head}</tr></thead>\n<tbody>\n"
                        + "\n".join(body) + "\n</tbody></table></section>")
    if not sections:
        sections.append('<p class="empty">No part matches the filters.</p>')
    dates = price_dates(rows, tz) if show_platinum else ""
    lines = [terms(kind, trade_for)]
    if dates:
        lines.append("Platinum: the cheapest seller on warframe.market, "
                     f"checked {dates}.")
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{esc(page_title)}</title>\n<style>{css}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{esc(page_title)}</h1>\n"
        f'<p class="asof">{esc(texts["as_of"])} {as_of:%Y-%m-%d}</p>\n'
        + "".join(f'<p class="terms">{esc(x)}</p>\n' for x in lines)
        + "\n".join(sections) + "\n</main>\n</body>\n</html>\n")
