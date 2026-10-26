#!/usr/bin/env python3
"""Look for private words in what a release archive carries.

    packaging/private_words.py --words FILE [--tree DIR] [--images DIR] [--pdf FILE]

Used by `make-release.sh`; build tool, not part of the archive. The word
list is a plain file outside the repository (names of people, handles, anything
that must not reach a stranger): one word per line, `#` starts a comment. A
word matches as a whole word and without regard to case, so "vexa" is found in
"Vexa says" and not in "vexation".

Three places are searched, because a word can hide in each:

* every text file of the unpacked archive (and every file name);
* every picture, through `tesseract`: the guide's screenshots show text that no
  text search can see;
* every page of the PDF, rendered with `pdftoppm` and read the same way.

The words are never printed except as the match that was found, and the list
is never copied anywhere.

Exit status: 0 nothing found, 1 something found, 2 the list or a tool is missing.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")


def load_words(path: Path) -> list[str]:
    """The words of a list file, in order, without comments and blank lines."""
    words = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        word = line.split("#", 1)[0].strip()
        if word:
            words.append(word)
    return words


def _pattern(word: str) -> re.Pattern:
    # Whole word: not preceded or followed by a letter, digit or underscore.
    # `\w` is Unicode-aware, so an umlaut next to a name is part of the word.
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


def find_in_text(text: str, words: list[str]) -> list[str]:
    """The listed words that occur in `text`, each once, in list order."""
    return [word for word in words if _pattern(word).search(text)]


def _is_text(path: Path) -> bool:
    """A file that is text: no NUL byte in its first 8 KiB."""
    try:
        with open(path, "rb") as handle:
            return b"\0" not in handle.read(8192)
    except OSError:
        return False


def scan_tree(root: Path, words: list[str]) -> list[tuple[str, str]]:
    """(relative path, word) for every hit in file names and text files below `root`."""
    root = Path(root)
    hits: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_dir():
            continue
        relative = str(path.relative_to(root))
        for word in find_in_text(relative, words):
            hits.append((relative + " (file name)", word))
        if path.suffix.lower() in IMAGE_SUFFIXES or not _is_text(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in find_in_text(text, words):
            hits.append((relative, word))
    return hits


def ocr(image: Path) -> str:
    """The text `tesseract` finds in a picture. Raises when it cannot run."""
    if shutil.which("tesseract") is None:
        raise RuntimeError("tesseract is not installed")
    # Page segmentation 11 (sparse text): a screenshot is words scattered over
    # panels, not a page of paragraphs. Light text on a dark ground is handled
    # by tesseract's own inversion.
    done = subprocess.run(["tesseract", str(image), "stdout", "--psm", "11"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"tesseract failed on {image}: {done.stderr.strip()}")
    return done.stdout


def scan_image(image: Path, words: list[str]) -> list[str]:
    """The listed words in the text of one picture."""
    return find_in_text(ocr(image), words)


def scan_images(folder: Path, words: list[str]) -> list[tuple[str, str]]:
    """(picture, word) for every hit in the pictures below `folder`."""
    folder = Path(folder)
    hits = []
    for image in sorted(p for p in folder.rglob("*")
                        if p.suffix.lower() in IMAGE_SUFFIXES):
        for word in scan_image(image, words):
            hits.append((str(image.relative_to(folder)), word))
    return hits


def pdf_pages(pdf: Path, into: Path, resolution: int = 130) -> list[Path]:
    """The pages of a PDF as PNG files in `into`, in page order."""
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm is not installed")
    done = subprocess.run(["pdftoppm", "-r", str(resolution), "-png", str(pdf),
                           str(into / "page")], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"pdftoppm failed: {done.stderr.strip()}")
    return sorted(into.glob("page-*.png"))


def scan_pdf(pdf: Path, words: list[str]) -> list[tuple[str, str]]:
    """(page label, word) for every hit on a page of the PDF, read as a picture."""
    hits = []
    with tempfile.TemporaryDirectory(prefix="wft-pdfpages-") as folder:
        for number, page in enumerate(pdf_pages(Path(pdf), Path(folder)), start=1):
            for word in scan_image(page, words):
                hits.append((f"{Path(pdf).name} page {number}", word))
    return hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--words", type=Path, required=True,
                        help="the word list (one word per line, # comments)")
    parser.add_argument("--word", action="append", default=[],
                        help="one more word to look for (repeatable), for example "
                             "the user name of the machine that builds the archive")
    parser.add_argument("--tree", type=Path, help="unpacked archive: text files and names")
    parser.add_argument("--images", type=Path, help="folder of pictures, read with tesseract")
    parser.add_argument("--pdf", type=Path, action="append", default=[],
                        help="a PDF, read page by page with tesseract (repeatable)")
    args = parser.parse_args(argv)

    try:
        words = load_words(args.words)
    except OSError as exc:
        print(f"cannot read the word list: {exc}", file=sys.stderr)
        return 2
    words += [word for word in args.word if word and word not in words]
    if not words:
        print("the word list holds no words", file=sys.stderr)
        return 2

    hits: list[tuple[str, str]] = []
    try:
        if args.tree:
            hits += scan_tree(args.tree, words)
        if args.images:
            hits += scan_images(args.images, words)
        for pdf in args.pdf:
            hits += scan_pdf(pdf, words)
    except RuntimeError as exc:
        print(f"cannot check: {exc}", file=sys.stderr)
        return 2
    for where, word in hits:
        print(f"PRIVATE WORD {word!r} in {where}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
