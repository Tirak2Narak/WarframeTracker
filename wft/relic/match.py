"""Matching OCR text to a known part.

The whole design rests on one observation: the reward screen can only show
parts that the open relics actually drop. Every relic has exactly six rewards,
so four open relics allow at most 24 distinct names. Matching against 24
candidates is a different problem from matching against the 18952 entries of
the manifest — the previous attempt did the latter, with Fuse.js at threshold
0.35, and spent fifteen commits tuning it.

Real OCR output from a low-quality recording shows why this matters:

    "Cedo lﬂime Barrel"      -> Cedo Prime Barrel
    "Neuroplics Blueprint"   -> Neuroptics Blueprint

Both are unambiguous among six candidates and genuinely ambiguous among a
thousand. The fix for bad OCR is a smaller search space, not a looser threshold.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz, process

#: Below this, a match is reported as uncertain rather than returned. Set high
#: on purpose: with a candidate list this small, a genuine match scores far
#: above it, so a low score means the text is not a reward name at all — a
#: player name, a UI label, a misfired crop.
MIN_SCORE = 70.0

#: Above this, the match is treated as settled and never queued for review.
CONFIDENT_SCORE = 88.0

#: What a wide-list match must score before its name goes on the board.
#: Cedo Prime Barrel at 100 and Lex Prime Receiver at 90 are named; nothing
#: under 88 is: six hundred candidates are held to a bar
#: twenty-three would sail over. It tracks CONFIDENT_SCORE on purpose, so
#: that retuning the review threshold widens what the full list may name
#: knowingly rather than by side effect.
WIDE_NAME_SCORE = CONFIDENT_SCORE

#: A candidate list at or below this size came from the relics the game logged,
#: which means it is not a guess about what might be on screen - it is the set
#: of things that can be. Four relics drop at most 24 distinct parts.
NARROW_LIST = 40

#: The rescue pass on a narrow list uses `partial_ratio` instead of a lower
#: threshold on `ratio`, because the failure it repairs has a shape.
#:
#: 'g Y TR Forma Blueprint Qu: fi; -' contains its answer intact, wrapped in
#: glyphs Tesseract invented at the edges of the crop. `ratio` charges for every
#: one of those characters and returned 69.8 - below the threshold, on the most
#: common reward in the game. `partial_ratio` aligns the candidate inside the
#: noise and returns 100, with the runner-up at 65.
#:
#: An earlier version handled this by accepting a `ratio` of 69.8 because it led
#: by eleven points. That worked by two tenths of a point and would have failed
#: on a slightly noisier crop, which is not a foundation for the one feature
#: that has fifteen seconds to be right.
#:
#: Only on the narrow list, and only as a rescue: `partial_ratio` scores any
#: substring as perfect, so on 746 candidates it would find something plausible
#: for anything at all.
RESCUE_SCORE = 90.0

#: And only with room to spare. Two candidates that both contain the reading are
#: not an answer, they are a coin toss - the tie goes to the review rather than
#: to whichever the sort put first.
RESCUE_LEAD = 8.0

#: Characters Tesseract routinely invents at the edges of Warframe's font.
#: The vertical bar is spared here and dealt with by `_EDGE_BAR` below, because
#: it is the one mark that is sometimes a letter: it is what this font's capital
#: I and lowercase l come back as. `_CONFUSIONS` has always known that, but the
#: knowledge was unreachable - the bar was deleted before the table could weigh
#: it.
#:
#: What that cost, measured on a relics capture: a tile holding nine
#: Requiem I read as "Requiem | Relic" sixteen times over. Stripped, it became
#: "Requiem Relic" - which is the exact name of a resource in the decoy list.
#: The decoy matched at 100 against the relic's 92.9, so every one of those
#: sixteen readings was dismissed as scenery and the tile never reached the
#: database. Carried, the same reading scores 93.3 against the relic, 92.9
#: against the decoy, and 100 once the confusions are collapsed.
#:
#: No character is invented: the bar is carried, not translated. Reading it as
#: an "I" stays the job of `ocr_key`, where the substitution is weighed against
#: a named alternative instead of asserted.
_NOISE = re.compile(r"[^A-Za-z0-9| ]+")
_SPACES = re.compile(r"\s+")

#: A bar at either end of a reading, dropped again. There it is not a letter,
#: it is the tile's own border caught by the crop, and carrying it costs real
#: matches: "Xa lueprinit |" scores 84.6 against Xaku Blueprint without the
#: trailing bar and 78.6 with it, which is the difference between a tile that
#: is read and one that is not - measured on `crop_c.png` in the fixtures,
#: where it was the whole cost of the change above.
#:
#: Position is what separates the two, and it separates them cleanly. A bar
#: standing between words sits where a word sits, and in "Requiem | Relic" the
#: word it stands in for is the name's identity. A bar at the edge borders on
#: nothing.
_EDGE_BAR = re.compile(r"^[|\s]+|[|\s]+$")


_RANK_SUFFIX = re.compile(r"[\[\(]?\s*\d{1,2}\s*[\]\)]?\s*$")

#: A relic's refinement, printed in brackets after its name. Removed for the
#: same reason the rank is: "Meso L3 Relic [Radiant]" has to match "Meso L3
#: Relic", or a refined relic scores like a misreading of an unrefined one.
_REFINEMENT_SUFFIX = re.compile(
    r"\s*[\[\(]?\s*(?:Intact|Exceptional|Flawless|Radiant)\s*[\]\)]?\s*$",
    re.IGNORECASE)


def strip_rank(text: str) -> str:
    """Remove a trailing rank from an inventory reading.

    The game writes "Nataruk [30]" and OCR usually loses the brackets, leaving
    "Nataruk 30". Matched as-is that scores 82 against Nataruk instead of 100,
    which pushes a correct reading into the review queue and, worse, close
    enough to a wrong one to lose to it.

    Only a trailing one- or two-digit group is removed. Numbers inside a name
    are part of it — "Mk1-Braton", "AX-52" — and a three-digit trailing number
    is a quantity, not a rank.
    """
    stripped = _RANK_SUFFIX.sub("", text).strip()
    # Never strip the whole thing: "30" on its own is not a name with a rank.
    return stripped if len(stripped) >= 3 else text


def normalise(text: str) -> str:
    """Strip what OCR adds without touching what it got right.

    Deliberately conservative: this removes punctuation and collapses spaces,
    but performs no character substitutions. Fixing "lﬂime" into "Prime" by
    rule would need a confusion table that grows forever — which is exactly the
    repair work the previous attempts drowned in. The candidate list does that
    job better.
    """
    cleaned = _NOISE.sub(" ", text)
    tidied = _EDGE_BAR.sub("", _SPACES.sub(" ", cleaned).strip())
    without_refinement = _REFINEMENT_SUFFIX.sub("", tidied).strip()
    # Never strip the whole reading: a tile that read as nothing but "Radiant"
    # is noise, and keeping it lets it lose against the decoys as usual.
    if len(without_refinement) >= 3:
        tidied = without_refinement
    return strip_rank(tidied)


#: Case folding is done by the scorer rather than by `normalise`, so the reading
#: shown in the panel and the review stays as it was read. Without it the
#: matching is silently case-sensitive: "LEX PRIME BARREL" scores 31 against
#: "Lex Prime Barrel" instead of 100. The game writes reward names in mixed
#: case today, which is the only reason this has not already broken - it is one
#: UI change away from taking out every match at once.
_FOLD = str.casefold

#: Characters OCR trades for one another, mapped to a single stand-in. Used only
#: to break ties, never to score: collapsing these outright would make "Bo" and
#: "80" the same word.
#:
#: The case that forced it: a tile plainly reading "Lith O4 Relic" was read as
#: "Lith 04 Relic", and Lith Z4, V4, N4, W4 and O4 all scored 92.3 against it -
#: one character wrong is one character wrong, whichever character it is. But
#: they are not equally likely. A zero read for a capital O is the commonest
#: substitution there is; a zero read for a Z is not, and Lith O4 exists.
#: Keys are lower case because folding happens first. Kept to the pairs that
#: are near-identical in this font rather than merely similar: a zero reads as
#: an O far more readily than as a D, and including D as well made them
#: indistinguishable again - which is the tie this exists to break.
_CONFUSIONS = str.maketrans({
    "0": "o",
    "1": "i", "l": "i", "|": "i",
    "5": "s",
    "8": "b",
})


def ocr_key(text: str) -> str:
    """The reading with visually interchangeable characters collapsed."""
    return text.casefold().translate(_CONFUSIONS)


#: Words that qualify a name without being one. A crop that catches only the
#: second line of a wrapped reward name yields "Blueprint", which the rescue
#: pass would happily align inside any blueprint on the list.
_QUALIFIERS = {"blueprint", "prime", "component", "part", "set", "relic"}


def _is_only_qualifiers(text: str) -> bool:
    words = text.lower().split()
    return bool(words) and all(w in _QUALIFIERS or w.isdigit() for w in words)


@dataclass
class Candidate:
    """One part that could plausibly appear, with the relic that would drop it."""

    part_unique: str
    name: str
    relic_unique: str | None = None
    rarity: str = "COMMON"


@dataclass
class Match:
    part_unique: str | None
    name: str
    score: float
    raw_text: str
    alternatives: list[tuple[str, float]]

    @property
    def confident(self) -> bool:
        return self.part_unique is not None and self.score >= CONFIDENT_SCORE

    @property
    def needs_review(self) -> bool:
        return self.part_unique is not None and self.score < CONFIDENT_SCORE


def candidates_for_relics(connection: sqlite3.Connection,
                          relic_uniques: list[str]) -> list[Candidate]:
    """Everything the given relics can drop — the small search space."""
    if not relic_uniques:
        return []
    placeholders = ",".join("?" * len(relic_uniques))
    # No join to `relics` and no vault status: `Candidate` carried a `vaulted`
    # field, it was dropped as unread, and this call kept passing it. Five
    # arguments into a four-field class is a TypeError, and it was raised
    # inside the capture worker - so the panel reported "capture failed" for
    # every single trigger and the reason sat in a log nobody reads during a
    # fissure. Two days of the panel being dead on its feet.
    rows = connection.execute(
        f"SELECT DISTINCT p.unique_name, p.name, rr.relic_unique, rr.rarity "
        f"FROM relic_rewards rr "
        f"JOIN parts p ON p.unique_name = rr.part_unique "
        f"WHERE rr.relic_unique IN ({placeholders})", relic_uniques).fetchall()
    return [Candidate(r["unique_name"], r["name"], r["relic_unique"],
                      r["rarity"]) for r in rows]


def all_prime_parts(connection: sqlite3.Connection) -> list[Candidate]:
    """Fallback when the relics in play are unknown.

    Everything a relic can drop, which is not the same as every prime part.
    It used to be the latter, and Forma is the difference: a genuine relic
    reward that belongs to no prime set, so it was not in the list and a Forma
    on the screen matched whatever came closest — "Forma Blueprint" came back
    as "Bo Prime Blueprint". A wrong name the user confirms is a wrong number
    in the inventory, which is worse than no name at all.

    Around 600 entries rather than 24 — worse than the log's answer, and still
    two orders of magnitude better than matching against everything.
    """
    rows = connection.execute(
        "SELECT DISTINCT p.unique_name, p.name FROM parts p "
        "JOIN relic_rewards r ON r.part_unique = p.unique_name").fetchall()
    return [Candidate(r["unique_name"], r["name"]) for r in rows]


def match(text: str, candidates: list[Candidate],
          min_score: float = MIN_SCORE) -> Match:
    """Best candidate for one OCR reading.

    Plain `ratio` — Levenshtein over the whole string — beats every cleverer
    scorer here, measured on real OCR output against 724 candidates:

        ratio             11/11
        token_sort_ratio  10/11
        WRatio            10/11
        partial_ratio     10/11
        token_set_ratio    8/11

    The token scorers fail for a structural reason worth remembering:
    `token_set_ratio` scores a subset as a perfect match, so "Lavos Prime
    Blueprint" gets 100 against "Lavos Prime Neuroptics Blueprint" and wins
    over the correct answer. Item names have a fixed word order and differ by
    exactly the words a token scorer discards, so order-insensitivity is not a
    feature here — it is the bug.
    """
    cleaned = normalise(text)
    if not cleaned or not candidates or _is_only_qualifiers(cleaned):
        return Match(None, "", 0.0, text, [])

    lookup = {c.name: c for c in candidates}
    results = process.extract(cleaned, list(lookup), scorer=fuzz.ratio,
                              processor=_FOLD, limit=10)

    accepted = bool(results) and results[0][1] >= min_score

    if results and not accepted and len(candidates) <= NARROW_LIST:
        rescue = process.extract(cleaned, list(lookup), scorer=fuzz.partial_ratio,
                                 processor=_FOLD, limit=2)
        if rescue and rescue[0][1] >= RESCUE_SCORE:
            runner_up = rescue[1][1] if len(rescue) > 1 else 0.0
            if rescue[0][1] - runner_up >= RESCUE_LEAD:
                # Report the ratio score, not the partial one. The partial score
                # says the answer was found inside the noise; it does not say
                # the reading was good, and a panel showing 100 for a crop that
                # read 'g Y TR Forma Blueprint Qu: fi; -' would be lying about
                # how much of it was legible.
                results = [(rescue[0][0], results[0][1], None)] + [
                    r for r in results if r[0] != rescue[0][0]]
                accepted = True

    if not accepted:
        return Match(None, "", results[0][1] if results else 0.0, text,
                     [(name, score) for name, score, _ in results])

    # Among candidates the scorer cannot separate, prefer the one that differs
    # only by characters OCR routinely confuses. "Lith 04 Relic" ties five
    # relics at 92.3, and only one of them - Lith O4 - is a plausible misread.
    if len(results) > 1 and results[0][1] == results[1][1]:
        key = ocr_key(cleaned)
        closest = process.extractOne(key, list(lookup), scorer=fuzz.ratio,
                                     processor=ocr_key)
        if (closest and closest[1] > fuzz.ratio(key, ocr_key(results[0][0]))
                and abs(closest[1] - results[0][1]) < 25):
            results = [(closest[0], results[0][1], 0)] + [
                r for r in results if r[0] != closest[0]]

    best_name, best_score, _ = results[0]
    return Match(lookup[best_name].part_unique, best_name, best_score, text,
                 [(name, score) for name, score, _ in results[1:4]])


def match_all(readings: list[str], candidates: list[Candidate]) -> list[Match]:
    """Match every reward on screen, keeping assignments unique.

    Two players can open the same relic and land on the same reward, so
    duplicates are legitimate and are not suppressed. What is suppressed is the
    same *candidate* winning twice on different text when a lower-scoring
    reading has a clear second choice — that is a sign the second reading was
    misassigned, not a genuine duplicate.
    """
    matches = [match(text, candidates) for text in readings]

    by_part: dict[str, list[int]] = {}
    for index, result in enumerate(matches):
        if result.part_unique:
            by_part.setdefault(result.part_unique, []).append(index)

    # Parts already spoken for, so a reassignment cannot walk into a collision
    # of its own. Without this the runner-up could be a part another reading
    # already holds, recreating exactly the state this loop exists to resolve.
    taken = {result.part_unique for result in matches if result.part_unique}

    for part_unique, indices in by_part.items():
        if len(indices) < 2:
            continue
        # Keep the strongest reading; let the others fall to their runner-up
        # only when that runner-up is itself credible and unclaimed.
        indices.sort(key=lambda i: matches[i].score, reverse=True)
        for weaker in indices[1:]:
            for name, score in matches[weaker].alternatives:
                if score < MIN_SCORE:
                    break
                replacement = next((c for c in candidates if c.name == name), None)
                if replacement is None or replacement.part_unique in taken:
                    continue
                rest = [alt for alt in matches[weaker].alternatives
                        if alt[0] != name]
                matches[weaker] = Match(replacement.part_unique, name, score,
                                        matches[weaker].raw_text, rest)
                taken.add(replacement.part_unique)
                break
    return matches
