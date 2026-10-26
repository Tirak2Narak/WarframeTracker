"""Configuration loading.

Every path, URL and threshold comes from config.toml — nothing is hardcoded,
so the tool survives a moved Steam library, a different Warframe install or a
changed upstream URL without a code change.

The packaged `config.example.toml` is the base and the user's file is laid over
it, so a user file only has to hold what differs. Search order for that file
mirrors the XDG conventions.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

APP_NAME = "warframe-tracker"
DB_NAME = "warframe.sqlite"

def _search_paths() -> list[Path]:
    """Where a user's config may be, worked out at every load.

    At the call, not at import: the tests point XDG_CONFIG_HOME at a scratch
    folder after this module is imported, and a list fixed at import kept
    reading a real player's config - whose calibrated regions hid that the
    shipped ones read nothing. And ~/.config only when XDG_CONFIG_HOME
    is unset, as the XDG base directory spec has it; looking there as well
    made a redirected XDG_CONFIG_HOME no redirect at all.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return [base / APP_NAME / "config.toml",
            Path(__file__).resolve().parent.parent / "config.toml"]


_EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


class Config:
    """Parsed configuration with convenience accessors.

    Values are reached by dotted path (`cfg.get("sources.index_url")`) so
    callers do not have to know how the TOML is nested.
    """

    def __init__(self, data: dict[str, Any], source: Path):
        self._data = data
        self.source = source

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def override(self, dotted: str, value: Any) -> None:
        """Replace one value in memory, without touching the file on disk.

        For running against something other than the configured data directory
        - the self-test needs a throwaway copy of the database, and pointing a
        whole application at one is a single value.
        """
        node = self._data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value

    def remove(self, dotted: str) -> None:
        """Drop one value from memory, so `get` no longer finds it.

        The other half of `override`; the file on disk is not touched. A key
        that is not there is not an error.
        """
        node = self._data
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(parts[-1], None)

    def path(self, dotted: str, default: str = "") -> Path:
        """A config value that names a filesystem path, with ~ expanded."""
        return Path(os.path.expanduser(str(self.get(dotted, default)))).resolve()

    def data_dir_path(self) -> Path:
        """The data directory without creating it, for read-only callers."""
        configured = str(self.get("paths.data_dir", "") or "").strip()
        if configured:
            return Path(os.path.expanduser(configured))
        return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_NAME

    @property
    def data_dir(self) -> Path:
        """Where manifests and the database live.

        Deliberately outside the repository: this is machine-specific state and
        contains the user's own inventory.
        """
        base = self.data_dir_path()
        base.mkdir(parents=True, exist_ok=True)
        return base

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    @property
    def droptables_path(self) -> Path:
        """DE's drop-table page as last accepted by the vault refresh.

        Kept where other tools can read the same page, so that one download
        serves all of them rather than each keeping a copy of its own age.
        """
        return self.data_dir / "droptables.html"

    @property
    def manifest_dir(self) -> Path:
        d = self.data_dir / "manifests"
        d.mkdir(parents=True, exist_ok=True)
        return d


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """`over` laid on `base`: tables merge key by key, everything else replaces.

    Lists replace as a whole on purpose. `sources.manifests` is the one that
    matters: merging it would make it impossible to drop an entry.
    """
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> Config:
    """The example config with the first existing user file laid over it.

    The example holds working defaults, not placeholders, so an empty or
    missing user file is a valid setup. Merging rather than taking the user
    file whole means a key added in a later version reaches existing installs
    instead of leaving its feature silently off. `Config.source` names the
    user file when there is one, otherwise the example.
    """
    with open(_EXAMPLE, "rb") as handle:
        data = tomllib.load(handle)
    for candidate in _search_paths():
        if candidate.is_file():
            with open(candidate, "rb") as handle:
                return Config(_merge(data, tomllib.load(handle)), candidate)
    return Config(data, _EXAMPLE)
