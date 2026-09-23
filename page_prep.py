"""Turn whatever the user dropped into a flat, ordered list of page images.

A note can arrive as loose screenshots (`IMG_1234.PNG`, `IMG_1235.PNG`) or as a
single PDF, which is what GoodNotes' own export produces. Both end up as the
same thing here: one image file per page in the upload session, numbered in
order, each with a small thumbnail for the UI.

PDFs are rasterised rather than handed to Claude whole. The output markdown
embeds `../assets/<slug>/pageN.png` for pages with a diagram, so per-page images
are needed regardless, and rasterising also sidesteps the page ceiling Claude's
Read tool applies to large PDFs.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

import config

try:  # pypdfium2 is only needed for PDF input.
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - depends on the install
    pdfium = None

PDF_MISSING_HINT = (
    "PDF support needs the pypdfium2 package. Run "
    "`pip install -r requirements.txt`, then restart the server."
)


class PagePrepError(ValueError):
    """Raised with a message meant for the UI."""


@dataclass
class PreparedPage:
    """One page image sitting in the upload session, ready to convert."""

    page_id: str  # filename in the session dir, also the id the UI sends back
    label: str  # what to show under the thumbnail
    thumb: str  # thumbnail filename in the session dir
    source: str  # "image" or "pdf"

    def as_dict(self, session_id: str) -> dict[str, Any]:
        base = f"/api/uploads/{session_id}"
        return {
            "id": self.page_id,
            "label": self.label,
            "url": f"{base}/{self.page_id}",
            "thumb": f"{base}/{self.thumb}",
            "source": self.source,
        }


def _write_thumbnail(image: Image.Image, path: Path) -> None:
    thumb = image.copy()
    thumb.thumbnail((config.THUMB_EDGE_PX, config.THUMB_EDGE_PX))
    if thumb.mode not in ("RGB", "L"):
        thumb = thumb.convert("RGB")
    thumb.save(path, format="JPEG", quality=80)


def _names(index: int, suffix: str) -> tuple[str, str]:
    return f"p{index:04d}{suffix}", f"t{index:04d}.jpg"


def _prepare_image(
    data: bytes, filename: str, session_dir: Path, index: int
) -> PreparedPage:
    suffix = Path(filename).suffix.lower()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            page_id, thumb_name = _names(index, suffix)
            # The original bytes are kept untouched: this file is what ends up
            # in assets/<slug>/ as the source screenshot.
            (session_dir / page_id).write_bytes(data)
            _write_thumbnail(image, session_dir / thumb_name)
    except (UnidentifiedImageError, OSError) as exc:
        raise PagePrepError(
            f"{filename!r} could not be read as an image. Export from GoodNotes "
            "as PNG, JPEG, or PDF."
        ) from exc
    return PreparedPage(page_id, Path(filename).name, thumb_name, "image")


def _prepare_pdf(
    data: bytes, filename: str, session_dir: Path, start_index: int
) -> list[PreparedPage]:
    if pdfium is None:
        raise PagePrepError(PDF_MISSING_HINT)

    try:
        document = pdfium.PdfDocument(io.BytesIO(data))
        page_count = len(document)
    except Exception as exc:  # noqa: BLE001 - pdfium raises assorted types
        raise PagePrepError(
            f"{filename!r} could not be opened as a PDF. If it is password "
            "protected, remove the password and export again."
        ) from exc

    if page_count == 0:
        raise PagePrepError(f"{filename!r} has no pages.")
    if page_count > config.MAX_PDF_PAGES:
        raise PagePrepError(
            f"{filename!r} has {page_count} pages, over the {config.MAX_PDF_PAGES} "
            "page limit. Split it, or raise GNMD_MAX_PDF_PAGES."
        )

    prepared: list[PreparedPage] = []
    base_scale = config.PDF_RENDER_DPI / 72
    try:
        for number in range(page_count):
            page = document[number]
            width, height = page.get_size()
            # Render at the target DPI, but never past the long-edge cap: a
            # poster-sized page would otherwise produce a huge PNG.
            longest = max(width, height) or 1
            scale = min(base_scale, config.MAX_PAGE_EDGE_PX / longest)
            image = page.render(scale=scale).to_pil()
            index = start_index + number
            page_id, thumb_name = _names(index, ".png")
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(session_dir / page_id, format="PNG")
            _write_thumbnail(image, session_dir / thumb_name)
            prepared.append(
                PreparedPage(
                    page_id,
                    f"{Path(filename).name} p{number + 1}",
                    thumb_name,
                    "pdf",
                )
            )
    finally:
        try:
            document.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
    return prepared


def prepare_file(
    data: bytes, filename: str, session_dir: Path, start_index: int
) -> list[PreparedPage]:
    """Expand one uploaded file into the page images it contains."""
    if not data:
        raise PagePrepError(f"{filename!r} was empty.")
    if len(data) > config.MAX_UPLOAD_BYTES:
        limit_mb = config.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise PagePrepError(f"{filename!r} is larger than {limit_mb} MB.")

    suffix = Path(filename or "").suffix.lower()
    if suffix in config.PDF_SUFFIXES:
        return _prepare_pdf(data, filename, session_dir, start_index)
    if suffix in config.ALLOWED_SUFFIXES:
        return [_prepare_image(data, filename, session_dir, start_index)]

    accepted = ", ".join(sorted(config.ALLOWED_SUFFIXES | config.PDF_SUFFIXES))
    raise PagePrepError(
        f"{filename!r} is not a supported file ({accepted}). Export from "
        "GoodNotes as PDF, PNG, or JPEG."
    )
