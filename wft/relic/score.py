"""Ranking the rewards on offer.

The question this answers is not "what is this worth" but "which of these two
to four should I take, right now, in the next few seconds". Those are different
questions: a part worth 100 ducats that you already own loses to a part worth
15 that completes a set you are three quarters through.

The tier system is inherited from the previous attempt's design,
which had it right. What it
lacked was ducats — it knew a part was "ducat fodder" but never what the fodder
was worth, so it could not tell a 100-ducat duplicate from a 15-ducat one.

Platinum is deliberately absent as a per-part price. Whole prime sets are a
different matter — they sell as a unit, which is why mastery does not remove a
part from the ranking. A set whose mastery is already banked is still worth
finishing; it just loses to one that is not.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum


class Tier(IntEnum):
    """Coarse buckets, ordered best first. Ties are broken inside a tier.

    The buckets ask one question: can this be had another way? Everything else
    - how close the set is, whether its mastery is banked, what it is worth in
    ducats - decides the order inside a bucket, because none of it matters if
    the part cannot be got at all.

    The old buckets asked whether a part was vaulted, which is a fact about the
    drop tables rather than about this player. Vaulting stops a relic dropping;
    it does not empty anyone's inventory. A vaulted part is one thing to
    someone holding ten of its relics and another to someone holding none.

    There were also separate PARTIAL buckets. They said nothing the set-progress
    ordering inside a bucket did not already say, except that they outranked
    mastery - and mastery is what the collection is actually for.
    """

    MISSING_UNREACHABLE = 0       # needed, and no realistic way to get it
    MISSING_SCARCE = 1            # needed, supply is thin
    MISSING_FARMABLE = 2          # needed, and gettable
    OWNED_VALUABLE = 3            # have it, but worth real ducats
    OWNED = 4                     # have it
    JUNK = 5                      # Forma and friends


#: A duplicate has to be worth at least this many ducats before it is presented
#: as an alternative worth considering. 45 is the second of the five ducat
#: values in the game (15/25/45/65/100), so this means "above the floor".
DUCAT_THRESHOLD = 45

RARITY_ORDER = {"RARE": 3, "UNCOMMON": 2, "COMMON": 1}

#: Mirrors the data layer, so a reason line can tell "you hold nothing" from
#: "you hold a little" without importing the database module into the phrasing.
FAINT_CHANCE = 0.05

#: The item types a relic reward can belong to, best first by default, with the
#: number each has in the default order. A Warframe is a bigger piece of
#: the collection than a weapon: it takes longer to farm, it is worth more
#: mastery, and it is the thing a set is usually being built for. The number is
#: the player's priority, 1 first, and equal numbers weigh the same: the four
#: named types ahead, every other type level behind them. It used to be a
#: table of four and a constant for the rest.
ITEM_TYPES = ("warframe", "archwing", "companion", "necramech", "primary",
              "secondary", "melee", "archgun", "archmelee", "companion_weapon",
              "other")
DEFAULT_TYPE_NUMBERS = {"warframe": 1, "archwing": 2, "companion": 3,
                        "necramech": 4, "primary": 5, "secondary": 5,
                        "melee": 5, "archgun": 5, "archmelee": 5,
                        "companion_weapon": 5, "other": 5}
#: What a category that is not listed (the catalogue also holds "weapon",
#: "special", "beast" and others) is worth: what the player gave "other".
OTHER_TYPE = "other"
MAX_TYPE_NUMBER = 99


#: What decides between two rewards, in the order the default applies them, as
#: one list. Each id has one value per reward;
#: smaller is better, and a reward the criterion does not apply to gets
#: `NOT_APPLICABLE`, which is behind every reward it does apply to. That holds
#: for a criterion below "collection"; above it every reward has a real value.
#:
#:   collection  a missing piece of a set whose mastery is still owed, against
#:               a duplicate or a piece of a mastered set
#:   reach       the tier: can it be had another way
#:   completes   taking it finishes the set
#:   progress    how far along the set already is
#:   type        the item type, by the player's numbers
#:   set_size    a smaller set is less work
#:   relics      fewer relics still drop it
#:   rarity      the rarer drop
#:   ducats      more ducats, for a reward that advances nothing
CRITERIA = ("collection", "reach", "completes", "progress", "type",
            "set_size", "relics", "rarity", "ducats")
DEFAULT_CRITERIA = CRITERIA

NOT_APPLICABLE = math.inf


@dataclass(frozen=True)
class RankingOrder:
    """How the player weighs the criteria: their order, and the item types."""

    criteria: tuple[str, ...] = DEFAULT_CRITERIA
    #: item type -> number (1 first); see `ITEM_TYPES`.
    types: tuple[tuple[str, int], ...] = tuple(DEFAULT_TYPE_NUMBERS.items())

    def type_number(self, category: str) -> int:
        numbers = dict(self.types)
        return numbers.get(category, numbers[OTHER_TYPE])


DEFAULT_ORDER = RankingOrder()


def order_from_config(cfg) -> RankingOrder:
    """The order set in `[ranking]`, read fresh from the config each time.

    Settings → Rewards writes `criteria` (a list of ids) and `types` (a table of
    numbers); a missing or damaged value falls back to the default for that
    part, so a hand-edited file cannot break the recommendation. Ids the file
    does not name are appended in their default order, which is what a
    criterion added by a later version needs.
    """
    chosen = cfg.get("ranking.criteria") if cfg is not None else None
    criteria: list[str] = []
    if isinstance(chosen, (list, tuple)):
        for name in chosen:
            if name in CRITERIA and name not in criteria:
                criteria.append(name)
    criteria += [name for name in DEFAULT_CRITERIA if name not in criteria]

    numbers = dict(DEFAULT_TYPE_NUMBERS)
    table = cfg.get("ranking.types") if cfg is not None else None
    if isinstance(table, dict):
        for name, value in table.items():
            if (name in numbers and isinstance(value, int)
                    and not isinstance(value, bool)
                    and 1 <= value <= MAX_TYPE_NUMBER):
                numbers[name] = value
    return RankingOrder(tuple(criteria), tuple(numbers.items()))


@dataclass
class Reward:
    """One option on the reward screen, as far as we understand it."""

    position: int                       # 1-based, left to right as shown in game
    raw_text: str                       # what OCR read
    part_unique: str | None = None      # what it was matched to
    name: str = ""
    item_name: str = ""
    ducats: int | None = None
    rarity: str = "COMMON"
    reach: str = "unknown"              # farmable | scarce | unreachable | unknown
    active_relics: int = 0              # relics still dropping it, for anyone
    own_chance: float = 0.0             # chance from the relics already held
    owned_count: int = 0                # copies of the part in the inventory
    count_needed: int = 1               # copies the recipe asks for
    item_built: int = 0                 # finished copies of the item it belongs to
    still_needed_for: list[str] = field(default_factory=list)  # items consuming this one
    set_owned: int = 0                  # distinct set pieces already held
    set_total: int = 0
    #: Which pieces the set is actually short of, named. "3/4" beside a
    #: duplicate reads as progress the card is not offering: a
    #: third Lavos Prime Chassis Blueprint sat over "Lavos Prime · 3/4 pieces"
    #: and lost to a piece the player did not have. The fraction was true and
    #: the piece it needed - Systems - was nowhere on the card.
    set_missing: tuple[str, ...] = ()
    item_mastered: bool = False         # rank 30 reached, points already banked
    category: str = ""                  # warframe, primary, melee, companion...
    match_confidence: float = 0.0

    tier: Tier = Tier.JUNK
    reasons: list[str] = field(default_factory=list)

    @property
    def is_missing(self) -> bool:
        """Whether this part is still wanted.

        Not the same as "do I hold one". A part whose item is already built is
        not wanted, even with none in the inventory — unless that item feeds
        another recipe. And not the same as "do I hold any": 67 parts are asked
        for twice by their recipe, and holding one of a pair is holding half of
        what the set needs. Four parts sat at 1 of 2 and were
        being announced as already owned, which is exactly the wrong answer in
        the fifteen seconds this is for.
        """
        if self.owned_count >= max(1, self.count_needed):
            return False
        if self.item_built > 0 and not self.still_needed_for:
            return False
        return True

    @property
    def set_progress(self) -> float:
        return self.set_owned / self.set_total if self.set_total else 0.0

    @property
    def completes_set(self) -> bool:
        """Whether taking **this** finishes the set.

        Its own criterion rather than a high set_progress, because the value
        is not continuous: a finished set can be built, and building is what
        earns the mastery the collection is for. Four quarters of a set are
        four pieces; the fourth one is a Warframe.

        `is_missing` is half of it and was missing. Without it this asked only
        whether the set was one piece short of anything, and answered yes for
        a piece already on the shelf: Velox Prime at 2 of 3 is short its
        barrel, and a second receiver leaves it at 2 of 3. That put a
        duplicate above a Warframe part still needed, on the strength
        of finishing a set it could not finish - and this criterion sits third
        in the ordering, ahead of both set progress and category, so it
        decided the pick on its own.
        """
        return (bool(self.set_total) and self.set_owned == self.set_total - 1
                and self.is_missing)


def looks_like_forma(text: str) -> bool:
    """Whether an unmatched reading is Forma rather than a failure.

    Forma is a genuine relic reward that belongs to no prime set, so it never
    resolves to a part. One rule, asked in three places — the tier here, the
    panel's badge, the layout's clean count — and it lives here so tightening
    it is one edit rather than three copies drifting apart.
    """
    return "forma" in (text or "").lower()


def is_forma(reward: Reward) -> bool:
    """Whether a reward is Forma, matched to the catalogue or not.

    The catalogue lists Forma Blueprint as a part of itself and the relics that
    drop it list it among their rewards, so with such a relic in play the
    reading resolves to that part. It is Forma all the same: the ranking calls
    it that, and taking it books no part, only the relic. Booking it as a
    part is what once happened.
    """
    if reward.part_unique is None:
        return looks_like_forma(reward.raw_text)
    return reward.item_name.lower().startswith("forma")


def classify(reward: Reward) -> Reward:
    """Assign a tier and the human-readable reasons behind it.

    The reasons are not decoration: the panel shows them instead of a score,
    because "3/4 parts" is something you can act on in four seconds and
    "1680" is not.
    """
    reward.reasons = []

    if reward.part_unique is None:
        # Forma is a genuine relic reward that is not a set piece, so it never
        # resolves to a part. Naming it beats reporting a recognition failure.
        if looks_like_forma(reward.raw_text):
            reward.tier = Tier.JUNK
            reward.reasons.append("Forma")
        else:
            reward.tier = Tier.JUNK
            reward.reasons.append("not recognised")
        return reward

    if is_forma(reward):
        reward.tier = Tier.JUNK
        reward.reasons.append("Forma")
        return reward

    partial = 0 < reward.set_owned < reward.set_total

    if reward.is_missing:
        # Unknown reachability is treated as farmable rather than guessed at.
        # The alternative puts a part the tool knows nothing about at the top
        # of the list, which is the one place a wrong answer is expensive.
        reward.tier = {
            "unreachable": Tier.MISSING_UNREACHABLE,
            "scarce": Tier.MISSING_SCARCE,
        }.get(reward.reach, Tier.MISSING_FARMABLE)

        reward.reasons.append("missing")
        if reward.item_built > 0 and reward.still_needed_for:
            # Built already, but another recipe eats it — keep, and say why.
            reward.reasons.append(f"needed for {reward.still_needed_for[0]}")
        if reward.completes_set:
            reward.reasons.append(f"completes the set ({reward.set_total} parts)")
        elif partial:
            reward.reasons.append(f"{reward.set_owned}/{reward.set_total} parts")
        if reward.item_mastered:
            # Said out loud because it changes what the pick is *for*: no
            # mastery points left to earn, so finishing the set is worth
            # something only as a set to sell.
            reward.reasons.append("already mastered")
        if reward.reach == "unreachable":
            reward.reasons.append("no relic drops it and you hold none")
        elif reward.reach == "scarce":
            if reward.own_chance >= FAINT_CHANCE:
                reward.reasons.append(
                    f"scarce — your relics give ~{reward.own_chance:.0%}")
            else:
                reward.reasons.append(
                    f"scarce — {reward.active_relics} relic still drops it")
        elif reward.reach == "unknown":
            reward.reasons.append("vault status unknown")
    else:
        # Say which of the two reasons applies. "already built" is the one the
        # game cannot tell the player, so it is worth spelling out.
        if reward.owned_count > 0:
            headline_reason = "owned"
            if reward.count_needed > 1:
                headline_reason = f"owned {reward.owned_count}/{reward.count_needed}"
            elif reward.owned_count > 1:
                headline_reason = f"owned x{reward.owned_count}"
        else:
            headline_reason = f"{reward.item_name} already built"

        if reward.ducats and reward.ducats >= DUCAT_THRESHOLD:
            reward.tier = Tier.OWNED_VALUABLE
            reward.reasons.append(f"{headline_reason}, {reward.ducats} ducats")
        else:
            reward.tier = Tier.OWNED
            reward.reasons.append(headline_reason)

    return reward


def _is_collection(reward: Reward) -> bool:
    """A missing piece of a set that still owes mastery."""
    return reward.is_missing and not reward.item_mastered


#: The criteria that belong to the collection: below "collection" in the order
#: they only tell collection pieces apart (the farming questions).
COLLECTION_ONLY = frozenset({"reach", "completes", "progress", "type",
                             "set_size", "relics", "rarity"})


def _criterion_values(reward: Reward, order: RankingOrder) -> dict[str, object]:
    """The real value of every criterion for one reward; smaller is better.

    Real means what the question says about this reward, whatever group it is
    in: the tier, whether it finishes its set, how far the set is, the type
    number, the set size, the relic count, the rarity and the ducats are all
    defined for a duplicate as well (a duplicate has a tier of its own, 3 or 4,
    and a set it belongs to). `_sort_key` decides where a value counts.
    """
    return {
        "collection": 0 if _is_collection(reward) else 1,
        "reach": int(reward.tier),
        "completes": not reward.completes_set,
        "progress": -reward.set_progress,
        "type": order.type_number(reward.category),
        "set_size": reward.set_total or 99,
        "relics": reward.active_relics or 99,
        "rarity": -RARITY_ORDER.get(reward.rarity.upper(), 0),
        "ducats": -(reward.ducats or 0),
    }


def _sort_key(reward: Reward, order: RankingOrder = DEFAULT_ORDER) -> tuple:
    """Order the rewards, best first, by the player's order.

    Forma is always last. The rest is compared criterion by criterion in the
    order the player set in Settings → Rewards, the first one that tells two
    rewards apart deciding. The default order is the one this key had before
    it could be set, which was two groups:

    **The collection**, a piece the user is missing from a set that still owes
    mastery: tier (can it be had another way), finishing a set, how far along
    that set is, item type, the size of the set, how many relics still drop it,
    its rarity. Every one of those is a farming question and they only make
    sense about something not yet held.

    **The purse**, everything else that is not Forma: a duplicate, or a missing
    piece of an already mastered set. Neither advances anything (the user holds
    Velox Prime Receiver twice and the set wants a Barrel; Braton Prime is
    mastered, so its Stock is worth its ducats and nothing more), so it is
    ordered by ducats and nothing else, then by the one thing that separates
    equal piles, then by the item type.

    The collection outranks the purse in the default because "a piece for a set
    not mastered yet" is the first criterion, not because the code draws a line
    there. Where a criterion stands decides what it compares:

    * **Above "collection"** it compares every reward by its real value
      (`_criterion_values`): ducats count for a missing piece too, the item
      type for a duplicate, the tier, set progress and so on for both.
    * **Below "collection"** it keeps the groups above: the farming questions
      only tell collection pieces apart and ducats only the rest. A criterion
      that does not apply to a reward puts it behind every reward it applies to
      (`NOT_APPLICABLE`). This is the default, with "collection" first.
    * With "collection" last, nothing is below it and the order is one list.
    """
    # Forma is not a pick, it is what is left when there is nothing to pick.
    if reward.tier is Tier.JUNK:
        return (1, reward.position)

    values = _criterion_values(reward, order)
    collection = _is_collection(reward)
    below = order.criteria.index("collection")
    ranked = []
    for place, name in enumerate(order.criteria):
        value = values[name]
        if place > below:
            if name in COLLECTION_ONLY and not collection:
                value = NOT_APPLICABLE
            elif name == "ducats" and collection:
                value = NOT_APPLICABLE
        ranked.append(value)
    return (
        0,
        *ranked,
        # What is left when the criteria are level, in the order the old key
        # had it and whatever the player's order: a piece not yet held starts a
        # set that can be sold whole where a duplicate starts nothing (a
        # collection piece is always missing, so this only separates the
        # purse); the item type, which the purse has only here; the chance from
        # the relics already held; the card.
        not reward.is_missing,
        order.type_number(reward.category),
        reward.own_chance,
        reward.position,
    )


@dataclass
class Verdict:
    """The answer, in the shape the panel needs it."""

    rewards: list[Reward]               # in game order, for display
    best: Reward | None                 # the recommendation
    alternative: Reward | None          # a duplicate worth taking for ducats
    headline: str                       # one line, the whole decision

    @property
    def uncertain(self) -> bool:
        return any(r.part_unique is None for r in self.rewards)


#: The player's own reward is the leftmost card, always. The game lays the four
#: out in squad order and puts the player first; the relic panel already relies
#: on this to work out which relic a run cost.
OWN_POSITION = 1


def _prefer_a_squadmates_copy(best: Reward | None,
                              identified: list[Reward]) -> Reward | None:
    """When the same part is on screen twice, take the other player's.

    Squad etiquette, and it costs nothing: the two cards give the identical
    item, so the choice is free, and taking a squadmate's copy is the courtesy
    for it. The tool was recommending the leftmost card - which is the player's own -
    purely because it reached it first.

    Only against an exact duplicate of the pick. Anything else is a different
    reward and the ranking has already decided between them.
    """
    if best is None or best.position != OWN_POSITION:
        return best
    same = [r for r in identified
            if r is not best and r.part_unique == best.part_unique]
    return same[0] if same else best


def rank(rewards: list[Reward], order: RankingOrder | None = None) -> Verdict:
    """Rank the options and phrase the outcome.

    `order` is the player's order (`order_from_config`); none means the default.

    An alternative is offered only when it is genuinely a trade-off: the pick
    is something the user needs, and one of the others is a duplicate worth
    real ducats. Offering an alternative in every case would train the user to
    ignore the line.
    """
    scored = [classify(r) for r in rewards]
    order = order or DEFAULT_ORDER
    ordered = sorted(scored, key=lambda reward: _sort_key(reward, order))

    # Only something that was actually identified can be recommended. Taking
    # the top of the ordering regardless meant a screen where nothing was read
    # still showed "TAKE THIS" on the first card — the headline said "nothing
    # worth taking" while the panel pointed at a reward it could not name.
    #
    # An unrecognised screen is a failure to report, not a decision to make.
    identified = [r for r in ordered if r.part_unique is not None]
    best = identified[0] if identified else None
    best = _prefer_a_squadmates_copy(best, identified)

    alternative = None
    if best is not None and best.is_missing:
        candidates = [
            r for r in ordered[1:]
            if not r.is_missing and r.ducats and r.ducats >= DUCAT_THRESHOLD
        ]
        if candidates:
            alternative = max(candidates, key=lambda r: r.ducats or 0)

    if best is None:
        headline = "nothing recognised"
    elif best.tier is Tier.JUNK:
        headline = "nothing worth taking"
    else:
        headline = f"{best.name} — {', '.join(best.reasons)}"
        if alternative is not None:
            headline += f"   (or {alternative.name} for {alternative.ducats} ducats)"

    return Verdict(
        rewards=sorted(scored, key=lambda r: r.position),
        best=best, alternative=alternative, headline=headline,
    )


def load_reward(connection, part_unique: str, position: int, raw_text: str,
                confidence: float = 1.0) -> Reward:
    """Fill a Reward from the database.

    Everything goes through `wft.data.db`, which resolves the alias group. A
    part can carry two unique names — relics drop the blueprint, the inventory
    holds the component — and querying only one silently returns nothing.
    """
    from wft.data import db as dbq

    row = dbq.part_row(connection, part_unique)
    if row is None:
        return Reward(position=position, raw_text=raw_text)

    reach, active, own_chance = dbq.reachability(connection, part_unique)
    item_unique = row["item_unique"]
    held, total = dbq.set_progress(connection, item_unique) if item_unique else (0, 0)
    built = dbq.item_built(connection, item_unique) if item_unique else 0
    # A consumer only counts while it still wants copies: Aklex Prime
    # needs two Lex Primes, and with three built the Lex is not owed to it.
    consumers = ([r["name"] for r in dbq.needed_as_ingredient(connection, item_unique)]
                 if item_unique and built > 0
                 and dbq.copies_still_wanted(connection, item_unique) > 0 else [])
    # Named short rather than counted short, and shortened against the item's
    # own name: "Systems" is what a person looks for on the next screen,
    # "Lavos Prime Systems" is the same word three times.
    short = dbq.pieces_short_for(connection, item_unique) if item_unique else []
    item_label = (row["item_name"] or "").strip()
    missing = tuple(
        (piece["name"].removeprefix(item_label).strip() or piece["name"])
        for piece, _lacking in short)

    return Reward(
        position=position, raw_text=raw_text, part_unique=part_unique,
        name=row["name"], item_name=row["item_name"] or "",
        ducats=dbq.ducats(connection, part_unique),
        rarity=dbq.best_rarity(connection, part_unique),
        reach=reach, active_relics=active, own_chance=own_chance,
        owned_count=dbq.owned_count(connection, part_unique),
        count_needed=row["count_needed"] if "count_needed" in row.keys() else 1,
        item_built=built, still_needed_for=consumers,
        set_owned=held, set_total=total, set_missing=missing,
        match_confidence=confidence,
        item_mastered=bool(item_unique) and dbq.is_mastered(connection, item_unique),
        category=row["category"] or "",
    )
