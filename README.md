# Warframe Tracker

A Linux desktop companion for Warframe. When a Void Fissure run ends, press one
key: the tool reads the reward screen and tells you which reward to take,
ranked against what you already own. The answer shows in its own window and,
drawn over the reward screen, in the game.

> **Fan project.** Not affiliated with or endorsed by Digital Extremes.
> Warframe and all related content are trademarks of Digital Extremes Ltd.
> The pictures in this repository show game content; they are used under the
> [Warframe Content Policy](https://www.warframe.com/contentpolicy): no
> commercial use, nothing to pay, no Warframe or Digital Extremes logos. The
> GPL covers the code only.

![The Rewards page after a hotkey press: a green banner names the best reward and one card per reward gives the reasons.](docs/guide/rewards-result.png)

## What it does

- **Rewards**: which reward to take, and between runs the relics worth opening
  and the sets closest to finished.
- **Fissures**: the open Void Fissures with the time left, and which of your
  relics suits each one, sorted by the chance of a part you still need.
- **Plan**: what to do next, sorted so that the top of the list finishes a set.
- **Collection**: what you hold, what you built, what you mastered, where each
  piece drops. Hand corrections and backups.
- **Ducats**: what you can hand in at the ducat kiosk without costing a set a
  piece; platinum prices from warframe.market on request.
- **I want / I offer**: a trading-chat message (also CSV or HTML) with the
  Prime sets you are missing parts of, and the Prime parts no set of yours
  needs.
- **Rivens**: what rivens for a weapon sell for, from Digital Extremes' weekly
  trade numbers.
- **Import**: reads your inventory from a screen recording or screenshots that
  you make yourself.
- **Overlays**: the recommendation drawn over the reward screen, and a grid
  over the game's colour palette.

## What it never does

It never reads the game's memory and never sends input to the game. It reads
the game's window (a screenshot when you press the key), the game's own log
file `EE.log`, and the recordings you choose to import. The overlays only draw;
clicks go through them. What it fetches from the network, and how often, is
listed in [docs/GUIDE.md](docs/GUIDE.md#what-the-tool-sends-over-the-network)
and [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md).

## Requirements

- An Arch-based Linux (it installs its packages with `pacman`). Tested on
  Garuda Linux with KDE Plasma 6.7 on Wayland; the game runs through XWayland.
  Plain Arch and EndeavourOS use the same packages and have not been tried.
- Warframe from Steam (Proton), game language English, a windowed or
  borderless 16:9 window.
- The packages in [packaging/arch-packages.txt](packaging/arch-packages.txt)
  and Python 3.11 or newer; the installer offers to install what is missing.

## Install

From a release: download `warframe-tracker.tar.gz` from the
[Releases](https://github.com/BriskWatchdogz1/WarframeTracker/releases) page,
unpack it and follow `GUIDE.pdf` inside (made from
[docs/GUIDE.md](docs/GUIDE.md)). It is written for people new to Linux and goes
from the archive to the first reward screen.

From a clone:

```bash
git clone https://github.com/BriskWatchdogz1/WarframeTracker.git warframe-tracker
cd warframe-tracker
./install.sh
```

The installer installs missing packages (the only step that uses `sudo`),
offers to add you to the `input` group for the hotkey, writes a desktop entry
and links `wf` and `wf-gui` into `~/.local/bin`. On the first start the Setup
page walks through the rest. To update a clone, `git pull`; to remove the tool,
`./install.sh --uninstall`. Configuration and file locations are in
[docs/SETUP.md](docs/SETUP.md).

## Documentation

- [docs/GUIDE.md](docs/GUIDE.md): the user guide, page by page.
- [docs/SETUP.md](docs/SETUP.md): requirements, configuration, building a
  release.
- [docs/DATA-SOURCES.md](docs/DATA-SOURCES.md): where the data comes from and
  on which terms.
- [docs/UI-LAYOUT.md](docs/UI-LAYOUT.md): how the inventory grid is measured.
- [PATCHNOTES.md](PATCHNOTES.md): what changed in each release.
- [CONTRIBUTING.md](CONTRIBUTING.md): tests and the rules a change has to keep.

## Credits

**This is AI-written code, and that is not hidden here.** The application, the
overlays, the installer, the tests and this documentation were written by
Claude (Anthropic), working from my requirements, my bug reports and my
judgement about what the tool should do: I specified, tested and decided;
Claude wrote. Every design decision, every measurement quoted in the docs and
every fix went through that loop. If that is a dealbreaker for you, you know it
before you clone rather than after.

## Licence

Copyright (C) 2026 BriskWatchdogz1.

The code is free software under the GNU General Public License, version 3 or
(at your option) any later version (GPL-3.0-or-later), see [LICENSE](LICENSE).
It comes without any warranty. Game content in pictures and data belongs to
Digital Extremes (see the notice at the top).
