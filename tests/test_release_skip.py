#!/usr/bin/env python3
"""The decision in packaging/make-release.sh whether the tests may be skipped.

The script is sourced (it then only defines its functions) in a throwaway git
repository with a temporary XDG_CACHE_HOME; the real repository, cache and dist
are never touched. Cases: clean + same tree + same list skips; another tree,
a dirty tree, another test list, a missing or garbled record, a failed run's
deleted record and --retest all run the tests.
"""
import os
import pathlib
import subprocess
import sys
import tempfile

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "packaging" / "make-release.sh"
problems = []


def sh(repo, cache, body):
    """Run bash `body` with the script's functions in `repo`; return (rc, stdout)."""
    env = dict(os.environ, XDG_CACHE_HOME=str(cache))
    r = subprocess.run(["bash", "-c", f'set -euo pipefail; source "{SCRIPT}"; {body}'],
                       cwd=repo, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def expect(label, got, want):
    if got != want:
        problems.append(f"{label}: got {got!r}, wanted {want!r}")


with tempfile.TemporaryDirectory() as tmp:
    repo = pathlib.Path(tmp) / "repo"
    cache = pathlib.Path(tmp) / "cache"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "one")
    record = cache / "warframe-tracker" / "release-tests-passed"

    expect("no record runs", sh(repo, cache, "tests_already_passed")[0], 1)
    sh(repo, cache, "tests_record_pass")
    expect("record is written", record.is_file(), True)
    rc, out = sh(repo, cache, "tests_already_passed")
    expect("clean, same tree, same list skips", rc, 0)
    expect("the time of the pass is printed", len(out), 19)
    expect("a failed run forgets the record",
           (sh(repo, cache, "tests_forget; tests_already_passed")[0], record.exists()), (1, False))

    sh(repo, cache, "tests_record_pass")
    (repo / "a.txt").write_text("two\n")
    expect("dirty tree runs", sh(repo, cache, "tests_already_passed")[0], 1)
    git(repo, "commit", "-q", "-am", "two")
    expect("another tree runs", sh(repo, cache, "tests_already_passed")[0], 1)
    sh(repo, cache, "tests_record_pass")
    expect("recorded again, skips", sh(repo, cache, "tests_already_passed")[0], 0)
    expect("another test list runs",
           sh(repo, cache, "TESTS+=(test_new.py); tests_already_passed")[0], 1)
    (repo / "untracked.txt").write_text("x\n")
    expect("an untracked file counts as dirty", sh(repo, cache, "tests_already_passed")[0], 1)
    (repo / "untracked.txt").unlink()

    for label, text in (("garbled record", "nonsense\n"), ("empty record", ""),
                        ("bad hash", "tree=zzz\ntime=now\ntests=x\n")):
        record.write_text(text)
        expect(f"{label} runs", sh(repo, cache, "tests_already_passed")[0], 1)

# --retest and --dry-run are read by the script's own argument loop; check the
# wiring without running a build: a bad argument stops at once with exit 2.
r = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True)
expect("unknown argument", r.returncode, 2)
r = subprocess.run(["bash", str(SCRIPT), "--retest", "--dry-run"], capture_output=True, text=True)
expect("--retest with --dry-run", r.returncode, 2)
text = SCRIPT.read_text()
expect("--retest bypasses the record",
       '[ "$RETEST" = no ] && passed_at=$(tests_already_passed)' in text.replace("elif ", ""), True)

for p in problems:
    print("   ", p)
print(f"FAILED — {len(problems)} problem(s)" if problems else "OK")
sys.exit(1 if problems else 0)
