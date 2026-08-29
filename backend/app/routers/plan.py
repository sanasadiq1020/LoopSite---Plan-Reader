from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.logging_setup import get_logger
from app.schemas import PlanReadingResponse, UploadResponse
from app import progress
from app.paths import discard_other_runs, run_plan_dir
from pipeline.model.build import (
    available_sheets,
    build_for_sheet,
    load_model,
    model_paths,
)
from app.session import get_session_id
from pipeline.plan.intake import (
    build_overlays_zip,
    load_config,
    load_manifest,
    load_plan_reading,
    load_release_info,
    plan_reading_is_outdated,
    process_upload,
    resolve_export_path,
    resolve_overlay_image_path,
    resolve_page_image_path,
    resolve_sheet_register_csv_path,
    resolve_unresolved_csv_path,
    run_belongs_to_session,
)

router = APIRouter(prefix="/api/plan", tags=["plan"])
logger = get_logger()


@router.post("/upload", response_model=UploadResponse)
async def upload_plan(
    file: UploadFile = File(...),
    token: str = "",
    session_id: str = Depends(get_session_id),
) -> UploadResponse:
    if file.content_type not in ("application/pdf", "application/octet-stream") and not (
        file.filename or ""
    ).lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted.")

    try:
        file_bytes = await file.read()
        if not file_bytes:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        # process_upload is synchronous/CPU-bound (PDF rendering, OCR) — running
        # it directly here would block the whole async event loop, freezing
        # every other request (even /api/health) until it finishes. Offloading
        # to a worker thread keeps the server responsive during long OCR runs.
        progress.start(token, file.filename or "unnamed.pdf")
        result = await run_in_threadpool(
            process_upload,
            file_bytes,
            file.filename or "unnamed.pdf",
            session_id,
            token,
        )
        return UploadResponse(**result)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        progress.fail(token, "This plan could not be read.")
        logger.exception(f"Unexpected error processing upload: {e}")
        raise HTTPException(
            status_code=500, detail="Could not process this PDF. See server logs."
        ) from e


@router.get("/{run_id}/reading", response_model=PlanReadingResponse)
async def get_plan_reading(
    run_id: str, session_id: str = Depends(get_session_id)
) -> PlanReadingResponse:
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail=f"No run found for run_id={run_id}")

    reading = load_plan_reading(run_id)
    if reading is None:
        if plan_reading_is_outdated(run_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "This upload was processed by an earlier version of the plan reader. "
                    "Upload the PDF again to see the current results."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="No extracted data is available for this upload.",
        )
    return PlanReadingResponse(**reading)


@router.get("/{run_id}/sheet-register.csv")
async def get_sheet_register_csv(
    run_id: str, session_id: str = Depends(get_session_id)
):
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_sheet_register_csv_path(run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")

    return FileResponse(
        path, media_type="text/csv", filename=f"sheet_register_{run_id}.csv"
    )


@router.get("/{run_id}/overlays/{filename}")
async def get_overlay_image(
    run_id: str,
    filename: str,
    download: bool = False,
    session_id: str = Depends(get_session_id),
):
    """The marked-up sheet for one page (Week 1 Gate 8).

    ``download=true`` returns it as an attachment. The interface runs on a
    different port from the API, and a browser ignores an ``<a download>`` that
    points at another origin — the image opened in place instead, replacing the
    results. An explicit attachment disposition makes it download wherever it is
    opened from, so nothing navigates away.

    Served through an authenticated route like every other output; there is
    deliberately no blanket static mount, so one session can never read
    another's plans.
    """
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_overlay_image_path(run_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")

    if download:
        return FileResponse(
            path, media_type="image/png", filename=f"marked_up_{run_id}_{filename}"
        )
    return FileResponse(path, media_type="image/png")


# The extracted tables, downloadable as CSV so results can be checked in a
# spreadsheet without going through the interface or writing any code.
_EXPORTS = {
    "rooms": "rooms.csv",
    "dimensions": "dimensions.csv",
    "schedule-rows": "schedule_rows.csv",
    "walls": "walls.csv",
    "openings": "openings.csv",
}


@router.get("/{run_id}/export/{name}.csv")
async def get_export_csv(
    run_id: str, name: str, session_id: str = Depends(get_session_id)
):
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    filename = _EXPORTS.get(name)
    if filename is None:
        raise HTTPException(status_code=404, detail="Unknown export.")

    path = resolve_export_path(run_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")

    return FileResponse(path, media_type="text/csv", filename=f"{name}_{run_id}.csv")


@router.get("/{run_id}/accuracy-report.csv")
async def get_accuracy_report_csv(run_id: str, session_id: str = Depends(get_session_id)):
    """The row-by-row comparison of this run against the manually checked
    reference, including which rows a person has actually confirmed."""
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_export_path(run_id, "accuracy_report.csv")
    if path is None:
        raise HTTPException(
            status_code=404,
            detail="No accuracy report — no ground truth has been recorded yet.",
        )
    return FileResponse(path, media_type="text/csv", filename=f"accuracy_{run_id}.csv")


@router.get("/{run_id}/ground-truth-template.csv")
async def get_ground_truth_template(run_id: str, session_id: str = Depends(get_session_id)):
    """A reference file for this plan, pre-filled from the run.

    Every row carries what the reader read, where on the sheet to look for it,
    and how it was read — so confirming a row is a glance at the drawing rather
    than a search. Rows count towards accuracy only once a name is written in
    the checked_by column.
    """
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_export_path(run_id, "ground_truth_template.csv")
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return FileResponse(
        path, media_type="text/csv", filename=f"ground_truth_{run_id}.csv"
    )


@router.get("/{run_id}/issues-log.csv")
async def get_issues_log_csv(run_id: str, session_id: str = Depends(get_session_id)):
    """The full issues log for a run.

    Every item the reader flagged, with the sheet, the internal category and
    severity, the wording and the position on the sheet. These are a tracing
    tool, which is why they live in a downloadable file rather than on screen.
    """
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_unresolved_csv_path(run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")

    return FileResponse(path, media_type="text/csv", filename=f"issues_log_{run_id}.csv")


@router.get("/{run_id}/marked-up-sheets.zip")
async def get_overlays_zip(run_id: str, session_id: str = Depends(get_session_id)):
    """Every marked-up sheet for this plan, as one download."""
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = await run_in_threadpool(build_overlays_zip, run_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No marked-up sheets for this plan.")

    return FileResponse(
        path, media_type="application/zip", filename=f"marked_up_sheets_{run_id}.zip"
    )


@router.get("/{run_id}/pages/{filename}")
async def get_page_image(
    run_id: str, filename: str, session_id: str = Depends(get_session_id)
):
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")

    path = resolve_page_image_path(run_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="Not found.")

    return FileResponse(path, media_type="image/png")

@router.get("/progress/{token}")
async def get_progress(token: str, session_id: str = Depends(get_session_id)):
    """How far the upload carrying this token has got.

    The browser brings its own token to the upload and asks about it while the
    upload request is still in flight, so the reader sees which sheet is being
    read rather than a spinner that says nothing. Reporting progress reveals
    nothing about the plan itself, so it needs no more than a valid session.
    """
    record = progress.read(token)
    if record is None:
        # Either the upload has not reached the server yet or the record has
        # been forgotten. Neither is an error; the browser simply asks again.
        return {"known": False}
    return {"known": True, **record}

@router.post("/{run_id}/discard")
async def discard_run(run_id: str, session_id: str = Depends(get_session_id)):
    """Removes a run the reader has finished with.

    The interface shows one plan at a time and keeps no history, so a run that
    is no longer on screen — the page was refreshed, or another plan was
    uploaded — has nothing left to show. Its folder goes with it.

    A session can only discard its own run, the same as every other route
    here: knowing a run id is never enough to touch someone else's plan.
    """
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")
    removed = discard_other_runs(keep_run_id="__none__")
    logger.info(f"run={run_id} discarded by the reader ({removed} folder(s) removed)")
    return {"discarded": True}

# --- Day 5: the canonical model and its 3D files -------------------------


def _run_dir_for(run_id: str, session_id: str):
    """The run's folder, once this session has been shown to own it."""
    if not run_belongs_to_session(run_id, session_id):
        raise HTTPException(status_code=404, detail="Not found.")
    return run_plan_dir(run_id)


def _reading_for(run_id: str):
    reading = load_plan_reading(run_id)
    if reading is None:
        raise HTTPException(
            status_code=404, detail="No extracted data is available for this upload."
        )
    return reading


@router.get("/{run_id}/model/sheets")
async def get_modellable_sheets(run_id: str, session_id: str = Depends(get_session_id)):
    """Which sheets a 3D model can be built from, and which is offered first.

    A sheet that cannot be modelled is listed with the reason rather than
    left out, so a reader can see why their floor plan is not on the list.
    """
    _run_dir_for(run_id, session_id)
    reading = _reading_for(run_id)
    return available_sheets(reading, load_config())


@router.post("/{run_id}/model/{page_number}")
async def build_sheet_model(
    run_id: str, page_number: int, session_id: str = Depends(get_session_id)
):
    """Builds the canonical model and the 3D files for one sheet.

    Built on request rather than during the upload, because the reader chooses
    the sheet: a plan set often draws the same outline three times over and
    only the reader knows which one they want.
    """
    run_dir = _run_dir_for(run_id, session_id)
    reading = _reading_for(run_id)
    manifest = load_manifest(run_id) or {}
    try:
        model = await run_in_threadpool(
            build_for_sheet,
            run_dir,
            reading,
            page_number,
            load_config(),
            run_id,
            manifest.get("original_filename", ""),
        )
        return model
    except ValueError as e:
        # A sheet that cannot be modelled says why, in the reader's words.
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"run={run_id} could not build the model for page {page_number}: {e}")
        raise HTTPException(
            status_code=500, detail="This sheet's model could not be built."
        ) from e


@router.get("/{run_id}/model/{page_number}")
async def get_sheet_model(
    run_id: str, page_number: int, session_id: str = Depends(get_session_id)
):
    """A model already built for this sheet."""
    run_dir = _run_dir_for(run_id, session_id)
    model = load_model(run_dir, page_number)
    if model is None:
        raise HTTPException(
            status_code=404, detail="No model has been built for this sheet yet."
        )
    return model


@router.get("/{run_id}/model/{page_number}/file/{kind}")
async def get_model_file(
    run_id: str,
    page_number: int,
    kind: str,
    download: bool = False,
    session_id: str = Depends(get_session_id),
):
    """One of a sheet's 3D files.

    ``glb`` is what the browser viewer loads, so it is served inline; the rest
    are offered as downloads. Served through an authenticated route like every
    other output — there is deliberately no blanket static mount.
    """
    run_dir = _run_dir_for(run_id, session_id)
    if kind not in ("glb", "obj", "ifc", "json"):
        raise HTTPException(status_code=404, detail="Not found.")

    path = model_paths(run_dir, page_number)[kind]
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="This file has not been built for this sheet."
        )

    media_types = {
        "glb": "model/gltf-binary",
        "obj": "text/plain",
        "ifc": "application/x-step",
        "json": "application/json",
    }
    sheet = f"page{page_number:03d}"
    if download or kind != "glb":
        return FileResponse(
            path,
            media_type=media_types[kind],
            filename=f"loopsite_model_{run_id}_{sheet}.{kind}",
        )
    return FileResponse(path, media_type=media_types[kind])

@router.get("/release")
async def get_release():
    """What this release is and what it cannot do.

    Read from ``/config/version.json`` on every request rather than compiled
    into the interface, so what the product claims about itself lives in one
    file and changing it never means editing a screen.

    No session is required: this describes the software, not anybody's plan.
    """
    info = load_release_info()
    if not info:
        raise HTTPException(
            status_code=503,
            detail="This build cannot read its own release information.",
        )
    return info

@router.get("/session")
async def get_session(session_id: str = Depends(get_session_id)):
    """The session this browser is using, creating one if it has none.

    The interface asks once, keeps the answer, and presents it on every
    request afterwards. That is what makes the tool work in a browser that
    blocks third-party cookies — which is now most of them, and which
    otherwise fails silently and completely.

    The identifier is anonymous and grants nothing beyond the plan uploaded
    under it.
    """
    return {"session_id": session_id}

