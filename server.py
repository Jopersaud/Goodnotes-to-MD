"""FastAPI backend for the GoodNotes -> Markdown converter.

Run with:  uvicorn server:app --reload
Then open: http://localhost:8000

Make sure `ANTHROPIC_API_KEY` is not set in the shell you start this from - see
the README. The server strips it from the Claude Code subprocess either way, but
an unset shell is one less thing to get wrong.
"""

from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import date as date_cls
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import claude_client
import config
import note_store
from claude_client import ConversionError, ProgressEvent

app = FastAPI(title="GoodNotes to Markdown")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SESSION_RE = re.compile(r"^[0-9a-f]{32}$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _safe_filename(name: str) -> str:
    base = Path(name or "").name
    cleaned = _SAFE_NAME_RE.sub("_", base).strip("._") or "page"
    return cleaned[:80]


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


async def _store_uploads(files: list[UploadFile]) -> tuple[Path, list[str]]:
    """Write uploads into a fresh session folder, preserving submitted order."""
    if not files:
        raise HTTPException(status_code=400, detail="No images were uploaded.")

    session_id = uuid.uuid4().hex
    session_dir = config.UPLOADS_DIR / session_id
    session_dir.mkdir(parents=True)

    stored: list[str] = []
    for index, upload in enumerate(files, start=1):
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in config.ALLOWED_SUFFIXES:
            shutil.rmtree(session_dir, ignore_errors=True)
            allowed = ", ".join(sorted(config.ALLOWED_SUFFIXES))
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{upload.filename!r} is not a supported image ({allowed}). "
                    "Export from GoodNotes as PNG or JPEG."
                ),
            )
        data = await upload.read()
        if not data:
            shutil.rmtree(session_dir, ignore_errors=True)
            raise HTTPException(
                status_code=400, detail=f"{upload.filename!r} was empty."
            )
        if len(data) > config.MAX_IMAGE_BYTES:
            shutil.rmtree(session_dir, ignore_errors=True)
            limit_mb = config.MAX_IMAGE_BYTES // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=f"{upload.filename!r} is larger than {limit_mb} MB.",
            )
        name = f"{index:02d}_{_safe_filename(upload.filename or 'page')}"
        (session_dir / name).write_bytes(data)
        stored.append(name)

    (session_dir / "meta.json").write_text(
        json.dumps({"originals": stored}, indent=2), encoding="utf-8"
    )
    return session_dir, stored


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
        "allowed_suffixes": sorted(config.ALLOWED_SUFFIXES),
        "notes_dir": str(config.NOTES_DIR),
        "assets_dir": str(config.ASSETS_DIR),
        "env_notices": claude_client.env_notices(),
    }


@app.post("/api/convert")
async def convert(files: list[UploadFile], model: str | None = None) -> StreamingResponse:
    """Upload pages and stream conversion progress as server-sent events.

    The last event is either `done` (with the assembled markdown and the upload
    session id needed to save) or `error` (with a message for the UI).
    """
    _prune_old_uploads()
    session_dir, originals = await _store_uploads(files)
    chosen_model = (model or config.DEFAULT_MODEL).strip() or config.DEFAULT_MODEL
    image_paths = [session_dir / name for name in originals]
    page_files = note_store.page_filenames(originals)

    async def stream() -> AsyncIterator[str]:
        yield _sse(
            "progress",
            {
                "kind": "status",
                "message": f"Uploaded {len(originals)} page(s).",
            },
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

        slug = note_store.unique_slug(payload["title"])
        markdown = note_store.assemble_markdown(
            payload, slug=slug, page_files=page_files
        )
        yield _sse(
            "done",
            {
                "session_id": session_dir.name,
                "slug_preview": slug,
                "model": chosen_model,
                "title": payload["title"],
                "subject": payload["subject"],
                "summary": payload["summary"],
                "date": payload["date_guess"] or date_cls.today().isoformat(),
                "date_guess": payload["date_guess"],
                "diagram_pages": [d["page"] for d in payload["diagrams"]],
                "exercise_count": len(payload["exercises"]),
                "markdown": markdown,
                "pages": [
                    {
                        "page": index,
                        "asset_name": page_files[index - 1],
                        "url": f"/api/uploads/{session_dir.name}/{name}",
                    }
                    for index, name in enumerate(originals, start=1)
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
    markdown: str
    title: str = Field(default="Untitled note")
    subject: str = ""
    date: str = ""


@app.post("/api/notes")
def save_note(request: SaveRequest) -> dict[str, Any]:
    session_dir = _session_dir(request.session_id)
    try:
        meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
        originals: list[str] = list(meta["originals"])
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=409,
            detail="This upload session is incomplete - convert the pages again.",
        ) from exc

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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(note_store.NoteStoreError)
def note_store_error_handler(_request: Any, exc: note_store.NoteStoreError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# Saved screenshots, so `../assets/<slug>/pageN.png` links can be previewed.
app.mount("/assets", StaticFiles(directory=config.ASSETS_DIR), name="assets")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
