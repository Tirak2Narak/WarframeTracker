# Warframe Tracker — patch notes

The changes worth knowing about, newest first. How everything works now is in
GUIDE.pdf.

## 1.5 — 2026-10-28

Replaces 1.3 (2026-10-23).

- **Settings has tabs.** Appearance, Game data, Capture, Rewards, Fissures and
  Reset, like the Collection page; the version line stays at the bottom.
- **You set the order of the recommendation.** Under Settings, Rewards, move
  the questions the tool asks about each reward (missing piece first, hard to
  get, finishes a set, ducats and so on) with the arrows and give each item
  type a number; Reset to default brings back the usual order, which is
  unchanged.
- **You choose the quick fissures.** Under Settings, Fissures, tick the
  mission types the Fissures page marks in green (Capture and Extermination to
  begin with); Reset to default brings those two back.
- **Open next is simpler and no longer asks for finished sets.** It shows one
  percentage, the chance that an opening gives a part you still need, and the
  same chance at Radiant. Parts of a set you already built (Bronco Prime's
  receiver once Akbronco Prime is done, Lex Prime's once 3 are built) are no
  longer counted as needed.
- **Beasts count for mastery.** Kubrows, Kavats, Vulpaphylas, Predasites and
  the Helminth Charger are marked from your profile and counted in the totals;
  a recording still cannot show them.
- **Fewer questions after a scan.** Names that lost their first letters
  ("andao Prime Blade" for Guandao) are read a second time without the
  neighbouring tiles' names; in one Prime Parts recording 11 such questions
  went down to 2.
- **Keep one for myself.** The window of a set has a box for weapons that are
  a piece of another (Lex Prime inside Aklex Prime). Without it, pieces of such
  a weapon count as spare as soon as the built copies cover the recipe, where
  before one set was always held back; with it, one more copy is counted.
- **Review after a scan.** When a badge icon's noise letters before "Lex Prime
  Blueprint" made "Aklex Prime Blueprint" win, the shorter twin ("Lex ...") is
  now offered first. Names with letters cut off the front are not changed.
- **Review after a scan.** The arrow keys move through the list and no longer
  change an answer you clicked, and they keep working after you click into a
  question. The mouse wheel scrolls the list instead of changing a number.
  **Esc** takes you from the name field back to the list.
- **Review with one hand.** The number pad picks (1 to 6), ignores (0) and
  changes the number (+ and -). **Shift+Up** and **Shift+Down** jump to the
  previous and next question you have not opened.
- **Requiem relics.** Whenever one of the four Requiem relics is suggested,
  they stand on 1 to 4 in the order I, II, III, IV.
- **Summary before writing.** The "gone?" rows, which would write 0, and the
  pieces a build used up are now at the top, with a line saying how many. **Enter** ticks or unticks
  the row you are on.
- **Mastery fetches itself.** Once your account id is in the Import page, the
  tool asks for your profile once a day (after a refusal not before the next
  day), shows when it did and what it found, and marks newly levelled items.
  The first time you review the list; the circular-arrow button fetches now, and
  the old way with a saved file still works. Untick the box to turn it off.
- **Pictures fetch themselves.** The item icons are now fetched when something
  is missing: right after an import you applied, and otherwise at most once a
  day. One missing icon is skipped; if the server refuses or cannot be reached
  the run ends and the tool waits a day, imports included. The card on the Import page
  shows the last run, has a circular-arrow button to fetch now and a box to
  turn it off.
- **Game data updates itself.** Once the first download is done, the tool
  updates the game data and which relics are vaulted by itself, at most once a
  day; after a failure it waits a day. Under Settings, Reference data, a box
  turns it off and the lines under it show the next check and the last run. The
  buttons there still work at any time.
- **Relic questions keep the refinement.** When a relic tile is unclear and the
  tool asks, the choices now carry the refinement the tile shows ("Meso V16 Relic
  [Radiant]") and writing records that stack; before, it recorded the Intact
  one and listed the refined one as gone. A name you type without a refinement
  takes the tile's, and "[Intact]" typed by hand still means Intact. The "gone?"
  rows name the refinement too.
- **Trading lists.** On **I want**, "Sets missing at most" accepts 0 for no
  limit, so the page can list every set you are missing parts of; with a limit,
  a line says how many sets it hides. Prime parts that cannot be traded
  (Galariak Prime, Sagek Prime) are no longer on either list.
- **A click after the screen closed.** A click on the next screen, landing where
  card 1 had been, was recorded as your pick. A click made once the reward
  drawing has noticed the screen is gone no longer counts.
- **"Gone?" rows for everything.** A capture now also offers rows you entered by
  hand or got from a relic pick as "gone?" when their category was captured.
  They are unticked and say where the number came from. The note reminds you
  that a part being built in the foundry is missing from every capture. Parts
  and built items are judged apart, so a Prime Parts recording offers every
  missing part of the categories it showed, however many, and never a built item.
- **Scan misreads.** A stack of 2 whose tile has part of the Warframe's picture
  next to the number was recorded as 24; and a Radiant "Meso V15" relic was
  recorded as "Meso V1" when two of the reading attempts dropped the 5.
- **Less processor load.** While a long list was open on the Plan, Fissures or
  Rivens page, the tool kept one processor core fully busy. It now sits idle.
- **Refining in the mission.** A relic you refine in the relic choice between
  the rounds of an endless mission is now recognised, and the tool takes the
  relic you refined off your shelf without asking.
- **Status line.** The line at the bottom now also says how old your last
  inventory import and your last mastery import are, so you can see when to
  import again. When it is wider than the window it scrolls slowly instead of
  being cut off.
- **New licence.** The tool is now under the GNU General Public License,
  version 3 or later (see LICENSE); before it was MIT.

## 1.3 — 2026-10-23

Replaces 1.2 (2026-10-21).

- **Trading.** A new group in the navigation with two pages: **I want** lists
  the prime sets you are only a part or two short of, **I offer** the prime
  parts no set of yours needs. Filters: how many parts may be missing, only
  sets you have not mastered, vaulted only, categories, and on I offer a
  platinum limit on the last looked-up price.
- **Message for the chat.** Each page writes the list as a message; **Trade
  for** picks parts of the same ducat value, platinum or both, and the title,
  the terms and the brackets follow it. **Copy message** puts it on the
  clipboard for Matrix. **Save as CSV...** gives a table for a spreadsheet,
  **Save as HTML...** and **Open in browser** a page to look at or print.
- **After a trade.** **Got one** on I want and **It’s gone** on I offer update
  your stock, so the lists stay right.
- **Add and correct.** Just type the name: the form finds out whether it is a
  part, a built item or a relic. Every search box forgives typos, and the
  name fields suggest names while you type, typos included.
- **Built but not mastered.** The parts of an item you have built but not yet
  mastered now count as spare, on the Ducats page too: the built copy is what
  you level.
- **Your relic after a reward screen.** The tool now sees all relics of a squad
  that fills up slowly, finds your own relic also when the game did not ask you
  to confirm it, and a Forma you take no longer counts as a part. Forma, Kuva
  and the like are never counted, wherever you enter them.

## 1.2 — 2026-10-21

Replaces 1.1 (2026-10-18).

- **Reward overlay.** After the hotkey, the recommendation is drawn over the
  game: a frame round the card to take, "TAKE THIS", and up to 3 lines on a
  dark box right under the player names of every card. It goes away with the
  reward screen, after 15 seconds at the latest. The desktop notification is
  now only the fallback, when the overlay is off or cannot be shown. Tried on
  KDE Plasma under Wayland with the game running through XWayland at
  3840x2160; other desktops have not been tried.
- **Overlay colours.** The Overlays page offers red and green, orange and blue
  for colour blindness, the colours of the app themes, or your own two colours.
- **Click to pick.** While the overlay is up, the card you click with the left
  mouse button in the game counts as your pick and is recorded when the overlay
  ends, with the same undo as **I took this**. The Overlays page can turn it
  off.
- **One pick per reward screen.** Reading the same screen again no longer
  records a second part; a later click or **I took this** moves the pick
  instead of adding one. Taking a Forma now spends its relic too.
- **Your own relic.** The relic taken off your shelf is the one you equipped,
  read from the game's log. Only tried with the game in English.
- **Calibration to the pixel.** The calibration dialog moves the card with the
  arrow keys, changes its size with Shift and the gap between cards with Alt,
  and shows magnified corners. An older calibration that overrides the measured
  values is shown in Settings and the dialog, and **Use the measured values**
  removes it.
- **Log files.** The tool writes one log file per week and keeps the last 4.
  Settings shows the folder; send the current file with a problem report.
- **Overlays page.** A new page with a see-through grid over the Arsenal's
  colour palette, so a square can be picked by letter and number.
- **Reward pictures.** Settings has a card for keeping a picture of every
  reward screen. It is off by default, saves one folder per day, and sends day
  folders older than 30 days (adjustable) to the trash. The pictures show your
  squad's names.
- **Version numbers.** The Settings footer and `wf doctor` show the version
  number, and after an update a small window, **What's new**, lists the changes.
  The footer button opens it again.

## 1.1 — 2026-10-18

Replaces 1.0 (2026-10-14).

- **Setup page.** A new page checks what the tool needs (game data, keyboard
  access, hotkey, game log, game window, first reward screen, inventory) and
  says what is missing and how to fix it. The tool opens on it until the
  essentials are done.
- **Hotkey.** You choose it by pressing the key. It works without restarting
  the tool.
- **Game log.** EE.log is found in any Steam library, not only the default one.
- **Reward screens.** The reading areas of the previous version read no names.
  They are fixed for 16:9 screens. If names still come out wrong, calibrate
  from the last reward screen with one button. "Capture the game window" in
  the calibration no longer crashes.
- **Inventory import.** Works with 1920x1080 pictures (tried on 4K pictures
  scaled down) and no longer stops at a tile cut off at the edge. Screenshots
  of nearly empty tabs are read in full, and artwork in a tile's corner is no
  longer taken for a stack count.
- **Counts.** A built weapon that is also part of another one (Lex Prime for
  Aklex Prime) is counted once.
- **Window.** The tool remembers its window size.
- **Notification.** After each reading the recommendation also appears as a
  desktop notification; Settings can turn it off. On KDE it needs one setting
  to show while the game is in fullscreen (GUIDE.pdf says which). Tried with
  two monitors, where it appears beside the game; with one monitor it has not
  been tried.
- **Installer.** Started from the file manager it opens a terminal. At the end
  it offers to start the tool. When a package step fails it says what helps.
  `./install.sh --uninstall` names what is left and prints the commands for
  the packages and the input group.
- **Guide.** GUIDE.pdf with pictures of every step. The Setup page has a button
  **Open the guide**.
- **Start over.** Settings has **Delete my inventory...** (parts, items,
  relics and mastery; a backup goes to the trash first) and **Delete
  everything...** (back to the first start).
- **Updating.** Unpack the new archive and run `./install.sh` from it. It
  closes the old version, moves the old folder to the trash and puts the new
  one in its place.
