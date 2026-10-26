"""Load the repo-root ``.env`` into the process environment for the CLI entry points.

Only the tools' ``__main__`` blocks call this, never an import, so the tests never pick up a real credential.
A variable already set in the environment always wins, empty values are skipped, and only names are reported:
values are never printed or stored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import MutableMapping

DEFAULT_PATH = Path(__file__).resolve().parents[2] / ".env"


def parse(text: str) -> list[tuple[str, str]]:
    """``KEY=VALUE`` pairs in file order; blank lines, ``#`` comments and lines without a name are skipped."""
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        pairs.append((name, value))
    return pairs


def load_env_file(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Set each non-empty variable from ``path`` (default: the repo-root ``.env``) that ``environ`` lacks.
    Returns the names set; a missing file sets nothing."""
    path = DEFAULT_PATH if path is None else path
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return []
    loaded = []
    for name, value in parse(path.read_text(encoding="utf-8-sig")):
        if value and not environ.get(name):
            environ[name] = value
            loaded.append(name)
    return loaded


def load_for_cli() -> None:
    """The entry points' call: load the default ``.env`` and say which names came from it (on stderr)."""
    loaded = load_env_file()
    if loaded:
        print(f"loaded {', '.join(loaded)} from {DEFAULT_PATH}", file=sys.stderr)
