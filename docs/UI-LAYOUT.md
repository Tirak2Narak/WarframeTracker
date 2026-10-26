# UI layout and OCR behaviour

Everything here was measured on real screenshots from the target machine, not
taken from documentation. Source material: eight screenshots at 3840x2160,
Warframe running under Proton in borderless windowed mode, default UI theme.

> **All coordinates are fractions of the window size (0.0 to 1.0), never
> pixels.** The tool must work at any resolution and on any
> monitor. A pixel constant is a bug waiting for the next resolution change —
> this is what the previous attempt got wrong with its global multi-monitor
> coordinates.

---

## 1. The decisive finding: Tesseract reads this UI

This was the open question the whole project hinged on. Three earlier attempts
assumed generic OCR engines were needed and reached for EasyOCR and PaddleOCR,
which are slow on CPU and were never accurate enough.

Tesseract 5 reads Warframe's font correctly, on **raw crops**, before any
preprocessing:

| Crop | Mode | Tesseract output | Correct? |
|---|---|---|---|
| Single name line | `--psm 7` | `Ash[30],` | essentially yes |
| Two-line name | `--psm 6` | `Excalibur Umbra` / `\ [30] )` | yes |
| Two tiles side by side | `--psm 6` | `Gotva Prime [0] Gotva Prime [30]` | yes |
| Quantity badge including the tick icon | any | `@2`, `W2` | **no** |
| Quantity digits only, icon cropped away | `--psm 7` / `--psm 10` | `2` | yes |

Consequences for the pipeline:

1. **Preprocessing is per-screen, not universal.** This is the one place where
   a rule would be wrong:

   | Screen | Background | Best result |
   |---|---|---|
   | Inventory | near-uniform dark blue | Otsu + inversion. A fixed 50% threshold gives **empty output** — the text is bright on very dark ground, so the midpoint lands wrong |
   | Reward screen | live gameplay behind the panel | **raw crop, no preprocessing at all** — measurably better than Otsu |

   On the reward screen thresholding an already low-contrast, compressed image
   destroyed more than it recovered. Start raw, threshold only where measured
   to help.
2. **Crop tightly around text, excluding artwork and icons.** The one failure
   above is the tick icon bleeding into the number. Every failure mode found so
   far comes from something non-textual inside the crop — which is exactly what
   the earlier attempts tried to repair afterwards with blocklists.
3. **`--psm 6` for names, `--psm 7`/`--psm 10` for the quantity.** Names wrap to
   two lines; psm 6 handles the block, psm 7 assumes a single line.

## 2. Inventory grid

Identical across the WARFRAMES, WEAPONS and RELICS categories.

```
6 columns, 4 fully visible rows
first tile (content area, excluding padding):
    x = 0.042    y = 0.183    w = 0.099    h = 0.163
pitch:
    horizontal = 0.109        vertical = 0.201

tile(column, row):
    x = 0.042 + column * 0.109
    y = 0.183 + row    * 0.201
```

Fixed chrome that does **not** scroll: category tabs, screen title, and the
currency display in the header. Only the tile list moves. This matters for the
video import: the scroll offset must be measured on the tile area alone, or the
static header will anchor the correlation and report no movement.

### Item name

- One line at y ≈ 0.74–0.81 **of the tile**, centred.
- Long names **wrap to two lines**, and the rank can end up alone on line two.
  Observed: `Excalibur Umbra` / `[30]`.
- This is why names are read per tile rather than per screen. An earlier attempt clustered
  loose text fragments by x position and guessed the reward count by counting
  occurrences of the word "Prime"; another grouped by y distance with a fixed
  10-pixel tolerance. Both guess.
  Cropping each tile first removes the question.

### Colours

| Element | RGB |
|---|---|
| Text | ≈ (114, 185, 255) |
| Background | ≈ (11–13, 25–31, 49–53) |

Near-uniform dark blue behind bright blue text — a strong, stable contrast.
Values depend on the UI theme, so the threshold colour belongs in `config.toml`,
not in the code.

## 3. Quantity versus rank — do not confuse them

Two different numbers appear on a tile, and reading one as the other corrupts
the inventory.

| | Quantity | Rank |
|---|---|---|
| Where | badge, **top left** of the tile | inside the name line, at the bottom |
| Form | tick icon, then the digits | in square brackets, e.g. `[30]` |
| Region | icon x 0.03–0.16, number x 0.16–0.28, both y 0.05–0.15 of the tile | part of the name text |
| Font | separate badge | same font and colour as the name |
| When | **only on stackable items** | on built equipment |

Confirmed by two tiles side by side: `Gotva Prime [0]` and `Gotva Prime [30]` —
the same weapon at two ranks, not two of anything.

**Stackability is the key point.** Warframes and weapons are not stackable: they
carry no quantity badge, and owning two means two separate tiles. Relics and
(presumably) blueprints stack and show a badge. So the inventory count is
*number of tiles* times *badge quantity where present* — never a count of
distinct names.

A trap: above the grid sits a **category total** in the same style as a badge
(observed: `✓18`). It is not a tile badge. Read badges only inside tile bounds.

## 3b. Reward screen

Measured on three frames filmed off a YouTube recording — deliberately poor
material, so anything working here works on a real 4K capture.

**Tiles.** Four tiles, each ≈0.14–0.15 of the width, top edge at y ≈ 0.19–0.20,
height to the divider ≈0.19. Evenly spaced within a frame — a true grid.

The tile row sits at slightly different x offsets across the three frames, but
that tracks the **aspect ratio of each capture** (1.715 / 1.625 / 1.646), not
anything the game does: it is an artefact of the inconsistent video crop.
Needs confirming on a real screenshot, where the aspect ratio is fixed.

The bright frame and corner fold on some tiles is a **"this player chose it"**
marker, unrelated to geometry. Do not use it as a position anchor.

**Name region.** y ≈ 0.38–0.44 of the frame. Two-line names fill the block;
one-line names are **bottom-anchored to the second line**, not centred, leaving
the upper half empty. So always crop the full two-line height — cropping to fit
one line would clip every two-line name.

Player names start about 0.03 below and are cleanly separable. They are grouped
by choice, not one per tile: up to four names can sit under a single tile when
everyone picked the same reward.

**Timer.** Centred above the row, x ≈ 0.47–0.60, y ≈ 0.09–0.19. OCR read "14"
and "10" correctly but failed on "8" despite a clean crop — the stylised font
has broken segments that are ambiguous for a single digit. **Do not depend on
reading the timer.**

**"Owned" badge.** Pill label at the top left of a tile, overlapping its edge
slightly, present only on owned parts. OCR reads it correctly. Note what it
does *not* mean: the badge shows only while the part is still in the
inventory, so a part that was already built into an item carries none.

**Colours.** Text RGB ≈ (120–155, 115–150, 125–150), background ≈ (30–52,
24–50, 30–56). Despite live gameplay behind the panel, variation across frames
stays within ±20 per channel.

**No per-tile indication of which relic a reward came from.** The diamond
marker below each tile appears to encode rarity, not relic identity. The
candidate list therefore has to come from the log.

---

## 3c. Inventory tile, measured precisely

From a screenshot annotated by hand with column separators. The five
freehand lines sit ~415 px apart — the same column pitch three independent
methods produced (416 px from the global grid fit over 25 frames, 415 px from
edge projection, 414 px from a manual measurement). Drawn by eye, and correct.

Positions are fractions **of the tile**, top-left origin:

| element | x | y |
|---|---|---|
| name, single line ("Knux [0]") | full width, centred | **0.82 – 0.95** |
| name, wrapped ("Agkuza" / "Blueprint") | full width, centred | **0.75 – 0.98** |
| artwork | full width | 0.0 – 0.75 |
| quantity badge (tick icon + digits) | 0.03 – 0.09 | 0.05 – 0.11 |
| infinity symbol | 0.90 – 0.97 | 0.03 – 0.09 |

**Rank has no position of its own.** It sits inline in the name text as `[26]`
or `[0]`. Quantity and rank are therefore not two readings to disambiguate but
two different places — no heuristic required.

Tile columns for a 4K capture, as fractions of the **frame** width:
0.040–0.139, 0.148–0.247, 0.256–0.355, 0.364–0.462, 0.471–0.570, 0.580–0.678.
Tile width ~379 px against a pitch of ~415 px, so roughly 36 px of gap.

**Why the grid is not found from image edges.** The tile borders are 1 px wide
with a colour difference of only 5–15 levels, which JPEG compression makes
unreliable to locate. Measured, edge projection recovered the pitch correctly
and missed the phase by half a tile. The agent's summary is worth keeping: the
hand-drawn lines are more precise than the actual tile borders.

This is also why a name band was tried and dropped. Guessing 0.55–1.00 let
header text in and dropped item names, halving the hit rate — the real band is
0.75–0.98. The band was removed rather than corrected, because matching against
the candidate list handles artwork noise without needing the geometry at all.

---

## 4. What is still unmeasured

- **The reward screen at a real resolution — the single most useful thing still
  missing.** Section 3b was measured on frames filmed off a recording at three
  different aspect ratios, which put the tile row at y 0.19-0.38. A live capture
  against the game showed that is wrong: the region that should hold a reward
  name read "SELEC A INVEN", text from the sell screen that happens to sit
  there. The row was then marked by hand on a 16:9 screenshot at roughly
  y 0.30-0.51, x 0.22-0.73, and the defaults now follow that.

  It is still an estimate — marked from memory, not from a reward screen. One
  native 3840x2160 capture of an actual Void Fissure reward screen replaces the
  guesswork entirely, and until then the recognition rate against real rewards
  is unknown rather than merely imperfect.
- **Reward screens with two or three players.** All available frames show four
  tiles. Whether fewer rewards stay in place or re-centre is unknown, and it
  decides whether one set of fractions covers every case.
- **Blueprints** did not appear in the material. Whether they look different
  from finished parts, and whether they stack, is unconfirmed.
- **Theme variants.** Only the default theme was seen.

## 5. Fixtures

Crops live in `tests/fixtures/ui-crops/`. The images themselves are not
committed — they are game content and may show account details — but the
measurements above and the expected OCR results are, so the tests remain
meaningful after a fresh checkout.
