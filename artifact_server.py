"""Serves agent-generated artifacts to a phone, safely.

Run it on its own port, which makes it its own ORIGIN. That matters: origin
is scheme+host+port, so a page served here cannot touch anything belonging to
the agent's own control surface even if something goes wrong.

The isolation has three layers, weakest last:

1. `sandbox="allow-scripts allow-downloads"` on the iframe, deliberately
   WITHOUT `allow-same-origin`. That gives the artifact a null, opaque
   origin: no cookies, no localStorage, no reach into the parent document.
   `allow-scripts` together with `allow-same-origin` would be a full sandbox
   escape, which is exactly why the pair never appears here.
2. A CSP injected as the FIRST `<meta>` in the document head. The first CSP
   meta wins, so this overrides anything the model wrote. The load-bearing
   clause is `connect-src 'none'`: inline scripts still run (nearly every
   artifact needs them) but fetch/XHR/WebSocket/beacon are dead. A generated
   page therefore cannot phone home with whatever it just read.
3. `srcdoc`, not a `blob:` URL. A blob URL inherits the creating page's
   origin, which would quietly undo layer 1.

Live reload is `watchfiles` -> SSE -> `location.reload()` in the frame. The
prompt box POSTs into a queue; nothing here runs the agent.
"""

from __future__ import annotations

import argparse
import asyncio
import html as html_mod
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse
from watchfiles import awatch

from rcoder.artifacts import ArtifactStore, BadSlug

logger = logging.getLogger("rcoder.artifacts")

# Permissive enough that ordinary artifacts work, closed where it counts.
# 'unsafe-inline'/'unsafe-eval' are conceded because a self-contained page is
# nothing but inline script and style; exfiltration is what we actually block.
ARTIFACT_CSP = (
    "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "connect-src 'none'; "
    "form-action 'none'"
)

IFRAME_SANDBOX = "allow-scripts allow-downloads"

SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; --bg:#faf9f7; --fg:#1a1a1a; --line:#d8d4cf; --muted:#6b6b6b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16161a; --fg:#eceae6; --line:#33333a; --muted:#9a9a9a; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg); height:100dvh;
         display:flex; flex-direction:column;
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ display:flex; align-items:center; gap:.6rem; padding:.6rem .8rem;
            border-bottom:1px solid var(--line); }}
  header h1 {{ font-size:.95rem; font-weight:600; margin:0; flex:1;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  header a {{ color:var(--muted); text-decoration:none; font-size:.85rem; }}
  #dot {{ width:8px; height:8px; border-radius:50%; background:#3aa675; flex:none; }}
  #dot.stale {{ background:#c9852b; }}
  iframe {{ flex:1; width:100%; border:0; background:#fff; }}
  form {{ display:flex; gap:.5rem; padding:.6rem; border-top:1px solid var(--line);
          padding-bottom:calc(.6rem + env(safe-area-inset-bottom)); }}
  input {{ flex:1; min-width:0; padding:.7rem .8rem; font-size:16px;
           border:1px solid var(--line); border-radius:9px;
           background:var(--bg); color:var(--fg); }}
  button {{ padding:.7rem 1rem; font-size:15px; border:0; border-radius:9px;
            background:#3a6ea5; color:#fff; font-weight:600; }}
  #note {{ padding:0 .8rem .5rem; color:var(--muted); font-size:.8rem; min-height:1.2em; }}
</style></head><body>
<header><span id="dot" title="live"></span><h1>{title}</h1><a href="/">all</a></header>
<iframe sandbox="{sandbox}" srcdoc="{srcdoc}" title="{title}"></iframe>
<div id="note"></div>
<form method="post" action="/a/{slug}/ask">
  <input name="prompt" placeholder="Ask for a change..." autocomplete="off" required>
  <button type="submit">Send</button>
</form>
<script>
  const dot = document.getElementById('dot');
  let es;
  function connect() {{
    es = new EventSource('/a/{slug}/sse');
    es.addEventListener('changed', () => location.reload());
    es.onopen = () => dot.classList.remove('stale');
    es.onerror = () => {{ dot.classList.add('stale'); es.close(); setTimeout(connect, 3000); }};
  }}
  connect();
</script>
</body></html>
"""

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Artifacts</title>
<style>
  :root {{ color-scheme: light dark; --bg:#faf9f7; --fg:#1a1a1a; --line:#d8d4cf; --muted:#6b6b6b; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#16161a; --fg:#eceae6; --line:#33333a; --muted:#9a9a9a; }}
  }}
  body {{ margin:0; padding:1rem; background:var(--bg); color:var(--fg);
         font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ font-size:1.1rem; }}
  ul {{ list-style:none; padding:0; }}
  li {{ border-bottom:1px solid var(--line); }}
  a {{ display:block; padding:.85rem .2rem; color:inherit; text-decoration:none; }}
  small {{ color:var(--muted); }}
</style></head><body>
<h1>Artifacts</h1>
<ul>{items}</ul>
</body></html>
"""


def inject_csp(document: str) -> str:
    """Put our CSP first in <head> so it beats anything the model wrote.

    If there is no <head> at all we prepend a minimal one; a fragment is a
    perfectly normal thing for a model to emit and must still be governed.
    """
    meta = f'<meta http-equiv="Content-Security-Policy" content="{ARTIFACT_CSP}">'
    lowered = document.lower()
    index = lowered.find("<head")
    if index == -1:
        return f"<!doctype html><html><head>{meta}</head><body>{document}</body></html>"
    close = lowered.find(">", index)
    return document[: close + 1] + meta + document[close + 1 :]


def build_app(store: ArtifactStore) -> FastAPI:
    app = FastAPI(title="remote-coder artifacts", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        artifacts = store.list()
        if not artifacts:
            items = "<li><a>Nothing yet. The agent has not written an artifact.</a></li>"
        else:
            items = "".join(
                '<li><a href="/a/{slug}">{title}<br><small>{slug}</small></a></li>'.format(
                    slug=html_mod.escape(a.slug), title=html_mod.escape(a.title)
                )
                for a in artifacts
            )
        return INDEX.format(items=items)

    @app.get("/a/{slug}", response_class=HTMLResponse)
    async def view(slug: str) -> str:
        artifact = _lookup(store, slug)
        # Escaping for the srcdoc ATTRIBUTE. html.escape(quote=True) covers
        # & < > " ' -- so the document cannot break out of the attribute and
        # inject markup into the shell page.
        srcdoc = html_mod.escape(inject_csp(artifact.html), quote=True)
        return SHELL.format(
            title=html_mod.escape(artifact.title),
            slug=html_mod.escape(artifact.slug),
            sandbox=IFRAME_SANDBOX,
            srcdoc=srcdoc,
        )

    @app.get("/a/{slug}/sse")
    async def sse(slug: str, request: Request) -> EventSourceResponse:
        artifact = _lookup(store, slug)
        directory = artifact.path.parent

        async def events():
            try:
                async for _changes in awatch(directory, stop_event=None):
                    if await request.is_disconnected():
                        break
                    yield {"event": "changed", "data": "1"}
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reload is a nicety, never fatal
                logger.exception("Watcher stopped for %s", slug)

        return EventSourceResponse(events())

    @app.post("/a/{slug}/ask")
    async def ask(slug: str, prompt: str = Form(...)) -> RedirectResponse:
        _lookup(store, slug)
        text = prompt.strip()
        if text:
            request_id = store.enqueue(slug, text)
            logger.info("Queued request %d for %s: %s", request_id, slug, text[:80])
        # 303 so a refresh does not re-post the same request.
        return RedirectResponse(f"/a/{slug}", status_code=303)

    return app


def _lookup(store: ArtifactStore, slug: str):
    try:
        artifact = store.get(slug)
    except BadSlug as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"No artifact named '{slug}'")
    return artifact


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="artifacts", help="artifact directory")
    ap.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to reach it from the tailnet/LAN")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    store = ArtifactStore(Path(args.root).resolve())
    logger.info("Artifacts at %s -> http://%s:%d", store.root, args.host, args.port)
    uvicorn.run(build_app(store), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
