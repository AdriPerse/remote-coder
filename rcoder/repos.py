"""Finding the repositories worth offering as a tap.

Typing `C:\\Projects\\MusicLore` on a phone keyboard is the kind of friction
that stops a tool being used. The bot knows where repositories live, so it
should offer them.

Discovery is deliberately shallow and boring: look for a `.git` directory
one and two levels under a few roots. No recursive walk of the whole disk --
that is slow, and a list of ninety directories is not a tap-to-pick list.
"""

from __future__ import annotations

import os
from pathlib import Path

# Scanned in order; missing roots are skipped silently. Personal layouts
# belong in RC_REPO_ROOTS (semicolon-separated), not in this default.
DEFAULT_ROOTS = (str(Path.home() / "Projects"), str(Path.home() / "Documents"))
MAX_CHOICES = 8


def _roots() -> list[Path]:
    raw = os.environ.get("RC_REPO_ROOTS", "").strip()
    names = [p.strip() for p in raw.split(";") if p.strip()] if raw else DEFAULT_ROOTS
    return [Path(n) for n in names]


def discover(extra: list[Path] | None = None) -> list[Path]:
    """Git repositories under the configured roots, plus `extra`.

    `extra` is for directories already known to be interesting -- the
    working directories of previous sessions -- so a repo outside the
    scanned roots stays one tap away once it has been used.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            resolved = path.resolve()
        except OSError:
            return
        key = str(resolved).lower()
        if key in seen or not resolved.is_dir():
            return
        seen.add(key)
        found.append(resolved)

    for path in extra or []:
        if (path / ".git").exists():
            add(path)

    for root in _roots():
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / ".git").exists():
                add(child)
                continue
            # One level deeper, for a grouping folder like C:\Projects\work\
            try:
                grandchildren = sorted(child.iterdir())
            except OSError:
                continue
            for grandchild in grandchildren:
                if grandchild.is_dir() and (grandchild / ".git").exists():
                    add(grandchild)

    return found[:MAX_CHOICES]
