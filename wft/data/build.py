"""Turning the downloaded manifests into a queryable database.

The export describes the game's build system, not a catalogue: items live in
`ExportWarframes`/`ExportWeapons`, their components in `ExportResources`, and
what belongs to what is only visible in `ExportRecipes`. This module joins the
three, then layers the curated files on top for what the export cannot express.

The join runs through recipes rather than through name prefixes on purpose.
"Rhino Prime Systems" starting with "Rhino Prime" is a coincidence of naming
that breaks on composite items; the recipe is the game's own answer.
"""
from __future__ import annotations

import collections
import json
import sqlite3
from pathlib import Path

from wft.config import Config
from wft.data import publicexport as pe

CURATED = Path(__file__).resolve().parent / "curated"

#: Markup the export puts in front of some names that the game never shows.
#: Thirteen archwing weapons are stored as "<ARCHWING> Veritux" while the game
#: and the player call them "Veritux". Left in place, OCR could never match
#: them: the reading is right and the stored name is wrong.
_NAME_PREFIXES = ("<ARCHWING> ", "<KDRIVE> ", "<NECRAMECH> ", "<VOID> ")


def _clean_name(name: str | None) -> str | None:
    if not name:
        return name
    cleaned = name.strip()
    for prefix in _NAME_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    return cleaned or None

# What counts as a set piece. Crafted components live under /Types/Recipes/;
# everything else an ingredient list mentions is raw material.
#
# The earlier marker-based exclusion list was too narrow and let 324 of 1542
# parts through that are ore, fish and trophies — Adramal Alloy
# (/Types/Items/Gems/Deimos/...), Aggristone (/Types/Gameplay/Duviri/...),
# Asterite (/Types/Items/RailjackMiscItems/...). They then counted towards set
# completion, so a Warframe could look closer to finished because the user
# happened to hold an ore.
#
# An allow-list on the path is the honest filter: relics drop recipe items, and
# recipe items are exactly what the user wants tracked.
_PART_PATH = "/Types/Recipes/"

#: The dual weapons break the rule above. Akbronco Prime is built from an
#: Akbronco Prime Link, a blueprint - and two whole Bronco Primes, which live
#: at /Lotus/Weapons/Tenno/Pistol/BroncoPrime and so failed the path test and
#: were filed as raw material. The tool then said "every piece is here, this
#: set can be built" while the game said NOT ENOUGH COMPONENTS.
#:
#: The test is two-part because neither half is enough. Being craftable alone
#: would promote Fieldron, Detonite Injector and Mutagen Mass, which have
#: recipes and are consumed by the dozen; measured over the manifests, 148
#: ingredients are craftable without living under /Types/Recipes/, and 105 of
#: them are gems, dog tags, focus lenses, helmets and bait. The 43 under
#: /Lotus/Weapons/ are the real ones - Bronco Prime, Lex Prime, Bolto and the
#: rest of the akimbo halves.
#:
#: `needed_as_ingredient` in the database layer was already written for this
#: case and documented with Aklex Prime consuming two Lex Prime. It had never
#: matched a row.
_WEAPON_PATH = "/Lotus/Weapons/"


def _is_built_weapon(unique: str, craftable: set) -> bool:
    """A finished weapon used as a component of another weapon."""
    return _WEAPON_PATH in unique and unique in craftable



def _curated(name: str) -> dict:
    path = CURATED / name
    return json.loads(path.read_text()) if path.is_file() else {}


def _strip_store(unique_name: str) -> str:
    """Relic rewards are named as store items; everything else is not.

    `/Lotus/StoreItems/Types/Recipes/...` and `/Lotus/Types/Recipes/...` are the
    same thing seen from two sides of the game's economy.
    """
    return unique_name.replace("/Lotus/StoreItems/", "/Lotus/")


#: Warframe's own categories, mapped to the ones a player counts in.
#: Verified against an in-game mastery screen: Suits 117 vs 116 counted,
#: Melee 224 vs 225, Pistols-with-slot-0 147 vs 149, SpaceMelee 8 vs 7. The
#: small gaps are version drift and items like Excalibur Prime that exist in the
#: data but not in a normal account.
#:
#: `slot` is what separates primary from secondary: productCategory "Pistols"
#: holds 328 entries, of which only the 147 with slot 0 are secondary weapons —
#: the rest are companion weapons and modular parts.
_PRODUCT_CATEGORY = {
    "Suits": "warframe",
    "SpaceSuits": "archwing",
    "MechSuits": "necramech",
    "Melee": "melee",
    "LongGuns": "primary",
    "Pistols": "secondary",
    "SpaceGuns": "archgun",
    "SpaceMelee": "archmelee",
    "SentinelWeapons": "companion_weapon",
    "Sentinels": "companion",
    # Beasts are their own tab in game — "COMPANIONS", 15 entries, holding
    # Kavats, Kubrows, Predasites, Vulpaphylas and the Helminth Charger. The
    # manifest's KubrowPets count is exactly 15, so the mapping is confirmed.
    # Counted for mastery, but never read from a recording: the
    # game's inventory shows a pet under its own name.
    "KubrowPets": "beast",
    "OperatorAmps": "amp",
    "SpecialItems": "special",
}

#: Categories the user wants tracked. Amps are excluded: they are
#: built from separately levelled parts, so a single "owned" flag says nothing.
#: `special` covers quest and event oddities that never appear in a mastery
#: count. Beasts count towards mastery.
#:
#: The untracked categories stay in the data rather than being discarded, so an
#: OCR reading of one is recognised and ignored on purpose instead of landing in
#: the review queue as unknown; which are counted and which are read in a scan
#: is defined once in `wft/data/db.py` (`NOT_FOR_MASTERY_CATEGORIES`,
#: `SCAN_IGNORED_CATEGORIES`).
TRACKED_CATEGORIES = {
    "warframe", "archwing", "necramech", "melee", "primary", "secondary",
    "archgun", "archmelee", "companion", "companion_weapon", "beast",
}


def _category(unique_name: str, entry: dict | None = None) -> str:
    """The category a player would file this under.

    Prefers the manifest's own productCategory, refined by slot where the
    product category is too coarse. Falls back to the path only for items
    reconstructed from recipes, which carry no product category at all.
    """
    if entry:
        product = _PRODUCT_CATEGORY.get(str(entry.get("productCategory")))
        # Venari and Venari Prime are filed as SpecialItems but are companions
        # and do count towards mastery. The manifest they live in is the
        # reliable signal here, not their product category.
        if product == "special" and "/Kavat/" in unique_name:
            return "companion"
        if product == "secondary" and entry.get("slot") != 0:
            # Pistols without slot 0 are companion weapons or modular pieces.
            return "companion_weapon" if entry.get("slot") == 1 else "other"
        if product:
            return product

    lowered = unique_name.lower()
    if "/powersuits/" in lowered or "warframerecipes" in lowered:
        return "warframe"
    if "/archwing" in lowered:
        return "archwing"
    if "sentinel" in lowered or "/companion" in lowered:
        return "companion"
    if "/weapons/" in lowered or "weaponparts" in lowered:
        return "weapon"
    return "other"



def _common_prefix_name(component_names: list[str]) -> str | None:
    """Derive the item name from the shared prefix of its component names.

    "Dual Zoren Prime Blade" and "Dual Zoren Prime Handle" agree on
    "Dual Zoren Prime". This is the last resort, and a good one: the components
    are named by the game itself, so the prefix is the game's own wording.
    """
    words = [n.split() for n in component_names if n]
    if len(words) < 2:
        return None
    shared: list[str] = []
    for column in zip(*words):
        if len(set(column)) != 1:
            break
        shared.append(column[0])
    # A single shared word ("Prime", "Dual") is coincidence, not an item name.
    return " ".join(shared) if len(shared) >= 2 else None


def _resolve_item_name(result: str, blueprint: str, components: dict,
                       names: dict[str, str], codenames: dict) -> str | None:
    """Find the display name of a crafted item.

    Four sources, in order of trustworthiness. More than one is needed because
    the export names items inconsistently: weapons often have no entry for their
    `resultType` at all — Dual Zoren Prime resolves to
    `/Lotus/Weapons/Tenno/Melee/Axe/PrimeZoren/PrimeZorenAxeWeapon`, which
    appears in no name index — and Warframes hide behind internal codenames.
    """
    direct = names.get(result)
    if direct:
        return direct

    for segment in result.split("/"):
        if segment in codenames:
            variants = codenames[segment]
            prime = [v for v in variants if "Prime" in v]
            return prime[0] if prime and "prime" in result.lower() else variants[0]

    blueprint_name = names.get(blueprint)
    if blueprint_name and blueprint_name.endswith(" Blueprint"):
        return blueprint_name[: -len(" Blueprint")]

    return _common_prefix_name([names.get(u, "") for u in components])


def build(cfg: Config, report: list[str] | None = None) -> dict[str, int]:
    """(Re)build the reference tables from the manifests.

    User tables (`owned_parts`, `owned_items`) are deliberately untouched: a
    data update must never cost the user their inventory.
    """
    notes = report if report is not None else []
    unnamed: list[str] = []

    warframes = pe.read_manifest(cfg, "ExportWarframes_en.json")
    weapons = pe.read_manifest(cfg, "ExportWeapons_en.json")
    # Railjack weapons sit in a second list in the same file and are real
    # weapons for mastery purposes.
    weapons += pe.manifest_sections(cfg, "ExportWeapons_en.json").get("ExportRailjackWeapons", [])
    resources = pe.read_manifest(cfg, "ExportResources_en.json")
    recipes = pe.read_manifest(cfg, "ExportRecipes_en.json")
    relics = pe.read_manifest(cfg, "ExportRelicArcane_en.json")
    sentinels = pe.read_manifest(cfg, "ExportSentinels_en.json")
    cosmetics = pe.read_manifest(cfg, "ExportCustoms_en.json")
    # Manifests loaded only to be recognised and dismissed. See the decoy pass
    # near the end of this function.
    decoy_sources = []
    for filename in ("ExportGear_en.json", "ExportUpgrades_en.json",
                     "ExportKeys_en.json", "ExportFusionBundles_en.json"):
        try:
            decoy_sources.append(pe.read_manifest(cfg, filename))
        except Exception:  # noqa: BLE001 - a manifest that is absent is not fatal
            notes.append(f"{filename} not available — its names cannot be dismissed")

    # One lookup for every name the export knows, plus the ducat values.
    #
    # `recipes` belongs in this list and was missing from it. The component
    # exports carry a ducat value for components, and the recipe export carries
    # one for blueprints — so leaving it out meant *every* blueprint had no
    # ducat value at all: 0 of 831, and 173 of the 595 parts a relic can drop.
    # The reward ranking breaks ties on ducats, so a blueprint worth 45 lost to
    # a component worth 25 because one of them was read as zero. Measured on
    # Venato Prime Blueprint, which the game shows as 45.
    names: dict[str, str] = {}
    ducats: dict[str, int] = {}
    for source in (resources, warframes, weapons, sentinels, relics, recipes):
        for entry in source:
            unique = entry.get("uniqueName")
            if not unique:
                continue
            cleaned = _clean_name(entry.get("name"))
            if cleaned:
                names[unique] = cleaned
            if entry.get("primeSellingPrice"):
                ducats[unique] = int(entry["primeSellingPrice"])

    # Warframe paths carry internal codenames and no name field, so the export
    # alone cannot say that /Lotus/Powersuits/Cowgirl/ is Mesa.
    codenames = _curated("warframe-codenames.json")

    connection = sqlite3.connect(cfg.db_path)
    connection.executescript((Path(__file__).resolve().parent / "schema.sql").read_text())
    cursor = connection.cursor()
    # Vault status is not in the manifests; `vaulted.apply` writes it after the
    # build, from DE's drop tables. It is carried across the rebuild so that a
    # vault refresh that fails - page unreachable, markup changed - leaves the
    # last accepted answer in place, still stamped with its date, instead of
    # NULL for every relic. A relic new in this build starts at NULL: nothing
    # has said anything about it yet.
    previous_vault = dict(cursor.execute("SELECT unique_name, vaulted FROM relics"))
    for table in ("relic_rewards", "relics", "parts", "items", "ignored_names"):
        cursor.execute(f"DELETE FROM {table}")

    # --- items and their parts, read off the recipes -----------------------
    items: dict[str, dict] = {}
    parts: dict[str, dict] = {}
    #: resultType -> blueprint, for items built from a blueprint plus raw
    #: materials only. Resolved after the manifest pass.
    single_blueprints: dict[str, str] = {}
    #: Everything any recipe produces, so an ingredient can be asked whether it
    #: is a built thing or a mined one.
    craftable = {r["resultType"] for r in recipes if r.get("resultType")}

    for recipe in recipes:
        result = recipe.get("resultType")
        blueprint = recipe.get("uniqueName")
        if not result or not blueprint:
            continue

        ingredients = collections.Counter()
        for ingredient in recipe.get("ingredients", []):
            ingredients[ingredient["ItemType"]] += int(ingredient.get("ItemCount", 1))

        # A set piece is an ingredient that is itself a craftable component,
        # never a raw resource - or a finished weapon, for the guns that are
        # built out of two other guns.
        components = {
            unique: count for unique, count in ingredients.items()
            if _PART_PATH in unique or _is_built_weapon(unique, craftable)
        }
        if not components:
            # A recipe whose ingredients are all raw resources builds its item
            # from one blueprint and nothing else - Gorgon, Catabolyst, and
            # several hundred more. Skipping it outright left the blueprint
            # unnamed, and a reading of "Gorgon Blueprint" then matched the
            # nearest thing that did exist: Gorgon Wraith Blueprint, an item the
            # user does not own. Remembered here and registered below, once the
            # manifest pass has established which results are real items.
            single_blueprints[result] = blueprint
            continue

        item_name = _resolve_item_name(result, blueprint, components, names, codenames)
        if not item_name:
            unnamed.append(result)
            continue

        is_prime = "prime" in item_name.lower()
        category = _category(result)
        # A Warframe lives under /Powersuits/. Anything else coming out of
        # WarframeRecipes has its own build recipe but is not a Warframe:
        # Chroma Chassis and Systems are components, Equinox Day and Night
        # Aspect are intermediate stages of a nine-piece set, and Chroma Mark is
        # a quest item from The New Strange. All six were listed as Warframes
        # because having a recipe was taken as evidence of being an item.
        #
        # They stay in `parts`, where they belong; they just stop claiming to be
        # equipment the user could own and level.
        if category == "warframe" and "/Powersuits/" not in result:
            category = "component_only"
        items[result] = {"name": item_name, "category": category, "is_prime": is_prime}

        # The blueprint is a set piece too — relics drop it like any other.
        parts[blueprint] = {
            "name": names.get(blueprint) or f"{item_name} Blueprint",
            "item": result, "count": 1, "blueprint": 1,
            "ducats": ducats.get(blueprint), "alias_of": None,
        }
        for unique, count in components.items():
            parts[unique] = {
                "name": names.get(unique) or unique.split("/")[-1],
                "item": result, "count": count, "blueprint": 0,
                "ducats": ducats.get(unique), "alias_of": None,
            }

    # --- component blueprints are the same part under another name ---------
    #
    # Relics drop the *blueprint* of a Warframe component, not the component:
    # /Lotus/Types/Recipes/WarframeRecipes/SarynPrimeSystemsBlueprint builds
    # SarynPrimeSystemsComponent. Both name the same thing to a player, so the
    # blueprint is registered as a part of the same item. Weapons differ — there
    # the relic drops the piece itself (DualZorenPrimeBlade) — which is why this
    # has to be read off the recipes rather than assumed either way.
    aliases = 0
    for recipe in recipes:
        blueprint, produced = recipe.get("uniqueName"), recipe.get("resultType")
        if not blueprint or not produced or blueprint in parts:
            continue
        target = parts.get(produced)
        if target is None:
            continue
        parts[blueprint] = {
            "name": names.get(blueprint) or f"{target['name']} Blueprint",
            "item": target["item"], "count": target["count"], "blueprint": 1,
            "ducats": ducats.get(blueprint) or target["ducats"],
            "alias_of": produced,
        }
        aliases += 1
    if aliases:
        notes.append(f"{aliases} component blueprints registered as aliases of their component")

    # Every Warframe, weapon and sentinel the game knows — not only those whose
    # recipe has components. Weapons built from a blueprint plus raw materials
    # (Acrid, and many others) have no component recipe at all and were missing
    # entirely, which matters because the user wants to have levelled every
    # weapon once: an item that is not in the list cannot be ticked off.
    for source in (warframes, weapons, sentinels):
        for entry in source:
            unique, name = entry.get("uniqueName"), _clean_name(entry.get("name"))
            if not unique or not name:
                continue
            category = _category(unique, entry)
            if unique in items:
                # A recipe-derived guess loses to the manifest's own category.
                items[unique]["category"] = category
                continue
            items[unique] = {"name": name, "category": category,
                             "is_prime": "prime" in name.lower()}

    # One-blueprint items, now that `items` is complete. The blueprint is the
    # item's only part, so owning it is the whole of that item's set progress -
    # and, more to the point, the name is now something a reading can match
    # instead of drifting to a similarly-spelled weapon.
    with_parts = {part["item"] for part in parts.values()}
    single = 0
    for result, blueprint in single_blueprints.items():
        if result not in items or blueprint in parts or result in with_parts:
            continue
        parts[blueprint] = {
            "name": names.get(blueprint) or f"{items[result]['name']} Blueprint",
            "item": result, "count": 1, "blueprint": 1,
            "ducats": ducats.get(blueprint), "alias_of": None,
        }
        single += 1
    if single:
        notes.append(f"{single} items built from a single blueprint got that "
                     f"blueprint registered as their one part")

    cursor.executemany(
        "INSERT OR REPLACE INTO items(unique_name,name,category,is_prime) VALUES(?,?,?,?)",
        [(u, d["name"], d["category"], int(d["is_prime"])) for u, d in items.items()])
    cursor.executemany(
        "INSERT OR REPLACE INTO parts"
        "(unique_name,name,item_unique,ducats,count_needed,is_blueprint,alias_of)"
        " VALUES(?,?,?,?,?,?,?)",
        [(u, d["name"], d["item"], d["ducats"], d["count"], d["blueprint"], d.get("alias_of"))
         for u, d in parts.items()])

    # --- relics and their drop tables --------------------------------------
    relic_rows, reward_rows = [], []
    unresolved = collections.Counter()
    for entry in relics:
        rewards = entry.get("relicRewards")
        if not rewards:
            continue
        unique, name = entry.get("uniqueName"), _clean_name(entry.get("name")) or ""
        tier = next((t for t in ("Lith", "Meso", "Neo", "Axi", "Requiem") if name.startswith(t)), None)
        relic_rows.append((unique, name, tier, previous_vault.get(unique)))
        for reward in rewards:
            target = _strip_store(reward.get("rewardName", ""))
            if target not in parts:
                unresolved[target] += 1
            reward_rows.append((unique, target, reward.get("rarity", "COMMON")))

    cursor.executemany("INSERT OR REPLACE INTO relics(unique_name,name,tier,vaulted) VALUES(?,?,?,?)", relic_rows)
    cursor.executemany(
        "INSERT OR REPLACE INTO relic_rewards(relic_unique,part_unique,rarity) VALUES(?,?,?)", reward_rows)

    if unnamed:
        notes.append(f"{len(unnamed)} recipes produced an item with no resolvable name "
                     f"(e.g. {', '.join(u.split('/')[-1] for u in unnamed[:3])})")
    # --- relic rewards that are not set pieces ------------------------------
    #
    # Forma Blueprint appears in 2180 relics, more than anything else in the
    # game, and it was not in the database at all: it belongs to no set, so the
    # recipe walk never produced it. On a live reward screen that means the most
    # common reward of all reads as "not recognised".
    #
    # Kuva, Riven Slivers, Ayatan sculptures and Exilus adapters are the same
    # case. They are stored as items in a category of their own so they can be
    # named and ranked as what they are - never worth taking over a missing
    # part, but worth telling apart from a failure to read.
    other_rewards = 0
    for target, _count in unresolved.items():
        name = names.get(target) or _clean_name(
            target.split("/")[-1].replace("Blueprint", " Blueprint"))
        if not name or target in parts or target in items:
            continue
        items[target] = {"name": name, "category": "reward_other", "is_prime": False}
        parts[target] = {"name": name, "item": target, "count": 1,
                         "blueprint": int("Blueprint" in target),
                         "ducats": ducats.get(target), "alias_of": None}
        other_rewards += 1
    if other_rewards:
        cursor.executemany(
            "INSERT OR REPLACE INTO items(unique_name,name,category,is_prime) VALUES(?,?,?,?)",
            [(u, items[u]["name"], "reward_other", 0) for u in items
             if items[u]["category"] == "reward_other"])
        cursor.executemany(
            "INSERT OR REPLACE INTO parts"
            "(unique_name,name,item_unique,ducats,count_needed,is_blueprint,alias_of)"
            " VALUES(?,?,?,?,?,?,?)",
            [(u, parts[u]["name"], parts[u]["item"], parts[u]["ducats"], 1,
              parts[u]["blueprint"], None) for u in parts
             if u in items and items[u]["category"] == "reward_other"])
        notes.append(f"{other_rewards} non-set relic rewards added "
                     f"(Forma and friends — Forma alone drops from 2180 relics)")

    if unresolved:
        notes.append(f"{len(unresolved)} reward targets did not resolve to a known part; "
                     f"most common: {', '.join(t.split('/')[-1] for t, _ in unresolved.most_common(5))}")

    # Cosmetics, recorded so they can be recognised and dismissed. Only names
    # that could plausibly collide with a tracked item are worth storing; the
    # generic ones ("Helmet Or Syandana", of which there are dozens) match
    # nothing and would only enlarge the search.
    # --- decoys: names worth recognising in order to dismiss ----------------
    #
    # An inventory screen is mostly things this tool has no opinion about, and
    # they were the largest remaining source of wrong rows. A tile holding
    # "Gravimag Blueprint" has no right answer among tracked names, so it went
    # to the nearest one that existed - Gram Blueprint at 87. No threshold can
    # separate that from a correct reading, because correct readings score lower
    # all the time: Forma Blueprint came in at 69.8 off a noisy crop.
    #
    # So the fix is not a better threshold, it is a better candidate list. Every
    # name the manifests know but this tool does not track goes in as a decoy,
    # and the scan discards a reading whenever a decoy explains it better.
    # "Gravimag Blueprint" then matches itself at 100 and loses to nothing.
    #
    # This is the same move as reading the relics out of EE.log: rather than
    # judging an uncertain answer more cleverly, put the right answer in the
    # search space and let it win.
    # Currencies and consumables are deliberately not inventory (see the
    # reward_other category), which left them in a gap: not tracked, so not
    # matchable, and not a decoy either, so nothing stopped them landing on a
    # tracked name. "Forma Blueprint" went to Soma Blueprint at 89.7. They are
    # decoys for the inventory scan and candidates for the relic screen, which
    # are separate lists and always were.
    currency = {unique for unique, d in items.items()
                if d.get("category") == "reward_other"}
    tracked_names = ({d["name"] for u, d in items.items() if u not in currency}
                     | {d["name"] for u, d in parts.items()
                        if d.get("item") not in currency})
    ignored: list[tuple[str, str]] = []
    seen_decoys: set[str] = set()

    def add_decoy(raw_name, kind: str):
        name = _clean_name(raw_name)
        # Below five characters a name is more likely to collide with a real one
        # than to describe anything: the cosmetics pass has always used this cut.
        if not name or name in tracked_names or name in seen_decoys or len(name) < 5:
            return
        seen_decoys.add(name)
        ignored.append((name, kind))

    for unique in currency:
        add_decoy(items[unique]["name"], "currency")
    for unique, part in parts.items():
        if part.get("item") in currency:
            add_decoy(part["name"], "currency")

    for entry in cosmetics:
        add_decoy(entry.get("name"), "cosmetic")

    # Resources, and the blueprints that build them. The blueprint names are not
    # in any manifest as such - they are the recipe's own name - which is why
    # "Auroxium Alloy X 20 Blueprint" had nothing to match against.
    for entry in resources:
        add_decoy(entry.get("name"), "resource")

    for source in decoy_sources:
        for entry in source:
            add_decoy(entry.get("name"), "untracked")

    # Recipe products that never became tracked items: gear, keys, consumables.
    for recipe in recipes:
        name = names.get(recipe.get("uniqueName"))
        if name:
            add_decoy(name, "recipe")

    # A resource blueprint is written "<Resource> Blueprint" on the tile, and
    # that string appears in no manifest - the manifest holds the resource, the
    # recipe holds the product. Without it "Gravimag Blueprint" only reached its
    # decoy "Gravimag" at 61, losing to Gram Blueprint at 87, so the decoy was
    # present and still lost. Derived rather than looked up, because the game
    # builds the label the same way.
    for name in list(seen_decoys):
        add_decoy(f"{name} Blueprint", "recipe")

    cursor.executemany("INSERT OR REPLACE INTO ignored_names(name, kind) VALUES(?,?)",
                       ignored)

    # Where each thing's official icon lives. Not the pictures themselves -
    # nineteen thousand of them exist and a few hundred are of any use - just
    # the addresses, so the ones the user actually holds can be fetched once
    # and kept. This is where the previous tool's lists got their artwork from,
    # and the reason ours looked like a spreadsheet was that we never read the
    # field.
    textures = 0
    for row in pe.read_manifest(cfg, "ExportManifest.json"):
        unique = row.get("uniqueName")
        location = row.get("textureLocation")
        if unique and location:
            cursor.execute(
                "INSERT INTO textures(unique_name, location) VALUES(?,?) "
                "ON CONFLICT(unique_name) DO UPDATE SET location=excluded.location",
                (unique, location))
            textures += 1

    connection.commit()
    counts = {
        "items": len(items), "parts": len(parts),
        "relics": len(relic_rows), "rewards": len(reward_rows),
        "parts_with_ducats": sum(1 for d in parts.values() if d["ducats"]),
        "set_pieces": sum(1 for d in parts.values() if not d.get("alias_of")),
        "unresolved_rewards": len(unresolved),
        "textures": textures,
        "decoys": len(ignored),
    }
    cursor.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('counts',?)", (json.dumps(counts),))
    # Stamped here rather than by the caller, because there are two callers.
    # The interface used to write this itself, so a `wf update` from the shell
    # left the Settings page reporting data that had never been updated while
    # it was minutes old.
    cursor.execute(
        "INSERT OR REPLACE INTO meta(key,value) "
        "VALUES('last_update_at', strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
    connection.commit()
    connection.close()
    return counts
