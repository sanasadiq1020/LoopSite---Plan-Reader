"""Entry point for the API when it is hosted on a Space.

**Why this file exists.** A Space runs one Python file. Everywhere else — a
laptop, a container platform — the API is started by pointing a server at
``app.main:app`` directly, and that is still what happens; this file only puts
the backend on the import path and starts the same server on the port a Space
expects to find it on. There is no second copy of the application here, and
nothing in the application knows it is running on a Space.

**Nothing but the API is served.** An earlier version mounted a page built with
the Space's own toolkit at ``/``, which failed on the Space and worked on a
laptop. Mounting that toolkit's app takes a port of its own — its default is
the very port the API is served on — so the API could never bind it:

    ERROR: [Errno 98] error while attempting to bind on address
           ('0.0.0.0', 7860): address already in use

A Space shows whatever is served at ``/``, and that is worth a page, so the
page is a few lines of plain HTML written here instead. It brings in nothing,
takes no port and cannot fail. The routes it sits beside are the API's own, and
each of those still checks the session before returning anybody's plan.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The recognition library's default CPU acceleration crashes on its own models
# on some machines. Set before anything imports it, as it is on every other
# way of starting this application.
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "False")

from fastapi.responses import HTMLResponse  # noqa: E402

from app.main import app as api  # noqa: E402

_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoopSite Plan Reader — API</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; padding: 3rem 1.5rem; font: 16px/1.65 system-ui, sans-serif;
         max-width: 42rem; margin-inline: auto; }}
  h1 {{ font-size: 1.4rem; margin: 0 0 .25rem; }}
  p.lead {{ margin: 0 0 1.5rem; opacity: .75; }}
  code {{ font-size: .9em; }}
  ul {{ padding-left: 1.1rem; }}
  .note {{ margin-top: 2rem; font-size: .875rem; opacity: .7; }}
</style>
<h1>LoopSite Plan Reader — API</h1>
<p class="lead">Release {version}</p>
<p>This address is the <strong>service behind the interface</strong>, not the
interface itself. There is nothing to use on this page: open the site you were
given a link to, and it will call this.</p>
<ul>
  <li><code><a href="/api/plan/health">/api/plan/health</a></code> — whether the
      service is up, and how many plans it is reading right now</li>
  <li><code><a href="/api/plan/release">/api/plan/release</a></code> — what this
      release does, and what it does not</li>
</ul>
<p class="note">Every route that returns a plan checks the session that uploaded
it, so nothing here lets one visitor read another's drawings.</p>
"""


@api.get("/", include_in_schema=False)
async def what_this_is() -> HTMLResponse:
    """A short page saying what this address is.

    Declared last, so it answers only for ``/`` and never stands in front of a
    route the API already publishes.
    """
    try:
        version = api.version
    except Exception:
        version = "unknown"
    return HTMLResponse(_PAGE.format(version=version))


if __name__ == "__main__":
    import uvicorn

    # One worker on purpose: a run's files are written to this container's own
    # disk and read back by the same process, so a second worker would answer
    # for uploads it cannot see.
    uvicorn.run(
        api,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7860)),
        workers=1,
        timeout_keep_alive=120,
    )
