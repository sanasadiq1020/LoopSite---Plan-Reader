"""What is worth compressing on the way to the browser, and what is not.

**The reading is the thing the reader waits on.** A plan set's reading is over
a megabyte of JSON, and until it has arrived the results screen has nothing to
show — which is exactly the pause after the progress bar reaches the end. It is
almost all repeated field names and numbers, so it compresses about ten times
over: measured, 298 KB goes to 31 KB in six milliseconds.

**A marked-up sheet is not.** It is a PNG, and a bundle of them is a zip; both
are compressed already. Measured, putting an 800 KB sheet through gzip costs 36
milliseconds to save two per cent, on every sheet a reader opens.

So the decision is made on what the file is, before the work is done. It lives
in its own module, with no dependency on the web framework, so that it can be
exercised by a test on this machine rather than found out on a deployment —
which is the lesson of the four failed deployments recorded in CLAUDE.md.
"""

from starlette.middleware.gzip import GZipMiddleware

# Anything already compressed. Nothing is gained by doing it twice, and the
# time it takes is paid on every sheet a reader opens.
ALREADY_COMPRESSED = (".png", ".jpg", ".jpeg", ".zip", ".gz", ".glb")


class CompressWhatIsWorthCompressing:
    """gzip for the data the browser waits on, and nothing for the pictures."""

    def __init__(self, app, minimum_size: int = 1024, compresslevel: int = 5):
        self._plain = app
        self._compressed = GZipMiddleware(
            app, minimum_size=minimum_size, compresslevel=compresslevel
        )

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope.get("type") == "http" else ""
        if not path or path.lower().endswith(ALREADY_COMPRESSED):
            await self._plain(scope, receive, send)
            return
        await self._compressed(scope, receive, send)
