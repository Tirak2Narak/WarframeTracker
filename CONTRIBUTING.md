# Contributing

Warframe Tracker is a small project with firm limits. This page says how to
run the tests and which rules a change has to keep.

## Limits that are not up for change

- **No reading of the game's memory, no input to the game.** The tool reads a
  screenshot of the game's window, the game's log file `EE.log`, and recordings
  or screenshots that the player makes and picks. Overlays draw only: clicks go
  through them, and they run in their own helper process on `xcb`
  (`ui/overlay/`), because the main window runs natively on Wayland.
- **No pictures taken for the inventory.** The inventory import reads only
  recordings and screenshots the player made with their own tool (Spectacle or
  similar) and chose with "Choose recording..." or "Choose folder...".
- **External services at a human pace.** A request goes out when a person asks
  for it, or on a schedule no faster than the one already in the code (the
  profile and missing pictures at most once in 24 hours, counted from the last
  request, a failed one too). Never measure a rate limit, and never repeat a
  request after a refusal (409, 403, 429): many profile requests in a short time
  have already locked an address out of the game's login. Tests never touch the
  network; they patch `urlopen` and the fetch functions.
- **Nothing written outside the tool's own folders without a visible setting.**
  Anything that creates files elsewhere (pictures, exports, logs) needs a
  setting that shows where, how much and how to clean up, and the interface
  says so when it writes.

## One function for what is shown and what is written

Where the interface shows something and another path writes the same thing
(trading, selling, counts, name lookup), both call the same function, for
example `wft.data.db.resolve_typed_name` and `wft.data.db.retained_counts`.
Before changing something that cannot be undone, find the other path and check
what the writing side means.

## Tests

The tests are scripts, not a pytest suite. Run them one after another, never
two at the same time (a parallel run has made the interface test fail before):

```bash
python3 tests/test_badges.py
python3 tests/test_droptables.py
python3 tests/test_inventory_fixtures.py
python3 tests/test_picture_fetch.py
python3 tests/test_profile_fetch.py
python3 tests/test_ranking.py
python3 tests/test_reference_update.py
python3 tests/test_release_skip.py
python3 tests/test_riven.py
python3 tests/test_trading.py
python3 tests/test_ui_smoke.py
```

Each ends with exit status 0 when it passes. `test_ui_smoke.py` drives every
page offscreen and takes 4 to 10 minutes, depending on whether the game data is
there; at the end it lists its 15 slowest checks.

- **Game data.** Many checks need the game data that `bin/wf update` downloads
  (Digital Extremes' public export). Without it they print a `SKIPPED:` line
  that says how to get it, and the script still ends with 0. The data comes
  from the database `WFT_TEST_DATABASE` names, or else the configured one. It
  is opened read-only and copied; the checks work on the copy.
- **Holdings.** No check depends on what the person running it owns: every
  test passes on a database with the game data and nothing held.
- **Pictures.** The tiles, badges and inventory screenshots the scan tests read
  are cut from real captures and stay out of the repository
  (`tests/fixtures/.gitignore`). Without them those tests print `SKIPPED:`. To
  measure the reader, put your own screenshots and an `expected.json` in
  `tests/fixtures/inventory/`: one key per picture file, each with a `tiles`
  list of `{"col": 0, "row": 0, "name": "Agkuza Blueprint", "quantity": null}`
  read off the picture by hand, the name as the screen writes it (a rank like
  `[26]` included). With them, the output of
  `tests/test_inventory_fixtures.py` must stay byte for byte the same across a
  change to the scan unless the change is meant to alter a reading: save it
  before and compare after.
- **Your own files.** No test may write `~/.config/warframe-tracker/config.toml`
  or your database. The tests that build pages or save settings point
  `XDG_CONFIG_HOME` and `XDG_DATA_HOME` at scratch folders; every test that
  writes a database writes a scratch one or a copy.

## Debugging the scan

These environment variables make the inventory scan show its work. They only
print or write when set, and nothing in the interface sets them.

- `WFT_DEBUG_SWEEP=all` (or `3:1,4:1`, "column:row") prints what the band
  reader saw at each tile position.
- `WFT_DEBUG_STATE=<file>` appends the scan's state after each stage to that
  file; run the same scan twice and compare the two files to find the stage
  whose answer depends on more than its input.
- `WFT_DEBUG_BADGE=all` (or part of a name) prints each count with the votes
  of its frames and the figures read.
- `WFT_DEBUG_BADGE_SAVE=<part of a name>` writes that tile's count badge, at
  the size the scan reads it, as a PNG into `WFT_DEBUG_BADGE_DIR` (default
  `/tmp`).

## Changes

- Commits are grouped by idea, with a subject in the imperative that names the
  change in plain words, and a body that says what was wrong and what was
  measured. Tests that belong to the change go in their own last commit.
- A change to a button, a page, a card or an installer text goes into
  [docs/GUIDE.md](docs/GUIDE.md) in the same change; the guide is what
  `GUIDE.pdf` is made from.
- A release gets a section at the top of [PATCHNOTES.md](PATCHNOTES.md) (only
  what a player notices, one line each) and an entry in `wft/whatsnew.toml`;
  `packaging/make-release.sh` checks that both name the same version and date.
- Building a release is described in [docs/SETUP.md](docs/SETUP.md#building-a-release);
  run `packaging/make-release.sh --dry-run` first after changing the script,
  the guide or the patch notes.
