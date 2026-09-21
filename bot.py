"""The phone-drivable coding agent.

Long-polls Telegram, so it needs no inbound port, no tunnel and no static
IP -- it works from any network the laptop can reach the internet on, which
is what makes it usable while travelling.

One task runs at a time per chat. That is not a limitation to work around
later: the agent holds a working directory and a shell, and two concurrent
runs editing the same repo is a corruption bug waiting to happen.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from langgraph.types import Command
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from rcoder.agent import build_agent, iter_node_updates, open_async_checkpointer
from rcoder.config import ConfigurationError, load_settings
from rcoder.pricing import PRICES, estimate_cost
from rcoder.progress import Progress
from rcoder.repos import discover
from rcoder.sessions import SessionStore

logger = logging.getLogger("rcoder.bot")

HELP = """Send a message and I work on it in the current repo.

/repo <full path>  - point me at a repository
/new [title]       - start a fresh session in the same repo
/sessions          - list recent sessions
/resume <part>     - switch to an earlier session
/auto on|off       - run without approval prompts
/cost              - tokens and estimated spend
/cancel            - stop the running task
/status            - repo, session, model, state
/logs [n]          - last n lines of the bot log
/model [name]      - show or switch the model
/help              - this
"""

# (command, one-line description) -- shown in Telegram's "/" menu. Keep
# descriptions under 256 chars and commands lowercase, per Bot API limits.
COMMANDS = [
    ("repo", "point me at a repository"),
    ("new", "start a fresh session in the same repo"),
    ("sessions", "list recent sessions"),
    ("resume", "switch to an earlier session"),
    ("auto", "run without approval prompts"),
    ("cost", "tokens and estimated spend"),
    ("cancel", "stop the running task"),
    ("status", "repo, session, model, state"),
    ("logs", "last n lines of the bot log"),
    ("model", "show or switch the model"),
    ("help", "list commands"),
]

LOG_PATH = Path(__file__).resolve().parent / "logs" / "bot.log"


def short(value, limit: int = 70) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


class Bot:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.store = SessionStore(settings.sessions_db)
        # Opened in post_init: the async saver's connection must live for
        # as long as the bot polls, so the Application owns its lifetime.
        self.checkpointer = None
        self._checkpointer_cm = None
        self.app: Application | None = None
        self.busy: dict[int, bool] = {}
        self.cancel: dict[int, bool] = {}
        self.pending: dict[int, dict] = {}
        # chat -> the repo list currently on offer. Lost on restart,
        # which is handled rather than ignored: a stale tap says so.
        self.repo_choices: dict[int, list[Path]] = {}
        self.started_at = time.monotonic()

    # -- guards -----------------------------------------------------------
    def authorised(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or chat.id != self.settings.allowed_chat_id:
            logger.warning("Rejected message from chat %s", chat.id if chat else "?")
            return False
        # Log what arrived, always. A handler that runs silently cannot be
        # told apart from one that never fired, and reading the log
        # afterwards then shows an idle bot that looks frozen.
        message = update.effective_message
        if message is not None:
            logger.info("<- %s", short(message.text or "<non-text>", 100))
        return True

    def agent_for(self, session, autonomous: bool):
        return build_agent(
            self.settings,
            session.workdir,
            autonomous=autonomous,
            checkpointer=self.checkpointer,
        )

    # -- repo picker ------------------------------------------------------
    async def offer_repos(self, chat_id: int, header: str) -> None:
        """Send a tap-to-pick list of repositories.

        Typing a Windows path on a phone is the friction this removes.
        """
        known = [s.workdir for s in self.store.list(chat_id, limit=20)]
        choices = discover(extra=known)
        if not choices:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="I could not find any git repositories to offer. "
                     "Send /repo <full path> instead.",
            )
            return
        self.repo_choices[chat_id] = choices
        rows = [
            [InlineKeyboardButton(path.name, callback_data="repo:{}".format(i))]
            for i, path in enumerate(choices)
        ]
        await self.app.bot.send_message(
            chat_id=chat_id, text=header,
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def use_repo(self, chat_id: int, path: Path) -> str:
        session = self.store.create(chat_id, path)
        return "Working in {}\nSession {}".format(path, session.thread_id)

    # -- commands ---------------------------------------------------------
    async def cmd_start(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        session = self.store.current(update.effective_chat.id)
        where = (
            "Current repo: {}".format(session.workdir)
            if session
            else "No repo set. Use /repo <full path>."
        )
        await update.message.reply_text(
            "remote-coder ready.\n{}\n\n{}".format(where, HELP)
        )

    async def cmd_help(self, update: Update, _ctx) -> None:
        if self.authorised(update):
            await update.message.reply_text(HELP)

    async def cmd_repo(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        raw = " ".join(ctx.args).strip()
        if not raw:
            session = self.store.current(chat_id)
            header = (
                "Current repo: {}\n\nPick another:".format(session.workdir)
                if session else "Pick a repository:"
            )
            await self.offer_repos(chat_id, header)
            return
        path = Path(raw)
        # Never create the directory: a typo would mint a junk folder and the
        # agent would happily start work in it.
        if not path.is_absolute() or not path.is_dir():
            await update.message.reply_text(
                "'{}' is not a full path to an existing folder. "
                "Nothing changed.".format(raw)
            )
            return
        session = self.store.create(update.effective_chat.id, path)
        await update.message.reply_text(
            "Working in {}\nSession {}".format(path, session.thread_id)
        )

    async def cmd_new(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        current = self.store.current(chat_id)
        if current is None:
            await update.message.reply_text("No repo set yet. Use /repo <full path>.")
            return
        session = self.store.create(chat_id, current.workdir, " ".join(ctx.args).strip())
        await update.message.reply_text(
            "New session {} in {}".format(session.thread_id, session.workdir.name)
        )

    async def cmd_sessions(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        sessions = self.store.list(chat_id)
        if not sessions:
            await update.message.reply_text("No sessions yet.")
            return
        current = self.store.current(chat_id)
        lines = []
        for s in sessions:
            mark = "* " if current and s.thread_id == current.thread_id else "  "
            title = " - {}".format(s.title) if s.title else ""
            lines.append("{}{} [{}]{}".format(mark, s.thread_id, s.workdir.name, title))
        await update.message.reply_text(
            "Sessions (* = current):\n" + "\n".join(lines)
        )

    async def cmd_resume(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        selector = " ".join(ctx.args).strip()
        if not selector:
            await update.message.reply_text("Usage: /resume <part of a session name>")
            return
        matches = self.store.find(chat_id, selector)
        if not matches:
            await update.message.reply_text(
                "No session matches '{}'.".format(selector)
            )
            return
        if len(matches) > 1:
            # Ambiguity is listed, never guessed -- resuming the wrong piece
            # of work is worse than one extra tap.
            await update.message.reply_text(
                "That matches several sessions:\n"
                + "\n".join("  " + s.thread_id for s in matches[:10])
            )
            return
        session = matches[0]
        self.store.set_current(chat_id, session.thread_id)
        await update.message.reply_text(
            "Resumed {} in {}\nIts full history is intact - just carry on.".format(
                session.thread_id, session.workdir
            )
        )

    async def cmd_auto(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        arg = ctx.args[0].lower() if ctx.args else ""
        if arg not in ("on", "off"):
            on = self.store.autonomous(chat_id)
            await update.message.reply_text(
                "Autonomous mode is {}. Approvals are {}.".format(
                    "ON" if on else "OFF", "OFF" if on else "ON"
                )
            )
            return
        self.store.set_autonomous(chat_id, arg == "on")
        await update.message.reply_text(
            "Autonomous mode ON - I will edit files and run commands without asking."
            if arg == "on"
            else "Autonomous mode OFF - I will ask before editing or running anything."
        )

    async def cmd_cost(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        session = self.store.current(update.effective_chat.id)
        lines: list[str] = []
        scopes = []
        if session is not None:
            scopes.append(("This session", session.thread_id))
        scopes.append(("All time", None))
        for label, thread in scopes:
            totals = self.store.usage_totals(thread)
            if not totals:
                continue
            lines.append(label + ":")
            total_cost = 0.0
            for model, (inp, out) in sorted(totals.items()):
                cost = estimate_cost(model, inp, out)
                total_cost += cost or 0.0
                money = "~${:.3f}".format(cost) if cost is not None else "unpriced"
                lines.append(
                    "  {}: {:,} in / {:,} out  {}".format(model, inp, out, money)
                )
            lines.append("  total ~${:.2f}".format(total_cost))
        if not lines:
            await update.message.reply_text("No usage recorded yet.")
            return
        await update.message.reply_text(
            "\n".join(lines) + "\n\n(upper bound: assumes no cache hits)"
        )

    async def cmd_cancel(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        if self.busy.get(chat_id):
            self.cancel[chat_id] = True
            await update.message.reply_text("Stopping after the current step...")
        else:
            await update.message.reply_text("Nothing is running.")

    async def cmd_status(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        session = self.store.current(chat_id)
        minutes = int((time.monotonic() - self.started_at) // 60)
        lines = [
            "Uptime: {}h {:02d}m".format(minutes // 60, minutes % 60),
            "State: {}".format("working on a task" if self.busy.get(chat_id) else "idle"),
            "Model: {} (reasoning {})".format(
                self.settings.model, self.settings.reasoning_effort
            ),
            "Approvals: {}".format(
                "OFF (autonomous)" if self.store.autonomous(chat_id) else "ON"
            ),
        ]
        if session is not None:
            lines.append("Repo: {}".format(session.workdir))
            lines.append("Session: {}{}".format(
                session.thread_id,
                " - {}".format(session.title) if session.title else "",
            ))
        else:
            lines.append("Repo: none set - /repo to pick one")
        await update.message.reply_text("\n".join(lines))

    async def cmd_logs(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        try:
            count = int(ctx.args[0]) if ctx.args else 20
        except ValueError:
            count = 20
        count = max(1, min(count, 100))
        if not LOG_PATH.exists():
            await update.message.reply_text("No log file at {} yet.".format(LOG_PATH))
            return
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-count:] or ["(log is empty)"]
        text = "\n".join(tail)
        await update.message.reply_text(
            text[-3900:] if len(text) > 3900 else text
        )

    async def cmd_model(self, update: Update, ctx) -> None:
        if not self.authorised(update):
            return
        known = ", ".join(sorted(PRICES))
        if not ctx.args:
            await update.message.reply_text(
                "Model: {} (reasoning {})\nPriced models: {}\n"
                "Usage: /model <name> - applies to the next task.".format(
                    self.settings.model, self.settings.reasoning_effort, known
                )
            )
            return
        name = ctx.args[0].strip()
        if name == self.settings.model:
            await update.message.reply_text("Already running {}.".format(name))
            return
        # Settings is frozen; the swap is cheap because agents are built per task.
        object.__setattr__(self.settings, "model", name)
        priced = "priced" if name in PRICES else "UNPRICED - /cost will show nothing"
        await update.message.reply_text(
            "Next tasks will use {} ({}).".format(name, priced)
        )

    # -- the actual work --------------------------------------------------
    async def on_message(self, update: Update, _ctx) -> None:
        if not self.authorised(update):
            return
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        if not text:
            return
        if self.busy.get(chat_id):
            await update.message.reply_text(
                "I am still working on the previous task. /cancel to stop it."
            )
            return
        session = self.store.current(chat_id)
        if session is None:
            # A refusal that hands back a form is a dead end. Offer the list,
            # and keep what they asked so it runs once a repo is chosen.
            self.queued_task = getattr(self, "queued_task", {})
            self.queued_task[chat_id] = text
            await self.offer_repos(
                chat_id,
                "I need a repository first. Pick one and I will then run:\n\n{}".format(
                    short(text, 200)
                ),
            )
            return
        if not session.title:
            self.store.touch(session.thread_id, title=text)
        await self.run(
            chat_id, session, {"messages": [{"role": "user", "content": text}]}
        )

    async def run(self, chat_id: int, session, payload) -> None:
        """Stream one agent turn, reporting progress and pausing on approvals."""
        self.busy[chat_id] = True
        self.cancel[chat_id] = False
        agent = self.agent_for(session, self.store.autonomous(chat_id))
        config = {
            "configurable": {"thread_id": session.thread_id},
            "recursion_limit": 150,
        }
        header = "[{}] {}".format(session.workdir.name, self.settings.model)
        progress = Progress(self.app.bot, chat_id, header)
        await progress.start()
        final_text = ""
        try:
            async for ns, update in agent.astream(
                payload, config=config, stream_mode="updates", subgraphs=True
            ):
                if self.cancel.get(chat_id):
                    progress.add("cancelled")
                    break
                prefix = "[{}] ".format(ns[0].split(":")[0]) if ns else ""
                for _node, data in iter_node_updates(update):
                    for msg in data.get("messages") or []:
                        line, text = self.describe(msg, session)
                        if line:
                            progress.add(prefix + line)
                        if text:
                            final_text = text
                await progress.flush()

            state = await agent.aget_state(config)
            interrupts = getattr(state, "interrupts", None) or ()
            if interrupts and not self.cancel.get(chat_id):
                await progress.finish("waiting for your approval")
                await self.ask_approval(chat_id, interrupts[0])
                return

            await progress.finish("done")
            if final_text:
                await self.send_long(chat_id, final_text)
        except Exception as exc:  # noqa: BLE001 - a failed task must not kill the bot
            logger.exception("Task failed")
            await progress.finish("FAILED")
            await self.app.bot.send_message(
                chat_id=chat_id,
                text="The task failed: {}: {}".format(
                    type(exc).__name__, short(exc, 300)
                ),
            )
        finally:
            self.busy[chat_id] = False
            self.store.touch(session.thread_id)

    def describe(self, msg, session) -> tuple[str, str]:
        """(progress line, final assistant text) for one streamed message."""
        kind = msg.__class__.__name__
        if kind == "AIMessage":
            usage = getattr(msg, "usage_metadata", None) or {}
            if usage:
                self.store.record_usage(
                    session.thread_id,
                    self.settings.model,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                )
            for call in getattr(msg, "tool_calls", []) or []:
                args = call.get("args", {})
                detail = (
                    args.get("command")
                    or args.get("file_path")
                    or args.get("path")
                    or ""
                )
                return "{}: {}".format(call["name"], short(detail)), ""
            content = msg.content if isinstance(msg.content, str) else ""
            return "", content.strip()
        if kind == "ToolMessage":
            if getattr(msg, "status", "") == "error":
                return "  failed: {}".format(short(msg.content)), ""
            return "", ""
        return "", ""

    async def ask_approval(self, chat_id: int, interrupt) -> None:
        value = interrupt.value or {}
        requests = value.get("action_requests") or []
        if not requests:
            return
        request = requests[0]
        self.pending[chat_id] = {"n": len(requests)}
        args = request.get("args", {})
        detail = (
            args.get("command") or args.get("file_path") or args.get("path") or ""
        )
        extra = (
            "\n\n(+{} more in this batch)".format(len(requests) - 1)
            if len(requests) > 1
            else ""
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data="approve"),
                    InlineKeyboardButton("Reject", callback_data="reject"),
                ]
            ]
        )
        await self.app.bot.send_message(
            chat_id=chat_id,
            text="Approve this?\n\n{}\n{}{}".format(
                request.get("name"), short(detail, 300), extra
            ),
            reply_markup=keyboard,
        )

    async def on_callback(self, update: Update, _ctx) -> None:
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat_id
        if chat_id != self.settings.allowed_chat_id:
            return
        if (query.data or "").startswith("repo:"):
            await self.on_repo_pick(query, chat_id)
            return
        pending = self.pending.pop(chat_id, None)
        if pending is None:
            await query.edit_message_text("That approval has expired.")
            return
        approved = query.data == "approve"
        await query.edit_message_text("Approved." if approved else "Rejected.")
        session = self.store.current(chat_id)
        if session is None:
            return
        decision = (
            {"type": "approve"}
            if approved
            else {"type": "reject", "message": "The user rejected this action."}
        )
        decisions = [dict(decision) for _ in range(pending["n"])]
        await self.run(chat_id, session, Command(resume={"decisions": decisions}))

    async def on_repo_pick(self, query, chat_id: int) -> None:
        choices = self.repo_choices.get(chat_id) or []
        try:
            index = int((query.data or "repo:").split(":", 1)[1])
            path = choices[index]
        except (ValueError, IndexError):
            # The list is in memory, so a restart invalidates old buttons.
            await query.edit_message_text(
                "That list is out of date - send /repo again."
            )
            return
        await query.edit_message_text(await self.use_repo(chat_id, path))
        # Run whatever they asked before they had a repo.
        queued = getattr(self, "queued_task", {}).pop(chat_id, None)
        if queued:
            session = self.store.current(chat_id)
            if session is not None:
                self.store.touch(session.thread_id, title=queued)
                await self.run(
                    chat_id, session,
                    {"messages": [{"role": "user", "content": queued}]},
                )

    async def send_long(self, chat_id: int, text: str) -> None:
        for i in range(0, len(text), 4000):
            await self.app.bot.send_message(chat_id=chat_id, text=text[i : i + 4000])

    # -- lifecycle --------------------------------------------------------
    async def on_startup(self, _app) -> None:
        self._checkpointer_cm = open_async_checkpointer(self.settings)
        self.checkpointer = await self._checkpointer_cm.__aenter__()
        logger.info("Checkpointer open at %s", self.settings.checkpoint_db)
        # Register the command menu so typing "/" in Telegram pops the list.
        await self.app.bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in COMMANDS]
        )

    async def on_shutdown(self, _app) -> None:
        if self._checkpointer_cm is not None:
            await self._checkpointer_cm.__aexit__(None, None, None)
            self._checkpointer_cm = None

    # -- wiring -----------------------------------------------------------
    def build(self) -> Application:
        self.app = (
            Application.builder()
            .token(self.settings.telegram_token)
            .post_init(self.on_startup)
            .post_shutdown(self.on_shutdown)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("repo", self.cmd_repo))
        self.app.add_handler(CommandHandler("new", self.cmd_new))
        self.app.add_handler(CommandHandler("sessions", self.cmd_sessions))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("auto", self.cmd_auto))
        self.app.add_handler(CommandHandler("cost", self.cmd_cost))
        self.app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("logs", self.cmd_logs))
        self.app.add_handler(CommandHandler("model", self.cmd_model))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message)
        )
        return self.app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs full URLs at INFO and the bot token is in the URL path.
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print("Configuration error: {}".format(exc), file=sys.stderr)
        return 2
    bot = Bot(settings)
    app = bot.build()
    logger.info("Polling as remote-coder (model=%s)", settings.model)
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
