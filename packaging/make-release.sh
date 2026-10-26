#!/usr/bin/env bash
# Build the release tarball from the last commit and prove it works unpacked.
#
#   packaging/make-release.sh              the real build
#   packaging/make-release.sh --retest     the real build, tests run even if
#                                          they already passed on this tree
#   packaging/make-release.sh --dry-run    every step except the tests and the
#                                          hand-over (2-3 minutes); may run on
#                                          a dirty tree and on a version not
#                                          dated yet, never touches dist/
#
# Produces dist/warframe-tracker.tar.gz, replacing the one before it only once
# every check has passed; dist holds that one archive and nothing else. Nothing is
# uploaded or published. The script refuses to build from a dirty tree (the
# archive is made from HEAD, so uncommitted work would silently be missing),
# checks that wft/whatsnew.toml and PATCHNOTES.md name the same version and date,
# runs the tests, makes the guide (pictures of the interface from
# packaging/guide_shots.py, GUIDE.pdf from docs/GUIDE.md by packaging/build_guide.py;
# neither is in git, they are made here from the reference data of this
# machine) and PATCHNOTES.pdf from PATCHNOTES.md, puts the two PDFs into the
# archive (docs/GUIDE.md, PATCHNOTES.md and the loose pictures stay out: the PDFs
# hold them, and one document per purpose cannot disagree with a second copy),
# then unpacks the result into a scratch directory and checks what a recipient
# would get: no files meant only for the build, none of this machine's paths or user
# name, a filled-in version stamp, both PDFs, every picture docs/GUIDE.md names made
# and every picture made shown, none of the words in the private word list (in
# text files, in the pictures and on the PDF pages), and a first start with
# empty home that does not crash.
#
# The tests are mandatory for every release. Their result is remembered per git
# tree: after a pass the tree hash (git rev-parse HEAD^{tree}), the time and the
# test list go to ${XDG_CACHE_HOME:-~/.cache}/warframe-tracker/release-tests-passed
# (outside the repository and outside dist). A later run on a clean tree with
# the same hash and the same list does not repeat the 5 minutes of tests; it
# says so. --retest runs them anyway, a failed test run deletes the record.
# --dry-run is for finding stale checks in this script early: use it after
# changing the script, the guide or the patch notes, before the real build.
#
# The private word list is a file outside the repository, one word per line, `#`
# for comments: ${WFT_PRIVATE_WORDS:-~/.config/warframe-tracker/release-private-words.txt}.
# Without it the script still builds, but says loudly that nothing was checked.
# WFT_GUIDE_DB names another reference database for the pictures (default: the
# one the configuration names; it is only copied, never changed).
set -euo pipefail

TESTS=(test_badges.py test_droptables.py test_inventory_fixtures.py
       test_ranking.py test_riven.py test_release_skip.py test_trading.py test_profile_fetch.py test_picture_fetch.py
       test_reference_update.py test_ui_smoke.py)
# Must match the export-ignore lines in .gitattributes.
EXCLUDED=(docs/GUIDE.md docs/guide docs/UI-LAYOUT.md tests
          packaging/make-release.sh packaging/guide_shots.py
          packaging/build_guide.py packaging/private_words.py .gitattributes
          .gitignore README.md CONTRIBUTING.md PATCHNOTES.md)
PRIVATE_WORDS=${WFT_PRIVATE_WORDS:-$HOME/.config/warframe-tracker/release-private-words.txt}

# -- remembered test result ----------------------------------------------------
tests_record() { printf '%s\n' "${XDG_CACHE_HOME:-$HOME/.cache}/warframe-tracker/release-tests-passed"; }

# Succeeds (and prints the time of the pass) only when the tree is clean, its
# hash equals the recorded one and the test list is the same. Anything else,
# including a missing or garbled record, fails: the tests then run.
tests_already_passed() {
    local record tree rec_tree rec_time rec_tests
    record=$(tests_record)
    [ -f "$record" ] || return 1
    [ -z "$(git status --porcelain)" ] || return 1
    tree=$(git rev-parse 'HEAD^{tree}' 2>/dev/null) || return 1
    rec_tree=$(sed -n 's/^tree=//p' "$record")
    rec_time=$(sed -n 's/^time=//p' "$record")
    rec_tests=$(sed -n 's/^tests=//p' "$record")
    [ "$(grep -c . "$record")" = 3 ] || return 1
    [[ "$rec_tree" =~ ^[0-9a-f]{40,64}$ ]] || return 1
    [ -n "$rec_time" ] || return 1
    [ "$rec_tree" = "$tree" ] || return 1
    [ "$rec_tests" = "${TESTS[*]}" ] || return 1
    printf '%s\n' "$rec_time"
}

tests_record_pass() {
    local record
    record=$(tests_record)
    mkdir -p "$(dirname "$record")"
    printf 'tree=%s\ntime=%s\ntests=%s\n' "$(git rev-parse 'HEAD^{tree}')" \
        "$(date '+%Y-%m-%d %H:%M:%S')" "${TESTS[*]}" >"$record"
}

tests_forget() { rm -f "$(tests_record)"; }

# Sourced (by the test of the decision above), the script only defines the functions.
[ "${BASH_SOURCE[0]}" = "$0" ] || return 0

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.."
ROOT=$PWD

RETEST=no
DRY_RUN=no
for arg in "$@"; do
    case "$arg" in
        --retest) RETEST=yes ;;
        --dry-run) DRY_RUN=yes ;;
        *) printf 'unknown argument: %s (use --dry-run or --retest)\n' "$arg" >&2; exit 2 ;;
    esac
done
[ "$RETEST$DRY_RUN" != yesyes ] || { echo "--retest and --dry-run exclude each other" >&2; exit 2; }
started=$SECONDS

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

SCRATCH=$(mktemp -d)
# find -delete rather than rm -r: the scratch directory is ours, but a typo in
# a variable must not be able to reach further than the find's start point.
trap 'find "$SCRATCH" -delete 2>/dev/null || true' EXIT

# -- 1. clean tree ---------------------------------------------------------
step "Working tree"
if [ -n "$(git status --porcelain)" ]; then
    if [ "$DRY_RUN" = yes ]; then
        git status --short >&2
        echo "dirty: the dry run builds from HEAD, the uncommitted changes above are NOT in it"
    else
        git status --short >&2
        fail "uncommitted changes; commit them first (the archive is built from HEAD)"
    fi
else
    echo "clean"
fi

# -- 1b. the number ----------------------------------------------------------
# The newest entry of wft/whatsnew.toml is the version this archive carries. It
# needs a date, and PATCHNOTES.md must start with the same number and date. A
# dry run only reports a disagreement: between releases the version is not
# dated yet, and the real build stops here within seconds, before the tests.
step "Version number"
number=$(python3 -c 'from wft import version; print(version.number())')
if python3 -m wft.whatsnew wft/whatsnew.toml PATCHNOTES.md; then
    echo "ok  version $number, dated and the same in PATCHNOTES.md"
elif [ "$DRY_RUN" = yes ]; then
    echo "not dated: wft/whatsnew.toml and PATCHNOTES.md do not agree (above); the real build stops here"
else
    fail "wft/whatsnew.toml and PATCHNOTES.md do not agree (see above)"
fi

# -- 1c. the guide's lists -----------------------------------------------------
# The same check the guide build runs at the end, done here first: a nested
# list python-markdown flattens used to fail the build only after the tests,
# 6 minutes in.
step "Guide text"
python3 -c 'import sys; sys.path.insert(0, "packaging"); import build_guide
from pathlib import Path
build_guide.markdown_to_html(Path("docs/GUIDE.md"))
build_guide.markdown_to_html(Path("PATCHNOTES.md"), contents=False)' \
    || fail "docs/GUIDE.md or PATCHNOTES.md does not survive python-markdown (see above)"
echo "ok  docs/GUIDE.md and PATCHNOTES.md keep their lists"

# -- 2. tests --------------------------------------------------------------
step "Tests"
if [ "$DRY_RUN" = yes ]; then
    echo "tests: not run (dry run)"
elif [ "$RETEST" = no ] && passed_at=$(tests_already_passed); then
    echo "tests: passed on this exact tree at $passed_at, not run again (--retest runs them)"
else
    tests_forget
    tests_start=$SECONDS
    for t in "${TESTS[@]}"; do
        t_start=$SECONDS
        if ! python3 "tests/$t" >"$SCRATCH/$t.log" 2>&1; then
            tail -n 30 "$SCRATCH/$t.log" >&2
            fail "tests/$t failed"
        fi
        echo "ok  $t ($((SECONDS - t_start)) s)"
    done
    echo "all ${#TESTS[@]} tests passed in $((SECONDS - tests_start)) s"
    tests_record_pass
fi

# -- 3. the guide ------------------------------------------------------------
# Made in the scratch directory, not in the working tree: the pictures depend
# on this machine's reference data, so they are not committed, and
# nothing here may leave untracked files behind.
step "Guide"
hash=$(git log -1 --format=%h)
date=$(git log -1 --format=%cd --date=format:%Y-%m-%d)
guide="$SCRATCH/guide"
mkdir -p "$guide/guide"
cp docs/GUIDE.md "$guide/GUIDE.md"
shots_args=(--out "$guide/guide")
[ -z "${WFT_GUIDE_DB:-}" ] || shots_args+=(--source-db "$WFT_GUIDE_DB")
python3 packaging/guide_shots.py "${shots_args[@]}" \
    || fail "packaging/guide_shots.py failed"
python3 packaging/build_guide.py --source "$guide/GUIDE.md" --out "$guide/GUIDE.pdf" \
    --stamp "Release $number, $hash, $date" || fail "packaging/build_guide.py failed"
cp PATCHNOTES.md "$guide/PATCHNOTES.md"
python3 packaging/build_guide.py --source "$guide/PATCHNOTES.md" \
    --out "$guide/PATCHNOTES.pdf" --stamp "Release $number, $hash, $date" --no-contents \
    || fail "packaging/build_guide.py failed on PATCHNOTES.md"
chmod 644 "$guide/GUIDE.pdf" "$guide/PATCHNOTES.pdf" "$guide"/guide/*.png

# -- 4. archive ------------------------------------------------------------
step "Archive"
# Built and checked in the scratch folder; dist gets it at the very end.
archive="$SCRATCH/warframe-tracker.tar.gz"
# git archive builds the tar (modes, the filled-in stamp); the guide is added
# to that tar before it is compressed, with the commit's time so the archive
# does not depend on when it was built.
plain="$SCRATCH/release.tar"
git archive --format=tar --prefix=warframe-tracker/ -o "$plain" HEAD
tar -rf "$plain" --owner=0 --group=0 --numeric-owner \
    --mtime="$(git log -1 --format=%cI)" --transform 's,^,warframe-tracker/,' \
    -C "$guide" GUIDE.pdf PATCHNOTES.pdf
gzip -n -c "$plain" > "$archive"
echo "$archive"

# -- 5. what is inside -----------------------------------------------------
step "Archive contents"
unpacked="$SCRATCH/unpacked"
mkdir "$unpacked"
tar -xzf "$archive" -C "$unpacked"
tree="$unpacked/warframe-tracker"

for path in "${EXCLUDED[@]}"; do
    [ ! -e "$tree/$path" ] || fail "excluded path is in the archive: $path"
done
echo "ok  excluded paths are absent"

# The pattern is assembled so that this script does not match itself.
home_prefix="/ho""me/"
hits=$(grep -rIn -F -- "$home_prefix" "$tree" || true)
[ -z "$hits" ] || { printf '%s\n' "$hits" >&2; fail "a home directory path is in the archive"; }
echo "ok  no home directory paths in the archive"

user=${USER:-$(id -un)}
[ -n "$user" ] || fail "cannot determine the user name to look for"
hits=$(grep -rIwn -- "$user" "$tree" || true)
[ -z "$hits" ] || { printf '%s\n' "$hits" >&2; fail "user name '$user' found in the archive"; }
echo "ok  user name does not occur"

stamp_commit=$(sed -n 1p "$tree/wft/release_stamp.txt")
case "$stamp_commit" in
    ''|'$Format'*) fail "wft/release_stamp.txt was not filled in by git archive" ;;
esac
[ "$stamp_commit" = "$hash" ] || fail "stamp says '$stamp_commit', expected '$hash'"
echo "ok  version stamp filled in ($stamp_commit)"

# -- the guide inside the archive --------------------------------------------
[ -f "$tree/GUIDE.pdf" ] || fail "GUIDE.pdf is not in the archive"
pages=$(pdfinfo "$tree/GUIDE.pdf" | sed -n 's/^Pages:[[:space:]]*//p')
[ "${pages:-0}" -gt 1 ] || fail "GUIDE.pdf has ${pages:-no} page(s), expected more than 1"
echo "ok  GUIDE.pdf is in the archive, $pages pages"
[ -f "$tree/PATCHNOTES.pdf" ] || fail "PATCHNOTES.pdf is not in the archive"
notes_pages=$(pdfinfo "$tree/PATCHNOTES.pdf" | sed -n 's/^Pages:[[:space:]]*//p')
[ "${notes_pages:-0}" -ge 1 ] || fail "PATCHNOTES.pdf has no pages"
echo "ok  PATCHNOTES.pdf is in the archive, $notes_pages page(s)"

# The pictures live inside GUIDE.pdf only. Checked where they were made: every
# picture docs/GUIDE.md names was made (build_guide.py stops on a missing one too),
# and every picture made is shown - one nobody shows is a wasted shot.
pictures=$(grep -oE '!\[[^]]*\]\([^)]+\)' "$guide/GUIDE.md" | sed -E 's/.*\(([^)]+)\)$/\1/' | sort -u)
[ -n "$pictures" ] || fail "docs/GUIDE.md names no pictures"
count=0
while IFS= read -r picture; do
    [ -f "$guide/$picture" ] || fail "docs/GUIDE.md names $picture and it was not made"
    count=$((count + 1))
done <<<"$pictures"
echo "ok  all $count pictures docs/GUIDE.md names were made"
for file in "$guide"/guide/*.png; do
    grep -qF "guide/$(basename "$file")" "$guide/GUIDE.md" \
        || fail "guide/$(basename "$file") was made and docs/GUIDE.md does not show it"
done
echo "ok  every picture made is shown by docs/GUIDE.md"

# -- private words -------------------------------------------------------------
# Text files and names, every picture (tesseract) and every page of the PDF
# (pdftoppm, then tesseract). The list itself is never copied or printed; a hit
# prints the word that was found and where. The user name of this machine is
# looked for as well: the pictures are rendered under another one.
step "Private words"
words_checked=no
if [ -f "$PRIVATE_WORDS" ]; then
    python3 packaging/private_words.py --words "$PRIVATE_WORDS" --word "$user" \
        --tree "$tree" --images "$guide/guide" --pdf "$tree/GUIDE.pdf" \
        --pdf "$tree/PATCHNOTES.pdf" \
        || fail "the private word check found something, or could not run (above)"
    words_checked=yes
    echo "ok  no private word in text files, pictures or PDF pages"
else
    printf '%s\n' "WARNING: no private word list at $PRIVATE_WORDS" \
        "         (set WFT_PRIVATE_WORDS, or create that file: one word per line)." \
        "         NOTHING WAS CHECKED for names in the text, the pictures or the PDF." >&2
fi

# -- 6. first start --------------------------------------------------------
step "First start (empty home, no network if possible)"
fs="$SCRATCH/firstrun"
mkdir -p "$fs/home" "$fs/config" "$fs/data" "$fs/cache" "$fs/state"
env_args=(HOME="$fs/home" XDG_CONFIG_HOME="$fs/config" XDG_DATA_HOME="$fs/data"
          XDG_CACHE_HOME="$fs/cache" XDG_STATE_HOME="$fs/state"
          QT_QPA_PLATFORM=offscreen)
wrap=()
if unshare -rn true 2>/dev/null; then
    wrap=(unshare -rn)
    echo "network namespace: yes (unshare -rn)"
else
    echo "NOTE: 'unshare -rn' does not work here; first start runs WITH network access"
fi

doctor_out="$SCRATCH/doctor.txt"
doctor_rc=0
env "${env_args[@]}" "${wrap[@]}" "$tree/bin/wf" doctor >"$doctor_out" 2>&1 || doctor_rc=$?
cat "$doctor_out"
if [ "$doctor_rc" -gt 1 ] || grep -q 'Traceback' "$doctor_out"; then
    fail "wf doctor crashed (exit $doctor_rc)"
fi
# The header reads "version 1.2 (abc1234)": the number, then the commit.
head -n 1 "$doctor_out" | grep -qF "version $number ($hash)" \
    || fail "wf doctor header does not show version $number ($hash)"
echo "ok  wf doctor ran (exit $doctor_rc), header shows $number ($hash)"

if ! env "${env_args[@]}" "${wrap[@]}" timeout 300 "$tree/bin/wf-gui" --self-test \
        >"$SCRATCH/selftest.txt" 2>&1; then
    tail -n 30 "$SCRATCH/selftest.txt" >&2
    fail "wf-gui --self-test failed"
fi
echo "ok  wf-gui --self-test exit 0"

# -- hand over ------------------------------------------------------------
if [ "$DRY_RUN" = yes ]; then
    step "Dry run done"
    echo "took $((SECONDS - started)) s; tests and hand-over were skipped, no archive was replaced (dist/ untouched)"
    exit 0
fi
# One archive in dist, always under the same name: which build it is stands in
# PATCHNOTES.pdf, the Settings footer and `wf doctor`, and an older archive
# beside it is only a second number to compare. Replaced only here, so a build
# that fails a check leaves the archive that is out there in place.
mkdir -p "$ROOT/dist"
for old in "$ROOT"/dist/warframe-tracker*.tar.gz; do
    [ -e "$old" ] || continue
    gio trash "$old" 2>/dev/null || rm -f "$old"
done
mv "$archive" "$ROOT/dist/warframe-tracker.tar.gz"
archive="$ROOT/dist/warframe-tracker.tar.gz"

# -- done ------------------------------------------------------------------
step "Done"
echo "build:   $hash, $date"
echo "archive: $archive"
echo "size:    $(du -h "$archive" | cut -f1) ($(stat -c %s "$archive") bytes)"
echo "sha256:  $(sha256sum "$archive" | cut -d' ' -f1)"
echo "guide:   GUIDE.pdf $pages pages, $count pictures; PATCHNOTES.pdf $notes_pages page(s)"
if [ "$words_checked" = yes ]; then
    echo "words:   private word list checked"
else
    echo "words:   NOT CHECKED (no word list)" >&2
fi
