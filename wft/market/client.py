"""Asking warframe.market what a set is worth.

Two calls, and they are not the same kind of call.

The catalogue — every tradable thing and its market name — is one request for
about four thousand entries, and it changes when Digital Extremes ships an
update. It is fetched rarely and stored.

Prices are per item and go stale in a day. They are fetched only for the sets
the player is actually looking at, which on a typical shelf is a handful, and
never without being asked.

The join is on `gameRef`, which is the `/Lotus/...` identifier this database
already uses as its primary key. The market publishes it, so nothing here
matches on names — the same reason the vault status joins the way it does. A
name is a label; an identifier is a fact.

**On rate limits.** The public limit is three requests a second. This module
sends at most two, and never fetches anything it was not asked for. That is
not politeness for its own sake: measuring a rate limit on a Warframe service
can get a player locked out of the game, and being wrong here costs them
something the tool cannot give back.

**On their rules** (docs.warframe.market/docs/rules/overview), which this
follows deliberately rather than by accident:

* *Identify your application.* A named agent (`wft.USER_AGENT`), never a
  browser string. They ask for it so unfamiliar traffic can be placed instead of
  blocked.
* *Do not clone warframe.market.* This shows a price beside a set the player
  already owns and offers no browsing, no search and no order placement. It
  answers "what is the pile on my shelf worth", which is a question their site
  cannot answer because it does not know the player's shelf.
* *Be careful with requests.* Cached for hours, fetched only on a button, and
  only for the rows on screen.
* *No trade bots.* Nothing here places, edits or answers an order.
"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

BASE = "https://api.warframe.market/v2"

#: The shared agent string, wft.USER_AGENT, sent on every request. The market's
#: rules ask for a dedicated, descriptive agent that names the project, and
#: forbid disguising a tool as a browser: undefined browser-like traffic is
#: expensive for them to investigate and easy to mistake for a scraper.
from wft import USER_AGENT  # noqa: E402,F401

#: Seconds between requests. The documented limit is three a second; this is
#: two, which keeps a margin to the limit because a ban would hit the player's
#: account.
MIN_INTERVAL = 0.5

TIMEOUT = 20

#: How long a price is worth reusing. Prices move within a day and the section
#: that shows them is opened far more often than that.
FRESH_HOURS = 6


class MarketError(RuntimeError):
    """The market could not be asked, or did not answer usefully."""


_last_request = 0.0


def _get(path: str) -> dict:
    """One GET against the API, no faster than `MIN_INTERVAL`."""
    global _last_request

    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    request = urllib.request.Request(
        f"{BASE}{path}", headers={"User-Agent": USER_AGENT,
                                  "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # 429 and 509 are the two the documentation names. Said in the words
        # the caller has to act on rather than as a status code.
        if exc.code in (429, 509):
            raise MarketError(
                "warframe.market is rate-limiting this connection. Wait a "
                "minute before trying again.") from exc
        raise MarketError(f"warframe.market answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MarketError(f"could not reach warframe.market: {exc}") from exc
    finally:
        _last_request = time.monotonic()

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise MarketError("warframe.market sent something that is not "
                          "JSON") from exc
    if payload.get("error"):
        raise MarketError(str(payload["error"]))
    return payload


def refresh_catalogue(connection: sqlite3.Connection) -> int:
    """Fetch the item list and store what joins to this database.

    Everything the market knows, filtered down to what is already here. The
    rest is mods, rivens and arcanes, which this tool does not track and would
    only be rows nothing ever reads.
    """
    payload = _get("/items")
    rows = payload.get("data") or []
    if not rows:
        raise MarketError("the item list came back empty")

    known = {row[0] for row in connection.execute(
        "SELECT unique_name FROM items UNION SELECT unique_name FROM parts")}
    written = 0
    for row in rows:
        ref = row.get("gameRef")
        if not ref or ref not in known:
            continue
        connection.execute(
            "INSERT INTO market_items(unique_name, slug, item_id, is_set, ducats) "
            "VALUES(?,?,?,?,?) ON CONFLICT(unique_name) DO UPDATE SET "
            "slug=excluded.slug, item_id=excluded.item_id, "
            "is_set=excluded.is_set, ducats=excluded.ducats",
            (ref, row["slug"], row.get("id") or "",
             int("set" in (row.get("tags") or [])), row.get("ducats")))
        written += 1
    connection.commit()
    return written


def slug_for(connection: sqlite3.Connection, unique_name: str) -> str | None:
    """The market's name for one tracked thing, under any of its spellings.

    The alias group is the point. A Warframe component is two rows in this
    database - `BaruukPrimeChassisComponent` is what the inventory holds and
    `BaruukPrimeChassisBlueprint` is what a relic drops - and the market
    publishes its `gameRef` as the blueprint. Asking with the component name
    alone found nothing, so ten of forty-eight spare parts showed a
    dash where a price exists: every Warframe component held.

    Items are unaffected - they have no aliases - so this costs one extra
    lookup on the rows that were already answered.
    """
    row = connection.execute(
        "SELECT slug FROM market_items WHERE unique_name = ?",
        (unique_name,)).fetchone()
    if row is not None:
        return row["slug"]
    from wft.data.db import alias_group

    for name in alias_group(connection, unique_name)[1:]:
        row = connection.execute(
            "SELECT slug FROM market_items WHERE unique_name = ?",
            (name,)).fetchone()
        if row is not None:
            return row["slug"]
    return None


def cached_price(connection: sqlite3.Connection, slug: str,
                 max_age_hours: int = FRESH_HOURS) -> sqlite3.Row | None:
    """The stored price for a slug, if it is recent enough to quote."""
    return connection.execute(
        "SELECT * FROM market_prices WHERE slug = ? "
        "AND fetched_at > datetime('now', ?)",
        (slug, f"-{max_age_hours} hours")).fetchone()


def stored_price(connection: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """The last stored price for a slug, whatever its age.

    `cached_price` answers "may this be quoted as current"; this answers "what
    was the last thing the market said", for lists that show the price with its
    age instead of hiding it. Nothing here reaches the network.
    """
    return connection.execute(
        "SELECT * FROM market_prices WHERE slug = ?", (slug,)).fetchone()


def unpriced(connection: sqlite3.Connection, uniques: list[str]) -> list[str]:
    """The rows a lookup would actually spend a request on.

    Everything already answered within `FRESH_HOURS` is skipped, and rows the
    market does not trade - Forma and quest blueprints - have no slug to ask
    about. On a typical shelf that turns forty-eight parts into forty-six
    questions the first time and none at all for the next six hours, which is
    the difference between a button that can be pressed and one a player learns to avoid.

    One row per slug, so two names for the same listing cost one request. The
    Ducats page and the Trading lists both count their button with this.
    """
    wanted, seen = [], set()
    for unique in uniques:
        slug = slug_for(connection, unique)
        if slug is None or slug in seen:
            continue
        seen.add(slug)
        if cached_price(connection, slug) is None:
            wanted.append(unique)
    return wanted


def fetch_price(connection: sqlite3.Connection, slug: str) -> sqlite3.Row:
    """The top orders for one slug, stored and returned.

    `sell_min` is what the cheapest seller is asking, which is the price to
    undercut or match. `buy_max` is what somebody is already offering, which
    is what a sale is worth right now without waiting. They are different
    questions and the answer to "what is this worth" is usually between them.
    """
    payload = _get(f"/orders/item/{slug}/top")
    data = payload.get("data") or {}
    sells = [o["platinum"] for o in data.get("sell", [])
             if o.get("platinum") is not None]
    buys = [o["platinum"] for o in data.get("buy", [])
            if o.get("platinum") is not None]
    connection.execute(
        "INSERT INTO market_prices(slug, sell_min, buy_max, sell_count, "
        "fetched_at) VALUES(?,?,?,?,datetime('now')) "
        "ON CONFLICT(slug) DO UPDATE SET sell_min=excluded.sell_min, "
        "buy_max=excluded.buy_max, sell_count=excluded.sell_count, "
        "fetched_at=excluded.fetched_at",
        (slug, min(sells) if sells else None, max(buys) if buys else None,
         len(sells)))
    connection.commit()
    return connection.execute(
        "SELECT * FROM market_prices WHERE slug = ?", (slug,)).fetchone()


def price_for(connection: sqlite3.Connection, unique_name: str,
              fetch: bool = False) -> sqlite3.Row | None:
    """The price for one tracked thing, from the cache or from the market.

    `fetch` is off by default on purpose. Every screen that shows a price
    would otherwise fetch it, and a page built four times in a minute would
    spend four requests on an answer that has not changed.
    """
    slug = slug_for(connection, unique_name)
    if slug is None:
        return None
    cached = cached_price(connection, slug)
    if cached is not None or not fetch:
        return cached
    return fetch_price(connection, slug)
