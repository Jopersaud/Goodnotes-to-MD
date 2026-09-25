"""FastAPI backend for the GoodNotes -> Markdown converter.

Run with:  uvicorn server:app --reload
Then open: http://localhost:8000

Make sure `ANTHROPIC_API_KEY` is not set in the shell you start this from - see
the README. The server strips it from the Claude Code subprocess either way, but
an unset shell is one less thing to get wrong.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import date as date_cls
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import claude_client
import config
import note_store
import page_prep
import pdf_export
import usage_log
from claude_client import ConversionError, ProgressEvent
from page_prep import PagePrepError

app = FastAPI(title="GoodNotes to Markdown")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")
_PAGE_ID_RE = re.compile(r"^p\d{4}\.[a-z]{3,4}$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _session_dir(session_id: str) -> Path:
    if not _SESSION_RE.match(session_id or ""):
        raise HTTPException(status_code=400, detail="Bad upload session id.")
    path = config.UPLOADS_DIR / session_id
    if not path.is_dir():
        raise HTTPException(
            status_code=404,
            detail="That upload session is gone - re-add the images and convert again.",
        )
    return path


def _prune_old_uploads() -> None:
    cutoff = time.time() - config.UPLOAD_TTL_HOURS * 3600
    for path in config.UPLOADS_DIR.iterdir():
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def _natural_key(name: str) -> list[Any]:
    """Sort key where IMG_9.PNG comes before IMG_10.PNG."""
    parts = re.split(r"(\d+)", Path(name).name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def _next_page_index(session_dir: Path) -> int:
    """One past the highest page number already prepared in this session."""
    existing = [int(path.stem[1:]) for path in session_dir.glob("p[0-9][0-9][0-9][0-9].*")]
    return max(existing, default=0) + 1


def _resolve_order(session_dir: Path, order: list[str]) -> list[str]:
    """Validate page ids the UI sent back and return them in that order."""
    if not order:
        raise HTTPException(status_code=400, detail="No pages were selected.")
    resolved: list[str] = []
    for page_id in order:
        name = Path(page_id or "").name
        if not _PAGE_ID_RE.match(name) or not (session_dir / name).is_file():
            raise HTTPException(
                status_code=400,
                detail="That page is no longer in the upload session - re-add "
                "the files and try again.",
            )
        if name in resolved:
            raise HTTPException(status_code=400, detail="A page was listed twice.")
        resolved.append(name)
    return resolved


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return {
        "models": config.AVAILABLE_MODELS,
        "default_model": config.DEFAULT_MODEL,
        "page_warn_threshold": config.PAGE_WARN_THRESHOLD,
        "allowed_suffixes": sorted(config.ALLOWED_SUFFIXES | config.PDF_SUFFIXES),
        "pdf_supported": page_prep.pdfium is not None,
        "pdf_export": pdf_export.available(),
        "notes_dir": str(config.NOTES_DIR),
        "assets_dir": str(config.ASSETS_DIR),
        "env_notices": claude_client.env_notices(),
    }


@app.post("/api/pages")
async def prepare_pages(
    files: list[UploadFile], session_id: str | None = Form(default=None)
) -> dict[str, Any]:
    """Upload files and expand them into page images ready to convert.

    Images are stored as-is; PDFs are rasterised one image per page. Passing an
    existing `session_id` appends to that session, so pages can be added in
    several drops. Returns the pages added by this call, in page order.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    if session_id:
        session_dir = _session_dir(session_id)
    else:
        _prune_old_uploads()
        session_dir = config.UPLOADS_DIR / uuid.uuid4().hex
        session_dir.mkdir(parents=True)

    # Loose screenshots arrive in whatever order the OS handed them over;
    # GoodNotes names them sequentially, so sort this batch by name. A PDF
    # carries its own page order and is expanded in place.
    uploads = sorted(files, key=lambda f: _natural_key(f.filename or ""))

    prepared: list[page_prep.PreparedPage] = []
    index = _next_page_index(session_dir)
    for upload in uploads:
        data = await upload.read()
        try:
            pages = page_prep.prepare_file(
                data, upload.filename or "page", session_dir, index
            )
        except PagePrepError as exc:
            if not session_id and not prepared:
                shutil.rmtree(session_dir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prepared.extend(pages)
        index += len(pages)

    return {
        "session_id": session_dir.name,
        "pages": [page.as_dict(session_dir.name) for page in prepared],
    }


@app.delete("/api/pages/{session_id}")
def discard_session(session_id: str) -> dict[str, Any]:
    """Throw away an upload session the user cleared without converting."""
    session_dir = _session_dir(session_id)
    shutil.rmtree(session_dir, ignore_errors=True)
    return {"discarded": session_id}


class ConvertRequest(BaseModel):
    session_id: str
    order: list[str]
    model: str | None = None


@app.post("/api/convert")
async def convert(request: ConvertRequest) -> StreamingResponse:
    """Convert prepared pages, streaming progress as server-sent events.

    The last event is either `done` (with the assembled markdown and the page
    order needed to save) or `error` (with a message for the UI).
    """
    session_dir = _session_dir(request.session_id)
    order = _resolve_order(session_dir, request.order)
    chosen_model = (
        request.model or config.DEFAULT_MODEL
    ).strip() or config.DEFAULT_MODEL
    image_paths = [session_dir / name for name in order]
    page_files = note_store.page_filenames(order)

    async def stream() -> AsyncIterator[str]:
        yield _sse(
            "progress",
            {"kind": "status", "message": f"Sending {len(order)} page(s) to Claude."},
        )
        payload: dict[str, Any] | None = None
        try:
            async for item in claude_client.convert_pages(
                image_paths, workdir=session_dir, model=chosen_model
            ):
                if isinstance(item, ProgressEvent):
                    yield _sse(
                        "progress",
                        {
                            "kind": item.kind,
                            "message": item.message,
                            **item.data,
                        },
                    )
                else:
                    payload = item
        except ConversionError as exc:
            yield _sse("error", {"message": exc.message, "detail": exc.detail})
            return
        except Exception as exc:  # noqa: BLE001 - surface, never crash the stream
            yield _sse(
                "error",
                {
                    "message": "Something went wrong while converting this note.",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )
            return

        if payload is None:
            yield _sse(
                "error",
                {
                    "message": "Claude finished without producing a note.",
                    "detail": claude_client.LOGIN_HINT,
                },
            )
            return

        usage_log.record(
            "convert",
            payload.get("usage") or {},
            title=payload["title"],
            pages=len(order),
            model=chosen_model,
        )

        slug = note_store.unique_slug(payload["title"])
        markdown = note_store.assemble_markdown(
            payload, slug=slug, page_files=page_files
        )
        yield _sse(
            "done",
            {
                "session_id": session_dir.name,
                "order": order,
                "slug_preview": slug,
                "model": chosen_model,
                "title": payload["title"],
                "subject": payload["subject"],
                "summary": payload["summary"],
                "date": payload["date_guess"] or date_cls.today().isoformat(),
                "date_guess": payload["date_guess"],
                "diagram_pages": [d["page"] for d in payload["diagrams"]],
                "has_exercises": False,
                "usage": payload.get("usage") or {},
                "markdown": markdown,
                "pages": [
                    {
                        "page": index,
                        "asset_name": page_files[index - 1],
                        "url": f"/api/uploads/{session_dir.name}/{name}",
                    }
                    for index, name in enumerate(order, start=1)
                ],
            },
        )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/uploads/{session_id}/{filename}")
def get_upload(session_id: str, filename: str) -> FileResponse:
    """Serve an as-yet-unsaved page image for the preview pane."""
    session_dir = _session_dir(session_id)
    path = (session_dir / Path(filename).name).resolve()
    if path.parent != session_dir.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="No such uploaded page.")
    return FileResponse(path)


class SaveRequest(BaseModel):
    session_id: str
    order: list[str]
    markdown: str
    title: str = Field(default="Untitled note")
    subject: str = ""
    date: str = ""


@app.post("/api/notes")
def save_note(request: SaveRequest) -> dict[str, Any]:
    session_dir = _session_dir(request.session_id)
    originals = _resolve_order(session_dir, request.order)

    title = request.title.strip() or "Untitled note"
    slug = note_store.unique_slug(title)
    page_files = note_store.page_filenames(originals)

    markdown = note_store.retarget_asset_links(request.markdown, slug)
    markdown = note_store.sync_front_matter(
        markdown,
        title=title,
        date=request.date.strip() or date_cls.today().isoformat(),
        subject=request.subject.strip(),
        source_images=page_files,
    )

    try:
        info = note_store.save_note(
            slug=slug,
            markdown=markdown,
            source_dir=session_dir,
            originals=originals,
            page_files=page_files,
        )
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    shutil.rmtree(session_dir, ignore_errors=True)
    info["path"] = str(config.NOTES_DIR / f"{slug}.md")
    return info


@app.get("/api/notes")
def get_notes() -> dict[str, Any]:
    return {"notes": note_store.list_notes()}


@app.get("/api/notes/{slug}")
def get_note(slug: str) -> dict[str, Any]:
    try:
        info = note_store.read_note(slug)
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    info["path"] = str(config.NOTES_DIR / f"{slug}.md")
    return info


class UpdateRequest(BaseModel):
    markdown: str


@app.put("/api/notes/{slug}")
def put_note(slug: str, request: UpdateRequest) -> dict[str, Any]:
    try:
        return note_store.update_note(slug, request.markdown)
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/notes/{slug}")
def remove_note(slug: str) -> dict[str, Any]:
    try:
        note_store.delete_note(slug)
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": slug}


class ExerciseRequest(BaseModel):
    markdown: str
    model: str | None = None


async def _exercises_for(
    markdown: str, model: str | None, *, slug: str | None, title: str | None
) -> dict[str, Any]:
    """Generate exercises from note text. Reads no images, so it is cheap."""
    try:
        result = await claude_client.generate_exercises(
            markdown, workdir=config.UPLOADS_DIR, model=model
        )
    except ConversionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"{exc.message} {exc.detail}".strip(),
        ) from exc

    usage_log.record(
        "exercises",
        result["usage"],
        slug=slug,
        title=title,
        model=model or config.DEFAULT_MODEL,
    )
    return result


@app.post("/api/exercises")
async def make_exercises(request: ExerciseRequest) -> dict[str, Any]:
    """Exercises for a note that has not been saved yet."""
    result = await _exercises_for(
        request.markdown, request.model, slug=None, title=None
    )
    return {
        "exercises": result["exercises"],
        "usage": result["usage"],
        "markdown": note_store.set_exercises(request.markdown, result["exercises"]),
    }


@app.post("/api/notes/{slug}/exercises")
async def make_note_exercises(slug: str, request: ExerciseRequest) -> dict[str, Any]:
    """Exercises for a saved note, written straight back to the file."""
    try:
        info = note_store.read_note(slug)
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = await _exercises_for(
        info["markdown"], request.model, slug=slug, title=info.get("title")
    )
    markdown = note_store.set_exercises(info["markdown"], result["exercises"])
    updated = note_store.update_note(slug, markdown)
    updated["markdown"] = markdown
    updated["exercises"] = result["exercises"]
    updated["usage"] = result["usage"]
    return updated


@app.get("/api/usage")
def get_usage() -> dict[str, Any]:
    usage_log.maybe_prune()
    return usage_log.summary()


@app.get("/api/notes/{slug}/pdf")
async def note_pdf(slug: str, request: Request) -> Response:
    """Render a saved note to PDF with headless Chromium, if it is installed."""
    note_store.validate_slug(slug)
    try:
        info = note_store.read_note(slug)
    except note_store.NoteStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        pdf = await pdf_export.render_note_pdf(str(request.base_url), slug)
    except pdf_export.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    filename = f"{slug}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Note-Title": info["title"].encode("ascii", "replace").decode(),
        },
    )


@app.post("/api/notes/export")
async def export_all(request: Request) -> Response:
    """Render every saved note to PDF and return them as one zip."""
    notes = note_store.list_notes()
    if not notes:
        raise HTTPException(status_code=404, detail="There are no notes to export.")

    slugs = [note["slug"] for note in notes]
    try:
        rendered = await pdf_export.render_many(str(request.base_url), slugs)
    except pdf_export.PdfUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for note in notes:
            pdf = rendered.get(note["slug"])
            if pdf:
                archive.writestr(f"{note['slug']}.pdf", pdf)
    buffer.seek(0)

    stamp = date_cls.today().isoformat()
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="notes-{stamp}.zip"'
        },
    )


@app.get("/print")
def print_view() -> FileResponse:
    """Printable single-note sheet, used by the print dialog and by Chromium."""
    return _page("print.html")


@app.get("/")
def index() -> FileResponse:
    return _page("index.html")


@app.exception_handler(note_store.NoteStoreError)
def note_store_error_handler(_request: Any, exc: note_store.NoteStoreError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


class AppStatic(StaticFiles):
    """Static files that never go stale after a code change.

    The app's own JS and CSS are served `no-cache`, which still allows a 304
    via ETag but forces the browser to revalidate every load. Without this a
    cached `markdown.js` silently keeps running after an update, and the app
    looks broken in a way that reads exactly like a rendering bug.

    Vendored third-party files are content-stable, so they keep a long cache.
    """

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if path.startswith("vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _page(name: str) -> FileResponse:
    return FileResponse(
        STATIC_DIR / name, headers={"Cache-Control": "no-cache"}
    )


# Saved screenshots, so `../assets/<slug>/pageN.png` links can be previewed.
app.mount("/assets", StaticFiles(directory=config.ASSETS_DIR), name="assets")
app.mount("/static", AppStatic(directory=STATIC_DIR), name="static")
