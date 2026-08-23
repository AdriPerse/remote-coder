"""Consumes follow-up requests typed on the artifact page.

This is the half of the loop that makes an artifact interactive rather than
just viewable: you open the page on your phone, type "make the bars
horizontal", and this process picks it up, runs the agent in the SESSION AND
REPOSITORY that produced the artifact, and the agent rewrites the file. The
server's file watcher then pushes a reload to the open page.

    python watch.py --artifacts ./artifacts

Deliberate choices:

- **Requests run one at a time.** Two agents editing the same repository
  concurrently is a corruption bug, and follow-ups on a drawing are naturally
  serial anyway.
- **A claimed request is never retried.** `take()` marks it before the agent
  runs, so a request that crashes the agent is dropped rather than replayed
  into a crash loop. A silent retry loop burning tokens unattended, while the
  owner is away, is the worse failure.
- **An unroutable request is reported, not guessed.** If the artifact has no
  recorded origin (written by an older build, or by hand), the request is
  claimed and logged rather than run against some default directory.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from rcoder.agent import build_agent, open_checkpointer
from rcoder.artifacts import ArtifactStore
from rcoder.config import ConfigurationError, load_settings

logger = logging.getLogger("rcoder.watch")

FOLLOW_UP_TEMPLATE = """\
The user is looking at the artifact '{slug}' on their phone and asked for \
this change:

{prompt}

Update that artifact with `write_artifact` using the SAME slug ('{slug}') so \
their open page reloads with the change. Keep everything they did not ask you \
to change. Reply in one short sentence saying what you changed."""


def handle(settings, store: ArtifactStore, request, base_url: str, autonomous: bool) -> None:
    slug = request["slug"]
    origin = store.origin(slug)
    if origin is None:
        logger.warning(
            "Request %d for '%s' has no recorded origin; not run. "
            "Ask for this change from the session that created it.",
            request["id"], slug,
        )
        return

    thread_id, workdir = origin
    if not workdir.is_dir():
        logger.warning(
            "Request %d for '%s': recorded repo %s no longer exists; not run.",
            request["id"], slug, workdir,
        )
        return

    logger.info("Request %d on '%s' [%s]: %s",
                request["id"], slug, workdir.name, request["prompt"][:80])
    agent = build_agent(
        settings, workdir,
        autonomous=autonomous,
        checkpointer=open_checkpointer(settings),
        artifact_store=store,
        artifact_base_url=base_url,
        session_id=thread_id,
    )
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    prompt = FOLLOW_UP_TEMPLATE.format(slug=slug, prompt=request["prompt"])

    final = ""
    for _ns, update in agent.stream(
        {"messages": [{"role": "user", "content": prompt}]},
        config=config, stream_mode="updates", subgraphs=True,
    ):
        for _node, data in (update or {}).items():
            for msg in (data or {}).get("messages", []) or []:
                if msg.__class__.__name__ == "AIMessage" and isinstance(msg.content, str):
                    if msg.content.strip():
                        final = msg.content.strip()
                for call in getattr(msg, "tool_calls", []) or []:
                    logger.info("  -> %s", call["name"])
    logger.info("  done: %s", final[:200] or "(no summary)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default="artifacts", help="artifact directory")
    ap.add_argument("--artifact-url", default="http://127.0.0.1:8001")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds")
    ap.add_argument("--once", action="store_true",
                    help="drain what is pending and exit (used by the tests)")
    ap.add_argument("--ask", action="store_true",
                    help="keep approval gates on (default: autonomous, since "
                         "nobody is watching a terminal)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = load_settings(require_telegram=False)
    except ConfigurationError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        return 2

    store = ArtifactStore(Path(args.artifacts).resolve())
    autonomous = not args.ask
    logger.info("Watching %s (approvals %s)",
                store.root, "ON" if args.ask else "OFF")
    try:
        while True:
            for row in store.pending():
                claimed = store.take(row["id"])
                if claimed is None:
                    continue          # another consumer won the race
                try:
                    handle(settings, store, claimed, args.artifact_url, autonomous)
                except Exception:  # noqa: BLE001 - one bad request must not stop the loop
                    logger.exception("Request %d failed", claimed["id"])
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
