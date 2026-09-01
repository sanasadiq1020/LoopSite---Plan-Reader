"""Entry point for the API when it is hosted on a Space.

**Why this file exists.** A Space runs one Python file. Everywhere else — a
laptop, a container platform — the API is started by pointing a server at
``app.main:app``, and that is still what happens here on any ordinary host.
There is no second copy of the application: the routes come from
``app.main.add_the_api_to`` and the browser-origin rules from
``app.main.cross_origin_settings``, which are the same two things the ordinary
way uses.

**On a Space, the toolkit owns the server.** That is the part worth explaining,
because four deployments failed before it was understood.

A Space of this kind allocates a GPU only to functions decorated for it, and
**refuses to start an application that presents none**::

    stage:   RUNTIME_ERROR
    message: No @spaces.GPU function detected during startup

Declaring one is not enough — that was tried, logged, and still reported as
none. What the platform looks for is the **toolkit's own launch** collecting
them, and an API served by its own server never calls that. So on a Space the
toolkit launches the server, and the API's routes are added to the application
it creates. Everything is on one port: the toolkit's page at ``/``, and every
API route beside it.

Two details that are easy to get wrong and cost a deployment each:

*   **Middleware cannot be added to an application after it has started**, so
    the browser-origin rules are handed to the toolkit as it builds the
    application, through ``app_kwargs``. Without them every request from the
    interface is refused by the browser and the screen simply never loads.
*   **``ssr_mode=False``.** Left alone, the toolkit starts a second, Node-based
    rendering server on the very port the API is served on, and the API can
    then never bind it. It worked on a laptop, where Node is not installed, and
    failed on the Space, whose image installs it.

**What the GPU is asked for.** Character recognition — reading the lettering
off a drawing stored as a picture — is the only work here a GPU could speed up;
a dense scanned sheet takes minutes on a processor. Everything else is reading
PDFs, measuring line work and writing files. So that is what the Space's own
page offers, and it is the same recognition the reader runs on a sheet that
carries no text of its own. Whether a GPU is actually used depends on which
build of the recognition library is installed; the work is real either way.
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
from app.main import add_the_api_to, app as api, cross_origin_settings  # noqa: E402

logger = get_logger()

_INTRO = """
# LoopSite Plan Reader — API

This address is the **service behind the interface**, not the interface itself.
Open the site you were given a link to, and it will call this.

* `/api/plan/health` — whether the service is up, and how many plans it is
  reading right now
* `/api/plan/release` — what this release does, and what it does not

Every route that returns a plan checks the session that uploaded it, so nothing
here lets one visitor read another's drawings.

---

### Read the lettering off a drawing

The one piece of work here that a GPU can speed up, offered on its own. This is
the same character recognition the reader uses on a sheet carrying no text of
its own — a scan, or a drawing exported as a picture.
"""


def read_the_lettering(image_path) -> str:
    """The text recognised on one drawing image, one line per piece of text.

    A real piece of this application rather than a demonstration: it is the
    recognition step, called exactly as the plan reader calls it.
    """
    if not image_path:
        return "Choose a drawing image first."
    try:
        from pipeline.plan.ocr import run_ocr_on_page

        result = run_ocr_on_page(Path(image_path))
        if result["status"] != "ok":
            return result.get("error") or "Nothing could be read from that image."
        lines = [block["text"] for block in result["blocks"] if block.get("text")]
        if not lines:
            return "No lettering was found in that image."
        return "\n".join(lines)
    except Exception as e:
        logger.exception(f"reading the lettering failed: {e}")
        return f"That image could not be read: {e}"


def _asking_for_a_gpu(work):
    """The same function, marked as wanting a GPU where the platform offers one.

    Returned untouched anywhere else, so nothing about running this on an
    ordinary machine changes.
    """
    try:
        import spaces
    except Exception:
        return work
    try:
        return spaces.GPU(duration=120)(work)
    except Exception as e:
        logger.warning(f"the GPU declaration could not be made: {e}")
        return work


def build_the_page():
    """The Space's own page: what this address is, and the recognition step."""
    import gradio as gr

    with gr.Blocks(title="LoopSite Plan Reader API") as page:
        gr.Markdown(_INTRO)
        drawing = gr.Image(type="filepath", label="A drawing stored as a picture")
        found = gr.Textbox(label="What was read", lines=12, max_lines=30)
        gr.Button("Read the lettering", variant="primary").click(
            _asking_for_a_gpu(read_the_lettering), inputs=drawing, outputs=found
        )
    return page


def serve_through_the_toolkit(port: int) -> None:
    """Lets the toolkit start the server, and puts the API on it."""
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware import Middleware

    page = build_the_page()
    page.launch(
        server_name="0.0.0.0",
        server_port=port,
        # Otherwise a second, Node-based rendering server takes this very port.
        ssr_mode=False,
        prevent_thread_lock=True,
        # Middleware can only be given to an application as it is built.
        app_kwargs={
            "middleware": [Middleware(CORSMiddleware, **cross_origin_settings())]
        },
    )
    add_the_api_to(page.app)
    logger.info(f"the API is being served by the toolkit on port {port}")
    page.block_thread()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))

    if os.environ.get("SPACE_ID"):
        serve_through_the_toolkit(port)
    else:
        import uvicorn

        # One worker on purpose: a run's files are written to this container's
        # own disk and read back by the same process, so a second worker would
        # answer for uploads it cannot see.
        uvicorn.run(api, host="0.0.0.0", port=port, workers=1, timeout_keep_alive=120)
