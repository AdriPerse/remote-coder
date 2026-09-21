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


class TestIterNodeUpdates(unittest.TestCase):
    """LangGraph stream chunks must not crash the Telegram loop."""

    def test_dict_chunks_yield_dict_payloads(self):
        from rcoder.agent import iter_node_updates

        msg = object()
        pairs = list(iter_node_updates({"agent": {"messages": [msg]}}))
        self.assertEqual(pairs, [("agent", {"messages": [msg]})])

    def test_tuple_chunk_with_dict_payload_is_accepted(self):
        from rcoder.agent import iter_node_updates

        payload = {"messages": [object()]}
        pairs = list(iter_node_updates(("tools", payload)))
        self.assertEqual(pairs, [("tools", payload)])

    def test_tuple_payload_is_skipped_not_crashed(self):
        """This is the live failure: `'tuple' object has no attribute 'get'`."""
        from rcoder.agent import iter_node_updates

        self.assertEqual(list(iter_node_updates(("tools", ("not", "a dict")))), [])
        self.assertEqual(list(iter_node_updates(("tools", None))), [])
        self.assertEqual(list(iter_node_updates(None)), [])
        self.assertEqual(
            list(iter_node_updates({"ok": {"messages": []}, "bad": (1, 2)})),
            [("ok", {"messages": []})],
        )


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


class TestInfoCommands(unittest.IsolatedAsyncioTestCase):
    """/status, /logs and /model: read-only except the deliberate model swap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self.tmp.name)

    def tearDown(self):
        self.bot.store.close()
        self.tmp.cleanup()

    async def test_status_without_repo_says_so(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_status(update, FakeCtx())
        text = update.message.replies[0]
        self.assertIn("kimi-k3", text)
        self.assertIn("Uptime:", text)
        self.assertIn("none set", text)

    async def test_status_reports_the_current_session(self):
        self.bot.store.create(42, Path(self.tmp.name), "titled work")
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_status(update, FakeCtx())
        text = update.message.replies[0]
        self.assertIn(self.tmp.name, text)
        self.assertIn("titled work", text)
        self.assertIn("idle", text)

    async def test_logs_without_a_file_says_so(self):
        import bot as bot_mod

        update = FakeUpdate(chat_id=42)
        with mock.patch.object(bot_mod, "LOG_PATH",
                               Path(self.tmp.name) / "absent.log"):
            await self.bot.cmd_logs(update, FakeCtx())
        self.assertIn("No log file", update.message.replies[0])

    async def test_logs_tails_and_caps(self):
        import bot as bot_mod

        log = Path(self.tmp.name) / "bot.log"
        log.write_text("\n".join("line {:03d}".format(i) for i in range(500)),
                       encoding="utf-8")
        update = FakeUpdate(chat_id=42)
        with mock.patch.object(bot_mod, "LOG_PATH", log):
            await self.bot.cmd_logs(update, FakeCtx("5000"))   # capped at 100
        text = update.message.replies[0]
        self.assertIn("line 499", text)
        self.assertNotIn("line 398", text)                     # beyond the cap
        self.assertLessEqual(len(text), 4000)

    async def test_model_without_args_reports_current(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_model(update, FakeCtx())
        text = update.message.replies[0]
        self.assertIn("kimi-k3", text)
        self.assertIn("Usage:", text)

    async def test_model_switch_applies_to_the_settings(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_model(update, FakeCtx("kimi-k2.7-code"))
        self.assertEqual(self.bot.settings.model, "kimi-k2.7-code")
        self.assertIn("kimi-k2.7-code", update.message.replies[0])

    async def test_model_switch_to_same_is_a_noop_message(self):
        update = FakeUpdate(chat_id=42)
        await self.bot.cmd_model(update, FakeCtx("kimi-k3"))
        self.assertIn("Already running", update.message.replies[0])


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
        # Telegram exposes both; authorised() logs via effective_message.
        self.effective_message = self.message


class FakeCtx:
    def __init__(self, *args):
        self.args = list(args)


def make_bot(tmpdir):
    """A real Bot, with Telegram and the checkpointer left unopened.

    This calls the REAL __init__ on purpose. An earlier version hand-set the
    attributes it thought Bot had, then drifted the moment a field was added
    and every bot test errored at once. A helper that reimplements the thing
    it is testing tests the helper.
    """
    import bot as bot_mod

    settings = make_settings()
    object.__setattr__(settings, "state_dir", Path(tmpdir))
    return bot_mod.Bot(settings)


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

    async def test_no_repo_offers_a_picker_and_keeps_the_task(self):
        """A refusal that hands back a form is a dead end.

        The old behaviour replied "use /repo <full path>" -- which, on a
        phone, means typing a Windows path. Now it offers the list and
        remembers what was asked so it runs once a repo is chosen.
        """
        sent = []

        class FakeAppBot:
            async def send_message(self, chat_id, text, reply_markup=None):
                sent.append((text, reply_markup))

        self.bot.app = type("A", (), {"bot": FakeAppBot()})()
        self.bot.repo_choices[42] = []
        with mock.patch("bot.discover", lambda extra=None: [Path(self.tmp.name)]):
            update = FakeUpdate(chat_id=42, text="fix the tests")
            await self.bot.on_message(update, FakeCtx())
        self.assertTrue(sent, "should have offered a repo list")
        text, markup = sent[0]
        self.assertIn("fix the tests", text)      # the task is echoed back
        self.assertIsNotNone(markup)              # with tappable buttons
        self.assertEqual(self.bot.queued_task[42], "fix the tests")

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


class TestRepoDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("alpha", "beta"):
            (self.root / name / ".git").mkdir(parents=True)
        (self.root / "not-a-repo").mkdir()
        (self.root / "group" / "gamma" / ".git").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _discover(self, extra=None):
        from rcoder import repos

        with mock.patch.dict(os.environ, {"RC_REPO_ROOTS": str(self.root)}):
            return repos.discover(extra=extra)

    def test_finds_repos_one_and_two_levels_deep(self):
        names = {p.name for p in self._discover()}
        self.assertEqual(names, {"alpha", "beta", "gamma"})

    def test_ignores_directories_without_git(self):
        self.assertNotIn("not-a-repo", {p.name for p in self._discover()})

    def test_extra_paths_are_included_and_not_duplicated(self):
        extra = [self.root / "alpha"]
        found = self._discover(extra=extra)
        self.assertEqual(sum(1 for p in found if p.name == "alpha"), 1)

    def test_extra_must_still_be_a_repo(self):
        found = self._discover(extra=[self.root / "not-a-repo"])
        self.assertNotIn("not-a-repo", {p.name for p in found})

    def test_missing_root_is_survived(self):
        from rcoder import repos

        with mock.patch.dict(os.environ, {"RC_REPO_ROOTS": r"C:\nope\nowhere"}):
            self.assertEqual(repos.discover(), [])

    def test_list_is_capped(self):
        from rcoder import repos

        for i in range(20):
            (self.root / "r{}".format(i) / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"RC_REPO_ROOTS": str(self.root)}):
            self.assertLessEqual(len(repos.discover()), repos.MAX_CHOICES)


class TestRepoPicker(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = make_bot(self.tmp.name)

    def tearDown(self):
        self.bot.store.close()
        self.tmp.cleanup()

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.message = type("M", (), {"chat_id": 42})()
            self.edited = None

        async def answer(self):
            pass

        async def edit_message_text(self, text):
            self.edited = text

    async def test_a_stale_button_says_so_instead_of_picking_wrongly(self):
        """Choices live in memory; a restart must not silently pick repo 0."""
        query = self.FakeQuery("repo:3")
        await self.bot.on_repo_pick(query, 42)
        self.assertIn("out of date", query.edited)
        self.assertIsNone(self.bot.store.current(42))

    async def test_out_of_range_index_is_refused(self):
        self.bot.repo_choices[42] = [Path(self.tmp.name)]
        query = self.FakeQuery("repo:99")
        await self.bot.on_repo_pick(query, 42)
        self.assertIn("out of date", query.edited)

    async def test_picking_sets_the_session(self):
        self.bot.repo_choices[42] = [Path(self.tmp.name)]
        query = self.FakeQuery("repo:0")
        await self.bot.on_repo_pick(query, 42)
        current = self.bot.store.current(42)
        self.assertIsNotNone(current)
        self.assertIn("Working in", query.edited)

    async def test_a_repo_pick_is_not_treated_as_an_approval(self):
        """The two button kinds share one handler; they must not cross."""
        self.bot.repo_choices[42] = [Path(self.tmp.name)]
        self.bot.pending[42] = {"n": 1}
        query = self.FakeQuery("repo:0")
        update = type("U", (), {"callback_query": query})()
        self.bot.settings = make_settings(allowed_chat_id=42)
        await self.bot.on_callback(update, FakeCtx())
        # The approval must still be waiting, untouched.
        self.assertIn(42, self.bot.pending)


class FakeSearchResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestWebSearchTool(unittest.TestCase):
    def test_results_are_formatted_with_title_url_extract(self):
        from rcoder import agent as agent_mod

        payload = {
            "results": [
                {"title": "T1", "url": "https://a", "content": "hello"},
                {"title": "T2", "url": "https://b", "content": "world"},
            ]
        }
        with mock.patch.object(
            agent_mod.httpx, "post", lambda *a, **k: FakeSearchResponse(payload)
        ):
            tool = agent_mod.make_search_tool("tvly-test")
            out = tool.invoke({"query": "anything"})
        self.assertIn("https://a", out)
        self.assertIn("hello", out)
        self.assertIn("https://b", out)

    def test_empty_results_say_so(self):
        from rcoder import agent as agent_mod

        with mock.patch.object(
            agent_mod.httpx, "post",
            lambda *a, **k: FakeSearchResponse({"results": []}),
        ):
            tool = agent_mod.make_search_tool("tvly-test")
            self.assertIn("No results", tool.invoke({"query": "x"}))

    def test_http_errors_are_reported_not_raised(self):
        """A dead network must never kill a running task."""
        from rcoder import agent as agent_mod

        def boom(*a, **k):
            raise RuntimeError("connection refused")

        with mock.patch.object(agent_mod.httpx, "post", boom):
            tool = agent_mod.make_search_tool("tvly-test")
            out = tool.invoke({"query": "x"})
        self.assertIn("Search failed", out)

    def test_the_key_is_sent_but_not_leaked_in_output(self):
        from rcoder import agent as agent_mod

        captured = {}

        def fake_post(url, json, timeout):
            captured.update(json)
            return FakeSearchResponse({"results": []})

        with mock.patch.object(agent_mod.httpx, "post", fake_post):
            tool = agent_mod.make_search_tool("tvly-secret")
            out = tool.invoke({"query": "x"})
        self.assertEqual(captured["api_key"], "tvly-secret")
        self.assertNotIn("tvly-secret", out)

    def test_max_results_is_bounded(self):
        from rcoder import agent as agent_mod

        captured = {}

        def fake_post(url, json, timeout):
            captured.update(json)
            return FakeSearchResponse({"results": []})

        with mock.patch.object(agent_mod.httpx, "post", fake_post):
            tool = agent_mod.make_search_tool("tvly-test")
            tool.invoke({"query": "x", "max_results": 999})
        self.assertEqual(captured["max_results"], 10)
