"""Artifact storage: what the agent writes, and what the phone reads.

An artifact is one self-contained HTML file under `artifacts/<slug>/index.html`.
Self-contained is the whole design: no bundler, no CDN, no build step, no
third party. The agent inlines its CSS, its SVG and its data, and the result
is a web page -- which is also, not coincidentally, the best thing to open on
a phone.

Two rules are enforced here rather than trusted to the model:

1. **Slugs are validated, not sanitised.** A slug is `[a-z0-9-]{1,64}`, full
   stop. Anything else is refused. Sanitising a bad path (stripping `..`,
   rewriting separators) is how directory traversal bugs are written; a flat
   refusal cannot be smuggled past.

2. **The queue is data, not control.** A follow-up typed on the phone lands
   in a SQLite row. Nothing in this module executes it. Whatever drives the
   agent decides if and when to pick it up.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_requests (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    slug     TEXT NOT NULL,
    prompt   TEXT NOT NULL,
    at       REAL NOT NULL,
    taken_at REAL
);
"""


class BadSlug(ValueError):
    """Raised for a slug that is not plainly safe as a directory name."""


@dataclass(frozen=True)
class Artifact:
    slug: str
    path: Path
    title: str
    modified: float

    @property
    def html(self) -> str:
        return self.path.read_text(encoding="utf-8")


def check_slug(slug: str) -> str:
    if not SLUG_PATTERN.match(slug or ""):
        raise BadSlug(
            f"'{slug}' is not a valid artifact name. "
            "Use lowercase letters, digits and hyphens only."
        )
    return slug


def _title_of(html: str, fallback: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return match.group(1).strip()[:120] if match else fallback


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.root / "queue.db", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(QUEUE_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- artifacts --------------------------------------------------------
    def path_for(self, slug: str) -> Path:
        check_slug(slug)
        return self.root / slug / "index.html"

    def write(self, slug: str, html: str) -> Artifact:
        path = self.path_for(slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        return self.get(slug)

    def get(self, slug: str) -> Artifact | None:
        path = self.path_for(slug)
        if not path.is_file():
            return None
        html = path.read_text(encoding="utf-8")
        return Artifact(slug, path, _title_of(html, slug), path.stat().st_mtime)

    def list(self) -> list[Artifact]:
        found = []
        for child in self.root.iterdir():
            if not child.is_dir() or not SLUG_PATTERN.match(child.name):
                continue
            artifact = self.get(child.name)
            if artifact is not None:
                found.append(artifact)
        return sorted(found, key=lambda a: a.modified, reverse=True)

    # -- follow-up requests from the phone ---------------------------------
    def enqueue(self, slug: str, prompt: str) -> int:
        check_slug(slug)
        cursor = self.conn.execute(
            "INSERT INTO artifact_requests (slug, prompt, at) VALUES (?,?,?)",
            (slug, prompt.strip(), time.time()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def pending(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM artifact_requests WHERE taken_at IS NULL "
            "ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()

    def take(self, request_id: int) -> sqlite3.Row | None:
        """Claim one request. Returns None if it was already claimed.

        The UPDATE...WHERE taken_at IS NULL is the claim: two consumers
        racing cannot both win, because SQLite serialises the write.
        """
        cursor = self.conn.execute(
            "UPDATE artifact_requests SET taken_at=? "
            "WHERE id=? AND taken_at IS NULL",
            (time.time(), request_id),
        )
        self.conn.commit()
        if cursor.rowcount == 0:
            return None
        return self.conn.execute(
            "SELECT * FROM artifact_requests WHERE id=?", (request_id,)
        ).fetchone()
