"""Local harness: drive the agent from this terminal, no Telegram needed.

Exists so the agent can be developed and proven before a bot token does,
and so there is always a way in when the transport is broken.

    python cli.py <workdir> "<task>" [--auto] [--thread NAME]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rcoder.agent import build_agent, open_checkpointer
from rcoder.artifacts import ArtifactStore
from rcoder.config import ConfigurationError, load_settings


def render(msg):
    """One or more human-readable lines for a single agent message."""
    kind = msg.__class__.__name__
    if kind == "AIMessage":
        for call in getattr(msg, "tool_calls", []) or []:
            args = call.get("args", {})
            detail = args.get("command") or args.get("file_path") or args.get("path") or ""
            yield "  -> {}({})".format(call["name"], str(detail)[:90])
        text = (msg.content or "").strip() if isinstance(msg.content, str) else ""
        if text:
            yield "  ai: {}".format(text[:400])
    elif kind == "ToolMessage":
        body = str(msg.content or "").strip().replace("\n", " ")
        status = "ERR " if getattr(msg, "status", "") == "error" else ""
        yield "     <- {}{}".format(status, body[:160])


def stream(agent, task, config):
    """Yield one readable line per agent step."""
    for ns, update in agent.stream(
        {"messages": [{"role": "user", "content": task}]},
        config=config,
        stream_mode="updates",
        subgraphs=True,
    ):
        prefix = "[{}] ".format(ns[0].split(":")[0]) if ns else ""
        for _node, payload in (update or {}).items():
            for msg in (payload or {}).get("messages", []) or []:
                for line in render(msg):
                    yield prefix + line


def main() -> int:
    # A Windows console defaults to cp1252 and dies on the first emoji the
    # model emits. Losing a whole run to a print() is absurd.
    for stream_obj in (sys.stdout, sys.stderr):
        try:
            stream_obj.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("task")
    ap.add_argument("--auto", action="store_true", help="run without approval gates")
    ap.add_argument("--thread", default="cli", help="session id to resume")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--artifacts", metavar="DIR", default=None,
                    help="enable write_artifact, publishing into DIR")
    ap.add_argument("--artifact-url", default="http://127.0.0.1:8001",
                    help="public base URL of the artifact server")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        settings = load_settings(require_telegram=False)
    except ConfigurationError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        return 2

    store = ArtifactStore(Path(args.artifacts).resolve()) if args.artifacts else None
    agent = build_agent(
        settings,
        Path(args.workdir),
        autonomous=args.auto,
        checkpointer=open_checkpointer(settings),
        artifact_store=store,
        artifact_base_url=args.artifact_url,
        session_id=args.thread,
    )
    config = {"configurable": {"thread_id": args.thread}, "recursion_limit": 100}

    print("[{}] {} :: {}".format(settings.model, args.workdir, args.task))
    print("-" * 60)
    for line in stream(agent, args.task, config):
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
