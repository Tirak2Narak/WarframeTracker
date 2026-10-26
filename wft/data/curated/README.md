# Curated data

Hand-maintained facts that the official Public Export does **not** contain.
These are not redundant with the manifests — each was verified against them:

- `warframe-codenames.json` — Warframes appear in `uniqueName` paths under
  internal codenames (`/Lotus/Powersuits/Cowgirl/` is Mesa, `Alchemist` is
  Lavos, `AntiMatter` is Nova). The manifests carry no mapping back, and the
  `name` field is empty for those paths. Without this table a path cannot be
  resolved to a Warframe.
- `special-items.json` — Equinox and Equinox Prime are built from a Day and a
  Night Aspect, each with four sub-parts, nine parts in total. The export lists
  only three components, so a recipe parser gets this item wrong.
- `duplicate-components.json` — weapons needing several copies of one
  component (Dual Zoren Prime: 2x Blade, 2x Handle). Several have no recipe in
  the export at all.
- `item-dependencies.json` — weapons built from other complete weapons
  (Akbronco Prime needs 2x Bronco Prime). Partly derivable from recipes, kept
  as the authority and as a cross-check.
- `component-types.json` — the component names per category, used to split an
  OCR'd string like "Rhino Prime Systems" into item and component.
- `necramech-exalted.json` — maps exalted weapons to their Necramech.

**How they are used:** the manifests are the primary source; these files fill
what the manifests cannot express and are cross-checked against them where both
speak. A disagreement is reported rather than silently resolved — it usually
means the game changed and one of the two needs updating.

Inherited from an earlier attempt, where they were built up by hand over time.
