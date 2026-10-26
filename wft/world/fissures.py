"""Which Void Fissures are open right now.

The shelf says which relic is worth opening. It cannot say whether that can be
done in the next ten minutes, and that is the half a person actually acts on:
twelve Lith O4 are worth nothing at all if every fissure on the board is Axi.

**The source.** Digital Extremes publish the world state, but the documented
endpoint (`content.warframe.com/dynamic/worldState.php`) answers 404 from here
on both hosts, over http and https — checked before choosing anything else. So
this reads warframestat.us, which parses that same world state and serves it as
JSON. It is a community service and not the game's own, which is why it is
addressed through the config like every other URL and why failing to reach it
is never allowed to be more than a missing line on a page.

**The stand-in.** warframestat's poller has been seen to fall behind for the
better part of a day, answering 200 with a board whose every fissure had
already ended. The world state itself did move house rather than die:
`api.warframe.com/cdn/worldState.php` serves it live (found in a
comment on oracle.browse.wf, of all places). It is the raw form — eras are
`VoidT4`, missions `MT_SURVIVAL`, nodes `SolNode717` — so warframestat stays
first for its cooked names, and the raw feed is read only when the first
answer is stale or missing, translated by the small tables below. Node names
come from WFCD's community data (solNodes.json); without
them the board still shows, ids and all, because a readable board with ugly
names beats an error box.

**Three boards, not one.** A single request returns thirty-odd fissures, and
the game shows them as three separate lists: the ordinary ones, Steel Path
(`isHard`), and Void Storms in Railjack (`isStorm`). Counting them together
gives a number that matches nothing the player can see — thirty-two against the
ten in front of the player.

**On asking too often.** Their edge sets `cache-control: max-age=120`, so two
requests inside two minutes get the same bytes and one of them is waste. That
is the interval here. Nothing is written to the database: a fissure expires in
under two hours and is worthless the moment the application is closed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

#: The shared agent string, wft.USER_AGENT (see there for why it is sent).
from wft import USER_AGENT  # noqa: E402,F401

#: Their own edge cache. Asking faster than this cannot produce a new answer.
CACHE_SECONDS = 120

TIMEOUT = 15

#: The Duviri fissure. It takes a relic of any era, so it is not an era of its
#: own on the board - it is a plus-one against every row. Confirmed in play
#: rather than assumed from the payload, which says nothing
#: about what a fissure will accept.
ANY_ERA = "Omnia"


class FissureError(RuntimeError):
    """The board could not be read."""


@dataclass(frozen=True)
class Fissure:
    tier: str
    tier_num: int
    node: str
    mission: str
    enemy: str
    expiry: datetime
    hard: bool
    storm: bool

    @property
    def kind(self) -> str:
        """Which of the game's three lists this one appears in."""
        if self.storm:
            return "storm"
        return "hard" if self.hard else "normal"

    @property
    def is_quick(self) -> bool:
        """Short enough to be worth crossing the board for.

        A Void Storm never is, whatever it is called. Railjack puts a boarding
        run and a ship fight around the mission, so an Extermination in a storm
        has nothing in common with an Extermination on a node - and the payload
        gives both the same name.
        """
        return self.mission in quick_missions() and not self.storm

    def minutes_left(self, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        return max(0, int((self.expiry - now).total_seconds() // 60))


#: Missions that are over in a couple of minutes. The point of a fissure run
#: is the relic at the end of it, so the shortest route to ten reactant is the
#: best one - and on a board of thirty-three, that is the only thing separating
#: most of the rows.
#:
#: Named rather than derived: nothing in the payload says how long a mission
#: takes, and no rule about mission types would survive Digital Extremes adding
#: one. These two are the default; the user chooses in Settings → Fissures
#: (`[fissures] quick_missions`).
QUICK_MISSIONS = ("Capture", "Extermination")

#: The mission types a fissure can have, as the board spells them: what
#: `_MISSIONS` above and warframestat's `missionType` produce. The code holds
#: no complete list (a payload with an unknown type just shows it title-cased),
#: so this is those names plus the types of the game's fissure nodes that no
#: code or test had named yet (Alchemy, Void Cascade) - from play, not from a
#: payload, so a new type DE adds is missing here until it is added.
MISSION_TYPES = ("Alchemy", "Capture", "Defense", "Disruption", "Excavation",
                 "Extermination", "Interception", "Mobile Defense", "Rescue",
                 "Sabotage", "Spy", "Survival", "Void Cascade", "Void Flood")

_config = None


def use_config(cfg) -> None:
    """Remember which configuration says what counts as quick.

    `Fissure.is_quick` is asked from table code that has no config to hand, and
    the answer must follow a Settings change at once: the object is the same
    one Settings writes to, so reading it live needs no restart.
    """
    global _config
    _config = cfg


def quick_missions() -> tuple[str, ...]:
    """The mission types that count as quick now."""
    chosen = _config.get("fissures.quick_missions") if _config else None
    if isinstance(chosen, (list, tuple)):
        return tuple(str(name) for name in chosen)
    return QUICK_MISSIONS


#: The three lists, in the order the game presents them.
KINDS = ("normal", "hard", "storm")

KIND_LABELS = {"normal": "Normal", "hard": "Steel Path",
               "storm": "Void Storms"}

_cache: tuple[float, list[Fissure]] | None = None


def _url(cfg) -> str:
    template = str(cfg.get("sources.fissures_url", "") or "").strip()
    if not template:
        raise FissureError("no sources.fissures_url is configured")
    platform = str(cfg.get("sources.platform", "pc") or "pc").strip()
    return template.replace("{platform}", platform)


def _ask(url: str):
    """One GET, decoded as JSON, every failure spoken as a FissureError."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FissureError(f"{request.host} answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FissureError(f"could not reach {request.host}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FissureError(f"{request.host} did not answer with JSON") from exc


def _parse_time(text: str) -> datetime:
    # Their timestamps end in Z, which fromisoformat only learned to read in
    # 3.11. Spelled out rather than relied upon, because a parse failure here
    # would make every fissure look expired.
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _parse(payload) -> list[Fissure]:
    out = []
    for row in payload if isinstance(payload, list) else []:
        try:
            out.append(Fissure(
                tier=str(row["tier"]),
                tier_num=int(row.get("tierNum") or 0),
                node=str(row.get("node") or "?"),
                mission=str(row.get("missionType") or "?"),
                enemy=str(row.get("enemy") or "?"),
                expiry=_parse_time(str(row["expiry"])),
                hard=bool(row.get("isHard")),
                storm=bool(row.get("isStorm"))))
        except (KeyError, TypeError, ValueError):
            # One malformed entry is not a reason to show no board at all.
            continue
    return out


#: The raw feed's era names. VoidT5 and T6 confirmed against warframestat's
#: own cooked output for the same board, not guessed from the numbering.
_TIERS = {"VoidT1": ("Lith", 1), "VoidT2": ("Meso", 2), "VoidT3": ("Neo", 3),
          "VoidT4": ("Axi", 4), "VoidT5": ("Requiem", 5),
          "VoidT6": ("Omnia", 6)}

#: MT_* names whose spoken form is not just the word behind the prefix. The
#: rest — MT_SURVIVAL, MT_MOBILE_DEFENSE — title-case into themselves, and an
#: unknown one shows title-cased rather than hiding the fissure it names.
_MISSIONS = {"MT_ARTIFACT": "Disruption", "MT_INTEL": "Spy",
             "MT_TERRITORY": "Interception", "MT_CORRUPTION": "Void Flood",
             "MT_EXCAVATE": "Excavation"}


def _mission_name(raw: str) -> str:
    return _MISSIONS.get(raw) or raw.removeprefix("MT_").replace(
        "_", " ").title()


_nodes_cache: dict | None = None


def _nodes(cfg) -> dict:
    """SolNode id → {value, enemy, type}, fetched once per run.

    Cached only on success: a failed fetch returns an empty table and is
    allowed to try again next board, because the fallback works without it —
    the page just says SolNode717 where it would say Persto (Deimos).
    """
    global _nodes_cache
    if _nodes_cache is not None:
        return _nodes_cache
    url = str(cfg.get("sources.solnodes_url", "") or "").strip()
    if not url:
        return {}
    try:
        table = _ask(url)
    except FissureError:
        return {}
    if not isinstance(table, dict):
        return {}
    _nodes_cache = table
    return table


def _epoch_ms(value) -> datetime:
    # The raw feed writes times as Mongo's {"$date": {"$numberLong": ms}}.
    return datetime.fromtimestamp(
        int(value["$date"]["$numberLong"]) / 1000, timezone.utc)


def _parse_worldstate(payload, nodes: dict) -> list[Fissure]:
    """The official feed's two fissure lists, in this module's one shape.

    `ActiveMissions` carries the ordinary and Steel Path boards (`Hard`);
    `VoidStorms` is Railjack, names no mission of its own, and borrows the
    node table's — which is why a storm's mission can be "?" but its era
    never is.
    """
    out = []
    if not isinstance(payload, dict):
        return out
    for row in payload.get("ActiveMissions") or []:
        try:
            tier, num = _TIERS[str(row["Modifier"])]
            place = nodes.get(str(row.get("Node")), {})
            out.append(Fissure(
                tier=tier, tier_num=num,
                node=str(place.get("value") or row.get("Node") or "?"),
                mission=_mission_name(str(row.get("MissionType") or "?")),
                enemy=str(place.get("enemy") or "?"),
                expiry=_epoch_ms(row["Expiry"]),
                hard=bool(row.get("Hard")), storm=False))
        except (KeyError, TypeError, ValueError):
            continue
    for row in payload.get("VoidStorms") or []:
        try:
            tier, num = _TIERS[str(row["ActiveMissionTier"])]
            place = nodes.get(str(row.get("Node")), {})
            out.append(Fissure(
                tier=tier, tier_num=num,
                node=str(place.get("value") or row.get("Node") or "?"),
                mission=str(place.get("type") or "?"),
                enemy=str(place.get("enemy") or "?"),
                expiry=_epoch_ms(row["Expiry"]),
                hard=False, storm=True))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _stand_in(cfg) -> list[Fissure]:
    """The official world state, or an empty list if it cannot help either.

    Empty rather than raising: the caller is already holding warframestat's
    error, which names the service the user configured and the failure that
    started this. A second exception would only bury it.
    """
    url = str(cfg.get("sources.worldstate_url", "") or "").strip()
    if not url:
        return []
    try:
        payload = _ask(url)
    except FissureError:
        return []
    return _unexpired(_parse_worldstate(payload, _nodes(cfg)))


def active(cfg, force: bool = False) -> list[Fissure]:
    """Every open fissure, from the cache unless it has gone stale.

    Expired entries are dropped here rather than at the call site: the answer
    is up to two minutes old by design, and in that time a fissure can end.
    """
    global _cache

    use_config(cfg)
    now = time.monotonic()
    if not force and _cache is not None and now - _cache[0] < CACHE_SECONDS:
        fresh = _unexpired(_cache[1])
        if fresh:
            return fresh

    try:
        fresh = _from_primary(cfg)
    except FissureError:
        # warframestat could not produce a live board. Before showing its
        # error, ask the game's own feed — seen carrying a fresh board while
        # warframestat's poller was a day behind.
        fresh = _stand_in(cfg)
        if not fresh:
            raise
    _cache = (now, fresh)
    return fresh


def _from_primary(cfg) -> list[Fissure]:
    payload = _ask(_url(cfg))
    fissures = _parse(payload)
    if not fissures:
        raise FissureError("the fissure list came back empty")

    fresh = _unexpired(fissures)
    behind = _minutes_behind(fissures)
    try:
        limit = float(cfg.get("sources.fissures_stale_after_minutes", 10) or 10)
    except (TypeError, ValueError):
        limit = 10.0
    if fresh and behind > limit:
        # Behind, but not yet every fissure ended. In one observed case the newest
        # fissure had opened 75 minutes earlier, 24 of 35 had ended up to 73
        # minutes ago and 11 still ran - and those 11 were shown as the board
        # while the game had 24 open in the ordinary and Steel Path lists
        # alone. What still runs on a board this late is a remnant: every
        # fissure opened since is missing from it.
        raise FissureError(
            f"warframestat.us is behind - its board still lists a fissure "
            f"that ended {behind} minutes ago, so the fissures opened since "
            f"are missing from it")
    if not fresh:
        # Their whole board has expired. Seen for real: at 22:05 UTC the
        # service served thirty fissures whose last one ended at 21:45, its
        # own Date header agreeing with our clock to the second, so this is
        # their poller falling behind rather than a clock to argue with.
        #
        # Reported rather than returned as an empty list. Nothing filtered out
        # and nothing to show look identical from the outside, and the page
        # spent the difference saying "Reading the board…" forever.
        late = int((datetime.now(timezone.utc)
                    - max(f.expiry for f in fissures)).total_seconds() // 60)
        raise FissureError(
            f"warframestat.us is serving an expired board — its newest "
            f"fissure ended {late} minutes ago")
    return fresh


def _minutes_behind(fissures: list[Fissure]) -> int:
    """How long ago the longest-ended fissure on a board ended; 0 if none has.

    A live poller drops a fissure soon after it ends, so one that ended
    minutes ago is the edge cache, and one that ended an hour ago is a poller
    that stopped.
    """
    now = datetime.now(timezone.utc)
    ended = [f.expiry for f in fissures if f.expiry <= now]
    if not ended:
        return 0
    return int((now - min(ended)).total_seconds() // 60)


def _unexpired(fissures: list[Fissure]) -> list[Fissure]:
    now = datetime.now(timezone.utc)
    return [f for f in fissures if f.expiry > now]


def of_kinds(fissures: list[Fissure], kinds) -> list[Fissure]:
    return [f for f in fissures if f.kind in kinds]


def counts_by_era(fissures: list[Fissure], eras) -> dict[str, tuple[int, int]]:
    """{era: (its own fissures, Omnia fissures)} for the eras asked about.

    Two numbers rather than one sum, because they are not the same offer. Four
    open Lith fissures is a choice of node and mission; one Omnia fissure is a
    single run in Duviri that happens to accept a Lith relic. Adding them would
    say "Lith 5" and send somebody looking for five doors.
    """
    anywhere = sum(1 for f in fissures if f.tier == ANY_ERA)
    return {era: (sum(1 for f in fissures if f.tier == era), anywhere)
            for era in eras}
