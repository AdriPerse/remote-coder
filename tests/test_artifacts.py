"""Tests for artifact storage and the preview server.

These pin the isolation properties, which are the entire reason the server
exists. Every one of them is a rule that would fail silently and invisibly
if it regressed: an artifact would still look fine on the phone while no
longer being contained.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from artifact_server import ARTIFACT_CSP, IFRAME_SANDBOX, build_app, inject_csp  # noqa: E402
from rcoder.artifacts import ArtifactStore, BadSlug, check_slug  # noqa: E402


class TestSlugs(unittest.TestCase):
    def test_good_slugs(self):
        for slug in ("chart", "sales-chart-2026", "a", "x9"):
            self.assertEqual(check_slug(slug), slug)

    def test_traversal_is_refused_not_sanitised(self):
        """Refusing beats cleaning: a cleaner can be smuggled past."""
        for slug in ("../etc/passwd", "..", "a/b", "a\\b", "C:/windows",
                     "", "UPPER", "sp ace", "dot.dot", "~root"):
            with self.assertRaises(BadSlug, msg=slug):
                check_slug(slug)

    def test_slug_length_is_bounded(self):
        with self.assertRaises(BadSlug):
            check_slug("a" * 65)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_write_read_and_title(self):
        self.store.write("demo", "<html><head><title>My Chart</title></head><body>x</body></html>")
        artifact = self.store.get("demo")
        self.assertEqual(artifact.title, "My Chart")
        self.assertIn("My Chart", artifact.html)

    def test_title_falls_back_to_slug(self):
        self.store.write("untitled", "<p>no title here</p>")
        self.assertEqual(self.store.get("untitled").title, "untitled")

    def test_rewriting_a_slug_replaces_it(self):
        self.store.write("demo", "<p>first</p>")
        self.store.write("demo", "<p>second</p>")
        self.assertIn("second", self.store.get("demo").html)
        self.assertEqual(len(self.store.list()), 1)

    def test_write_refuses_a_traversal_slug(self):
        with self.assertRaises(BadSlug):
            self.store.write("../escape", "<p>nope</p>")

    def test_missing_artifact_is_none(self):
        self.assertIsNone(self.store.get("nothing-here"))

    def test_queue_take_is_exactly_once(self):
        """Two consumers racing must not both run the same request."""
        self.store.write("demo", "<p>x</p>")
        request_id = self.store.enqueue("demo", "make it blue")
        self.assertEqual(len(self.store.pending()), 1)
        first = self.store.take(request_id)
        second = self.store.take(request_id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(first["prompt"], "make it blue")
        self.assertEqual(self.store.pending(), [])


class TestCspInjection(unittest.TestCase):
    def test_our_csp_precedes_a_model_supplied_one(self):
        """First CSP meta wins, so ours must come first."""
        document = (
            "<html><head>"
            '<meta http-equiv="Content-Security-Policy" content="default-src *">'
            "</head><body>x</body></html>"
        )
        out = inject_csp(document)
        self.assertLess(out.index(ARTIFACT_CSP), out.index("default-src *"))

    def test_fragment_without_head_is_still_governed(self):
        out = inject_csp("<p>just a fragment</p>")
        self.assertIn(ARTIFACT_CSP, out)
        self.assertIn("just a fragment", out)

    def test_head_with_attributes_is_handled(self):
        out = inject_csp('<html><head lang="en" data-x="1"><title>t</title></head></html>')
        self.assertIn(ARTIFACT_CSP, out)
        self.assertLess(out.index(ARTIFACT_CSP), out.index("<title>"))

    def test_connect_src_none_is_present(self):
        """The clause that actually stops exfiltration."""
        self.assertIn("connect-src 'none'", ARTIFACT_CSP)


class TestServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tmp.name))
        self.client = TestClient(build_app(self.store))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sandbox_never_grants_same_origin(self):
        """allow-scripts + allow-same-origin is a full sandbox escape."""
        self.assertIn("allow-scripts", IFRAME_SANDBOX)
        self.assertNotIn("allow-same-origin", IFRAME_SANDBOX)
        self.store.write("demo", "<p>hello</p>")
        body = self.client.get("/a/demo").text
        self.assertIn('sandbox="allow-scripts allow-downloads"', body)

    def test_artifact_cannot_break_out_of_the_srcdoc_attribute(self):
        """A quote in the artifact must not end the attribute and inject markup."""
        self.store.write("evil", '<p title="x"></iframe><script>alert(1)</script>')
        body = self.client.get("/a/evil").text
        # The raw closing tag must not appear unescaped in the shell.
        self.assertNotIn("</iframe><script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_view_serves_the_artifact_inside_srcdoc(self):
        self.store.write("demo", "<html><head><title>Chart</title></head><body>hi</body></html>")
        response = self.client.get("/a/demo")
        self.assertEqual(response.status_code, 200)
        self.assertIn("srcdoc=", response.text)
        self.assertIn("Chart", response.text)

    def test_missing_artifact_is_404_and_bad_slug_is_400(self):
        self.assertEqual(self.client.get("/a/nothing-here").status_code, 404)
        self.assertEqual(self.client.get("/a/UPPERCASE").status_code, 400)

    def test_index_lists_artifacts_and_escapes_titles(self):
        self.store.write("a1", "<title>Safe &amp; Sound</title>")
        self.store.write("b2", "<title><script>alert(1)</script></title>")
        body = self.client.get("/").text
        self.assertIn("a1", body)
        self.assertIn("b2", body)
        self.assertNotIn("<script>alert(1)</script>", body)

    def test_empty_index_says_so(self):
        self.assertIn("Nothing yet", self.client.get("/").text)

    def test_ask_enqueues_and_redirects(self):
        self.store.write("demo", "<p>x</p>")
        response = self.client.post(
            "/a/demo/ask", data={"prompt": "make the bars horizontal"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)   # not 302: refresh must not re-post
        pending = self.store.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["prompt"], "make the bars horizontal")

    def test_ask_on_a_missing_artifact_is_404_and_queues_nothing(self):
        response = self.client.post("/a/ghost/ask", data={"prompt": "hi"},
                                    follow_redirects=False)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.pending(), [])

    def test_blank_prompt_queues_nothing(self):
        self.store.write("demo", "<p>x</p>")
        self.client.post("/a/demo/ask", data={"prompt": "   "}, follow_redirects=False)
        self.assertEqual(self.store.pending(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestOrigin(unittest.TestCase):
    """Provenance is what makes a queued follow-up routable at all."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_origin_round_trips(self):
        self.store.write("demo", "<p>x</p>")
        self.store.record_origin("demo", "thread-7", Path("C:/Projects/MusicLore"))
        thread_id, workdir = self.store.origin("demo")
        self.assertEqual(thread_id, "thread-7")
        self.assertEqual(workdir, Path("C:/Projects/MusicLore"))

    def test_unknown_slug_has_no_origin(self):
        """Must be None, never a default -- guessing runs the wrong repo."""
        self.assertIsNone(self.store.origin("never-written"))

    def test_rewriting_updates_the_origin(self):
        self.store.record_origin("demo", "old", Path("C:/old"))
        self.store.record_origin("demo", "new", Path("C:/new"))
        self.assertEqual(self.store.origin("demo"), ("new", Path("C:/new")))

    def test_origin_rejects_a_bad_slug(self):
        with self.assertRaises(BadSlug):
            self.store.record_origin("../evil", "t", Path("C:/x"))


class TestWatchRouting(unittest.TestCase):
    """The consumer must refuse to guess rather than run somewhere wrong."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ArtifactStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _request(self, slug="demo", prompt="make it blue"):
        self.store.write(slug, "<p>x</p>")
        request_id = self.store.enqueue(slug, prompt)
        return self.store.take(request_id)

    def test_no_origin_means_the_agent_is_never_built(self):
        import watch

        built = []
        original = watch.build_agent
        watch.build_agent = lambda *a, **k: built.append(1)
        try:
            watch.handle(None, self.store, self._request(), "http://x", True)
        finally:
            watch.build_agent = original
        self.assertEqual(built, [])   # refused, not guessed

    def test_vanished_repo_means_the_agent_is_never_built(self):
        import watch

        self.store.record_origin("demo", "t1", Path(self.tmp.name) / "gone")
        built = []
        original = watch.build_agent
        watch.build_agent = lambda *a, **k: built.append(1)
        try:
            watch.handle(None, self.store, self._request(), "http://x", True)
        finally:
            watch.build_agent = original
        self.assertEqual(built, [])

    def test_a_claimed_request_is_not_replayed(self):
        """A crash must not become a token-burning retry loop."""
        self.store.write("demo", "<p>x</p>")
        request_id = self.store.enqueue("demo", "boom")
        self.assertIsNotNone(self.store.take(request_id))
        self.assertEqual(self.store.pending(), [])
        self.assertIsNone(self.store.take(request_id))
