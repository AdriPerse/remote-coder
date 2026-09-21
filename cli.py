"""Local harness: drive the agent from this terminal, no Telegram needed.

Exists so the agent can be developed and proven before a bot token does,
and so there is always a way in when the transport is broken.

    python cli.py <workdir> ["<task>"] [--auto] [--thread NAME]

With no task, drops into an interactive session: the scorpion banner,
then a prompt per task until /exit.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from rcoder.agent import build_agent, iter_node_updates, open_checkpointer
from rcoder.artifacts import ArtifactStore
from rcoder.config import ConfigurationError, load_settings


# Placeholder until the winner of artifacts/scorpion-picks is chosen.
BANNER = """
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m▄[0m[38;5;60m▄[0m[38;5;60m▄[0m[38;5;60m▄[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m█[0m[38;5;60m█[0m[0m [0m [38;5;60m█[0m[38;5;60m█[0m[0m [38;5;60m▄[0m[38;5;60m▄[0m[38;5;60m▄[0m[38;5;60m▄[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m█[0m[38;5;60m▄[0m[38;5;60m█[0m[38;5;60m█[0m[0m [0m [38;5;60m█[0m[38;5;60m█[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m▄[0m[0m [38;5;60m█[0m[38;5;60m█[0m[38;5;60m▀[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m█[0m[38;5;60m█[0m[0m [0m [38;5;60m█[0m[38;5;60m█[0m[0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▀[0m[38;5;66m▀[0m[38;5;66m▄[0m[38;5;66m▄[0m[0m [0m [0m [0m [38;5;60m▄[0m[38;5;60m▄[0m[38;5;103m▄[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m[48;5;103m▀[0m[38;5;60m█[0m[38;5;60m▄[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [38;5;66m▄[0m[38;5;66m▀[0m[0m [38;5;120m▄[0m[38;5;120m▄[0m[38;5;120m▄[0m[38;5;120m▄[0m[0m [38;5;66m▀[0m[38;5;66m▄[0m[0m [38;5;60m█[0m[38;5;60m[48;5;103m▀[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;60m[48;5;103m▀[0m[38;5;60m█[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [38;5;66m▀[0m[38;5;66m▄[0m[0m [38;5;120m█[0m[38;5;120m█[0m[38;5;120m█[0m[38;5;120m█[0m[0m [38;5;66m▄[0m[38;5;66m▀[0m[38;5;60m▀[0m[38;5;60m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [0m [0m [0m [38;5;66m▀[0m[38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▀[0m[0m [0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m▀[0m[38;5;103m[48;5;60m▀[0m[38;5;103m[48;5;60m▀[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m█[0m[38;5;103m[48;5;60m▀[0m[38;5;103m[48;5;60m▀[0m[38;5;103m▀[0m[38;5;103m[48;5;60m▀[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m
[0m [38;5;66m▄[0m[38;5;66m█[0m[38;5;66m▀[0m[0m [38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▄[0m[0m [0m [38;5;60m▄[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [38;5;60m▀[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [38;5;60m▀[0m[38;5;60m█[0m[38;5;60m▄[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m
[38;5;66m█[0m[38;5;66m█[0m[0m [0m [0m [38;5;66m▄[0m[38;5;66m█[0m[38;5;66m█[0m[38;5;66m▀[0m[0m [38;5;60m▀[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[0m [0m [0m [0m [0m [0m [38;5;60m▀[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m
[0m [38;5;66m▀[0m[38;5;66m▄[0m[38;5;66m▄[0m[38;5;66m▀[0m[38;5;66m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [38;5;60m▀[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m█[0m[38;5;60m▀[0m[0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m [0m

      R E M O T E   C O D E R
"""

INTERACTIVE_HELP = """Type a task, Enter to run it.
  /new     fresh session (new thread)
  /auto    toggle approval prompts
  /exit    quit (Ctrl+C works too)
"""


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
        for _node, payload in iter_node_updates(update):
            for msg in payload.get("messages") or []:
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
    ap.add_argument("task", nargs="?", default=None,
                    help="omit for an interactive session")
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

    # State the REPL can mutate: a /new mints a fresh thread and agent,
    # /auto rebuilds the agent with different gates. One-shot mode never
    # touches these after the first build.
    state = {"thread": args.thread, "auto": args.auto, "agent": None}

    def rebuild() -> None:
        state["agent"] = build_agent(
            settings,
            Path(args.workdir),
            autonomous=state["auto"],
            checkpointer=open_checkpointer(settings),
            artifact_store=store,
            artifact_base_url=args.artifact_url,
            session_id=state["thread"],
        )

    def run(task: str) -> None:
        print("[{}] {} :: {}".format(settings.model, args.workdir, task))
        print("-" * 60)
        config = {
            "configurable": {"thread_id": state["thread"]},
            "recursion_limit": 100,
        }
        for line in stream(state["agent"], task, config):
            print(line, flush=True)

    rebuild()
    if args.task is not None:
        run(args.task)
        return 0

    print(BANNER)
    print("[{}] workdir={} thread={} auto={}".format(
        settings.model, args.workdir, state["thread"],
        "on" if state["auto"] else "off"))
    print(INTERACTIVE_HELP)
    while True:
        try:
            task = input("{}> ".format(settings.model)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            continue
        if task in ("/exit", "/quit"):
            return 0
        if task == "/new":
            state["thread"] = "cli-" + uuid.uuid4().hex[:8]
            rebuild()
            print("fresh session: {}".format(state["thread"]))
            continue
        if task == "/auto":
            state["auto"] = not state["auto"]
            rebuild()
            print("approvals {}.".format(
                "OFF - running autonomously" if state["auto"] else "on"))
            continue
        try:
            run(task)
        except KeyboardInterrupt:
            print("\n(interrupted - session kept, type another task or /exit)")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
