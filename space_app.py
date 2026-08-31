"""Entry point for the API when it is hosted on a Space.

**Why this file exists.** A Space runs one Python file. Everywhere else — a
laptop, a container platform — the API is started by pointing a server at
``app.main:app`` directly, and that is still what happens; this file only puts
the backend on the import path and starts the same server on the port a Space
expects to find it on. There is no second copy of the application here, and
nothing in the application knows it is running on a Space.

**The page at ``/`` is the Space's own toolkit, with its rendering server
turned off.** Two failures taught this, and both are worth keeping:

*   Mounted with its defaults, that toolkit starts a second, Node-based
    rendering server on **the very port the API is served on**, so the API
    could never bind it::

        ERROR: [Errno 98] error while attempting to bind on address
               ('0.0.0.0', 7860): address already in use

    It worked on a laptop, where Node is not installed and the rendering
    server never starts, and failed on the Space, where the image installs
    Node. A check that passes only because the machine lacks something proves
    nothing about the machine it has to run on.

*   Serving the API alone, behind a page of plain HTML, bound the port
    perfectly and was stopped from outside moments later.

So the toolkit is used, and ``ssr_mode=False`` keeps it from taking a port.
Whatever happens to it, the API is unaffected: the mount is attempted inside a
``try`` that says out loud when it fails, and the routes it sits beside are the
API's own, each of which still checks the session before returning anybody's
plan.

**This will not run on GPU-allocating hardware, and that is not a bug to fix
here.** Such hardware hands a GPU only to functions registered through the
toolkit's own launch path, and refuses to start an application that presents
none::

    No @spaces.GPU function detected during startup

Declaring one was tried and changed nothing: the declaration was made, logged,
and the platform still reported none, because what it looks for is the
toolkit's launch collecting them — and a REST API served by its own server
never calls that. The code for it was removed rather than left in looking like
it helped. This service wants processors and memory, not a GPU; run it on a
CPU tier.
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

from app.logging_setup import get_logger  # noqa: E402
from app.main import app as api  # noqa: E402

logger = get_logger()


def _with_a_landing_page(application):
    """The API, plus a page at ``/`` saying what this address is.

    ``ssr_mode=False`` is the whole point of this function's care: with it left
    alone, the toolkit starts a rendering server on the port the API needs.

    Returns the application unchanged if anything goes wrong. A page is a
    courtesy; it may never stop the API starting.
    """
    try:
        import gradio as gr

        try:
            version = application.version
        except Exception:
            version = "unknown"

        with gr.Blocks(title="LoopSite Plan Reader API") as page:
            gr.Markdown(
                f"""
# LoopSite Plan Reader — API

This address is the **service behind the interface**, not the interface itself.
There is nothing to use on this page: open the site you were given a link to,
and it will call this.

**Release {version}**

* `/api/plan/health` — whether the service is up, and how many plans it is
  reading right now
* `/api/plan/release` — what this release does, and what it does not

Every route that returns a plan checks the session that uploaded it, so nothing
here lets one visitor read another's drawings.
"""
            )

        return gr.mount_gradio_app(application, page, path="/", ssr_mode=False)
    except Exception as e:
        # Said out loud rather than swallowed. A page that quietly failed to
        # mount looked identical, from the outside, to one that mounted fine —
        # and the difference was the whole of one wasted deployment.
        logger.warning(f"the landing page could not be mounted, so it is not shown: {e}")
        return application


if __name__ == "__main__":
    import uvicorn

    # One worker on purpose: a run's files are written to this container's own
    # disk and read back by the same process, so a second worker would answer
    # for uploads it cannot see.
    uvicorn.run(
        _with_a_landing_page(api),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 7860)),
        workers=1,
        timeout_keep_alive=120,
    )
