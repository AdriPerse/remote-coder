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

    return create_deep_agent(
        model=build_model(settings),
        system_prompt=SYSTEM_PROMPT.format(workdir=workdir),
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
