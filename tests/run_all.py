#!/usr/bin/env python3
"""Run every test script one after the other and say which ones skipped something.

    python3 tests/run_all.py            skips are listed, the run still passes
    python3 tests/run_all.py --strict   a skip fails the run

A test that cannot find its material (screenshots, tiles, the game data) prints
a line with `SKIPPED` and exits 0. That is right in a fresh clone, where the
material does not exist, and wrong anywhere else: a pass that skipped half of
its checks says nothing. So this runner never reports a skip as plain "ok".
`--strict` is what the maintainer's machine uses before an export:
the material is there, and a skip means
it is not, which must not go unnoticed.

Never run two test runs at the same time; the interface test fails when it has
to share the machine.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: What a skipped check prints. The release script greps for the same word.
MARK = "SKIPPED"


def skip_lines(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if MARK in line]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--strict", action="store_true",
                        help="a skipped check fails the run")
    args = parser.parse_args(argv)

    failed: list[str] = []
    skipped: dict[str, list[str]] = {}
    for test in sorted(HERE.glob("test_*.py")):
        began = time.monotonic()
        done = subprocess.run([sys.executable, str(test)], capture_output=True,
                              text=True)
        seconds = time.monotonic() - began
        lines = skip_lines(done.stdout + done.stderr)
        if done.returncode:
            failed.append(test.name)
            print(f"FAIL {test.name} (exit {done.returncode}, {seconds:.0f} s)")
            print("\n".join((done.stdout + done.stderr).strip().splitlines()[-30:]))
        elif lines:
            skipped[test.name] = lines
            print(f"SKIP {test.name} ({len(lines)} {MARK} line(s), {seconds:.0f} s)")
        else:
            print(f"ok   {test.name} ({seconds:.0f} s)")

    for name, lines in skipped.items():
        for line in lines:
            print(f"  {name}: {line[:200]}")
    count = sum(len(lines) for lines in skipped.values())
    print(f"\n{len(failed)} failed, {len(skipped)} test(s) with skipped checks "
          f"({count} {MARK} line(s))")
    if failed:
        return 1
    if skipped and args.strict:
        print("strict: a skipped check counts as a failure. The screenshots, "
              "tiles and game data are missing on this machine.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
