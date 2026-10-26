-- Normalised view of Digital Extremes' Public Export, plus the user's own
-- holdings. Rebuilt from the manifests on every update; user tables survive.

CREATE TABLE IF NOT EXISTS items (
    unique_name TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,          -- warframe | weapon | sentinel | archwing | other
    is_prime    INTEGER NOT NULL DEFAULT 0
);

-- A part is anything a relic can drop that belongs to a set: the components
-- and the blueprint itself. `count_needed` is how many of it the recipe wants,
-- which is what makes the akimbo weapons work (Aklex Prime needs 2x Lex Prime).
CREATE TABLE IF NOT EXISTS parts (
    unique_name  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    item_unique  TEXT REFERENCES items(unique_name),
    ducats       INTEGER,
    count_needed INTEGER NOT NULL DEFAULT 1,
    is_blueprint INTEGER NOT NULL DEFAULT 0,
    -- Set when this row is only another name for an existing part: relics drop
    -- "Saryn Prime Systems Blueprint", which builds "Saryn Prime Systems".
    -- Both must be matchable by OCR, but counting both would report seven
    -- pieces for a four-piece Warframe set.
    -- Anything counting towards a set MUST filter on alias_of IS NULL.
    alias_of     TEXT REFERENCES parts(unique_name)
);

CREATE TABLE IF NOT EXISTS relics (
    unique_name TEXT PRIMARY KEY,
    name        TEXT NOT NULL,          -- e.g. "Axi H3 Relic"
    tier        TEXT,                   -- Lith | Meso | Neo | Axi | Requiem
    vaulted     INTEGER                 -- NULL until drop data is loaded
);

CREATE TABLE IF NOT EXISTS relic_rewards (
    relic_unique TEXT NOT NULL REFERENCES relics(unique_name),
    part_unique  TEXT NOT NULL,
    rarity       TEXT NOT NULL,         -- COMMON | UNCOMMON | RARE
    PRIMARY KEY (relic_unique, part_unique)
);

-- What the user owns. Counts, not booleans: duplicates are the basis of the
-- ducat recommendation, so collapsing them to yes/no would destroy the answer.
CREATE TABLE IF NOT EXISTS owned_parts (
    part_unique TEXT PRIMARY KEY REFERENCES parts(unique_name),
    count       INTEGER NOT NULL DEFAULT 0,
    -- Where this number came from: manual | video | pick | test. Recorded so a
    -- bad video import can be undone without touching hand-entered data, and so
    -- a recommendation can always be traced back to its evidence.
    source      TEXT NOT NULL DEFAULT 'manual',
    updated_at  TEXT
);

-- Fully built items the user has. Kept apart from parts: once built, the parts
-- are consumed, but mastery and "do I still need this" depend on the built item.
CREATE TABLE IF NOT EXISTS owned_items (
    item_unique TEXT PRIMARY KEY REFERENCES items(unique_name),
    count       INTEGER NOT NULL DEFAULT 0,
    -- Mastery is independent of ownership: an item can be levelled to 30 and
    -- then sold, and it still counts. So this is not derivable from `count`.
    mastered    INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',
    updated_at  TEXT
);

-- Items of which the player keeps one built copy for themselves, not for crafting
-- the items built from them. Only meaningful for an item that is a
-- piece of another recipe (Lex Prime inside Aklex Prime).
CREATE TABLE IF NOT EXISTS kept_items (
    item_unique TEXT PRIMARY KEY,
    updated_at  TEXT
);

-- Things the tool deliberately does not track, kept so they can be recognised
-- and dismissed rather than arriving as unknowns.
--
-- Cosmetics are the reason this exists. "Yareli Prime Helmet" is a helmet skin;
-- matched against tracked names alone it resolves to "Yareli Prime" with a high
-- score and counts as a built Warframe the user may not own. Being able to say
-- "this is a skin" is what stops that, and it also keeps hundreds of cosmetics
-- out of the import review queue.
CREATE TABLE IF NOT EXISTS ignored_names (
    name     TEXT PRIMARY KEY,
    kind     TEXT NOT NULL          -- cosmetic | scene | other
);

-- Relics held. A separate table because a relic is neither a part nor an item:
-- it is not a component of anything and it is consumed rather than built. They
-- were being recognised - dozens per scan - and then dropped on the floor,
-- because the import could only write the two tables that existed.
--
-- Worth having beyond completeness: knowing which relics are held is what
-- turns "this reward is from a vaulted relic" into "you hold three of that
-- relic", which is a different decision.
CREATE TABLE IF NOT EXISTS owned_relics (
    relic_unique TEXT PRIMARY KEY,
    count        INTEGER NOT NULL DEFAULT 1,
    source       TEXT NOT NULL DEFAULT 'manual',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Readings the user dismissed during a review. Deliberately a separate table
-- from ignored_names, which is rebuilt from the manifests on every update and
-- would take these with it. What is stored is the *reading*, not the name it
-- was matched to: the match was wrong, that is why it was dismissed. A later
-- scan compares new readings against these and stops asking.
CREATE TABLE IF NOT EXISTS learned_ignores (
    reading  TEXT PRIMARY KEY,
    noted_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per request this installation made to an outside address on a
-- schedule or a button. `job` names what it was for ('profile'; the
-- pictures later), `at` is UTC 'YYYY-MM-DD HH:MM:SS' of the request, `kind`
-- is 'auto' or 'manual', `status` the HTTP status (NULL when there was no
-- answer) and `detail` the outcome or the error text. The next automatic
-- request is due 24 hours after the newest row of its job, whatever its kind
-- and result.
CREATE TABLE IF NOT EXISTS fetch_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job     TEXT NOT NULL,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    status  INTEGER,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_job ON fetch_log(job, at);

CREATE INDEX IF NOT EXISTS idx_parts_item    ON parts(item_unique);
CREATE INDEX IF NOT EXISTS idx_parts_name    ON parts(name);
-- alias_of is the inner loop of every per-part helper: alias_group runs under
-- relics_dropping, vault_state, reachability, owned_count and ducats, and
-- without this index each of those was a full scan of parts.
CREATE INDEX IF NOT EXISTS idx_parts_alias   ON parts(alias_of);
CREATE INDEX IF NOT EXISTS idx_rewards_part  ON relic_rewards(part_unique);
CREATE INDEX IF NOT EXISTS idx_items_name    ON items(name);

-- One row per import that was written. The inventory tables hold only the
-- latest number, so before this there was no way to answer "what did that
-- import change" or to take one back: a stack of 12 stored as 2 left no trace
-- of the 12, and the only undo was clearing every row from a source at once.
CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    written_at    TEXT NOT NULL DEFAULT (datetime('now')),
    source        TEXT NOT NULL DEFAULT '',   -- what was scanned
    kind          TEXT NOT NULL DEFAULT 'scan',  -- scan | profile | backup
    scan_revision INTEGER,
    tiles         INTEGER,                    -- candidates the scan produced
    questions     INTEGER,                    -- how many needed review
    note          TEXT,
    undone_at     TEXT
);

-- What each import wrote, and what stood there before it. The `before` column
-- is the whole point: it is what makes an import reversible and what makes a
-- wrong number visible after the fact rather than only in the moment.
CREATE TABLE IF NOT EXISTS observations (
    import_id   INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                -- part | item | relic | mastery
    unique_name TEXT NOT NULL,
    before      INTEGER,
    after       INTEGER,
    -- Which source the row carried before this import took it over. Restoring
    -- the number without it stamped every row 'undo', and the next import then
    -- could not tell what a previous recording had held - so its "what did
    -- this capture not see" question quietly answered "nothing".
    before_source TEXT,
    reading     TEXT,                         -- what OCR actually read
    score       REAL,
    PRIMARY KEY (import_id, kind, unique_name)
);

CREATE INDEX IF NOT EXISTS idx_observations_name ON observations(unique_name);

-- One picture per thing, cut from real captures.
--
-- Lists were text and only text, which is what made them read as a database
-- front-end rather than an inventory: the tool an earlier attempt used gave
-- the artwork two thirds of every card. The pictures do not have to be fetched
-- from anywhere - every scanned tile already carries a crop of exactly the
-- artwork the game draws, and the import was throwing it away.
CREATE TABLE IF NOT EXISTS thumbnails (
    unique_name TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,          -- part | item | relic
    image       BLOB NOT NULL,          -- small PNG or JPEG
    source      TEXT NOT NULL DEFAULT 'export',   -- export | video
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Where each thing's official icon lives, from ExportManifest.json. The path
-- carries its own content hash, so a changed icon is a changed path and the
-- stored picture can be told apart from the current one without asking.
CREATE TABLE IF NOT EXISTS textures (
    unique_name TEXT PRIMARY KEY,
    location    TEXT NOT NULL
);

-- Warframe.market's name for each thing, and what it last sold for.
--
-- Joined on `game_ref`, which is the same `/Lotus/...` identifier this
-- database already uses - the market publishes it, so no name matching is
-- involved. That is the same reason the vault status joins the way it does:
-- a name is a label and an identifier is a fact.
--
-- Prices are cached with the hour they were fetched because they are the one
-- fact here that goes stale in a day, and because the public API allows three
-- requests a second and this tool has no business spending them twice on the
-- same question.
CREATE TABLE IF NOT EXISTS market_items (
    unique_name TEXT PRIMARY KEY,       -- the game's identifier, = gameRef
    slug        TEXT NOT NULL,          -- warframe.market's own name
    item_id     TEXT NOT NULL,
    is_set      INTEGER NOT NULL DEFAULT 0,
    ducats      INTEGER
);

CREATE TABLE IF NOT EXISTS market_prices (
    slug        TEXT PRIMARY KEY,
    sell_min    INTEGER,                -- cheapest standing sell order
    buy_max     INTEGER,                -- best standing buy order
    sell_count  INTEGER,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Digital Extremes' weekly riven trade data, one snapshot per week.
--
-- The source file is rewritten every Monday and only the current week is
-- ever retrievable, so history exists exactly as far back as this table
-- goes - a week the tool never fetched is a hole nothing can refill. Rows
-- are immutable once published, which is why writes are INSERT OR IGNORE
-- and `fetched_at` records the first collection, not the last.
CREATE TABLE IF NOT EXISTS riven_prices (
    week        TEXT NOT NULL,             -- ISO date of the publication Monday (UTC); the trades span the seven days before it
    item_type   TEXT NOT NULL,             -- 'Rifle Riven Mod', 'Melee Riven Mod', ...
    weapon      TEXT NOT NULL DEFAULT '',  -- the compatibility name; '' is the category's veiled aggregate
    rerolled    INTEGER NOT NULL,          -- 0 or 1
    avg         REAL,
    stddev      REAL,
    price_min   INTEGER,                   -- named so SQL's own min()/max() stay out of the way
    price_max   INTEGER,
    pop         INTEGER,                   -- trades behind this row; 1-2 is a rumour, not a price
    median      REAL,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (week, item_type, weapon, rerolled)
);
