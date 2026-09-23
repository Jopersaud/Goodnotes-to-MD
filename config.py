"""Runtime configuration for the GoodNotes -> Markdown converter.

Everything is overridable with environment variables so the app can write its
output anywhere on disk without touching the code.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _dir_from_env(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    path = Path(raw).expanduser().resolve() if raw else default
    path.mkdir(parents=True, exist_ok=True)
    return path


# Where the finished .md files land.
NOTES_DIR = _dir_from_env("GNMD_NOTES_DIR", BASE_DIR / "notes")

# Where the original screenshots live, one subfolder per note slug.
ASSETS_DIR = _dir_from_env("GNMD_ASSETS_DIR", BASE_DIR / "assets")

# Scratch space for uploads that have not been saved into a note yet.
UPLOADS_DIR = _dir_from_env("GNMD_UPLOADS_DIR", BASE_DIR / "uploads")

# Claude Code CLI binary and the model used for conversions.
CLAUDE_BIN = os.environ.get("GNMD_CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("GNMD_MODEL", "sonnet")

# Models offered in the UI dropdown.
AVAILABLE_MODELS = [
    {"id": "sonnet", "label": "Sonnet - fast, good default"},
    {"id": "opus", "label": "Opus - slower, best on messy handwriting"},
    {"id": "haiku", "label": "Haiku - cheapest, short simple notes"},
]

# Hard ceiling on a single conversion, in seconds.
CONVERT_TIMEOUT = int(os.environ.get("GNMD_TIMEOUT", "900"))

# Soft warning threshold in the UI; not enforced server-side.
PAGE_WARN_THRESHOLD = int(os.environ.get("GNMD_PAGE_WARN", "15"))

# Image formats Claude Code's Read tool handles. HEIC is deliberately absent:
# export from GoodNotes as PNG, JPEG, or PDF instead.
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# GoodNotes' own export format. PDFs are rasterised into page images on upload.
PDF_SUFFIXES = {".pdf"}

# Per-file upload ceiling, in bytes. A multi-page PDF is one file, so this is
# more generous than a single screenshot needs.
MAX_UPLOAD_BYTES = int(os.environ.get("GNMD_MAX_UPLOAD_BYTES", str(80 * 1024 * 1024)))

# PDF rasterisation. 150 DPI puts a letter or A4 page at roughly 1650-1750 px
# on the long edge: comfortably under the 2576 px native limit of Claude's
# high-resolution tier, so pages are not downscaled on the way in, and under
# the 2000 px per-image ceiling that applies once a request carries more than
# 20 images - which a long note does, since every page is an image.
PDF_RENDER_DPI = int(os.environ.get("GNMD_PDF_DPI", "150"))
MAX_PAGE_EDGE_PX = int(os.environ.get("GNMD_MAX_PAGE_EDGE", "2000"))
MAX_PDF_PAGES = int(os.environ.get("GNMD_MAX_PDF_PAGES", "200"))

# Long edge of the thumbnails shown in the page strip.
THUMB_EDGE_PX = int(os.environ.get("GNMD_THUMB_EDGE", "320"))

# Upload sessions older than this (in hours) are cleaned up automatically.
UPLOAD_TTL_HOURS = int(os.environ.get("GNMD_UPLOAD_TTL_HOURS", "24"))
