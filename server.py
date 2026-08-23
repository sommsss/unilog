"""Web backend for the uniLog UI.

A pipeline run takes roughly four seconds per product — a 1,000-product catalog is
over an hour — so a run cannot happen inside an HTTP request. Uploading starts a
background job and returns its id; the browser then polls for progress and, when
the job finishes, downloads the finished catalog.

    python server.py           # http://127.0.0.1:8000
"""

import os
import shutil
import threading
import time
import traceback
import uuid
from typing import Any, Dict

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from main import EXPECTED_INPUT_COLUMNS, run_pipeline, validate_input_columns
from src.config import BASE_DIR, DATA_DIR, REQUESTS_PER_MINUTE, ensure_directories
from src.logging_setup import get_logger, setup_logging

log = get_logger("server")

JOBS_DIR = os.path.join(DATA_DIR, "jobs")
UI_FILE = os.path.join(BASE_DIR, "ui", "index.html")
PUBLIC_FILE = os.path.join(BASE_DIR, "public", "index.html")

# Selectable batch sizes offered by the UI. 0 means "every product in the file".
ALLOWED_LIMITS = [5, 10, 20, 50, 100, 200, 300, 1000, 0]

MAX_UPLOAD_BYTES = 32 * 1024 * 1024

app = FastAPI(title="uniLog", docs_url=None, redoc_url=None)

# job_id -> job state. Single-process, in-memory: restarting the server clears it.
JOBS: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def _set(job_id: str, **fields: Any) -> None:
    with _LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def _eta_seconds(remaining: int, rpm: int) -> int:
    """Extraction is rate-limited, so time left is a function of products left."""
    if remaining <= 0 or rpm <= 0:
        return 0
    return int(remaining * (60.0 / rpm))


def _run_job(job_id: str, limit: int, use_ai: bool, fetch: bool, rpm: int) -> None:
    """Executed on a worker thread. Never raises — failures land in the job state."""
    work_dir = _job_dir(job_id)
    started = time.monotonic()

    def progress(stage: int, name: str, message: str, done: int = 0, total: int = 0) -> None:
        payload: Dict[str, Any] = {
            "stage": stage,
            "stage_name": name,
            "message": message,
            "done": done,
            "total": total,
            "elapsed": int(time.monotonic() - started),
        }
        # Only Stage 4 has a meaningful ETA; the other stages are near-instant.
        if stage == 4 and total and use_ai:
            payload["eta"] = _eta_seconds(total - done, rpm)
        _set(job_id, **payload)

    try:
        _set(job_id, state="running")
        summary = run_pipeline(
            limit=limit,
            fetch=fetch,
            use_ai=use_ai,
            rpm=rpm,
            input_path=os.path.join(work_dir, "input.csv"),
            work_dir=work_dir,
            progress=progress,
        )
        _set(job_id, state="done", summary=summary, eta=0,
             elapsed=int(time.monotonic() - started))
        log.info("job %s finished: %s", job_id, summary)

    except Exception as exc:  # noqa: BLE001 — surface any failure to the browser
        log.error("job %s failed: %s", job_id, traceback.format_exc())
        _set(job_id, state="error", error=str(exc), eta=0,
             elapsed=int(time.monotonic() - started))


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """Serve the built page when it exists, otherwise the source fragment."""
    path = PUBLIC_FILE if os.path.exists(PUBLIC_FILE) else UI_FILE
    with open(path, encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


@app.get("/api/config")
def config() -> JSONResponse:
    return JSONResponse({
        "limits": ALLOWED_LIMITS,
        "rpm": REQUESTS_PER_MINUTE,
        "expected_columns": EXPECTED_INPUT_COLUMNS,
        "seconds_per_product": round(60.0 / REQUESTS_PER_MINUTE, 1),
    })


@app.post("/api/run")
async def start_run(
    file: UploadFile = File(...),
    limit: int = Form(0),
    use_ai: bool = Form(True),
    fetch: bool = Form(False),
) -> JSONResponse:
    if limit not in ALLOWED_LIMITS:
        raise HTTPException(400, "Unsupported batch size %s. Choose one of: %s."
                            % (limit, ", ".join(str(x) for x in ALLOWED_LIMITS)))

    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, "uniLog reads CSV. Save the export as .csv and upload it again.")

    job_id = uuid.uuid4().hex[:12]
    work_dir = _job_dir(job_id)
    os.makedirs(work_dir, exist_ok=True)
    input_path = os.path.join(work_dir, "input.csv")

    size = 0
    with open(input_path, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                shutil.rmtree(work_dir, ignore_errors=True)
                raise HTTPException(
                    413, "That file is larger than %d MB. Split the export and upload it in parts."
                    % (MAX_UPLOAD_BYTES // (1024 * 1024))
                )
            out.write(chunk)

    # Fail fast on a malformed file, before a worker thread or an API call is spent.
    try:
        head = pd.read_csv(input_path, nrows=5)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(400, "That file could not be read as CSV (%s). "
                                 "Re-export it and try again." % str(exc)[:120])

    missing = validate_input_columns(head)
    if missing:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(
            400,
            "Missing required column(s): %s. uniLog reads a supplier export with %s."
            % (", ".join(missing), ", ".join(EXPECTED_INPUT_COLUMNS))
        )

    total_rows = int(sum(1 for _ in open(input_path, encoding="utf-8", errors="replace")) - 1)
    planned = total_rows if limit == 0 else min(limit, total_rows)

    with _LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "state": "queued",
            "filename": file.filename,
            "limit": limit,
            "planned": planned,
            "total_rows": total_rows,
            "stage": 0,
            "stage_name": "Queued",
            "message": "Starting",
            "done": 0,
            "total": planned,
            "elapsed": 0,
            "eta": _eta_seconds(planned, rpm=REQUESTS_PER_MINUTE) if use_ai else 0,
            "error": None,
            "summary": None,
        }

    threading.Thread(
        target=_run_job,
        args=(job_id, limit, use_ai, fetch, REQUESTS_PER_MINUTE),
        daemon=True,
    ).start()

    log.info("job %s queued: %s, %d of %d products", job_id, file.filename, planned, total_rows)
    return JSONResponse({"job_id": job_id, "planned": planned, "total_rows": total_rows})


@app.get("/api/status/{job_id}")
def status(job_id: str) -> JSONResponse:
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job. It may have finished before the server restarted.")
    return JSONResponse(job)


@app.get("/api/result/{job_id}")
def result(job_id: str) -> JSONResponse:
    """Preview rows and the routing split, once a job is done."""
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if job["state"] != "done":
        raise HTTPException(409, "That run is still %s." % job["state"])

    summary = job["summary"]
    catalog = pd.read_csv(summary["output_path"], keep_default_na=False, nrows=25)

    # Show the columns that carry the enrichment, not all 252.
    preview_cols = [
        "Mfg_Part_Num", "BRAND_NAME", "MANUFACTURER_NAME", "Product Name", "Classpath",
        "SHORT_DESC", "ATTRIBUTE_LABEL 1", "ATTRIBUTE_VALUE 1", "ATTRIBUTE_UOM 1",
    ]
    preview_cols = [c for c in preview_cols if c in catalog.columns]

    validation = pd.read_csv(summary["validation_path"], keep_default_na=False)
    worst = validation.sort_values("quality_score").head(5)

    return JSONResponse({
        "summary": summary,
        "preview_columns": preview_cols,
        "preview_rows": catalog[preview_cols].head(10).astype(str).values.tolist(),
        "flagged": worst[["product_id", "quality_score", "status", "review_reasons"]]
                       .astype(str).values.tolist(),
    })


@app.get("/api/download/{job_id}")
def download(job_id: str) -> FileResponse:
    with _LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    if job["state"] != "done":
        raise HTTPException(409, "That run is still %s." % job["state"])

    path = job["summary"]["output_path"]
    if not os.path.exists(path):
        raise HTTPException(404, "The output file is gone from disk.")
    return FileResponse(path, media_type="text/csv", filename="FINAL_MASTER_CATALOG.csv")


@app.get("/api/report/{job_id}")
def report(job_id: str) -> FileResponse:
    """The quality dashboard — one row per product with score and reasons."""
    with _LOCK:
        job = JOBS.get(job_id)
    if not job or job["state"] != "done":
        raise HTTPException(404, "No finished job with that id.")
    return FileResponse(job["summary"]["validation_path"], media_type="text/csv",
                        filename="validation_report.csv")


if __name__ == "__main__":
    import uvicorn

    ensure_directories()
    os.makedirs(JOBS_DIR, exist_ok=True)
    setup_logging()
    log.info("uniLog server on http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
