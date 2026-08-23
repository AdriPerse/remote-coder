"""The agent itself: deepagents (LangGraph) driven by Kimi over Moonshot.

Three things here were learned by testing against the live API, not from
documentation, and each one is the difference between working and a
confusing failure:

1. `use_responses_api=False` is REQUIRED. langchain-openai now defaults to
   OpenAI's Responses API, which Moonshot does not implement; the failure is
   an opaque HTTP 500 non-streaming, and a ToolMessage validation error when
   streaming.
2. `temperature` must be exactly 1 on kimi-k3 and kimi-k2.7-code. Anything
   else is rejected outright: "invalid temperature: only 1 is allowed for
   this model". LangChain's own default of 0.7 therefore fails closed.
3. The shell backend does NOT inherit the environment by default, so `git`
   and `python` do not resolve. We inherit it -- and scrub the secrets out,
   because otherwise the first thing the agent can do is read its own API
   key and the bot token out of its own environment.

Reasoning depth is the cost dial. K3 always thinks, reasoning tokens bill as
output at $15/M, and `reasoning_effort` is the only way to spend less without
changing model.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver

from rcoder.artifacts import ArtifactStore, BadSlug
from rcoder.config import Settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a coding agent working directly on the user's machine, driven from \
their phone over a chat app. Act like a careful colleague with commit rights.

How to work:
- Read before you write. Understand the surrounding code and match its \
style, naming and idiom rather than importing your own conventions.
- Use the shell for real verification: run the tests, run the linter, run \
the thing. A change you have not executed is a guess.
- Work in small, checkable steps and keep the todo list current, so the \
user watching on a phone can see where you are.
- Prefer the smallest change that actually fixes the problem.

How to report:
- The user is reading you on a phone screen, often one-handed. Lead with the \
outcome. Keep it short. No walls of code unless asked.
- If tests fail, say so plainly and show the failing output. Never report \
success you have not verified.
- If you are blocked or the request is ambiguous in a way that changes what \
you would build, say so and ask -- do not guess and carry on.

Showing your work:
- When the answer is better SEEN than described -- a chart, a diagram, a
table, a mock-up, a rendered document -- use `write_artifact` and give the
user the link, instead of describing the picture in words. They are on a
phone; a rendered page beats a paragraph about one.
- Artifacts have NO network access by design. Inline everything: CSS,
JavaScript, data, and charts as inline SVG or canvas. Anything fetched from
the internet will silently fail.

Editing files:
- Never issue two `edit_file` calls for the SAME file in one turn. They are applied in parallel and corrupt each other -- this is a known upstream bug, not a limitation you can be clever about. Make multiple changes to one file either as a single `edit_file` with enough surrounding context, one call per turn, or one `write_file` with the whole corrected file.
- After any edit that a test did not immediately exercise, `read_file` it back before moving on.

Paths -- read this once, carefully:
- The file tools (`read_file`, `write_file`, `edit_file`, `ls`, `glob`, `grep`) see the repository as a virtual filesystem whose root `/` IS the repository root. `/src/main.py` means `<repo>/src/main.py`. Never pass a host path like `C:\\...` or `/tmp/...` to a file tool -- it will not be found.
- The `execute` shell tool runs with its working directory ALREADY set to the repository root, and prints real host paths. Use relative paths there (`python test_stats.py`, `./src/main.py`).
- So: file tools take `/foo.py`, the shell takes `foo.py`. Both mean the same file. This repository is at `{workdir}` on the host.

Safety:
- Destructive actions (deleting files, force-pushing, rewriting history) \
need explicit approval; the harness will prompt for it. Do not attempt to \
work around an approval prompt.
- Never print secrets, API keys or tokens into the conversation.
"""


def _scrubbed_env(settings: Settings) -> dict[str, str]:
    """The real environment minus anything secret.

    The agent needs PATH to run git and python. It does not need the key
    that pays for it.
    """
    env = {k: v for k, v in os.environ.items() if k not in set(settings.scrub_env)}
    removed = sorted(set(os.environ) & set(settings.scrub_env))
    if removed:
        logger.info("Scrubbed %d secret(s) from agent shell env: %s",
                    len(removed), ", ".join(removed))
    return env


def make_artifact_tool(
    store: ArtifactStore, base_url: str, thread_id: str, workdir: Path
):
    """A `write_artifact` tool bound to one store.

    Built as a closure rather than a module-level function because the tool
    needs the store and the public URL, and threading those through the model
    as arguments would let the model choose where files land.
    """
    from langchain_core.tools import tool

    @tool
    def write_artifact(slug: str, html: str) -> str:
        """Publish a self-contained HTML page the user can open on their phone.

        Use this whenever the answer is better SEEN than described: a chart, a
        diagram, a table, a mock-up, a rendered document, a small interactive
        demo.

        slug: short lowercase name, letters/digits/hyphens only (e.g. "sales-chart").
              Reusing a slug REPLACES that artifact and live-reloads any open view.
        html: a COMPLETE, self-contained HTML document. Inline all CSS and
              JavaScript. Inline data directly. Draw charts as inline SVG or on
              a canvas. There is no network: external scripts, fonts, images and
              fetch() are blocked by policy, so anything remote will silently
              fail. Include <title> and a viewport meta -- it is read on a phone.

        Returns the URL to open.
        """
        try:
            artifact = store.write(slug, html)
        except BadSlug as exc:
            return f"Rejected: {exc}"
        # Provenance, so a follow-up typed on the phone can be routed back to
        # this session and this repository.
        store.record_origin(artifact.slug, thread_id, workdir)
        logger.info("Wrote artifact %s (%d bytes)", artifact.slug, len(html))
        return f"Published: {base_url}/a/{artifact.slug}"

    return write_artifact


def build_model(settings: Settings, model_name: str | None = None) -> ChatOpenAI:
    """A chat model pointed at Moonshot. See module docstring for the flags."""
    name = model_name or settings.model
    extra: dict = {}
    # reasoning_effort is a K3-only top-level field; K2.x rejects/ignores it.
    # It is a first-class ChatOpenAI parameter -- passing it via model_kwargs
    # warns and is not guaranteed to reach the wire.
    if name.startswith("kimi-k3"):
        extra["reasoning_effort"] = settings.reasoning_effort
    return ChatOpenAI(
        model=name,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=1,              # non-negotiable on kimi-k3 / k2.7-code
        use_responses_api=False,    # Moonshot speaks Chat Completions only
        timeout=600,
        max_retries=3,
        **extra,
    )


def build_agent(
    settings: Settings,
    workdir: Path,
    *,
    autonomous: bool = False,
    checkpointer: SqliteSaver | None = None,
    artifact_store: ArtifactStore | None = None,
    artifact_base_url: str = "http://127.0.0.1:8001",
    session_id: str = "default",
):
    """Compile an agent rooted at `workdir`.

    `autonomous=True` drops the approval gates -- meant for a trusted repo
    and a task the user has already sanctioned, never as the default.
    """
    workdir = Path(workdir).resolve()
    if not workdir.is_dir():
        raise NotADirectoryError(f"Working directory does not exist: {workdir}")

    backend = LocalShellBackend(
        root_dir=workdir,
        virtual_mode=True,          # paths are rooted at workdir, not the disk
        timeout=settings.execute_timeout,
        env=_scrubbed_env(settings),
        inherit_env=False,          # we pass an explicit, scrubbed env instead
    )

    interrupt_on = {} if autonomous else {t: True for t in settings.interrupt_on}

    # Publishing an artifact is additive and reversible -- it writes outside
    # the repo and overwrites only its own slug -- so it is deliberately NOT
    # behind an approval gate. Gating it would mean a tap for every redraw.
    tools = []
    if artifact_store is not None:
        tools.append(
            make_artifact_tool(
                artifact_store, artifact_base_url.rstrip("/"), session_id, workdir
            )
        )

    return create_deep_agent(
        model=build_model(settings),
        system_prompt=SYSTEM_PROMPT.format(workdir=workdir),
        tools=tools,
        backend=backend,
        middleware=[TodoListMiddleware()],   # opt-in since deepagents 0.7
        interrupt_on=interrupt_on,
        checkpointer=checkpointer,
        name="remote-coder",
    )


def open_checkpointer(settings: Settings) -> SqliteSaver:
    """A checkpointer that survives process restart and reboot.

    check_same_thread=False because the Telegram transport touches it from
    its event loop while the agent runs in a worker thread. InMemorySaver
    would lose every session on restart, which is exactly the failure mode
    this project exists to fix.
    """
    conn = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
    return SqliteSaver(conn)


def open_async_checkpointer(settings: Settings):
    """Async checkpointer for the bot's event loop.

    SqliteSaver raises NotImplementedError on every async method, so the
    Telegram side -- which is async all the way down -- must use this one.
    The CLI keeps the sync saver. Both read and write the same file and the
    same format, so a session started from the terminal resumes on the phone.

    Returns an async context manager: the caller owns its lifetime, because
    the connection must stay open for as long as the bot polls.
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    return AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db))
