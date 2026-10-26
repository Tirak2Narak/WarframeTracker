#!/usr/bin/env python3
"""Build GUIDE.pdf from docs/GUIDE.md (and PATCHNOTES.pdf from PATCHNOTES.md).

    packaging/build_guide.py [--source docs/GUIDE.md] [--out GUIDE.pdf]
                             [--stamp TEXT] [--engine auto|webengine|textdoc]
                             [--no-contents]

Release-build tool, not part of the release archive: the recipient gets the PDF,
not the means to make it. Needs `python-markdown` and PySide6 (with
QtWebEngine for the first way below); `make-release.sh` runs it.

docs/GUIDE.md is the one source. It is turned into HTML (python-markdown: tables,
fenced code, a table of contents) with a print stylesheet of our own, and that
is printed to A4 PDF in one of two ways:

1. `webengine`: QtWebEngine's `printToPdf`, offscreen. Chromium lays the page
   out, so the pictures keep their column width, code keeps its monospace font
   without hyphenation, tables break at rows, and links are clickable. The
   first choice.
2. `textdoc`: QTextDocument and QPdfWriter, no browser engine. Plainer: Qt's
   rich text takes only part of CSS. Used when the first way cannot print (no
   QtWebEngine, or it produced nothing within the time limit).

`--engine auto` (the default) tries the first and falls back to the second; the
way that was used is printed. Nothing is opened on screen: both run offscreen.

Pictures are the files docs/GUIDE.md names (guide/*.png, that is docs/guide/*.png),
read relative to the source file; a missing one is an error, not a blank.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: How long QtWebEngine may take to print before the fallback is used.
WEBENGINE_TIMEOUT_S = 90

#: The print stylesheet. A4, readable body text, code in a monospace font that
#: never hyphenates or wraps in the middle of a command (a command copied from
#: the PDF must come out whole), pictures no wider than the text column and
#: never cut by a page break.
PRINT_CSS = """
@page { size: A4; margin: 18mm 17mm 20mm 17mm; }
html { font-family: "Noto Sans", "DejaVu Sans", sans-serif; font-size: 10.5pt;
       line-height: 1.5; color: #1b1f27; hyphens: none; }
body { margin: 0; }
h1 { font-size: 26pt; margin: 0 0 4pt 0; line-height: 1.15; }
h2 { font-size: 17pt; margin: 22pt 0 6pt 0; padding-top: 6pt;
     border-top: 1.5pt solid #c9a13b; break-after: avoid; }
h3 { font-size: 12.5pt; margin: 14pt 0 4pt 0; break-after: avoid; }
h4 { font-size: 11pt; margin: 10pt 0 3pt 0; break-after: avoid; }
p { margin: 5pt 0; orphans: 3; widows: 3; }
ul, ol { margin: 5pt 0 5pt 0; padding-left: 20pt; }
li { margin: 2pt 0; }
a { color: #1d4f91; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace; font-size: 9.2pt;
       background: #eef0f4; padding: 0 2pt; border-radius: 2pt; hyphens: none; }
pre { font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace; font-size: 9.2pt;
      background: #eef0f4; border-left: 3pt solid #c9a13b; padding: 6pt 8pt;
      margin: 6pt 0; white-space: pre-wrap; overflow-wrap: anywhere;
      hyphens: none; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }
img { display: block; max-width: 100%; height: auto; margin: 8pt auto;
      border: 0.6pt solid #b8bec9; break-inside: avoid; }
p > img:only-child { margin: 8pt auto; }
/* A sentence that introduces a picture stays on the picture's page. */
p:has(+ p > img:only-child) { break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 7pt 0; font-size: 9.6pt; }
th, td { border: 0.6pt solid #b8bec9; padding: 3pt 6pt; text-align: left;
         vertical-align: top; }
th { background: #e6e9ef; }
tr { break-inside: avoid; }
blockquote { margin: 6pt 0; padding: 2pt 10pt; border-left: 3pt solid #b8bec9;
             color: #3a414d; }
hr { border: 0; border-top: 0.6pt solid #b8bec9; margin: 12pt 0; }
.stamp { color: #5b6472; font-size: 9pt; margin: 0 0 10pt 0; }
.toc { margin: 10pt 0 6pt 0; padding: 8pt 12pt; background: #f4f5f8;
       border: 0.6pt solid #d3d7df; break-inside: avoid; }
.toc-title { font-weight: bold; margin: 0 0 3pt 0; }
.toc ul { list-style: none; padding-left: 0; margin: 0; }
.toc ul ul { padding-left: 14pt; }
.toc li { margin: 1pt 0; }
"""


def markdown_to_html(source: Path, stamp: str = "", contents: bool = True) -> str:
    """The guide as one HTML page with the print stylesheet and a contents list.

    `contents=False` leaves the list out, for a short page such as the patch
    notes, where it would only repeat the dates.
    """
    try:
        import markdown
    except ImportError as exc:  # pragma: no cover - environment
        raise SystemExit("python-markdown is needed to build the guide "
                         "(pacman -S python-markdown)") from exc

    text = Path(source).read_text(encoding="utf-8")
    converter = markdown.Markdown(
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
    body = converter.convert(text)
    lost = list_items_lost(text, body)
    if lost:
        raise SystemExit(f"{source}: python-markdown lost the shape of a list near: "
                         f"{lost!r}. Indent what belongs to a list item by 4 spaces "
                         f"(8 for a code block) and put a blank line before the next "
                         f"item.")

    # The contents list goes under the opening paragraphs, before the first
    # chapter. It is built here rather than with a [TOC] marker in the source,
    # which the repository's own page would show as the literal text.
    first_chapter = body.find("<h2")
    if contents and first_chapter >= 0:
        toc = ('<div class="toc"><p class="toc-title">Contents</p>' + converter.toc
               + "</div>")
        body = body[:first_chapter] + toc + body[first_chapter:]
    if stamp:
        body = body.replace("</h1>", f'</h1><p class="stamp">{html.escape(stamp)}</p>', 1)

    # A link to another file of the archive (LICENSE, docs/SETUP.md) would point
    # at the folder the PDF was built in, which is nobody's folder: keep the
    # words, drop the link. Links to a chapter ("#...") and to the web stay.
    body = re.sub(r'<a href="(?![a-z][a-z0-9+.-]*:|#)[^"]*">(.*?)</a>', r"\1",
                  body, flags=re.S)

    title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    heading = html.escape(title.group(1) if title else "Guide")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{heading}</title><style>{PRINT_CSS}</style></head>"
            f"<body>{body}</body></html>")


_LIST_ITEM = re.compile(r"^( *)(?:[-*+]|\d+\.) +(\S.*)$")


def list_items_lost(text: str, body: str) -> str:
    """The first list item of `text` that `body` does not show at its depth, or "".

    python-markdown wants 4 spaces for anything nested in a list item and a
    blank line before an item that follows a paragraph in an item; the
    repository's page (CommonMark) takes less. Where they disagree the PDF
    quietly merges items into one paragraph or flattens a nested list, so the
    items of the source, by depth, are compared with the <li> of the HTML.
    """
    from html.parser import HTMLParser

    wanted: list[tuple[int, str]] = []
    open_indents: list[int] = []
    fenced = False
    previous_blank = True
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        item = _LIST_ITEM.match(line)
        if item:
            indent = len(item.group(1))
            while open_indents and indent <= open_indents[-1]:
                open_indents.pop()
            wanted.append((len(open_indents), item.group(2)))
            open_indents.append(indent)
        elif line.strip() and not line.startswith(" ") and previous_blank:
            open_indents = []
        previous_blank = not line.strip()

    class Items(HTMLParser):
        def __init__(self):
            super().__init__()
            self.lists = 0
            self.depths: list[int] = []

        def handle_starttag(self, tag, attrs):
            if tag in ("ul", "ol"):
                self.lists += 1
            elif tag == "li":
                self.depths.append(self.lists - 1)

        def handle_endtag(self, tag):
            if tag in ("ul", "ol"):
                self.lists -= 1

    parser = Items()
    parser.feed(body)
    for index, (depth, words) in enumerate(wanted):
        if index >= len(parser.depths) or parser.depths[index] != depth:
            return words[:60]
    if len(parser.depths) > len(wanted):
        return "(the HTML has more list items than the source)"
    return ""


def missing_pictures(page: str, folder: Path) -> list[str]:
    """The pictures the HTML names that are not there (relative to `folder`)."""
    missing = []
    for source in re.findall(r'<img[^>]+src="([^"]+)"', page):
        if "://" in source or source.startswith("data:"):
            continue
        if not (folder / source).is_file():
            missing.append(source)
    return missing


def print_with_webengine(page_file: Path, out: Path) -> None:
    """Print an HTML file to `out` with QtWebEngine, offscreen. Raises on failure."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # No GPU and no sandbox in a build: the page is only laid out and printed,
    # and the sandbox needs privileges that a plain terminal may not have.
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")
    from PySide6.QtCore import QMarginsF, QTimer, QUrl
    from PySide6.QtGui import QPageLayout, QPageSize
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    page = QWebEnginePage()
    outcome: dict = {}

    layout = QPageLayout(QPageSize(QPageSize.A4), QPageLayout.Portrait,
                         QMarginsF(17, 18, 17, 20), QPageLayout.Millimeter)

    def loaded(ok: bool) -> None:
        if not ok:
            outcome["error"] = "the page did not load"
            app.quit()
            return
        # One more turn of the loop: images decode after the load signal.
        QTimer.singleShot(500, lambda: page.printToPdf(str(out), layout))

    def printed(path: str, ok: bool) -> None:
        outcome["ok"] = ok
        app.quit()

    page.loadFinished.connect(loaded)
    page.pdfPrintingFinished.connect(printed)
    QTimer.singleShot(WEBENGINE_TIMEOUT_S * 1000, lambda: (
        outcome.setdefault("error", "timed out"), app.quit()))
    page.load(QUrl.fromLocalFile(str(page_file)))
    app.exec()
    page.deleteLater()
    if not outcome.get("ok"):
        raise RuntimeError(outcome.get("error") or "printToPdf reported failure")
    if not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError("printToPdf wrote no file")


def print_with_textdoc(page: str, base: Path, out: Path) -> None:
    """Print the HTML with QTextDocument and QPdfWriter. Raises on failure."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QMarginsF, QUrl
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    writer = QPdfWriter(str(out))
    writer.setPageSize(QPageSize(QPageSize.A4))
    writer.setPageMargins(QMarginsF(17, 18, 17, 20), QPageLayout.Millimeter)
    writer.setResolution(120)
    document = QTextDocument()
    document.setBaseUrl(QUrl.fromLocalFile(str(base) + "/"))
    document.setDefaultStyleSheet(PRINT_CSS)
    # QTextDocument takes a subset of HTML: leave out the page wrapper and
    # keep the body.
    document.setHtml(re.search(r"<body>(.*)</body>", page, re.S).group(1))
    text_size = writer.pageLayout().paintRectPixels(writer.resolution()).size()
    document.setPageSize(text_size)
    _fit_pictures(document, text_size.width())
    document.print_(writer)
    del app


def _fit_pictures(document, width: int) -> None:
    """Shrink pictures wider than the text column; QTextDocument has no max-width."""
    from PySide6.QtGui import QTextCursor

    cursor = QTextCursor(document)
    block = document.begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            fmt = fragment.charFormat()
            if fmt.isImageFormat():
                image = fmt.toImageFormat()
                if image.width() == 0 or image.width() > width:
                    image.setWidth(width * 0.98)
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(fragment.position() + fragment.length(),
                                       QTextCursor.KeepAnchor)
                    cursor.setCharFormat(image)
            iterator += 1
        block = block.next()


def build(source: Path, out: Path, stamp: str = "", engine: str = "auto",
          contents: bool = True) -> str:
    """Make `out` from `source`. Returns the engine that printed it."""
    source = Path(source).resolve()
    out = Path(out).resolve()
    page = markdown_to_html(source, stamp, contents)
    missing = missing_pictures(page, source.parent)
    if missing:
        raise SystemExit(f"{source.name} names pictures that are not there: "
                         + ", ".join(missing))
    if out.exists():
        out.unlink()
    out.parent.mkdir(parents=True, exist_ok=True)

    failures = []
    if engine in ("auto", "webengine"):
        # The page file sits next to the source so that relative picture links
        # resolve; it is removed afterwards.
        handle, name = tempfile.mkstemp(prefix=".guide-", suffix=".html",
                                        dir=str(source.parent))
        page_file = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(page)
            print_with_webengine(page_file, out)
            return "webengine"
        except Exception as exc:  # noqa: BLE001 - any failure means: try the other way
            failures.append(f"webengine: {exc}")
            if engine == "webengine":
                raise SystemExit("; ".join(failures))
        finally:
            page_file.unlink(missing_ok=True)
    try:
        print_with_textdoc(page, source.parent, out)
        if failures:
            print("note: " + "; ".join(failures) + " - used textdoc", file=sys.stderr)
        return "textdoc"
    except Exception as exc:  # noqa: BLE001
        failures.append(f"textdoc: {exc}")
        raise SystemExit("could not print the guide: " + "; ".join(failures))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=ROOT / "docs" / "GUIDE.md")
    parser.add_argument("--out", type=Path, default=ROOT / "GUIDE.pdf")
    parser.add_argument("--stamp", default="",
                        help="a line under the title, e.g. the release it belongs to")
    parser.add_argument("--engine", choices=("auto", "webengine", "textdoc"),
                        default="auto")
    parser.add_argument("--no-contents", action="store_true",
                        help="no contents list (for a short page such as the patch notes)")
    args = parser.parse_args(argv)
    used = build(args.source, args.out, args.stamp, args.engine,
                 contents=not args.no_contents)
    print(f"{args.out} ({args.out.stat().st_size / 1024:.0f} KiB) made with {used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
