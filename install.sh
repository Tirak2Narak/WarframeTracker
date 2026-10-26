#!/usr/bin/env bash
# Install warframe-tracker for the current user, from the folder this script is in.
#
#   ./install.sh              install or update (safe to run again)
#   ./install.sh --uninstall  remove the desktop entry and the command links
#
# What it does: installs missing system packages (the only step that needs
# sudo), offers to add you to the `input` group, writes the desktop entry and
# links `wf` / `wf-gui` into ~/.local/bin. It copies no config: the program
# reads config.example.toml itself and your own file only holds deviations.
#
# To update: unpack the new archive and run ./install.sh from the new folder.
# It finds the old folder by the `wf-gui` link, offers to close the old version,
# moves the old folder to the trash and the new one into its place.
#
# Started from a file manager (Dolphin's "Launch", a double click) it opens a
# terminal and runs there. WFT_NO_TERMINAL=1 keeps a scripted run where it is
# and makes it ask nothing.
#
# At the end it offers to start the program. WFT_START_COMMAND replaces the
# command it starts (a test sets it to a placeholder; nobody else needs it).
set -euo pipefail

# From a file manager the script has no terminal: sudo cannot ask for the
# password, nothing it prints is seen, and the person is left with "nothing
# happens" - the first recipient's first report. So it reopens itself in a
# terminal, and that window waits for Enter before it closes, also after an
# error, so what went wrong can be read. The marker is set inside the command
# the terminal runs, not around the terminal: Konsole and GNOME Terminal can
# hand the window to a process that is already running, whose environment
# would not carry it.
if [ ! -t 0 ] && [ ! -t 1 ] && [ -z "${WFT_IN_TERMINAL:-}${WFT_NO_TERMINAL:-}" ] \
        && [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    self=$(readlink -f "${BASH_SOURCE[0]}")
    if command -v konsole >/dev/null 2>&1; then
        exec konsole --workdir "$(dirname "$self")" -e env WFT_IN_TERMINAL=1 bash "$self" "$@"
    elif command -v gnome-terminal >/dev/null 2>&1; then
        exec gnome-terminal --working-directory="$(dirname "$self")" -- env WFT_IN_TERMINAL=1 bash "$self" "$@"
    elif command -v xterm >/dev/null 2>&1; then
        exec xterm -e env WFT_IN_TERMINAL=1 bash "$self" "$@"
    fi
fi
if [ -n "${WFT_IN_TERMINAL:-}" ]; then
    trap 'printf "\n"; read -r -p "Press Enter to close this window. " _ || true' EXIT
fi

ROOT=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
PACKAGES_FILE="$ROOT/packaging/arch-packages.txt"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APPS_DIR/warframe-tracker.desktop"
BIN_DIR="$HOME/.local/bin"
LINKS=(wf wf-gui)
USER_NAME="${USER:-$(id -un)}"
#: Set when the hotkey cannot work yet because the login session predates the
#: membership of the `input` group; the closing text says so.
NEEDS_RELOGIN=

say() { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }

# A link is ours when it points at a bin/<name> of some warframe-tracker
# folder: the target exists next to a wft/ package, or is gone because that
# folder was deleted (an older unpacked version). Anything else is the user's.
is_own_link() {
    local link=$1 name=$2 target
    [ -L "$link" ] || return 1
    target=$(readlink "$link")
    case "$target" in */bin/"$name") ;; *) return 1 ;; esac
    [ ! -e "$target" ] || [ -d "$(dirname "$target")/../wft" ]
}

# The package names of packaging/arch-packages.txt, one per line, without comments.
package_list() {
    sed -e 's/#.*//' -e 's/[[:space:]]//g' "$PACKAGES_FILE" | grep -v '^$'
}

uninstall() {
    local name pkgs
    if [ -e "$DESKTOP_FILE" ]; then
        rm -f "$DESKTOP_FILE"
        command -v update-desktop-database >/dev/null 2>&1 \
            && update-desktop-database "$APPS_DIR" 2>/dev/null || true
        say "Removed desktop entry: $DESKTOP_FILE"
    else
        say "No desktop entry to remove."
    fi
    for name in "${LINKS[@]}"; do
        if is_own_link "$BIN_DIR/$name" "$name"; then
            rm -f "$BIN_DIR/$name"
            say "Removed link: $BIN_DIR/$name"
        elif [ -e "$BIN_DIR/$name" ] || [ -L "$BIN_DIR/$name" ]; then
            say "Left alone (not a link of this program): $BIN_DIR/$name"
        fi
    done
    say
    say "Kept: your config, your data, the system packages and the input group."
    say "To remove them too (all optional):"
    say "  config: ${XDG_CONFIG_HOME:-$HOME/.config}/warframe-tracker"
    say "  data:   ${XDG_DATA_HOME:-$HOME/.local/share}/warframe-tracker"
    mapfile -t pkgs < <(package_list)
    say "  packages, only if you installed them for this tool (pacman refuses to"
    say "  remove one that another program still needs):"
    say "    sudo pacman -Rns ${pkgs[*]}"
    say "  the input group: sudo gpasswd -d $USER_NAME input"
}

install_packages() {
    local pkgs missing
    mapfile -t pkgs < <(package_list)
    if ! command -v pacman >/dev/null 2>&1; then
        warn "pacman not found. Install these packages with your package manager, then run this again:"
        printf '  %s\n' "${pkgs[@]}" >&2
        exit 1
    fi
    # pacman -T prints the packages that are not installed and exits 127.
    mapfile -t missing < <(pacman -T "${pkgs[@]}" || true)
    if [ "${#missing[@]}" -eq 0 ]; then
        say "Packages: all ${#pkgs[@]} installed."
        return
    fi
    say "Packages: installing ${missing[*]} (needs sudo)"
    if ! sudo pacman -S --needed "${missing[@]}"; then
        # The usual cause on a system that was not updated for a while: the
        # mirrors no longer have the versions its package list names (404).
        warn "The packages were not installed (see pacman's message above)."
        warn "  If it could not download a file (404, \"failed retrieving file\"), update"
        warn "  the system first (on Garuda: garuda-update; otherwise: sudo pacman -Syu),"
        warn "  then run ./install.sh again."
        exit 1
    fi
}

# Prints missing, pending or active, same words as `wf doctor`.
input_group_state() {
    local entry gid members
    entry=$(getent group input || true)
    [ -n "$entry" ] || { echo nogroup; return; }
    gid=$(cut -d: -f3 <<<"$entry")
    members=$(cut -d: -f4 <<<"$entry")
    if ! { [[ ",$members," == *",$USER_NAME,"* ]] || [ "$(id -g)" = "$gid" ]; }; then
        echo missing
    elif [[ " $(id -G) " == *" $gid "* ]]; then
        echo active
    else
        echo pending
    fi
}

setup_input_group() {
    local cmd="sudo usermod -aG input $USER_NAME"
    case "$(input_group_state)" in
        active)
            say "Group input: member and active." ;;
        pending)
            NEEDS_RELOGIN=1
            say "Group input: you are a member, but this session started before that."
            say "  Log out and back in once; the hotkey cannot read the keyboard before." ;;
        nogroup)
            warn "Group input: this system has no 'input' group; the hotkey will not work."
            warn "  The manual trigger button in the app still does." ;;
        missing)
            say "Group input: the hotkey works by reading keyboard events directly, which"
            say "  Linux only allows members of the 'input' group. Membership means programs"
            say "  you run can read input devices, keystrokes included."
            if [ -t 0 ]; then
                local answer
                read -r -p "Add $USER_NAME to the input group now? [y/N] " answer
                if [[ "$answer" =~ ^[Yy]$ ]]; then
                    $cmd
                    NEEDS_RELOGIN=1
                    say "Done. Log out and back in for it to take effect."
                else
                    say "Skipped. Later: $cmd"
                fi
            else
                say "Not asking (no terminal). To do it yourself: $cmd"
            fi ;;
    esac
}

# Value safe for the right-hand side of a sed s||| expression.
sed_escape() { printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'; }

install_desktop_entry() {
    local tmp
    [ -x "$ROOT/bin/wf-gui" ] || { warn "bin/wf-gui not found or not executable in $ROOT"; exit 1; }
    mkdir -p "$APPS_DIR"
    tmp=$(mktemp)
    # From $ROOT at the time of writing: replace_previous may have moved it.
    sed -e "s|@EXEC@|$(sed_escape "$ROOT/bin/wf-gui")|g" \
        -e "s|@PROJECT@|$(sed_escape "$ROOT")|g" \
        -e "s|@ICON@|$(sed_escape "$ROOT/packaging/warframe-tracker.svg")|g" \
        "$ROOT/packaging/warframe-tracker.desktop.in" > "$tmp"
    if [ -f "$DESKTOP_FILE" ] && cmp -s "$tmp" "$DESKTOP_FILE"; then
        rm -f "$tmp"
        say "Desktop entry: up to date."
        return
    fi
    mv "$tmp" "$DESKTOP_FILE"
    chmod 644 "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 \
        && update-desktop-database "$APPS_DIR" 2>/dev/null || true
    say "Desktop entry: written to $DESKTOP_FILE"
}

install_links() {
    local name link target
    mkdir -p "$BIN_DIR"
    for name in "${LINKS[@]}"; do
        link="$BIN_DIR/$name"
        target="$ROOT/bin/$name"
        if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
            say "Link $name: up to date."
        elif [ ! -e "$link" ] && [ ! -L "$link" ] || is_own_link "$link" "$name"; then
            ln -sfn "$target" "$link"
            say "Link $name: $link -> $target"
        else
            warn "Link $name: $link exists and is not a link of this program. Left alone."
        fi
    done
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) say "Note: $BIN_DIR is not in your PATH, so 'wf' and 'wf-gui' will not be found by"
           say "  name until it is (log in again; most distributions add it when it exists)." ;;
    esac
}

# The folder of the version this one replaces: where the `wf-gui` link points,
# when that is another unpacked warframe-tracker folder. Empty when there is
# none, when it is this folder or holds it, and for a git checkout (somebody's
# working copy, not an unpacked archive).
previous_folder() {
    local link="$BIN_DIR/wf-gui" target old
    is_own_link "$link" wf-gui || return 0
    target=$(readlink -f "$link") && [ -e "$target" ] || return 0
    old=$(cd "$(dirname "$target")/.." && pwd -P) || return 0
    [ -d "$old/wft" ] && [ ! -e "$old/.git" ] || return 0
    case "$ROOT/" in "$old/"*) return 0 ;; esac
    printf '%s\n' "$old"
}

# The programs running from that folder, by the path in their command line:
# the menu starts <folder>/bin/wf-gui, a terminal the `wf-gui` link, which
# still points there when this runs.
running_from() {
    local old=$1 pid cwd arg
    for pid in $(pgrep -u "$(id -u)" -f 'bin/wf-gui' || true); do
        cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null) || continue
        while IFS= read -r -d '' arg; do
            case "$arg" in */bin/wf-gui) ;; *) continue ;; esac
            case "$arg" in /*) ;; *) arg="$cwd/$arg" ;; esac
            if [ "$(readlink -f "$arg" 2>/dev/null)" = "$old/bin/wf-gui" ]; then
                printf '%s\n' "$pid"
                break
            fi
        done < "/proc/$pid/cmdline" 2>/dev/null || true
    done
}

# Ends the processes (SIGTERM) and waits up to five seconds; false when one stays.
# A process that ended but was not yet collected by its parent (state Z) is
# gone, although `kill -0` still finds it.
close_processes() {
    local pid alive state _
    kill "$@" 2>/dev/null || true
    for _ in $(seq 50); do
        alive=
        for pid in "$@"; do
            state=$(sed -n 's/^.*) \(.\).*/\1/p' "/proc/$pid/stat" 2>/dev/null || true)
            [ -n "$state" ] && [ "$state" != Z ] && alive=1
        done
        [ -n "$alive" ] || return 0
        sleep 0.1
    done
    return 1
}

# The desktop's trash, through Qt as the program's own reset uses it; the
# packages step has installed PySide6 by now. Qt does not create the data
# folder the trash lives in, and the menu entry goes there next anyway.
to_trash() {
    mkdir -p "${XDG_DATA_HOME:-$HOME/.local/share}"
    python3 -c '
import sys
from PySide6.QtCore import QFile
result = QFile.moveToTrash(sys.argv[1])
sys.exit(0 if (result[0] if isinstance(result, tuple) else result) else 1)
' "$1" 2>/dev/null
}

# An update: offer to close the old version, move its folder to the trash and
# this one into its place, so the program stays where it was installed instead
# of moving into the download folder. Only asked of a person; a scripted run
# says where the old folder is and changes nothing.
replace_previous() {
    local old answer pids
    old=$(previous_folder)
    [ -n "$old" ] || return 0
    mapfile -t pids < <(running_from "$old")
    say
    say "Older version found: $old"
    if [ ! -t 0 ] || [ -n "${WFT_NO_TERMINAL:-}" ]; then
        say "  Left in place. Move that folder to the trash when you no longer need it."
        return 0
    fi
    if [ "${#pids[@]}" -gt 0 ]; then
        read -r -p "It is running. Close it now? [Y/n] " answer || answer=n
        if [[ "$answer" =~ ^[Nn]$ ]]; then
            say "  Left running, and its folder left in place. This version runs from $ROOT."
            return 0
        fi
        if ! close_processes "${pids[@]}"; then
            warn "It did not close. Its folder is left in place; this version runs from $ROOT."
            return 0
        fi
        say "  Closed."
    fi
    say "Your data and settings are not in that folder."
    read -r -p "Move it to the trash and put this version in its place? [Y/n] " answer || answer=n
    if [[ "$answer" =~ ^[Nn]$ ]]; then
        say "  Left in place. This version runs from $ROOT."
        return 0
    fi
    if ! to_trash "$old"; then
        warn "Could not move $old to the trash. This version runs from $ROOT."
        return 0
    fi
    say "  Old folder moved to the trash."
    if [ -e "$old" ] || ! mv "$ROOT" "$old"; then
        warn "Could not move this folder to $old. This version runs from $ROOT."
        return 0
    fi
    ROOT=$old
    say "  This version is now in $ROOT."
}

main() {
    if [ "${1:-}" = "--uninstall" ]; then
        uninstall
        return
    fi
    [ "$#" -eq 0 ] || { say "usage: ./install.sh [--uninstall]" >&2; exit 2; }
    install_packages
    setup_input_group
    replace_previous
    install_desktop_entry
    install_links
    finish
}

# Where the ending is told, in the order a newcomer needs it: start now, find it
# again later, what the group means for the hotkey, where the guide is.
finish() {
    local start_cmd answer
    say
    say "Installed."
    # Only a person at a terminal with a desktop is asked. A scripted run
    # (WFT_NO_TERMINAL) or a login over ssh has nobody to answer and no screen
    # to show the window on.
    if [ -t 0 ] && [ -z "${WFT_NO_TERMINAL:-}" ] && [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
        # End of input (Ctrl+D) counts as "no": with `set -e` a failed read would
        # end the script before it said how to find the program later.
        read -r -p "Start Warframe Tracker now? [Y/n] " answer || answer=n
        if [[ ! "$answer" =~ ^[Nn]$ ]]; then
            if [ -n "${WFT_START_COMMAND:-}" ]; then
                read -r -a start_cmd <<<"$WFT_START_COMMAND"
            else
                start_cmd=("$ROOT/bin/wf-gui")
            fi
            # Detached from this terminal: closing the window must not close
            # the program, and its messages must not land in the installer.
            setsid -f "${start_cmd[@]}" >/dev/null 2>&1 </dev/null
            say "Started."
        fi
    fi
    say "To start it later: on KDE press Alt+Space, type warframe tracker and press Enter."
    say "  On other desktops open the application launcher and look under \"Games\"."
    if [ -n "$NEEDS_RELOGIN" ]; then
        say
        say "The hotkey works only after you log out of the desktop and log in again."
        say "The Setup page of the program shows whether it is active yet."
    fi
    if [ -f "$ROOT/GUIDE.pdf" ]; then
        say
        say "The illustrated guide is the file GUIDE.pdf in $ROOT."
    fi
    if [ -f "$ROOT/PATCHNOTES.pdf" ]; then
        say "What changed in this version is in PATCHNOTES.pdf."
    fi
    say "If something does not work, run 'wf doctor' in a terminal and send the whole"
    say "output to whoever gave you the tool, or to the place you downloaded it from."
}

main "$@"
