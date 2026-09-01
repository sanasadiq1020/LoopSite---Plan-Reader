from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.logging_setup import get_logger
from app.paths import ensure_core_dirs
from app.routers import plan
from app.settings import allowed_origins, describe
from pipeline.plan.intake import load_release_info

logger = get_logger()
ensure_core_dirs()

app = FastAPI(
    title="LoopSite Plan Reader API",
    # The one place the release is stated is /config/version.json. An API that
    # advertises a different version from the product is a bug report waiting
    # to be filed.
    version=load_release_info().get("version", "unknown"),
)

def cross_origin_settings() -> dict:
    """Which browser origins may call this API, and what they may see.

    Returned rather than applied, because a host may create the application
    object itself — a Space starts one through its own toolkit — and middleware
    can only be given to an application as it is built, never added afterwards.
    Both ways of starting therefore ask this one function.

    The allowed origin comes from the environment, so the deployed address is a
    setting rather than a code change. Never "*": this API sends a session
    cookie, and a wildcard origin with credentials is both refused by browsers
    and wrong.
    """
    return {
        "allow_origins": allowed_origins(),
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        # A browser hides response headers from page scripts unless they are
        # named here. The interface needs both: the filename of a download, and
        # the session it has been given.
        "expose_headers": ["Content-Disposition", "X-Session-Id"],
    }


def add_the_api_to(application) -> None:
    """Puts every route this API publishes onto an application.

    The application is usually the one created just below. It is not always:
    where a host insists on creating its own, the routes go onto that one, and
    this is the only description of what "the API" consists of.
    """
    application.include_router(plan.router)

    # No blanket static file mount: every output file — page images, overlays,
    # model and export downloads — is served through an authenticated route in
    # its router instead, so ownership is checked before any bytes go out.

    @application.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}


app.add_middleware(CORSMiddleware, **cross_origin_settings())

logger.info(f"starting with {describe()}")

add_the_api_to(app)


@app.on_event("startup")
async def on_startup() -> None:
    logger.info("LoopSite backend started.")
