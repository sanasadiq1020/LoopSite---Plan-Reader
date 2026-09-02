"""The reading is compressed on the way to the browser; the images are not.

A plan set's reading is over a megabyte of JSON and the results screen has
nothing to show until it arrives — that is the pause after the progress bar
reaches the end. It compresses about ten times over. A marked-up sheet is a
PNG and is compressed already, so putting it through gzip costs time to save
almost nothing.

These run against Starlette directly rather than through the application,
because the decision being tested is the middleware's own and the point is to
exercise it here rather than find out on a deployment (see the note in
CLAUDE.md about a check that passes because the machine lacks something).
"""

import gzip

import pytest

starlette = pytest.importorskip("starlette")
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.compression import CompressWhatIsWorthCompressing  # noqa: E402


BIG = {"rows": [{"wall_id": f"A02-W{n:03d}", "length_mm": 1234.5} for n in range(400)]}


def _client():
    async def reading(request):
        return JSONResponse(BIG)

    async def sheet(request):
        return Response(b"\x89PNG\r\n" + b"\x00" * 40000, media_type="image/png")

    app = Starlette(routes=[
        Route("/api/plan/r1/reading", reading),
        Route("/api/plan/r1/overlays/overlay_004.png", sheet),
    ])
    return TestClient(CompressWhatIsWorthCompressing(app))


def test_the_reading_is_compressed_on_the_way_to_the_browser():
    response = _client().get(
        "/api/plan/r1/reading", headers={"accept-encoding": "gzip"}
    )
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    # And it is still the reading once unpacked.
    assert response.json()["rows"][0]["wall_id"] == "A02-W000"
    assert int(response.headers["content-length"]) < len(
        response.content.decode()
    ) / 4


def test_a_marked_up_sheet_is_sent_as_it_is():
    """A PNG is compressed already: gzip costs 36 ms to save two per cent."""
    response = _client().get(
        "/api/plan/r1/overlays/overlay_004.png", headers={"accept-encoding": "gzip"}
    )
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_a_browser_that_asks_for_no_compression_gets_none():
    response = _client().get("/api/plan/r1/reading", headers={"accept-encoding": ""})
    assert "content-encoding" not in response.headers
    assert response.json()["rows"][0]["wall_id"] == "A02-W000"
