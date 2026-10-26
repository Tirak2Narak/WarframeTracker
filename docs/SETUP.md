# Setup

Details and background for installing and configuring the tool. The step-by-step
instructions for a first start, daily use, updating and debugging are in
`GUIDE.pdf` in the archive (in the repository its source is
[GUIDE.md](GUIDE.md)); this page does not repeat them.

## Requirements

An Arch-based system with `pacman`. The packages are listed in
`packaging/arch-packages.txt`, the only copy of the list: `install.sh` installs
what is missing from it and `wf doctor` checks it. Python 3.11 or newer is also
needed (the config loader uses `tomllib`).

**System packages, no virtualenv.** pip's PySide6 bundles its own Qt, which then
drifts from the Qt the desktop runs, and a virtualenv breaks whenever Arch moves
to a new Python minor version. The system's PySide6 is the desktop's own Qt.

**Pillow** (`python-pillow`) is in the package list and should stay there. The
reward path needs it to turn a captured window into an image. Only the video
import can do without it: it uses Pillow to hand a dozen crops to each
`tesseract` call instead of paying a process start per crop, and without it every
crop pays its own, about four times slower.

**`tesserocr`** (AUR, not in the package list) is optional. If it can be
imported, the in-process OCR engine uses the Tesseract API kept loaded between
calls instead of starting `tesseract` for each read; otherwise it falls back to
the command line, and everything works either way. The inventory scan runs plain
`tesseract` processes in any case, so do not expect it to speed up an import.
`wf doctor` shows whether it is present (INFO when it is missing).

**Qt's `xcb` platform plugin** (for the Overlays page). The tracker runs on the
platform the session gives it (Wayland on the development machine). The grid
over the game is drawn by a helper process that runs on `xcb`, because only
there can a window lie above the game's own window (the game is an X11 window
under XWayland). On Arch the plugin (`libqxcb.so`) is part of `qt6-base`, which
`pyside6` depends on, so it is not listed separately in
`packaging/arch-packages.txt`; other distributions were not checked. Without it,
or without an X11 display, the page shows the helper's first error line and
nothing else is affected.

**The `input` group.** The hotkey listener reads the key from the input device,
which is what lets it hear the key while the game has focus. Linux allows that
only to members of the group. `install.sh` offers to add you; the membership
takes effect at the next login. `wf doctor` reports it as "member, active in
this session", "member, but not active yet" (log out and back in) or "not a
member".

## Installation

`./install.sh`, run from the unpacked program folder, does everything in one go
and can be run again at any time:

- installs the missing packages with `sudo pacman -S --needed` (the only step
  that needs `sudo`; without `pacman` it prints the list and stops; when pacman
  fails it stops and suggests updating the system first, `garuda-update` or
  `sudo pacman -Syu`, the usual cure for "failed retrieving file");
- offers the `input` group (it asks only when you are not yet a member and a
  terminal is attached, otherwise it prints the command);
- writes `~/.local/share/applications/warframe-tracker.desktop` from
  `packaging/warframe-tracker.desktop.in`, with this folder's absolute path;
- links `wf` and `wf-gui` into `~/.local/bin`. A link that already exists and
  belongs to someone else is left alone with a warning.

Because the entry and the links hold an absolute path, run `./install.sh` again
after moving the folder. `./install.sh --uninstall` removes the entry and the
links it made. It keeps config, data, packages and the `input` group, and
prints for each how to remove it: the two folders, `sudo pacman -Rns` with the
package names of `packaging/arch-packages.txt`, and `sudo gpasswd -d <user>
input`.

At the end it asks "Start Warframe Tracker now? [Y/n]" and starts the program
detached from the terminal (`setsid -f`). It asks only when it runs at a
terminal on a desktop session, and never with `WFT_NO_TERMINAL=1` set. It then
says how to find the program later (KDE: Alt+Space and the name; elsewhere the
application launcher, group Games), says that the hotkey needs a new login when
the `input` group was just added or is not active yet, and points at
`GUIDE.pdf` when the archive holds it. `WFT_START_COMMAND` replaces the command
that is started; the tests set it to a placeholder.

To update, unpack the new archive anywhere and run `./install.sh` from it. It
finds the old folder by the `wf-gui` link and, at a terminal, offers to close
the old version, move the old folder to the trash and the new one into its
place. Nothing the program writes lives in its folder except Python's
`__pycache__`.

## Configuration

`config.example.toml` is the base: it holds every setting with working defaults
and an explanation, and the program reads it itself. Your own file is laid over
it and needs only the keys that differ, under the same section names. A table is
merged key by key; a list (such as `sources.manifests`) is replaced as a whole.
So a setting added in a later version reaches you without you doing anything.
Do not edit `config.example.toml`; an update replaces it.

The first existing file of these is used:

1. `~/.config/warframe-tracker/config.toml`, or
   `$XDG_CONFIG_HOME/warframe-tracker/config.toml` if `XDG_CONFIG_HOME` is set
2. `config.toml` in the program folder

Saving a setting in the app creates the first one with just that key and a short
header. The app writes only a few keys: the Settings page the theme, text size,
window class, hotkey and the notification switch, the Setup page the hotkey and the game log path, the
calibration dialog the reward regions, and the Import page the account id. Everything else you edit by hand.

Settings worth knowing:

- **`game.window_class`.** Run `wf windows` with the game open and it lists what
  is there. Launched through Steam the window's class is the app id
  (`steam_app_230410`), not the executable name, so `"warframe"` is the sensible
  setting: it is matched as a substring against both the class and the title.
- **`paths.ee_log`.** Where Warframe writes its log. The default is the standard
  Steam/Proton location. When the file is not there, the Setup page searches
  the Steam libraries (`libraryfolders.vdf` of each folder in `paths.steam_roots`:
  standard Steam, `~/.steam/steam`, the Flatpak install) and offers what it
  finds with "Use this file", or "Choose file…". Both write this key. By hand:
  the path ends in
  `compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log`
  inside a library's `steamapps` folder, and `find ~ /run/media /mnt -name EE.log`
  finds it. `config.example.toml` has the Flatpak Steam and other-library
  variants. The log holds an IP address and an e-mail address; the tool only
  stats it and reads the relic names the squad opened.
- **`hotkey.key`.** An evdev key name such as `SCROLLLOCK`, `PAUSE` or `F8`. The
  default is empty (not set up); the Setup page and Settings, Capture choose it
  by listening for the next key pressed. The running program uses a new value at
  once, no restart. The key is not swallowed: the game and the desktop see it
  as well.
- **`notifications.enabled`.** `true` by default: after every reading of a
  reward screen the recommendation also appears as a desktop notification,
  sent with `notify-send` (package `libnotify`, in
  `packaging/arch-packages.txt`). Settings, Capture has a checkbox for it
  (saved at once, no restart). Without `notify-send` nothing is shown and
  `wf doctor` says so (INFO).
- **`game.reward_count`.** How many reward tiles to expect (default 4). Every
  read tries 4, 3 and 2 and keeps what recognises the most names, so it matters
  only for calibration, which centres the row using it: when you calibrate on a
  screen with 2 or 3 rewards, set this to that number first.
- **`profile.account_id`.** Optional; see "Mastery from the profile" below.
- **`regions.*`.** Screen positions as fractions of the game window. The defaults
  were measured on 16:9; `Calibrate reward regions...` on the Settings page
  measures them for another layout and saves them. Keys here override the
  built-in values: Settings and `wf doctor` say when they do, and the dialog's
  button `Use the measured values` removes them again.

Run `wf doctor` after editing: it names the config file in use and flags an
unknown hotkey name.

## Importing an inventory: details

The guide shows how to make the pictures (a recording or screenshots from
your own screenshot program, for example Spectacle on KDE) and how to read them.
What follows is what it leaves out.

An inventory import is a photo of the current state: it replaces what an earlier
recording wrote rather than adding to it. Nothing reaches the database until
"Write to database", and every import can be taken back.

**Sources.** Recordings (`.mp4 .mkv .webm .mov .avi`) and screenshots
(`.png .jpg .jpeg .bmp .webp`) made with any tool work. The tool keeps no
pictures of its own.

The tool recognises Prime parts and blueprints, built Warframes, weapons and
companions, and relics, and ignores everything else it knows about, so a tab can
be recorded without worrying what is in it. Which in-game tab holds which of
these is not documented here; capture the ones that list what you want tracked.
Cosmetics (skins, helmets) read as the Warframe they belong to and make up most
of what lands in the review queue; the cosmetics tab is not in a capture of the
Warframes tab, so leave it out. A big, readable capture matters: the reference
captures were 4K, and 1080p has been tried only on 4K pictures scaled down.

- **Recording:** open a tab, then scroll slowly from top to bottom. The tool
  reads each tile several times across the frames and uses the repetition to
  correct itself.
- **Screenshots:** every picture is read as a page of
  its own; nothing is stitched. So that an overlap does not count a tile twice,
  an item is kept only as many times as the one picture showing it most often
  has it. The flip side: two tiles of the same item that really lie on two
  different pictures count as one, so use a recording for long tabs.

**Choose and scan.** "Choose recording..." picks one file, "Choose folder..."
a folder: every recording and screenshot in it is read as one import, in name
order, each file as its own page. Then "Start scan". A recording of about 2.5
minutes with a large inventory took about 3 minutes on the machine the tool was
developed on and gave around 390 items, of which about 30 needed a decision.
"Cancel scan" keeps what was read so far.

**Review.** One list, questions first. Identical readings are one decision, not
one per tile. Keys: 1 to 6 pick a candidate, Enter jumps to the next question,
I ignores a reading, M lets you type a name, Space keeps or drops a row the scan
was already sure of, and clicking a tile shows the whole frame with the tile
ringed. Counts can be corrected. "Finish review" leads to the next step,
"Discard import" throws the scan away.

**Apply.** The table lists every change against what is recorded, one tick per
row; the "Write this" column can be edited by double-clicking. Rows marked
"gone?" were recorded before and are missing from this capture. They are
proposed as 0 and left unticked, because a tab you did not capture looks the same
as one you sold. Nothing is proposed as gone when too few tiles of a category were
seen, or when more than 30% of a category is missing, since that means the
capture did not cover it.

**Take it back.** At the foot of the Import page, "What past imports wrote" lists
past imports with what each changed (read as, was, now). "Take this import back"
puts every number back to what stood there before. Anything you changed since,
by hand or by a later import, is left alone. An amber note that the stored
numbers were read by an older version of the reader means: scan again, the
reader has been corrected since.

**Item pictures.** The tool downloads icons from Warframe's content host for what you hold,
about one every tenth of a second, each once. It starts by itself after an applied
inventory import and otherwise at most once in 24 hours, only when pictures are missing
(`[pictures] auto_fetch`, the card's checkbox turns it off); the card's circular-arrow
button fetches now and **Stop** ends a run. The card shows the last run and the next
automatic one.

## Mastery from the profile

Mastery survives selling an item, and most players sell a Prime at rank 30, so a
scan of what you own cannot see it. Warframe's profile page knows every item you
have ever levelled. The tool fetches it for you, once a day:

1. Log in at warframe.com, press F12, open Storage (Firefox) or Application
   (Chrome), then Cookies, `https://www.warframe.com`. Select the cookie
   `user-info` and copy `user_id` from its parsed value into the "Account id"
   field on the Import page (24 hexadecimal characters). It is stored as soon as
   it is valid.
2. That is all. With the box "Fetch my profile once a day" ticked (it is by
   default), the tool asks for your profile about 30 seconds after it starts if
   the last request is more than 24 hours old, and checks again every 10 minutes
   while it runs. The card shows when it last asked, what came back (after a
   successful request also when the server's copy was made) and when the next
   request is due. The newest answer is
   kept as `profile.json` in the data folder.
3. The first profile this installation sees is not written by itself: the card
   says how many items reached their rank cap and "Review..." opens the list;
   confirm to apply. Later profiles mark the new items without asking and the
   card names them.

The button with the circular arrow fetches right now. Use it sparingly: after 5
requests in 24 hours the card warns, because many requests in a short time once
got an address locked out of the game's login, and the game then refused to log
in. A refused or failed request is not repeated; the next automatic one comes 24
hours after it. To fetch only by button, or never, untick the box (config key
`[profile] auto_fetch`).

If the server refuses the tool, "Copy address" and "Load saved profile..." still
work: open the address in your browser once, save the page as a file and load it.

## The desktop entry

`install.sh` writes it (see above). It is generated rather than shipped as a
finished file because it needs an absolute path and the archive holds none.

The entry has two quick actions for KRunner, "Import inventory recording" and
"Update game data". They start the app with `--page import` and `--page
settings`; `wf-gui --page NAME` opens on any page of the navigation
(case-insensitive) and refuses an unknown name with the list of valid ones.

## Window placement on Wayland (KDE Plasma)

This section describes KDE Plasma and its window manager, KWin; other desktops
have their own ways.

The tool has one window; the rewards display is a page in it. Its Wayland app id
is `warframe-tracker`, set in `ui/app.py` and matching the desktop entry, so KWin
pairs the window with the entry and matches rules against that class. The entry
has `StartupNotify=true`. With both, a launch from KRunner brings the window to
the front even over the fullscreen game (verified on KDE Plasma 6.7.5).

The rule the application keeps for itself: nothing raises or activates the window
when a reward screen appears. The hotkey switches the page inside the already
open window, which is a response to a keypress and takes no focus from the game.
The cost is that the window cannot sit over the game on a single monitor and that
a minimised tracker shows nothing; the intended setup is the tracker on a second
monitor.

Under a native Wayland session the compositor owns placement and ignores a
client asking to place itself. That is by design, not a bug in Qt or here. A KWin
rule puts the window where you want it:

1. Right-click the title bar, *More Actions*, *Configure Special Window Settings*
2. Set **Position** and **Size** to *Apply Initially* with the values you want
3. Optionally set **Screen** to the monitor you want it on

Use the rule for placement only. Do **not** add "prevent focus stealing": the
application keeps its own rule, and KWin's focus-stealing rule has an open
enforcement bug for Wayland windows (KDE #488060), so relying on it would be
relying on something that may not fire. The app has no monitor setting of its
own for the same reason: under Wayland it could only ask, and KWin decides.

## Checking it works

    wf doctor                  # packages, input group, hotkey name, log, data, game window
    wf windows                 # is the game seen, does the class match
    wf shot --out /tmp/check   # does capture return real pixels
    wf info "Rhino Prime Systems"

`wf doctor` reads and reports only: no network, no screenshot, no writes. Each
check line is OK, INFO, WARN or FAIL, and the account id is only reported as set or not
set. Its exit status is 1 when any line is FAIL.

Then start the interface, put the game in focus and press the hotkey. The log
under Settings, Rewards page should show a line and the Rewards page should fill.
In a menu the contents will be nonsense, since there are no reward names to
read, and that is the success case: the chain ran without the game losing focus.

If nothing happens, the *Trigger test capture* button in Settings, Rewards page
bypasses the hotkey. If that works and the key does not, the problem is the key;
if neither works, it is the pipeline.

## Building a release

`packaging/make-release.sh` builds `dist/warframe-tracker.tar.gz`
from HEAD and checks it. It needs, besides the program's own
packages, on the building machine only:

- `python-markdown` and `qt6-webengine` (with PySide6's QtWebEngine) for
  `packaging/build_guide.py`, which prints `docs/GUIDE.md` to `GUIDE.pdf` and
  PATCHNOTES.md to `PATCHNOTES.pdf`; without QtWebEngine it falls back to
  QTextDocument and QPdfWriter, which is plainer;
- `tesseract` and `poppler` (`pdfinfo`, `pdftoppm`) for the private-word check
  and the page count; `optipng` to compress the guide's pictures (optional);
- unprivileged user namespaces (`unshare -rmn`) for `packaging/guide_shots.py`,
  which renders the pictures with an empty `/home` and no network;
- the reference database of the machine (the pictures are made from a copy of it
  with a small sample inventory in place of the real one), or another one named by
  `WFT_GUIDE_DB`;
- a private word list, `~/.config/warframe-tracker/release-private-words.txt` or
  the file `WFT_PRIVATE_WORDS` names: one word per line, `#` for comments. The
  words are looked for in the archive's text files, in the pictures and on the
  PDF pages. Without the list the script builds but says that nothing was checked.

The pictures and the PDFs are not in git (`/docs/guide/` and `/GUIDE.pdf` are
ignored): they are made for every archive. The archive holds the two PDFs only;
`docs/GUIDE.md`, PATCHNOTES.md and the loose pictures stay out (`export-ignore`), so
the recipient has one document per purpose. PATCHNOTES.md gets a new section,
newest first, for every archive that goes out: the changes a recipient notices,
one line each, not every commit.

Two options help while preparing a release. `packaging/make-release.sh --dry-run`
runs every step except the tests and the hand-over (about 2 minutes, from HEAD
even if the tree is dirty, never touches `dist/`): it finds a stale check in the
script, the guide text or the patch notes before the real build does. A version
that has no date yet, or that `PATCHNOTES.md` does not start with, is reported
and the dry run goes on; the real build stops there, before the tests. The tests
are mandatory for every release; after they have passed, the script remembers
the git tree (`git rev-parse HEAD^{tree}`), the time and the test list in
`${XDG_CACHE_HOME:-~/.cache}/warframe-tracker/release-tests-passed` and does not
repeat them for the same clean tree, saying so. `--retest` runs them anyway; a
failed run deletes the record.
