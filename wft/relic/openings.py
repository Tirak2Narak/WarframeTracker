"""Which relic to open next.

The tool could say what a reward is worth once it is on screen, and what parts
a set still needs, but not the question that comes before both: of the relics
already on the shelf, which one is most likely to hand over something wanted.

Everything needed is already stored — the six rewards a relic drops, their
rarities, the drop chances per refinement, and what the inventory holds — so
this is arithmetic rather than new data.

The number is the chance that one opening hands over a part still needed.
One opening gives exactly one of the six rewards, so the chances of the wanted
ones add up to a probability; `expected` is that sum.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from wft.data import db as dbq


@dataclass
class Opening:
    """One relic on the shelf, and what opening it is worth."""

    relic_unique: str
    name: str
    tier: str
    refinement: str
    held: int
    vaulted: bool
    #: Chance that a single opening gives a wanted part (a sum of at most 1).
    expected: float = 0.0
    #: The wanted parts it can drop, best chance first.
    wanted: list[tuple[str, str, float]] = field(default_factory=list)
    #: What the same relic would be worth refined to Radiant. Refining is
    #: 100 void traces and the whole point of spending them: a rare goes from
    #: 2% to 10%, a common down from 25.33% to 16.67%. Whether that trade is
    #: worth making depends on which of the six the user actually wants, which
    #: is a question only the inventory can answer.
    refined: float = 0.0

    @property
    def refining_gain(self) -> float:
        """How much more a Radiant opening is worth than this one."""
        return self.refined - self.expected

    @property
    def chance_of_any(self) -> float:
        """Probability that one opening yields at least one wanted part.

        The six slots of a relic are one roll, not six independent ones, so the
        chances of the wanted rewards add rather than compound.
        """
        return min(1.0, sum(chance for _name, _rarity, chance in self.wanted))


def percent(chance: float) -> str:
    """A chance as the pages show it: "38 %". One place, so the Fissures
    table, the Rewards briefing and the tests read the same figure."""
    return f"{min(1.0, max(0.0, chance)) * 100:.0f} %"


def needed(connection, part_unique: str,
           retained: dict[str, int] | None = None) -> bool:
    """Whether this part would fill a gap rather than pile up.

    The same floor Ducats, I offer and Plan use (`db.retained_counts`): a part is needed while the copies held are below
    what the sets still to be built ask for. A mastered or built set asks for nothing unless
    something above it still wants copies, so Bronco Prime Receiver (Akbronco
    Prime done) and Lex Prime Receiver (3 Lex built, Aklex wants 2) are not.

    `retained_counts` walks the whole recipe graph. Callers that ask about many
    parts compute it once and pass it in.

    Public because the relic window asks the same question of every reward in
    a relic, and answering it a second way is how two screens come to disagree
    about whether the user needs a Bronco Prime Receiver.
    """
    row = connection.execute(
        "SELECT p.count_needed, i.category FROM parts p "
        "LEFT JOIN items i ON i.unique_name = p.item_unique "
        "WHERE p.unique_name = ?", (part_unique,)).fetchone()
    if row is None:
        return False
    # Forma is in 2180 of 18532 relic rewards and in almost every relic, so
    # counting it as wanted made the ranking a ranking of nothing. A player
    # filling a collection does not count Forma, which is not part of one - it
    # is a build material three of which cost 30 platinum, against a Prime set
    # that sells for two hundred. Same list the rest of the tool ignores.
    if (row["category"] or "") in dbq.NOT_FOR_MASTERY_CATEGORIES:
        return False
    if retained is None:
        retained = dbq.retained_counts(connection)
    root = dbq.canonical(connection, part_unique)
    keep = retained.get(root, max(1, int(row["count_needed"])))
    return dbq.owned_count(connection, part_unique) < keep


def rank(connection, limit: int | None = None) -> list[Opening]:
    """Every held relic, best first.

    Ties break on the vaulted ones first: an unvaulted relic can be farmed
    again and a vaulted one cannot, so between two equal openings the finite
    one is worth spending.
    """
    openings: list[Opening] = []
    retained = dbq.retained_counts(connection)
    rows = connection.execute(
        "SELECT o.relic_unique, o.count, r.name, r.tier, r.vaulted "
        "FROM owned_relics o JOIN relics r ON r.unique_name = o.relic_unique "
        "WHERE o.count > 0").fetchall()

    for row in rows:
        refinement = dbq.refinement_of(row["relic_unique"])
        chances = dbq.DROP_CHANCE.get(refinement, dbq.DROP_CHANCE["INTACT"])
        radiant = dbq.DROP_CHANCE["RADIANT"]
        wanted: list[tuple[str, str, float]] = []
        expected = 0.0
        refined = 0.0
        for reward in connection.execute(
                "SELECT rr.part_unique, rr.rarity, p.name FROM relic_rewards rr "
                "LEFT JOIN parts p ON p.unique_name = rr.part_unique "
                "WHERE rr.relic_unique = ?", (row["relic_unique"],)):
            rarity = (reward["rarity"] or "COMMON").upper()
            chance = chances.get(rarity, 0.0)
            if not needed(connection, reward["part_unique"], retained):
                continue
            wanted.append((reward["name"] or reward["part_unique"], rarity, chance))
            expected += chance
            refined += radiant.get(rarity, 0.0)
        # Rarest first. The column shows three of these and then "+2 more",
        # and sorted by chance the three shown were always the commons - so a
        # relic held for its rare part advertised the three parts nobody opens
        # it for. The rare is the reason, and it is also the part refining is
        # for; hiding it made the Radiant column next to it unreadable.
        wanted.sort(key=lambda entry: (entry[2], entry[0]))
        openings.append(Opening(
            relic_unique=row["relic_unique"], name=row["name"],
            tier=row["tier"] or "", refinement=refinement,
            held=row["count"], vaulted=bool(row["vaulted"]),
            expected=expected, refined=refined, wanted=wanted))

    openings.sort(key=lambda o: (-o.expected, not o.vaulted, -o.held, o.name))
    return openings[:limit] if limit else openings
