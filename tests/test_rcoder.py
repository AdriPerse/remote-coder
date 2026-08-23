"""Tests that need no API key, no bot token and no network.

The expensive parts (a real model, a real Telegram) are faked. What is
actually pinned here is the logic that would otherwise only fail live, on
the phone, while the owner is travelling: authorisation, path refusal,
ambiguity handling, cost arithmetic, and the secret-scrubbing that stops
the agent reading its own API key.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rcoder import config as config_mod  # noqa: E402
from rcoder.config import ConfigurationError, Settings, load_settings  # noqa: E402
from rcoder.pricing import estimate_cost  # noqa: E402
from rcoder.progress import Progress  # noqa: E402
from rcoder.sessions import SessionStore  # noqa: E402


def make_settings(**overrides) -> Settings:
    base = dict(
        api_key="sk-test",
        base_url="https://api.moonshot.ai/v1",
        model="kimi-k3",
        small_model="kimi-k2.6",
        reasoning_effort="high",
        telegram_token="token",
        allowed_chat_id=42,
        interrupt_on=("execute", "write_file", "edit_file", "delete"),
        execute_timeout=120,
        scrub_env=("MOONSHOT_API_KEY", "RC_TELEGRAM_TOKEN"),
    )
    base.update(overrides)
    return Settings(**base)


class TestConfig(unittest.TestCase):
    def test_missing_api_key_is_named(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(config_mod, "load_env_file", lambda *a, **k: None):
            with self.assertRaises(ConfigurationError) as ctx:
                load_settings(require_telegram=False)
        self.assertIn("MOONSHOT_API_KEY", str(ctx.exception))

    def test_cli_does_not_require_a_bot_token(self):
        """The terminal harness must work before a bot exists."""
        with mock.patch.dict(os.environ, {"MOONSHOT_API_KEY": "sk-x"}, clear=True), \
             mock.patch.object(config_mod, "load_env_file", lambda *a, **k: None):
            settings = load_settings(require_telegram=False)
        self.assertEqual(settings.model, "kimi-k3")

    def test_bad_reasoning_effort_is_rejected(self):
        with mock.patch.dict(
            os.environ,
            {"MOONSHOT_API_KEY": "sk-x", "RC_REASONING_EFFORT": "turbo"},
            clear=True,
        ), mock.patch.object(config_mod, "load_env_file", lambda *a, **k: None):
            with self.assertRaises(ConfigurationError):
                load_settings(require_telegram=False)

    def test_process_env_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MOONSHOT_API_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MOONSHOT_API_KEY": "from-env"}, clear=True):
                config_mod.load_env_file(path)
                self.assertEqual(os.environ["MOONSHOT_API_KEY"], "from-env")


class TestSecretScrubbing(unittest.TestCase):
    def test_secrets_never_reach_the_agent_shell(self):
        """The agent must not be able to read the key that pays for it."""
        from rcoder.agent import _scrubbed_env

        settings = make_settings()
        env = {
            "PATH": "/usr/bin",
            "MOONSHOT_API_KEY": "sk-secret",
            "RC_TELEGRAM_TOKEN": "123:abc",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            scrubbed = _scrubbed_env(settings)
        self.assertIn("PATH", scrubbed)          # git and python must resolve
        self.assertNotIn("MOONSHOT_API_KEY", scrubbed)
        self.assertNotIn("RC_TELEGRAM_TOKEN", scrubbed)


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name) / "s.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_sets_current(self):
        session = self.store.create(42, Path.cwd())
        self.assertEqual(self.store.current(42).thread_id, session.thread_id)

    def test_toggling_autonomy_does_not_clobber_current_session(self):
        """An upsert on one column must not wipe the other."""
        session = self.store.create(42, Path.cwd())
        self.store.set_autonomous(42, True)
        self.assertTrue(self.store.autonomous(42))
        self.assertEqual(self.store.current(42).thread_id, session.thread_id)

    def test_sessions_survive_a_new_store_object(self):
        """Standing in for a process restart: the point of the whole project."""
        session = self.store.create(42, Path.cwd(), "fix the router")
        path = Path(self.tmp.name) / "s.db"
        reopened = SessionStore(path)
        try:
            self.assertEqual(reopened.current(42).thread_id, session.thread_id)
            self.assertEqual(reopened.get(session.thread_id).title, "fix the router")
        finally:
            reopened.close()

    def test_find_is_substring_and_can_be_ambiguous(self):
        a = self.store.create(42, Path("C:/Projects/MusicLore"))
        b = self.store.create(42, Path("C:/Projects/MusicLore"))
        matches = self.store.find(42, "musiclore")
        self.assertEqual({m.thread_id for m in matches}, {a.thread_id, b.thread_id})
        self.assertEqual(self.store.find(42, "nothing-here"), [])

    def test_usage_totals_group_by_model(self):
        session = self.store.create(42, Path.cwd())
        self.store.record_usage(session.thread_id, "kimi-k3", 100, 10)
        self.store.record_usage(session.thread_id, "kimi-k3", 200, 20)
        self.store.record_usage(session.thread_id, "kimi-k2.6", 50, 5)
        totals = self.store.usage_totals(session.thread_id)
        self.assertEqual(totals["kimi-k3"], (300, 30))
        self.assertEqual(totals["kimi-k2.6"], (50, 5))


class TestPricing(unittest.TestCase):
    def test_known_model(self):
        # 1M in + 1M out on k3 = 3.00 + 15.00
        self.assertAlmostEqual(estimate_cost("kimi-k3", 1_000_000, 1_000_000), 18.0)

    def test_unknown_model_is_none_not_zero(self):
        """Reporting $0.00 for an unpriced model would be a lie."""
        self.assertIsNone(estimate_cost("some-new-model", 1_000_000, 1_000_000))


class FakeMessage:
    def __init__(self):
        self.text = None
        self.edits = 0

    async def edit_text(self, text):
        self.text = text
        self.edits += 1


class FakeBot:
    def __init__(self):
        self.message = FakeMessage()
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return self.message


class TestProgress(unittest.IsolatedAsyncioTestCase):
    async def test_edits_are_throttled_but_forced_flush_always_lands(self):
        bot = FakeBot()
        progress = Progress(bot, 42, "header")
        await progress.start()
        for i in range(10):
            progress.add("step {}".format(i))
            await progress.flush()          # throttled: mostly no-ops
        self.assertLessEqual(bot.message.edits, 2)
        await progress.finish("done")
        self.assertIn("done", bot.message.text)

    async def test_render_is_clipped_to_telegram_limit(self):
        bot = FakeBot()
        progress = Progress(bot, 42, "header")
        await progress.start()
        for i in range(200):
            progress.add("x" * 200)
        await progress.finish()
        self.assertLessEqual(len(bot.message.text), 3600)

    async def test_a_failing_edit_never_breaks_the_run(self):
        """Telegram 429s must not kill a task that is otherwise fine."""
        bot = FakeBot()

        async def boom(text):
            raise RuntimeError("429 Too Many Requests")

        progress = Progress(bot, 42, "header")
        await progress.start()
        bot.message.edit_text = boom
        progress.add("step")
        await progress.finish()   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeUserMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class FakeChat:
    def __init__(self, chat_id):
        self.id = chat_id


class FakeUpdate:
    def __init__(self, chat_id, text=""):
        self.effective_chat = FakeChat(chat_id)
        self.message = FakeUserMessage(text)


class FakeCtx:
    def __init__(self, *args):
        self.args = list(args)


def make_bot(tmpdir):
    """A Bot with real logic but no Telegram and no checkpointer."""
    import bot as bot_mod

    settings = make_settings()
    object.__setattr__(settings, "state_dir", Path(tmpdir))
    instance = bot_mod.Bot.__new__(bot_mod.Bot)
    instance.settings = settings
    instance.store = SessionStore(Path(tmpdir) / "s.db")
    instance.checkpointer = None
    instance._checkpointer_cm = None
    instance.app = None
    instance.busy = {}
    instance.cancel = {}
    instance.pending = {}
    return instance


class TestBotGuards(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self.tmp.name)

    def tearDown(self):
        self.bot.store.close()
        self.tmp.cleanup()

    async def test_a_stranger_is_ignored_entirely(self):
        """Anyone who finds the bot gets a shell on this machine otherwise."""
        update = FakeUpdate(chat_id=999, text="rm -rf /")
        await self.bot.on_message(update, FakeCtx())
        self.assertEqual(update.message.replies, [])

    async def test_owner_without_a_repo_is_told_so(self):
        update = FakeUpdate(chat_id=42, text="fix the tests")
        await self.bot.on_message(update, FakeCtx())
        self.assertIn("/repo", update.message.replies[0])

    async def test_repo_refuses_a_path_that_does_not_exist(self):
        """A typo must never mint a junk folder to work in."""
        ghost = str(Path(self.tmp.name) / "definitely-not-here")
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_repo(update, FakeCtx(ghost))
        self.assertIn("Nothing changed", update.message.replies[0])
        self.assertFalse(Path(ghost).exists())
        self.assertIsNone(self.bot.store.current(42))

    async def test_repo_refuses_a_relative_path(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_repo(update, FakeCtx("some/relative/dir"))
        self.assertIn("Nothing changed", update.message.replies[0])

    async def test_repo_accepts_a_real_directory(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_repo(update, FakeCtx(self.tmp.name))
        self.assertIn("Working in", update.message.replies[0])
        self.assertIsNotNone(self.bot.store.current(42))

    async def test_resume_lists_matches_instead_of_guessing(self):
        """Resuming the wrong work is worse than one extra tap."""
        self.bot.store.create(42, Path(self.tmp.name))
        self.bot.store.create(42, Path(self.tmp.name))
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_resume(update, FakeCtx(Path(self.tmp.name).name))
        self.assertIn("matches several sessions", update.message.replies[0])

    async def test_a_second_task_does_not_start_while_one_is_running(self):
        self.bot.store.create(42, Path(self.tmp.name))
        self.bot.busy[42] = True
        update = FakeUpdate(chat_id=42, text="do something else")
        await self.bot.on_message(update, FakeCtx())
        self.assertIn("still working", update.message.replies[0])

    async def test_an_expired_approval_does_not_resume_anything(self):
        class FakeQuery:
            def __init__(self):
                self.data = "approve"
                self.message = type("M", (), {"chat_id": 42})()
                self.edited = None

            async def answer(self):
                pass

            async def edit_message_text(self, text):
                self.edited = text

        query = FakeQuery()
        update = type("U", (), {"callback_query": query})()
        self.bot.run = None   # must never be called; would raise if it were
        await self.bot.on_callback(update, FakeCtx())
        self.assertIn("expired", query.edited)
