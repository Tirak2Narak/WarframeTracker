# Data sources

Everything here was verified by fetching the source and inspecting the result,
not taken from documentation. Numbers are from 2026-08-23.

---

## 1. Public Export — the official manifests

**Index:** `https://origin.warframe.com/PublicExport/index_en.txt.lzma`
(HTTP 200, 490 bytes, 16 entries)

**Manifests:** `https://content.warframe.com/PublicExport/Manifest/{filename}!{hash}`

Two traps, both of which cost the previous attempt time:

1. The index is **LZMA_ALONE** framed, not `.xz`. Python needs an explicit
   `lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)`.
2. Its lines end in **CRLF**. An untrimmed `\r` lands in the manifest URL and in
   the stored hash, so that every stored value ends in `\r`. An earlier attempt
   had exactly that.

Each line reads `filename!hash`. The hash changes whenever the data changes, so
comparing it against the stored one is what makes an update cheap: when nothing
changed, an update costs a single 490-byte fetch. A full download of the six
manifests we use takes about 3.3 seconds.

The tool runs this update by itself, at most once in 24 hours and only
when reference data is already there; the first download stays the Setup page's
button, and `[update] auto_fetch = false` turns the automatic run off. One run is the index request,
the manifests that changed, a rebuild (0.5 s on a full reference database) and one request
for the drop-table page (section 3), so vault status, which rotates by itself,
no longer waits for the button. Each run is a `fetch_log` row of job `reference`;
a failed run is not repeated for 24 hours, and a `wf update` from a shell counts
as a run for the schedule.

### What we use

| Manifest | Size | Contains |
|---|---|---|
| `ExportWarframes_en.json` | 226 KB | Warframes, 125 entries |
| `ExportWeapons_en.json` | 595 KB | Weapons, 837 entries |
| `ExportResources_en.json` | 979 KB | Components and resources, 3522 entries — **and the ducat values** |
| `ExportRecipes_en.json` | 1263 KB | Recipes, 1866 entries — the only place set composition exists |
| `ExportRelicArcane_en.json` | 3169 KB | Relics, 3261 entries, 3089 with drop tables |
| `ExportSentinels_en.json` | 11 KB | Sentinels |

### Structure notes

**Every relic has exactly 6 rewards.** Verified: 3088 of 3089 have six, a single
one has eight. This is the basis of the search-space reduction.

```json
{
  "name": "Axi H3 Relic",
  "uniqueName": "/Lotus/Types/Game/Projections/T4VoidProjectionZephyrPrimeCSilver",
  "relicRewards": [
    {"rewardName": "/Lotus/StoreItems/Types/Recipes/WarframeRecipes/HydroidPrimeSystemsBlueprint",
     "rarity": "RARE", "tier": 0, "itemCount": 1}
  ]
}
```

**Rarity is right there in the data.** An earlier attempt ignored it and hardcoded
`rarity: 'Common'` on every reward.

**Reward names are store items.** `/Lotus/StoreItems/Types/Recipes/…` and
`/Lotus/Types/Recipes/…` are the same object seen from two sides of the game's
economy. Strip the `StoreItems/` segment to join.

**Ducats live in `primeSellingPrice`** on `ExportResources` entries. 406
components carry it; values are 15, 25, 45, 65 or 100. Blueprints carry none —
still an open question.

---

## 2. Naming is inconsistent, and this is the hard part

The export describes a build system, not a catalogue. Three separate obstacles
sit between a `uniqueName` and a name a player would recognise:

**a) Warframes hide behind codenames.**
`/Lotus/Powersuits/Cowgirl/` is Mesa, `Alchemist` is Lavos, `AntiMatter` is
Nova, `YinYang` is Equinox. These paths carry **no `name` field at all**, and no
manifest maps the codename back. This is why `curated/warframe-codenames.json`
cannot be derived and must be maintained.

**b) Weapons often have no entry for their result type.**
Dual Zoren Prime builds into
`/Lotus/Weapons/Tenno/Melee/Axe/PrimeZoren/PrimeZorenAxeWeapon`, which appears
in no name index. The readable name has to come from the blueprint
(`DualZorenPrimeBlueprint`) or from the shared prefix of the component names
("Dual Zoren Prime Blade" + "Dual Zoren Prime Handle").

Getting this wrong is expensive: an early version of the loader discarded any
recipe whose result had no name, losing 520 reward targets and two thirds of
the ducat values.

**c) Relics drop component *blueprints*, not always components.**
`SarynPrimeSystemsBlueprint` builds `SarynPrimeSystemsComponent` — two unique
names for what a player calls one thing. Warframes work this way; weapons drop
the piece itself (`DualZorenPrimeBlade`). Both must be matchable by OCR, so both
are stored, with the blueprint marked `alias_of` the component. **Anything that
counts set progress must filter on `alias_of IS NULL`**, or a four-piece
Warframe set reports seven pieces.

Resolution order used by the loader, after these findings: direct name lookup →
codename table → blueprint name minus " Blueprint" → shared prefix of component
names. Result: 20 unresolved reward targets out of 595 distinct ones, and those
20 are legitimately not set pieces (Forma, Kuva, Ayatan sculptures) except the
Akbronco Prime pair.

---

## 3. Vaulted status — not in the official data

The Public Export does not say whether a relic still drops.

**Source used (since 2026-09-25):** DE's official drop tables,
`sources.droptables_url`
(`warframe-web-assets.nyc3.cdn.digitaloceanspaces.com/…/hnfvc0o3jnfvc873njb03enrf56.html`,
the link in the Update 44 patch notes; HTTP 200, 4.48 MB on 2026-09-25).

- One HTML page: every mission, key, dynamic-location, sortie, bounty and
  enemy reward table, then the contents of every relic (`<h3 id="relicRewards">`).
- A relic named in any reward table is available; every other relic is
  vaulted. The contents section never counts.
- Joined on the relic name, exact and case folded (the page writes "Requiem
  Iii"); all 799 names in the database have a contents table on the page
  (2026-09-25).
- Kept as `droptables.html` in the data directory for other local tools;
  `wf droptables` refreshes it and the vault status alone. The page is fetched
  with every reference data update, which also runs by itself once a
  day (section 1).
- A page that does not look like this is refused and nothing is written —
  see `wft/data/vaulted.py`.

**Replaced: WFCD `Relics.json`**
(`raw.githubusercontent.com/WFCD/warframe-items/master/data/json/Relics.json`,
8.65 MB). Its `"vaulted"` looked like a stated fact and joins on `uniqueName`,
which is why it was chosen first. It is derived: WFCD's relic
generator sets `vaulted: drops.length === 0` from drop locations read off the
previous `warframe-items` build, so it trails DE by at least one build. Two
days after Update 44 it had all five new Citrine relics vaulted and fifteen
rotated-out relics available, although the same file's `drops` field already
matched DE's page.

**Rejected: `https://drops.warframestat.us/data/relics.json`** (HTTP 200,
2.39 MB). It lists all 771 relics, active and vaulted alike, **with no flag
distinguishing them** — identical in that respect to the official manifest. It
was in an early draft of our config and would have silently produced a vaulted
status of "unknown" for everything.

**Note:** `api.warframestat.us` has no relic endpoint — `/relics` and
`/pc/relics` both return 404.

### Vaulted at the part level

Vaulted is a property of the **relic**, not of the part. A part usually drops
from several relics, some vaulted and some not — the wiki page for Dual Zoren
Prime shows its Handle coming from Axi A22 (available) and Axi P8 (vaulted)
alike.

- For the reward screen: use the relic actually opened. No aggregation needed,
  and no ambiguity.
- For a general "is this hard to get" display: compute
  `vaulted_relics / relics_containing_part`. 1.0 means vaulted, 0.0 means
  farmable, anything between means restricted availability.

---

## 4. Curated data

See `wft/data/curated/README.md`. Short version: the manifests cannot express
Warframe codenames or Equinox's nested nine-part structure, so those files are
required rather than convenient. The duplicate-component and item-dependency
files were *confirmed derivable* from recipes and are kept as cross-checks —
a disagreement between the two is reported, since it usually means the game
changed.

---

## 5. warframe.market — what a set is worth in platinum

**Base:** `https://api.warframe.market/v2/` (API version 0.25.0, checked
2026-08-28)

| Endpoint | What it gives | How often |
| --- | --- | --- |
| `/items` | 3840 tradable things: slug, id, tags, ducats, `gameRef` | once, stored |
| `/orders/item/{slug}/top` | five sell and five buy orders | per set, cached 6h |

**The join is exact.** Every item carries `gameRef`, which is the `/Lotus/...`
identifier this database already uses as its primary key —
`orthos_prime_set` → `/Lotus/Weapons/Tenno/Melee/Polearms/PrimePolearmWeapon`.
No name matching, the same as the vault status. 1108 of the 3840 are things
this tool tracks.

**Their rules** (docs.warframe.market/docs/rules/overview) are followed
deliberately, not by accident:

* a named `User-Agent` with a version — they ask for it so unfamiliar traffic
  can be placed rather than blocked, and explicitly forbid browser strings;
* no mirroring of their site — this shows a price beside a set a player owns
  and offers no browsing, search or order placement;
* requests only for rows on screen, only on a button, cached six hours;
* nothing that places, edits or answers an order.

**Rate limit: three requests a second published, two used.** Staying below the
limit is deliberate: probing a rate limit on a Warframe service has led to an
account being locked out of the game. A request is never
sent by opening a page.

`sell_min` is what the cheapest seller asks; `buy_max` is what somebody already
offers. Both are shown, because a sale lands between them and picking one would
be a recommendation this tool should not make.

---

## 6. weeklyRivensPC.json — what rivens actually trade for

**URL:** `https://www-static.warframe.com/repos/weeklyRivensPC.json`
(config key `sources.riven_prices_url`; checked 2026-09-01, Last-Modified
that Monday 00:00:03 UTC — the cadence promised in DE's 2019 announcement
still holds)

| Property | Value |
| --- | --- |
| Publisher | Digital Extremes (forums.warframe.com topic 1077490) |
| Cadence | rewritten every Monday 00:00 UTC with the prior week's trades |
| Contents | ~800 rows: itemType, compatibility, rerolled, avg, stddev, min, max, pop, median |
| Format | JavaScript object literals, **not** strict JSON — unquoted keys, single quotes |
| Auth / limits | none; edge caches a day (`max-age=86400`) |

**This is trade data, not listings.** DE aggregates the platinum actually
paid in riven trades, per weapon and per rolled/unrolled, and excludes
1-platinum friend trades. It is the only source that sees real sales;
warframe.market sees asks.

**Only the current week exists.** Monday's write replaces the file, so
`riven_prices` stores every week the tool sees and history grows locally —
a week never fetched is a hole nothing can refill. One fetch attempt per
program run; the data cannot reward more.

**The numbers are thin.** In a normal week 65 % of weapon rows carry
`pop ≤ 2` — one or two trades. The page pools `pop` across all stored weeks
to tell a thin-but-real market from a rumour, and dims rows where even the
pooled count stays at two or below. `compatibility: null` rows are the
per-category averages for unopened (veiled) rivens.

---

## 7. Fissure board — warframestat.us and the official fallback

**Primary:** `api.warframestat.us` (config `sources.fissures_url`), the
only reachable parsed fissure feed. Its poller sometimes falls behind for hours
while the API itself still answers 200 with valid JSON — seen 2026-08-30 with a
lag of about 3.5 hours, serving an expired board.

**Diagnosis:** compare the newest `expiry` in the payload, and the bundle
`timestamp` from `https://api.warframestat.us/pc`, against the response `Date`
header. The same stale payload under a browser user agent rules out a ban.

**Fallback:** the game's own world state,
`https://api.warframe.com/cdn/worldState.php` (config
`sources.worldstate_url`), refreshed constantly. It replaced the 404ing
`content.warframe.com/dynamic/worldState.php`. Raw format: eras `VoidT1..T6`
(Lith, Meso, Neo, Axi, Requiem, Omnia), missions `MT_*`, nodes `SolNode*`; node
names come from WFCD's `solNodes.json` (config `sources.solnodes_url`).
`fissures.active()` falls back to it when the primary cannot produce a live
board (`_stand_in`, `_parse_worldstate`): when it fails, when every fissure on
it has ended, and when it still lists a fissure that ended longer ago than
`sources.fissures_stale_after_minutes` (default 10). The last case is a poller
that stopped while some of its fissures still run: on 2026-10-14 such a board
showed 11 fissures where the game had 36 open. The official board is the one
shown then, and `FissureView` retries an errored
or empty board on its own every `RETRY_SECONDS`.

## 8. EE.log — the relics of a reward screen

The game writes `EE.log` (path: config `paths.ee_log`, found by `wft/gamelog.py`)
while it runs; the tool only reads it, from the end (`wft/relic/eelog.py`). Time
stamps are seconds since the game started. Measured in one session of the
player's own log (7 MB, 6 reward screens with known outcome) and in 3 further screens; only with the game language English (`-language:en`), other
languages not tried.

- **Relics of the screen** (`read`): `ResourceLoader ... (/Lotus/Types/
  Game/Projections/T?VoidProjection<Name><Refinement>)`, logged as each player
  loads into the mission, 58 to 248 s before the screen in the 10 screens
  measured and spread over up to 158 s. The relics of a screen are the lines
  after the previous screen's last marker, the 4 newest kept. These are
  everyone's relics; they do not say whose is whose.
- **The player's own relic** (`own_relic`): the last `Dialog::CreateOkCancel(
  description=Are you sure you want to equip <Tier> <Code> Relic for this
  mission? ...` between the previous screen's markers and this screen's. It
  names only the relic taken in, by the printed name of the `relics` table
  ("Meso D8 Relic"); it is logged again in each round of an endless run. The
  search runs backwards in growing blocks up to 30 MB. Without a dialog in that
  stretch the local loader line below names the relic by its identifier. One measured screen had no dialog in a log begun
  about 5 hours before.
- **Refinement of it**: `Resource load completed (/Temp/LocalPlayer...)`, then
  `<player> loadout loader finished.`, then the next `ResourceLoader` line of a
  `Projections/T?VoidProjection...<Bronze|Silver|Gold|Platinum>` within 1 s. Other
  players load as `/Temp/OtherPlayerN_...`. Not logged in the later rounds of an
  endless run; there the refinement comes from the screen's relic lines when
  exactly one carries the name, else it is unknown. In the 7 screens with both,
  this line and the dialog named the same relic.
- **Start of a screen**: `VoidProjections: OpenVoidProjectionRewardScreen...` and
  `VoidProjections: <id> gets reward ...`; markers 30 s or less apart are one
  screen. A screen after a host migration has only the first kind.

Player names and ids appear in these lines and are matched, never stored or
logged. Fixtures: `tests/fixtures/eelog/` (anonymised).

---

## 9. Account profile — what has ever been levelled

`https://api.warframe.com/cdn/getProfileViewingData.php?playerId=<id>`, the
address the game uses for "view profile". No login. Under
`Results[0].LoadOutInventory.XPInfo` it lists `{"ItemType", "XP"}` for every
item the account has ever levelled, owned or not; `ItemType` is the
`unique_name` of this database. The answer comes from a CDN cache of about
240 s, so its age is the `Date` header minus `Age`.

The tool requests it once a day at most when a valid account id is set (`wft/profile/fetch.py`): one GET, 30 s timeout, a failure not retried (the
next automatic request comes 24 hours later), every request in the `fetch_log`
table. Around a dozen requests in some minutes
once returned 409, then 403, and the game refused the login for that address;
do not probe it. A saved copy of the page (the second way in) is read by
the same check.



---

## 10. Item pictures — DE's content host

`http://content.warframe.com/PublicExport<path>`, where `<path>` is the texture
location from `ExportManifest.json` (kept in the `textures` table; it carries its
own content hash). One GET per picture, 30 s timeout, a 0.12 s pause between
requests, only for things the inventory holds and that have no stored picture
yet (`wft/data/pictures.py`). A picture is stored once, scaled to 128
px wide.

The tool starts a run by itself (`[pictures] auto_fetch`, default on): right after
an applied inventory import, and otherwise at most once in 24 hours counted from
the last run of any kind, and only when pictures are missing. Each run is one
row in `fetch_log` (job `pictures`). A 404 for one picture (a few icons are not
on the host) is skipped and the run goes on; any other failure (no connection, a
timeout, 403, 429, 5xx) ends the run, and the next automatic one comes 24 hours
after it began; for those 24 hours an applied import starts no run either
(`pictures.refused_recently`). Only the button asks anyway.

---

## 11. Releases — GitHub's "latest release"

`https://api.github.com/repos/BriskWatchdogz1/WarframeTracker/releases/latest`
(`[updates] url`). One GET, 30 s timeout, no second attempt, the tool's user
agent (`wft/updatecheck.py`). Read by hand once on 2026-10-31: status 200,
a JSON object with `tag_name` (`v1.5`), `html_url` (the release page), `draft`
and `prerelease` (both false); only the first two are used. The tag is compared
as numbers with the running version (`wft/whatsnew.py`); a newer one is kept in
`meta` and shown as a notice, an equal or older one is silent.

The tool asks by itself (`[updates] auto_check`, default on) at most once in 24
hours counted from the last request of any kind, the button **Check for
updates** included. Each request is one row in `fetch_log` (job `update`). Any
failure (no connection, 403, 429, 404 while no release is published, an answer
that is not the expected JSON) changes nothing and is not repeated for 24 hours;
only the button asks anyway. Nothing is downloaded or installed.

