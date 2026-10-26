"""Every reward screen that has ever been argued about, kept arguing.

The panel decides in fifteen seconds and the player checks it in five. When they
disagree, one of two things is true — the ranking is wrong, or the card did
not show why it was right — and both have happened. What must never happen is
a case being fixed and then quietly un-fixed by the next rule.

So each case here is a real screen, named by the decision that came out of it.
The rewards are built by hand rather than loaded from the database: what is
under test is the ordering, and a fixture that needs a particular inventory to
exist is a fixture that stops testing anything the day that inventory changes.

Run: python3 tests/test_ranking.py
"""
from __future__ import annotations

import copy
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wft.relic import score  # noqa: E402
from wft.relic.score import (  # noqa: E402
    DEFAULT_CRITERIA, DEFAULT_ORDER, Reward, RankingOrder, Tier, rank,
)
from wft.inventory.scan import Decision, _choose_offer  # noqa: E402


def missing(name, *, ducats, set_owned=0, set_total=4, mastered=False,
            reach="farmable", category="warframe", position=1, relics=3):
    """A piece the user does not hold."""
    return Reward(
        position=position, raw_text=name, part_unique=f"/p/{name}", name=name,
        item_name=name.split(" Prime")[0] + " Prime", ducats=ducats,
        reach=reach, active_relics=relics, own_chance=0.5, owned_count=0,
        count_needed=1, set_owned=set_owned, set_total=set_total,
        item_mastered=mastered, category=category, match_confidence=100.0)


def owned(name, *, ducats, held=1, set_owned=2, set_total=4, mastered=False,
          category="warframe", position=1):
    """A piece the user already holds enough of."""
    reward = missing(name, ducats=ducats, set_owned=set_owned,
                     set_total=set_total, mastered=mastered,
                     category=category, position=position)
    reward.owned_count = held
    return reward


def forma(position):
    return Reward(position=position, raw_text="Forma Blueprint")


CASES = [
    (
        "two duplicates, the dearer one wins",
        # The screen that raised it: a third Velox Prime Receiver at 15
        # against a Braton Prime Stock at 25. Neither advances a set, neither
        # earns mastery. Mastery used to decide, which is mastery deciding
        # between two picks that earn none.
        [forma(1),
         owned("Yareli Prime Chassis Blueprint", ducats=15, mastered=True,
               set_owned=1, position=2),
         owned("Velox Prime Receiver", ducats=15, held=2, set_owned=2,
               set_total=3, category="primary", position=3),
         owned("Braton Prime Stock", ducats=25, mastered=True, set_owned=2,
               category="primary", position=4)],
        "Braton Prime Stock",
    ),
    (
        "a mastered set's missing piece is judged on ducats",
        [missing("Fang Prime Blueprint", ducats=15, set_owned=1, set_total=5,
                 mastered=True, category="melee", position=1),
         owned("Limbo Prime Blueprint", ducats=45, set_owned=2, position=2),
         owned("Larkspur Prime Blueprint", ducats=15, set_owned=1, position=3),
         owned("Yareli Prime Chassis Blueprint", ducats=15, mastered=True,
               position=4)],
        "Limbo Prime Blueprint",
    ),
    (
        # The open boundary of the mastered-set rule.
        "the dearer mastered piece now wins",
        # Left unsettled when mastery still broke the tie under the tier: a
        # mastered missing piece worth 100 lost to a duplicate worth 45.
        [missing("Voruna Prime Systems", ducats=100, set_owned=1,
                 mastered=True, position=1),
         owned("Limbo Prime Blueprint", ducats=45, position=2)],
        "Voruna Prime Systems",
    ),
    (
        "equal ducats: the piece not yet held",
        # Paris Prime String, missing from a mastered set, against a third
        # Lavos Prime Chassis Blueprint. Both 15 ducats; the Paris at least
        # starts a set that can be sold whole.
        [owned("Lavos Prime Chassis Blueprint", ducats=15, held=2,
               set_owned=3, position=2),
         missing("Paris Prime String", ducats=15, set_owned=0, set_total=5,
                 mastered=True, category="primary", position=3)],
        "Paris Prime String",
    ),
    (
        "the collection outranks the purse",
        # A missing piece of a set that still owes mastery beats any pile of
        # ducats. This is the stated priority and the reason the split
        # is drawn where it is.
        [owned("Voruna Prime Systems", ducats=100, position=1),
         missing("Baruuk Prime Neuroptics", ducats=15, set_owned=3,
                 position=2)],
        "Baruuk Prime Neuroptics",
    ),
    (
        "finishing a set beats starting one, whatever the category",
        [missing("Dakra Prime Blade", ducats=65, set_owned=2, set_total=3,
                 category="melee", position=1),
         missing("Grendel Prime Systems", ducats=15, set_owned=0,
                 category="warframe", position=2)],
        "Dakra Prime Blade",
    ),
    (
        "unreachable beats farmable",
        [missing("Grendel Prime Systems", ducats=15, set_owned=3,
                 reach="farmable", position=1),
         missing("Hildryn Prime Blueprint", ducats=15, set_owned=0,
                 reach="unreachable", position=2)],
        "Hildryn Prime Blueprint",
    ),
    (
        "Warframe before weapon, level in every other way",
        [missing("Okina Prime Handle", ducats=15, set_owned=1, set_total=4,
                 category="melee", position=1),
         missing("Titania Prime Neuroptics", ducats=15, set_owned=1,
                 set_total=4, category="warframe", position=2)],
        "Titania Prime Neuroptics",
    ),
    (
        "Forma is never the pick",
        [forma(1),
         owned("Larkspur Prime Blueprint", ducats=15, mastered=True,
               position=2)],
        "Larkspur Prime Blueprint",
    ),
    (
        "a squadmate's copy is taken over your own",
        # Established rule: the player's own card is always the leftmost, and when
        # the same thing drops twice it is good manners to take somebody
        # else's.
        [missing("Grendel Prime Systems", ducats=15, set_owned=3, position=1),
         missing("Grendel Prime Systems", ducats=15, set_owned=3, position=3)],
        None,  # checked by position below
    ),
]


#: The 14 decisions of one tile, "Meso V15 Relic [Radiant]" on screen, in the
#: order its 36 variants produced them: (variant, name, score). Two read the
#: rank as "1%" and score 100 for V1, five read it right and score 100 for V15.
V15_OFFERS = [
    (18, "Meso V1 Relic", 100.0), (19, "Meso V1 Relic", 96.3),
    (22, "Meso V1 Relic", 100.0), (23, "Meso V1 Relic", 96.3),
    (24, "Meso V1 Relic", 96.3), (25, "Meso V1 Relic", 96.3),
    (27, "Meso V5 Relic", 88.0), (28, "Meso V1 Relic", 96.3),
    (29, "Meso V1 Relic", 96.3), (30, "Meso V15 Relic", 100.0),
    (31, "Meso V15 Relic", 100.0), (33, "Meso V15 Relic", 100.0),
    (34, "Meso V15 Relic", 100.0), (35, "Meso V15 Relic", 100.0),
]


def check_needed_rule() -> list[str]:
    """Open next and the relic dialog use the floor the Ducats page uses."""
    import tempfile

    from wft.data import db as dbq
    from wft.relic import openings
    from wft.relic.score import load_reward

    problems = []
    home = pathlib.Path(tempfile.mkdtemp())

    def new(name):
        return dbq.connect(home / f"{name}.sqlite")

    def item(c, unique, name, category="secondary"):
        c.execute("INSERT INTO items(unique_name, name, category, is_prime) "
                  "VALUES(?,?,?,1)", (unique, name, category))

    def part(c, unique, name, owner, needed=1):
        c.execute("INSERT INTO parts(unique_name, name, item_unique, ducats, "
                  "count_needed) VALUES(?,?,?,10,?)", (unique, name, owner, needed))

    def own(c, unique, count=0, mastered=0):
        c.execute("INSERT INTO owned_items(item_unique, count, mastered, source) "
                  "VALUES(?,?,?,'test')", (unique, count, mastered))

    def pair(c, key, built, mastered, outer_mastered=0):
        item(c, f"W:{key}", f"{key.title()} Prime")
        part(c, f"P:{key}-bp", f"{key.title()} Prime Blueprint", f"W:{key}")
        part(c, f"P:{key}-r", f"{key.title()} Prime Receiver", f"W:{key}")
        item(c, f"W:ak{key}", f"Ak{key} Prime")
        part(c, f"P:ak{key}-bp", f"Ak{key} Prime Blueprint", f"W:ak{key}")
        part(c, f"W:{key}", f"{key.title()} Prime", f"W:ak{key}", needed=2)
        own(c, f"W:{key}", built, mastered)
        if outer_mastered:
            own(c, f"W:ak{key}", 0, outer_mastered)
        c.commit()

    def verdict(c, unique):
        return openings.needed(c, unique, dbq.retained_counts(c))

    def expect(label, got, want):
        if got != want:
            problems.append(f"needed rule, {label}: got {got}, wanted {want}")

    # Bronco: Bronco Prime mastered, Akbronco Prime done too.
    c = new("bronco")
    pair(c, "bronco", 0, 1, outer_mastered=1)
    expect("Bronco-like, consumer done", verdict(c, "P:bronco-r"), False)
    expect("Bronco-like, same answer without the shared dict",
           openings.needed(c, "P:bronco-r"), False)
    # Lex: 3 built, Aklex still open and wants 2.
    c = new("lex")
    pair(c, "lex", 3, 1)
    expect("Lex-like, built copies cover the chain", verdict(c, "P:lex-r"), False)
    expect("Lex-like, the item itself (Lex Prime as Aklex part)",
           verdict(c, "W:lex"), False)
    reward = load_reward(c, "P:lex-r", 1, "Lex Prime Receiver")
    if reward.still_needed_for or reward.is_missing:
        problems.append("needed rule, Lex-like: the reward is still owed to "
                        f"{reward.still_needed_for}, missing={reward.is_missing}")
    # The keep mark asks for a third copy: still covered with 3 built.
    dbq.set_kept(c, "W:lex", True)
    expect("Lex-like with the mark, 3 built", verdict(c, "P:lex-r"), False)
    c.execute("UPDATE owned_items SET count = 2 WHERE item_unique = 'W:lex'")
    c.commit()
    expect("Lex-like with the mark, 2 built", verdict(c, "P:lex-r"), True)
    reward = load_reward(c, "P:lex-r", 1, "Lex Prime Receiver")
    if reward.still_needed_for != ["Aklex Prime"] or not reward.is_missing:
        problems.append("needed rule, Lex 2 built + mark: reward lost its "
                        f"consumer ({reward.still_needed_for})")
    # Magnus: nothing built, keep mark.
    c = new("magnus")
    pair(c, "magnus", 0, 0)
    dbq.set_kept(c, "W:magnus", True)
    expect("Magnus-like, mark and none built", verdict(c, "P:magnus-r"), True)
    # An ordinary unbuilt set, and Forma.
    c = new("plain")
    item(c, "W:solo", "Solo Prime")
    part(c, "P:solo-r", "Solo Prime Receiver", "W:solo")
    item(c, "F:forma", "Forma", "reward_other")
    part(c, "P:forma", "Forma Blueprint", "F:forma")
    c.commit()
    expect("unbuilt set part", verdict(c, "P:solo-r"), True)
    expect("Forma", verdict(c, "P:forma"), False)
    # A mastered set's part stays 'missing' for the purse tie-break.
    c = new("mastered")
    pair(c, "vasto", 0, 1, outer_mastered=1)
    reward = load_reward(c, "P:vasto-r", 1, "Vasto Prime Receiver")
    expect("mastered set part stays missing", reward.is_missing, True)
    # rank() on a shelf: the relic with only the Bronco-like part vanishes.
    c = new("shelf")
    pair(c, "bronco", 0, 1, outer_mastered=1)
    item(c, "W:solo", "Solo Prime")
    part(c, "P:solo-r", "Solo Prime Receiver", "W:solo")
    c.execute("INSERT INTO relics(unique_name, name, tier, vaulted) "
              "VALUES('R:a','Lith A1 Relic','Lith',0)")
    c.execute("INSERT INTO relics(unique_name, name, tier, vaulted) "
              "VALUES('R:b','Lith B1 Relic','Lith',0)")
    for rel, rewards in (("R:a", ["P:bronco-r"]), ("R:b", ["P:solo-r"])):
        for part_unique in rewards:
            c.execute("INSERT INTO relic_rewards(relic_unique, part_unique, rarity) "
                      "VALUES(?,?,'RARE')", (rel, part_unique))
        c.execute("INSERT INTO owned_relics(relic_unique, count, source) "
                  "VALUES(?,1,'test')", (rel,))
    c.commit()
    ranked = [o.name for o in openings.rank(c) if o.expected > 0]
    if ranked != ["Lith B1 Relic"]:
        problems.append(f"needed rule, rank(): got {ranked}")
    entry = openings.rank(c)[0]
    if openings.percent(entry.expected) != "2 %" or \
            openings.percent(entry.refined) != "10 %":
        problems.append("percent text: "
                        f"{openings.percent(entry.expected)} / "
                        f"{openings.percent(entry.refined)}")
    return problems


def check_offer_choice() -> list[str]:
    """The tile's name is the one most top-scoring variants reached."""
    problems = []
    options = [(Decision(name, score, 3.7, "r", refinement="RADIANT"), index)
               for index, name, score in V15_OFFERS]
    got = _choose_offer(options)[0]
    if got.name != "Meso V15 Relic" or got.refinement != "RADIANT":
        problems.append(f"relic tile: chose {got.name} {got.refinement}, "
                        f"wanted Meso V15 Relic RADIANT")
    # A tie at the top score keeps the first, as before.
    tied = [(Decision("A", 90.0, 1.0, "a"), 0), (Decision("B", 90.0, 1.0, "b"), 1)]
    if _choose_offer(tied)[0].name != "A":
        problems.append("tie at the top score no longer keeps the first")
    return problems


# -- The order is the player's, and the default is the old one ---------

#: The key as it was before the order could be set, kept
#: verbatim as the reference. The default order must reproduce it exactly.
REF_RARITY_ORDER = {"RARE": 3, "UNCOMMON": 2, "COMMON": 1}
REF_CATEGORY_ORDER = {"warframe": 0, "archwing": 1, "companion": 2, "necramech": 3}
REF_CATEGORY_LAST = 9


def ref_sort_key(reward: Reward) -> tuple:
    category = REF_CATEGORY_ORDER.get(reward.category, REF_CATEGORY_LAST)

    # Forma is not a pick, it is what is left when there is nothing to pick.
    if reward.tier is Tier.JUNK:
        return (2, 0, False, 0, 0, 0, 0, 0, 0.0, reward.position)

    if reward.is_missing and not reward.item_mastered:
        return (
            0,
            int(reward.tier),
            not reward.completes_set,
            -reward.set_progress,
            category,
            reward.set_total or 99,
            reward.active_relics or 99,
            -REF_RARITY_ORDER.get(reward.rarity.upper(), 0),
            reward.own_chance,
            reward.position,
        )

    return (
        1,
        -(reward.ducats or 0),
        not reward.is_missing,
        category,
        0, 0, 0, 0,
        reward.own_chance,
        reward.position,
    )


def ref_rank(rewards):
    """`rank()` as it was: (positions in order, best, alternative)."""
    scored = [score.classify(r) for r in rewards]
    ordered = sorted(scored, key=ref_sort_key)
    identified = [r for r in ordered if r.part_unique is not None]
    best = identified[0] if identified else None
    best = score._prefer_a_squadmates_copy(best, identified)
    alternative = None
    if best is not None and best.is_missing:
        candidates = [r for r in ordered[1:]
                      if not r.is_missing and r.ducats
                      and r.ducats >= score.DUCAT_THRESHOLD]
        if candidates:
            alternative = max(candidates, key=lambda r: r.ducats or 0)
    return ([r.position for r in ordered],
            best.position if best else None,
            alternative.position if alternative else None)


CATEGORIES = ["warframe", "archwing", "companion", "necramech", "primary",
              "secondary", "melee", "archgun", "archmelee", "companion_weapon",
              "other", "weapon", "beast", "special", ""]


def random_part(rng, index):
    """One reward's facts, from small pools so that ties are common."""
    set_total = rng.choice([0, 2, 3, 4, 4, 5, 6])
    set_owned = rng.randint(0, set_total) if set_total else 0
    kind = rng.random()
    if kind < 0.06:
        return dict(part_unique=None, raw_text=rng.choice(
            ["Forma Blueprint", "Forma", "gibberish xx"]))
    if kind < 0.10:
        # The catalogue lists Forma as a part of itself.
        return dict(part_unique=f"/p/forma{index}", name="Forma Blueprint",
                    item_name="Forma", raw_text="Forma Blueprint",
                    ducats=0, category="other")
    return dict(
        part_unique=f"/p/{index}", name=f"Part {index}",
        item_name=f"Item {index} Prime", raw_text=f"part {index}",
        ducats=rng.choice([None, 0, 15, 15, 25, 45, 65, 100]),
        rarity=rng.choice(["COMMON", "UNCOMMON", "RARE", "COMMON", "odd"]),
        reach=rng.choice(["farmable", "scarce", "unreachable", "unknown"]),
        active_relics=rng.choice([0, 1, 1, 2, 3, 5]),
        own_chance=rng.choice([0.0, 0.0, 0.02, 0.1, 0.5]),
        owned_count=rng.choice([0, 0, 1, 2, 3]),
        count_needed=rng.choice([1, 1, 1, 2]),
        item_built=rng.choice([0, 0, 0, 1, 2]),
        still_needed_for=rng.choice([[], [], ["Other Prime"]]),
        set_owned=set_owned, set_total=set_total,
        item_mastered=rng.random() < 0.3,
        category=rng.choice(CATEGORIES),
        match_confidence=100.0)


def random_screen(seed):
    rng = random.Random(seed)
    cards = rng.choice([2, 4, 4])
    pool = [random_part(rng, i) for i in range(rng.randint(1, 5))]
    rewards = []
    for position in range(1, cards + 1):
        # Mostly the same few parts, so the same part is on screen twice now
        # and then (the squadmate rule); sometimes a fresh one.
        facts = (dict(rng.choice(pool)) if rng.random() < 0.6
                 else random_part(rng, 100 + position))
        rewards.append(Reward(position=position, **facts))
    return rewards


def check_default_is_the_old_key(screens: int = 20000) -> list[str]:
    """Every screen: the same order, the same best, the same alternative."""
    problems = []
    seen = dict(picked=0, alternative=0, squadmate=0, junk_only=0, cards=0)
    for seed in range(screens):
        base = random_screen(seed)
        old = ref_rank(copy.deepcopy(base))

        scored = [score.classify(r) for r in copy.deepcopy(base)]
        new_order = [r.position for r in sorted(scored, key=score._sort_key)]
        verdict = rank(copy.deepcopy(base))
        explicit = rank(copy.deepcopy(base), DEFAULT_ORDER)
        new = (new_order,
               verdict.best.position if verdict.best else None,
               verdict.alternative.position if verdict.alternative else None)
        again = (explicit.best.position if explicit.best else None,
                 explicit.alternative.position if explicit.alternative else None)

        seen["cards"] += len(base)
        seen["picked"] += old[1] is not None
        seen["alternative"] += old[2] is not None
        seen["junk_only"] += old[1] is None
        if new != old or again != old[1:]:
            problems.append(f"default order, screen {seed}: old {old}, new {new}")
            if len(problems) >= 5:
                break
    check_default_is_the_old_key.seen = seen
    return problems


def check_order_from_config() -> list[str]:
    from wft.config import Config

    problems = []

    def order(data):
        return score.order_from_config(Config(data, pathlib.Path("x")))

    if order({}) != DEFAULT_ORDER:
        problems.append("no [ranking] in the config is not the default order")
    if score.order_from_config(None) != DEFAULT_ORDER:
        problems.append("no config at all is not the default order")
    got = order({"ranking": {"criteria": ["ducats", "bogus", "type", "ducats"]}})
    want = ("ducats", "type") + tuple(
        c for c in DEFAULT_CRITERIA if c not in ("ducats", "type"))
    if got.criteria != want:
        problems.append(f"damaged criteria list: got {got.criteria}")
    if order({"ranking": {"criteria": "ducats"}}) != DEFAULT_ORDER:
        problems.append("a criteria value that is not a list is not ignored")
    got = order({"ranking": {"types": {"warframe": 7, "melee": 0, "primary": True,
                                       "nonsense": 1, "other": 2}}})
    numbers = dict(got.types)
    if (numbers["warframe"], numbers["melee"], numbers["primary"],
            numbers["other"]) != (7, 5, 5, 2) or "nonsense" in numbers:
        problems.append(f"type numbers read wrongly: {numbers}")
    if got.type_number("beast") != 2 or got.type_number("warframe") != 7:
        problems.append("a type that is not listed does not take the number of "
                        "'other'")
    # The default shipped in config.example.toml (commented out there) names
    # every criterion and type the code has, in the code's order.
    example = (pathlib.Path(__file__).resolve().parent.parent
               / "config.example.toml").read_text()
    wanted = '# criteria = [' + ", ".join(f'"{c}"' for c in DEFAULT_CRITERIA[:5])
    if wanted not in example:
        problems.append("config.example.toml does not show the default criteria")
    for name, number in score.DEFAULT_TYPE_NUMBERS.items():
        if f"# {name} = {number}" not in example:
            problems.append(f"config.example.toml: default for {name} differs")
    return problems


def order_of(rewards, order):
    """Names best first under `order`, Forma and the unread left out."""
    verdict = rank(copy.deepcopy(rewards), order)
    scored = [score.classify(r) for r in copy.deepcopy(rewards)]
    ranked = sorted(scored, key=lambda r: score._sort_key(r, order))
    return [r.name for r in ranked if r.name], verdict


def check_other_orders() -> list[str]:
    problems = []

    def expect(label, got, want):
        if got != want:
            problems.append(f"{label}\n      wanted {want}, got {got}")

    def with_criteria(*first):
        return RankingOrder(tuple(first) + tuple(
            c for c in DEFAULT_CRITERIA if c not in first))

    def with_types(**numbers):
        merged = dict(score.DEFAULT_TYPE_NUMBERS)
        merged.update(numbers)
        return RankingOrder(DEFAULT_CRITERIA, tuple(merged.items()))

    # Ducats first. A Warframe piece the player is missing against a duplicate
    # worth 100: the default takes the piece, ducats first takes the duplicate
    # and then no longer offers the duplicate as an alternative.
    screen = [missing("Khora Prime Chassis", ducats=15, set_owned=3, position=1),
              owned("Voruna Prime Systems", ducats=100, position=2),
              owned("Limbo Prime Blueprint", ducats=45, position=3)]
    names, verdict = order_of(screen, DEFAULT_ORDER)
    expect("default: the missing piece first", names[0], "Khora Prime Chassis")
    expect("default: the alternative is the dearest duplicate",
           verdict.alternative.name, "Voruna Prime Systems")
    names, verdict = order_of(screen, with_criteria("ducats"))
    expect("ducats first: the order", names,
           ["Voruna Prime Systems", "Limbo Prime Blueprint", "Khora Prime Chassis"])
    expect("ducats first: the pick", verdict.best.name, "Voruna Prime Systems")
    expect("ducats first: no alternative when the pick is a duplicate",
           verdict.alternative, None)

    # Ducats above "collection" count for every card: the dearest card wins
    # whether it is missing or a duplicate.
    cheap_missing = missing("Khora Prime Chassis", ducats=15, set_owned=3,
                            position=1)
    dear_dup = owned("Voruna Prime Systems", ducats=100, position=2)
    dear_missing = missing("Hildryn Prime Blueprint", ducats=100, set_owned=0,
                           position=1)
    cheap_dup = owned("Limbo Prime Blueprint", ducats=15, position=2)
    expect("ducats first: a duplicate worth 100 beats a missing piece worth 15",
           order_of([cheap_missing, dear_dup], with_criteria("ducats"))[1].best.name,
           "Voruna Prime Systems")
    expect("ducats first: a missing piece worth 100 beats a duplicate worth 15",
           order_of([dear_missing, cheap_dup], with_criteria("ducats"))[1].best.name,
           "Hildryn Prime Blueprint")
    # Below "collection" ducats keep the groups: they only tell the purse apart.
    expect("collection, then ducats: the missing piece beats the dearer duplicate",
           order_of([cheap_missing, dear_dup],
                    with_criteria("collection", "ducats"))[1].best.name,
           "Khora Prime Chassis")

    # Ducats below "collection" leave the collection's own order alone: two
    # missing pieces are still told apart by the criteria behind it.
    screen = [missing("Hildryn Prime Blueprint", ducats=100, set_owned=0,
                      reach="farmable", position=1),
              missing("Grendel Prime Systems", ducats=15, set_owned=0,
                      reach="unreachable", position=2)]
    expect("ducats first: collection pieces keep their own order",
           order_of(screen, with_criteria("collection", "ducats"))[0],
           ["Grendel Prime Systems", "Hildryn Prime Blueprint"])

    # Item type first. Dakra's Blade finishes a set, Titania's Neuroptics
    # starts one: finishing wins by default, the Warframe wins with the type
    # first.
    screen = [missing("Dakra Prime Blade", ducats=15, set_owned=2, set_total=3,
                      category="melee", position=1),
              missing("Titania Prime Neuroptics", ducats=15, set_owned=0,
                      category="warframe", position=2)]
    expect("default: finishing the set first",
           order_of(screen, DEFAULT_ORDER)[0][0], "Dakra Prime Blade")
    expect("item type first: the Warframe first",
           order_of(screen, with_criteria("type"))[0][0],
           "Titania Prime Neuroptics")

    # Item type above "collection" counts for every card: with Warframe 1 a
    # Warframe duplicate beats a needed weapon part. Below it, it does not.
    screen = [missing("Okina Prime Handle", ducats=15, set_owned=1,
                      category="melee", position=1),
              owned("Yareli Prime Chassis Blueprint", ducats=15,
                    category="warframe", position=2)]
    expect("default: the needed weapon part beats the Warframe duplicate",
           order_of(screen, DEFAULT_ORDER)[1].best.name, "Okina Prime Handle")
    expect("item type first: the Warframe duplicate beats the needed weapon part",
           order_of(screen, with_criteria("type"))[1].best.name,
           "Yareli Prime Chassis Blueprint")
    expect("collection first, then type: the needed weapon part again",
           order_of(screen, with_criteria("collection", "type"))[1].best.name,
           "Okina Prime Handle")

    # "Collection" last: one ordered list. The tier now decides first, so a
    # mastered set's unreachable piece beats an unmastered set's farmable one,
    # which the default would never do.
    last = RankingOrder(tuple(c for c in DEFAULT_CRITERIA if c != "collection")
                        + ("collection",))
    screen = [missing("Grendel Prime Systems", ducats=15, set_owned=3,
                      reach="farmable", position=1),
              missing("Hildryn Prime Blueprint", ducats=15, set_owned=0,
                      reach="unreachable", mastered=True, position=2)]
    expect("default: the unmastered farmable piece first",
           order_of(screen, DEFAULT_ORDER)[0][0], "Grendel Prime Systems")
    expect("collection last: the unreachable piece first",
           order_of(screen, last)[0][0], "Hildryn Prime Blueprint")
    # Ducats first with "collection" second: the 100-ducat duplicate wins.
    screen = [missing("Grendel Prime Systems", ducats=15, set_owned=2,
                      position=1),
              owned("Limbo Prime Blueprint", ducats=100, set_owned=2,
                    held=1, position=2)]
    ranked_last = order_of(screen, RankingOrder(("ducats", "collection")
                           + tuple(c for c in DEFAULT_CRITERIA
                                   if c not in ("ducats", "collection"))))
    expect("ducats first, collection second: the 100-ducat duplicate",
           ranked_last[0][0], "Limbo Prime Blueprint")

    # A weapon type above Warframe.
    screen = [missing("Titania Prime Neuroptics", ducats=15, set_owned=1,
                      category="warframe", position=1),
              missing("Okina Prime Handle", ducats=15, set_owned=1,
                      category="melee", position=2)]
    expect("default: the Warframe above the melee weapon",
           order_of(screen, DEFAULT_ORDER)[0][0], "Titania Prime Neuroptics")
    expect("melee 1, Warframe 2: the melee weapon first",
           order_of(screen, with_types(melee=1, warframe=2))[0][0],
           "Okina Prime Handle")
    # Equal numbers weigh the same: the next criterion (set size) decides.
    # Both are at 25 %; the Warframe's set has 8 pieces, the weapon's has 4.
    big = missing("Titania Prime Neuroptics", ducats=15, set_owned=2, set_total=8,
                  category="warframe", position=1)
    small = missing("Okina Prime Handle", ducats=15, set_owned=1, set_total=4,
                    category="melee", position=2)
    expect("equal numbers weigh the same",
           order_of([big, small], with_types(melee=1, warframe=1))[0][0],
           "Okina Prime Handle")
    expect("... while the default still puts the Warframe first",
           order_of([big, small], DEFAULT_ORDER)[0][0], "Titania Prime Neuroptics")
    # A type that is not listed counts as 'other'.
    odd = missing("Odd Prime Part", ducats=15, set_owned=1, category="beast",
                  position=2)
    expect("an unlisted type takes the number of 'other'",
           order_of([screen[0], odd], with_types(other=1, warframe=2))[0][0],
           "Odd Prime Part")

    # The item type also orders two duplicates that are level on ducats.
    pair = [owned("Braton Prime Stock", ducats=25, category="primary",
                  mastered=True, position=1),
            owned("Yareli Prime Chassis Blueprint", ducats=25,
                  mastered=True, position=2)]
    expect("default: the Warframe duplicate first",
           order_of(pair, DEFAULT_ORDER)[0][0], "Yareli Prime Chassis Blueprint")
    expect("primary 1: the primary duplicate first",
           order_of(pair, with_types(primary=1, warframe=2))[0][0],
           "Braton Prime Stock")

    # Type above completing: 2/3 finishes its set, 4/6 does not and is just as
    # far along, so by default the finisher wins and with the type first the
    # Warframe does.
    screen = [missing("Dakra Prime Blade", ducats=15, set_owned=2, set_total=3,
                      category="melee", position=1),
              missing("Grendel Prime Systems", ducats=15, set_owned=4,
                      set_total=6, category="warframe", position=2)]
    expect("default: finishing wins at equal progress",
           order_of(screen, DEFAULT_ORDER)[0][0], "Dakra Prime Blade")
    expect("type above completing: the Warframe wins instead",
           order_of(screen, with_criteria("type"))[0][0], "Grendel Prime Systems")

    # The squadmate rule stands whatever the order.
    twin = [missing("Grendel Prime Systems", ducats=15, set_owned=3, position=1),
            missing("Grendel Prime Systems", ducats=15, set_owned=3, position=3)]
    for order in (DEFAULT_ORDER, with_criteria("ducats"), with_criteria("type")):
        if rank(copy.deepcopy(twin), order).best.position == 1:
            problems.append("squadmate rule: took position 1 under a changed order")
    # Forma stays last under any order.
    forma_screen = [forma(1), owned("Larkspur Prime Blueprint", ducats=15,
                                    mastered=True, position=2)]
    for order in (DEFAULT_ORDER, with_criteria("ducats", "type"),
                  with_criteria("rarity")):
        if rank(copy.deepcopy(forma_screen), order).best.name != \
                "Larkspur Prime Blueprint":
            problems.append("Forma was picked under a changed order")
    return problems


def main() -> int:
    problems = check_offer_choice() + check_needed_rule()
    problems += check_default_is_the_old_key() + check_order_from_config()
    problems += check_other_orders()
    for label, rewards, expected in CASES:
        verdict = rank(rewards)
        got = verdict.best.name if verdict.best else None
        if expected is None:
            # The squadmate rule: same part twice, the tool must not point at
            # position 1.
            if verdict.best is None or verdict.best.position == 1:
                problems.append(f"{label}: took the player's own copy at position 1")
            continue
        if got != expected:
            order = " > ".join(r.name or "Forma"
                               for r in sorted(rewards, key=lambda r: r.position))
            problems.append(f"{label}\n      wanted {expected}, got {got}"
                            f"\n      screen: {order}")

    for problem in problems:
        print("   ", problem)
    seen = check_default_is_the_old_key.seen
    print(f"ranking            : {len(CASES)} documented screen(s), and the "
          f"default order against the old key on 20000 generated screens "
          f"({seen['cards']} cards, {seen['picked']} with a pick, "
          f"{seen['alternative']} with an alternative)")
    if problems:
        print(f"FAILED — {len(problems)} problem(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
