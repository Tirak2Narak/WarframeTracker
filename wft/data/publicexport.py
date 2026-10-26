"""Downloading Digital Extremes' Public Export.

The index lists one manifest per line as "filename!hash". The hash changes
whenever the game data changes, so comparing it against the last known hash is
how we skip unchanged files instead of re-downloading everything.

Two details cost the previous attempt some grief and are handled explicitly:

* The index uses LZMA_ALONE framing, not the .xz container, so it needs an
  explicit ``FORMAT_ALONE`` decompressor.
* Its lines end in CRLF. Leaving the ``\\r`` attached puts a stray carriage
  return into the manifest URL and into the stored hash.
"""
from __future__ import annotations

import json
import lzma
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from wft.config import Config

# The shared agent string (see wft.USER_AGENT); kept under this name for the
# modules that import it from here.
from wft import USER_AGENT  # noqa: E402,F401
TIMEOUT = 60


@dataclass
class ManifestRef:
    """One entry of the index: which file, and at which version."""

    filename: str
    hash: str

    @property
    def url_suffix(self) -> str:
        return f"{self.filename}!{self.hash}"


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def fetch_index(cfg: Config) -> dict[str, ManifestRef]:
    """Download and parse the index into {filename: ManifestRef}."""
    raw = _fetch(str(cfg.get("sources.index_url")))
    text = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(raw).decode("utf-8")

    refs: dict[str, ManifestRef] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or "!" not in line:
            continue
        filename, _, file_hash = line.partition("!")
        refs[filename.strip()] = ManifestRef(filename.strip(), file_hash.strip())
    return refs


def load_hashes(cfg: Config) -> dict[str, str]:
    path = cfg.manifest_dir / "index-hashes.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A corrupt hash cache must not block an update; treat it as empty and
        # re-download. Losing the cache costs bandwidth, not correctness.
        return {}


def save_hashes(cfg: Config, hashes: dict[str, str]) -> None:
    (cfg.manifest_dir / "index-hashes.json").write_text(json.dumps(hashes, indent=2, sort_keys=True))


def outdated(cfg: Config, index: dict[str, ManifestRef]) -> list[ManifestRef]:
    """Which of the manifests we care about changed since the last download."""
    known = load_hashes(cfg)
    wanted: Iterable[str] = cfg.get("sources.manifests", [])
    pending = []
    for filename in wanted:
        ref = index.get(filename)
        if ref is None:
            continue
        target = cfg.manifest_dir / filename
        if known.get(filename) != ref.hash or not target.is_file():
            pending.append(ref)
    return pending


def download(cfg: Config, refs: list[ManifestRef],
             progress: Callable[[str, int, int], None] | None = None) -> list[Path]:
    """Fetch the given manifests and record their hashes.

    Hashes are written per file rather than in one batch at the end, so an
    interrupted update leaves a consistent record of what actually arrived.
    """
    hashes = load_hashes(cfg)
    written: list[Path] = []
    for position, ref in enumerate(refs, start=1):
        if progress:
            progress(ref.filename, position, len(refs))
        payload = _fetch(str(cfg.get("sources.manifest_base")) + ref.url_suffix)

        # Some manifests carry stray control characters; the game's own parser
        # tolerates them, Python's json does not.
        text = payload.decode("utf-8", errors="replace")
        text = "".join(ch for ch in text if ch >= " " or ch in "\t\n\r")

        target = cfg.manifest_dir / ref.filename
        target.write_text(text)
        written.append(target)

        hashes[ref.filename] = ref.hash
        save_hashes(cfg, hashes)
    return written


def read_manifest(cfg: Config, filename: str, key: str | None = None) -> list[dict]:
    """Load one downloaded manifest and unwrap it.

    Manifests are not uniformly shaped. Most hold a single top-level key, but
    some hold two: `ExportWeapons_en.json` carries `ExportWeapons` **and**
    `ExportRailjackWeapons`, `ExportWarframes_en.json` carries `ExportWarframes`
    **and** `ExportAbilities`.

    An earlier version unwrapped only single-key documents and returned an empty
    list otherwise. Weapons and Warframes were therefore never read from the
    manifests at all — everything came from the recipes, silently and without an
    error, which is why weapons built from raw materials were missing entirely.

    Without `key`, the primary list is the one named after the file
    (`ExportWeapons_en.json` -> `ExportWeapons`), falling back to the longest
    list present.
    """
    path = cfg.manifest_dir / filename
    if not path.is_file():
        return []

    document = json.loads(path.read_text())
    if isinstance(document, list):
        return [entry for entry in document if isinstance(entry, dict)]
    if not isinstance(document, dict):
        return []

    if key is None:
        key = filename.split("_")[0].removesuffix(".json")

    payload = document.get(key)
    if not isinstance(payload, list):
        lists = [value for value in document.values() if isinstance(value, list)]
        payload = max(lists, key=len) if lists else []

    return [entry for entry in payload if isinstance(entry, dict)]


def manifest_sections(cfg: Config, filename: str) -> dict[str, list[dict]]:
    """Every list a manifest holds, keyed by its name.

    For the cases where the secondary list matters too — Railjack weapons are
    real weapons the user can level.
    """
    path = cfg.manifest_dir / filename
    if not path.is_file():
        return {}
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        return {}
    return {name: [e for e in value if isinstance(e, dict)]
            for name, value in document.items() if isinstance(value, list)}
