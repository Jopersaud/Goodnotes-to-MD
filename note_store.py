"""Markdown assembly and on-disk note storage.

Notes are plain files: `notes/<slug>.md` next to `assets/<slug>/pageN.png`, so
the markdown's `../assets/<slug>/pageN.png` links resolve in any editor that
opens the notes folder. There is no database.
"""

from __future__ import annotations

import re
import shutil
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Iterable

import config

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PLACEHOLDER_RE = re.compile(r"\{\{?\s*page[_\s-]?(\d+)\s*\}?\}", re.IGNORECASE)
_ASSET_LINK_RE = re.compile(r"(\]\()(?:\.\./)?assets/[^/)]+/([^)]+\))")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class NoteStoreError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Slugs
# --------------------------------------------------------------------------- #


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:60].strip("-")
    return slug or "note"


def unique_slug(title: str, *, exclude: str | None = None) -> str:
    """A slug that is not already taken, auto-suffixed `-2`, `-3`, ... if needed."""
    base = slugify(title)
    candidate = base
    counter = 2
    while candidate != exclude and _slug_taken(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _slug_taken(slug: str) -> bool:
    return (config.NOTES_DIR / f"{slug}.md").exists() or (
        config.ASSETS_DIR / slug
    ).exists()


def validate_slug(slug: str) -> str:
    """Reject anything that is not a plain slug, so paths cannot escape."""
    if not SLUG_RE.match(slug or "") or len(slug) > 80:
        raise NoteStoreError(f"Not a valid note id: {slug!r}")
    return slug


# --------------------------------------------------------------------------- #
# Page filenames
# --------------------------------------------------------------------------- #


def page_filenames(originals: Iterable[str]) -> list[str]:
    """Canonical `pageN.<ext>` names, in the given page order."""
    names = []
    for index, original in enumerate(originals, start=1):
        suffix = Path(original).suffix.lower() or ".png"
        if suffix not in config.ALLOWED_SUFFIXES:
            suffix = ".png"
        names.append(f"page{index}{suffix}")
    return names


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #


def _yaml_scalar(value: Any) -> str:
    if value is None or value == "":
        return '""'
    text = str(value)
    if _DATE_RE.match(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_front_matter(
    *, title: str, date: str, subject: str, source_images: list[str]
) -> str:
    images = ", ".join(_yaml_scalar(name) for name in source_images)
    return "\n".join(
        [
            "---",
            f"title: {_yaml_scalar(title)}",
            f"date: {_yaml_scalar(date)}",
            f"source_images: [{images}]",
            f"subject: {_yaml_scalar(subject)}",
            "---",
        ]
    )


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Minimal front-matter reader for the fields this app writes itself."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body_start = text.find("\n", end + 1)
    body = text[body_start + 1 :] if body_start != -1 else ""

    meta: dict[str, Any] = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line or line.startswith((" ", "\t", "#")):
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            items = [
                item.strip().strip('"').strip("'")
                for item in raw[1:-1].split(",")
                if item.strip()
            ]
            meta[key.strip()] = items
        else:
            meta[key.strip()] = raw.strip('"').strip("'")
    return meta, body


# --------------------------------------------------------------------------- #
# Markdown assembly
# --------------------------------------------------------------------------- #


def _image_block(slug: str, filename: str, alt: str, description: str) -> str:
    alt_text = (alt or "diagram").replace("]", ")").strip()
    block = f"![{alt_text}](../assets/{slug}/{filename})"
    if description:
        block += f"\n\n*{description.strip()}*"
    return block


def assemble_markdown(
    payload: dict[str, Any],
    *,
    slug: str,
    page_files: list[str],
    title: str | None = None,
    date: str | None = None,
) -> str:
    """Turn a parsed Claude payload into the app's output markdown template."""
    final_title = (title or payload.get("title") or "Untitled note").strip()
    final_date = (date or payload.get("date_guess") or "").strip() or (
        date_cls.today().isoformat()
    )
    subject = (payload.get("subject") or "").strip()
    summary = (payload.get("summary") or "").strip()
    body = (payload.get("markdown_body") or "").strip()

    diagrams = {int(d["page"]): d for d in payload.get("diagrams") or []}
    placed: set[int] = set()

    def substitute(match: re.Match[str]) -> str:
        page = int(match.group(1))
        if not 1 <= page <= len(page_files):
            return ""
        placed.add(page)
        diagram = diagrams.get(page, {})
        return _image_block(
            slug,
            page_files[page - 1],
            diagram.get("alt") or f"page {page}",
            diagram.get("description") or "",
        )

    body = _PLACEHOLDER_RE.sub(substitute, body)

    # A diagram Claude described but forgot to place still gets embedded.
    orphans = [page for page in sorted(diagrams) if page not in placed]
    if orphans:
        extras = [
            _image_block(
                slug,
                page_files[page - 1],
                diagrams[page].get("alt") or f"page {page}",
                diagrams[page].get("description") or "",
            )
            for page in orphans
            if 1 <= page <= len(page_files)
        ]
        if extras:
            body = (body + "\n\n" + "\n\n".join(extras)).strip()

    exercises = [ex.strip() for ex in payload.get("exercises") or [] if ex.strip()]

    parts = [
        build_front_matter(
            title=final_title,
            date=final_date,
            subject=subject,
            source_images=page_files,
        ),
        "",
        f"# {final_title}",
        "",
        "## Summary",
        "",
        summary or "_No summary was produced for this note._",
        "",
        "## Notes",
        "",
        body or "_No content was transcribed from these pages._",
        "",
        "## Practice Exercises",
        "",
    ]
    if exercises:
        parts.extend(f"{i}. {ex}" for i, ex in enumerate(exercises, start=1))
    else:
        parts.append("_No exercises were generated for this note._")

    return "\n".join(parts).rstrip() + "\n"


def retarget_asset_links(markdown: str, slug: str) -> str:
    """Point every `../assets/<something>/file` link at this note's own slug.

    The user can rename a note in the preview, which changes its slug after the
    markdown was assembled; this keeps the image links valid.
    """
    return _ASSET_LINK_RE.sub(rf"\1../assets/{slug}/\2", markdown)


def sync_front_matter(
    markdown: str, *, title: str, date: str, subject: str, source_images: list[str]
) -> str:
    """Replace (or add) the front matter on a markdown document."""
    _, body = parse_front_matter(markdown)
    front = build_front_matter(
        title=title, date=date, subject=subject, source_images=source_images
    )
    return f"{front}\n\n{body.lstrip()}".rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Saving, listing, deleting
# --------------------------------------------------------------------------- #


def save_note(
    *,
    slug: str,
    markdown: str,
    source_dir: Path,
    originals: list[str],
    page_files: list[str],
) -> dict[str, Any]:
    """Write the note and copy its screenshots into `assets/<slug>/`."""
    validate_slug(slug)
    asset_dir = config.ASSETS_DIR / slug
    asset_dir.mkdir(parents=True, exist_ok=True)

    for original, page_name in zip(originals, page_files):
        source = (source_dir / original).resolve()
        if source.parent != source_dir.resolve() or not source.is_file():
            raise NoteStoreError(f"Uploaded page is missing: {original}")
        shutil.copy2(source, asset_dir / page_name)

    note_path = config.NOTES_DIR / f"{slug}.md"
    note_path.write_text(markdown, encoding="utf-8")
    return describe_note(note_path)


def describe_note(note_path: Path) -> dict[str, Any]:
    text = note_path.read_text(encoding="utf-8")
    meta, _ = parse_front_matter(text)
    slug = note_path.stem
    source_images = meta.get("source_images")
    if not isinstance(source_images, list):
        source_images = []
    return {
        "slug": slug,
        "title": meta.get("title") or slug,
        "date": meta.get("date") or "",
        "subject": meta.get("subject") or "",
        "source_images": source_images,
        "page_count": len(source_images),
        "modified": note_path.stat().st_mtime,
        "filename": note_path.name,
    }


def list_notes() -> list[dict[str, Any]]:
    notes = [describe_note(path) for path in config.NOTES_DIR.glob("*.md")]
    notes.sort(key=lambda note: (note["date"] or "", note["modified"]), reverse=True)
    return notes


def read_note(slug: str) -> dict[str, Any]:
    validate_slug(slug)
    note_path = config.NOTES_DIR / f"{slug}.md"
    if not note_path.is_file():
        raise NoteStoreError(f"No note called {slug!r}.")
    info = describe_note(note_path)
    info["markdown"] = note_path.read_text(encoding="utf-8")
    return info


def update_note(slug: str, markdown: str) -> dict[str, Any]:
    validate_slug(slug)
    note_path = config.NOTES_DIR / f"{slug}.md"
    if not note_path.is_file():
        raise NoteStoreError(f"No note called {slug!r}.")
    note_path.write_text(markdown.rstrip() + "\n", encoding="utf-8")
    return describe_note(note_path)


def delete_note(slug: str) -> None:
    validate_slug(slug)
    note_path = config.NOTES_DIR / f"{slug}.md"
    if not note_path.is_file():
        raise NoteStoreError(f"No note called {slug!r}.")
    note_path.unlink()
    asset_dir = config.ASSETS_DIR / slug
    if asset_dir.is_dir():
        shutil.rmtree(asset_dir)
