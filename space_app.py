"""Entry point for the API when it is hosted on a Space.

**Why this file exists.** A Space runs one Python file. Everywhere else — a
laptop, a container platform — the API is started by pointing a server at
``app.main:app`` directly, and that is still what happens; this file only puts
the backend on the import path and starts the same server on the port a Space
expects to find it on. There is no second copy of the application here, and
nothing in the application knows it is running on a Space.

**The landing page.** A Space shows whatever its application serves at ``/``,
and an API on its own serves nothing there. So a short page is mounted saying
what the service is and where its health check is. It is built with the toolkit
the Space provides when one is available, and skipped without complaint when it
is not — the API is the point, and it is unaffected either way.

Nothing here is reachable except the same routes the API already publishes, and
every one of those still checks the session before returning anybody's plan.
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

from app.main import app as api  # noqa: E402


def _with_a_landing_page(application):
    """The API, plus a page at ``/`` saying what it is.

    Returns the application unchanged if the toolkit is not installed. The
    page is mounted **after** the API's own routes, so it can never stand in
    front of one of them.
    """
    try:
        import gradio as gr
    except Exception:
        return application

    try:
        release = api.version
    except Exception:
        release = "unknown"

    with gr.Blocks(title="LoopSite Plan Reader API") as page:
        gr.Markdown(
            f"""
# LoopSite Plan Reader — API

This address is the **service behind the interface**, not the interface itself.
There is nothing to use on this page: open the site you were given a link to,
and it will call this.

**Release {release}**

* `/api/plan/health` — whether the service is up, and how many plans it is
  reading right now
* `/api/plan/release` — what this release does and what it does not

Every route that returns a plan checks the session that uploaded it, so nothing
here lets one visitor read another's drawings.
"""
        )

    try:
        return gr.mount_gradio_app(application, page, path="/")
    except Exception:
        # A landing page is a courtesy. It may never stop the API starting.
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
