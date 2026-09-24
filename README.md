# GoodNotes → Markdown

A small local web app that turns handwritten notes exported from GoodNotes 5 on
iPad — as a **PDF** or as **screenshots** — into clean Markdown study notes. Each
generated file has a plain-language summary, the transcribed content organised
with headings, any diagrams embedded as the original image plus a written
description, and a set of practice exercises generated from the note's own
content.

PDF is the easier export: one file per note, pages already in the right order.
The app splits it into page images for you.

Handwriting is transcribed by a vision-capable Claude model, not by a classic
OCR library — traditional OCR does not cope with handwriting. Claude is reached
through the **Claude Code CLI in headless mode**, so the work draws on your
Claude subscription rather than a metered API key. See
[Billing](#billing-read-this-once) below.

## What it produces

```markdown
---
title: "Photosynthesis: Light Reactions and Calvin Cycle"
date: 2026-09-12
source_images: ["page1.png", "page2.png"]
subject: "biology"
---

# Photosynthesis: Light Reactions and Calvin Cycle

## Summary

These notes cover the basics of photosynthesis, including its overall chemical
equation and where it occurs in the chloroplast. ...

## Notes

light + CO2 + H2O -> glucose + O2

![Chloroplast sketch](../assets/photosynthesis-light-reactions/page1.png)

*A labeled sketch of a chloroplast, boxed off under the introductory notes. It
marks the thylakoid membrane and stroma referenced in the surrounding text.*

## Practice Exercises

1. Write the overall chemical equation for photosynthesis, including reactants
   and products.
2. ...
```

Notes land in `notes/<slug>.md` and their screenshots in `assets/<slug>/pageN.png`,
so the relative image links resolve in Obsidian, VS Code, or anything else that
opens the folder. Both directories are git-ignored — your notes stay local.

## Setup

**Requirements:** Python 3.10+, Node.js (for the Claude Code CLI), and a Claude
Pro or Max subscription.

```bash
# 1. Install the Claude Code CLI and log in once (subscription auth, no API key)
npm install -g @anthropic-ai/claude-code
claude login

# 2. Confirm the CLI answers
claude -p "hello"

# 3. Python dependencies
pip install -r requirements.txt

# 4. Make sure no API key is set in this shell - see Billing below
echo "ANTHROPIC_API_KEY is: ${ANTHROPIC_API_KEY:-(not set, good)}"

# 5. Run
uvicorn server:app --reload
```

Then open <http://localhost:8000>.

## Billing (read this once)

This app never calls the Anthropic API directly and holds no API key. It shells
out to `claude -p`, which authenticates with whatever `claude login` set up, so
conversions draw on your subscription's usage allowance.

That behaviour hinges on **`ANTHROPIC_API_KEY` not being set** in the process the
server runs in. If the CLI sees one it authenticates with it instead and bills
per token. The server removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`CLAUDE_CODE_USE_BEDROCK` and `CLAUDE_CODE_USE_VERTEX` from the subprocess
environment as a backstop, and the UI shows a banner if it had to. Unsetting them
in the shell you launch the server from is still the reliable fix.
`ANTHROPIC_BASE_URL` is passed through untouched, since it only redirects the
endpoint and stripping it would break a legitimate gateway setup.

Two things worth knowing:

- This usage counts against the same weekly and session caps as interactive
  Claude Code and claude.ai. A heavy note-processing day eats into that shared
  allowance.
- Anthropic has floated changing this billing behaviour before (and paused it).
  Re-check the current Claude Code / Agent SDK billing docs now and then in case
  the policy shifts.

## Using it

1. Export one note from GoodNotes. **PDF is the better route** (Share → Export →
   PDF): one file, pages in order, nothing to sort out. Screenshots work too — a
   multi-page note exports as several files, usually `IMG_1234.PNG`,
   `IMG_1235.PNG`.
2. Drop the file(s) on the app, or click to pick them. Uploading happens
   immediately: a PDF is split into one thumbnail per page, screenshots are
   sorted by filename (so `IMG_9` lands before `IMG_10`). Drag the thumbnails to
   reorder, or remove any page you don't want sent. You can mix a PDF and loose
   screenshots, and add more in a second drop.
3. Pick a model and hit **Convert**. Progress streams as Claude reads each page.
4. Check the preview. The title, subject and date are editable, and editing the
   title updates the front matter and the `#` heading. The Markdown pane is fully
   editable and the rendered pane follows it. **Regenerate** re-runs the same
   pages if the first pass was poor.
5. **Save note** writes the `.md` and copies the screenshots into `assets/<slug>/`.
6. The **Library** tab lists past conversions; open one to read it, edit it in
   place, delete it (which removes its screenshots too), or turn it into a PDF.

## Maths and PDF export

Mathematical notation is transcribed as LaTeX — `$...$` inline, `$$...$$` for
anything displayed, including matrices, systems, integrals and summations — and
rendered with [KaTeX](https://katex.org), which is vendored under
`static/vendor/katex/` so it works with no network and no build step. Dollar
signs in ordinary prose (`$40`) and maths inside code fences are left alone.

Two ways to get a PDF, both from the **Library** tab:

- **Print / Save as PDF** opens a print-styled sheet and your browser's print
  dialog. Needs nothing installed and works everywhere.
- **Download PDF** renders it server-side in one click, and **Export all as PDF**
  zips up the whole library. These appear only if Playwright is installed:

  ```bash
  pip install playwright && playwright install chromium
  ```

  Without it the buttons stay hidden and printing still works. If Playwright
  cannot find a browser, point `GNMD_CHROMIUM_PATH` at one.

Both routes render the same `/print` page, so the PDF matches the preview —
typeset maths, embedded diagrams, captions kept with their images, and page
breaks avoided inside equations, tables and figures.

Handwriting Claude could not read confidently is marked `[unclear: best guess]`
in the output, highlighted in the rendered view. Those spans are worth checking
by hand.

HEIC is not accepted — Claude's Read tool doesn't handle it. Export as PDF, PNG,
or JPEG instead.

## Configuration

All optional, all environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GNMD_NOTES_DIR` | `./notes` | Where `.md` files are written |
| `GNMD_ASSETS_DIR` | `./assets` | Where screenshots are stored |
| `GNMD_UPLOADS_DIR` | `./uploads` | Scratch space for unsaved uploads |
| `GNMD_MODEL` | `sonnet` | Default model in the dropdown |
| `GNMD_CLAUDE_BIN` | `claude` | Path to the Claude Code CLI |
| `GNMD_TIMEOUT` | `900` | Seconds of silence before a conversion is killed |
| `GNMD_PAGE_WARN` | `15` | Page count that triggers the "that's a lot" warning |
| `GNMD_MAX_UPLOAD_BYTES` | `83886080` | Per-file upload ceiling (a PDF is one file) |
| `GNMD_UPLOAD_TTL_HOURS` | `24` | Age at which unsaved upload folders are purged |
| `GNMD_PDF_DPI` | `150` | Resolution PDF pages are rasterised at |
| `GNMD_MAX_PAGE_EDGE` | `2000` | Long-edge pixel cap for a rasterised page |
| `GNMD_MAX_PDF_PAGES` | `200` | Refuse PDFs longer than this |
| `GNMD_THUMB_EDGE` | `320` | Long edge of the page-strip thumbnails |
| `GNMD_CHROMIUM_PATH` | *(auto)* | Chromium binary for one-click PDF, if Playwright can't find one |
| `GNMD_PDF_TIMEOUT_MS` | `30000` | Per-note ceiling when rendering a PDF |

To keep notes outside the repo entirely:

```bash
GNMD_NOTES_DIR=~/StudyNotes/notes GNMD_ASSETS_DIR=~/StudyNotes/assets \
  uvicorn server:app --reload
```

## How it works

```
static/          drag-and-drop UI, preview, library (vanilla JS, no build step)
static/print.*   the printable sheet, shared by the print dialog and Chromium
server.py        FastAPI: /api/pages, /api/convert (SSE), /api/notes CRUD, /print
page_prep.py     uploads -> page images: PDF rasterising, thumbnails
pdf_export.py    optional one-click PDF via headless Chromium
claude_client.py prompt construction, the `claude -p` subprocess, response parsing
note_store.py    markdown assembly, slugs, front matter, files on disk
config.py        environment-driven configuration
```

Dropping files uploads them straight away (`POST /api/pages`). Screenshots are
stored as-is; PDFs are rasterised with `pypdfium2` into one PNG per page. Either
way the result is a numbered page image plus a thumbnail in
`uploads/<session>/`, which is what lets the UI preview and reorder PDF pages
without shipping a PDF renderer to the browser.

PDFs are rasterised rather than handed to Claude whole for two reasons: the
output embeds `../assets/<slug>/pageN.png` for diagram pages, so per-page images
are needed regardless, and Claude's Read tool caps how many PDF pages it will
take in one request. Rasterising also means the two input routes converge
immediately — everything downstream sees the same list of page images.

Converting (`POST /api/convert`) takes the session id and the page order the UI
settled on, so **Regenerate costs no re-upload**. A single prompt naming each
page's absolute path in order is handed to:

```
claude -p <prompt> --output-format stream-json --verbose \
       --allowedTools Read --add-dir <session dir> --model <model>
```

Claude reads the images with its own Read tool — that is what gives it vision on
the pages — and returns one JSON object (title, date guess, subject, summary,
markdown body with `{{page_N}}` placeholders, per-diagram descriptions,
exercises). `stream-json` is used rather than plain `json` so the UI can show
which page is being read and how much has been written.

The CLI wraps the model's reply in its own envelope whose schema shifts between
versions, so the parser reads the `result` field defensively and falls back to
extracting the first balanced `{...}` span, tolerating code fences and the
unescaped newlines models sometimes emit inside JSON strings. When that fails,
or the subprocess errors, the UI gets a readable message that points at
`claude login` as the likely cause rather than a stack trace.

Diagrams are never redrawn as ASCII art, SVG, or Mermaid. Pages with a drawing
keep the original image, embedded at the point in the content where it belongs,
with a prose description directly below. Text-only pages contribute their
transcription and no image, though every uploaded page is still copied into
`assets/<slug>/` as source material.

## Planned

Specs for work that is designed but not built:

- [`docs/ipad-access.md`](docs/ipad-access.md) — reaching the app from an iPad
  over Tailscale, and the password gate and touch reordering that need to exist
  first. Explains why the converter cannot run on Supabase or similar.
- [`docs/review-pass.md`](docs/review-pass.md) — an opt-in second pass that
  re-reads the pages against the generated note and reports transcription and
  consistency errors, with the free local LaTeX and structure checks that should
  run before spending anything.

## Notes on behaviour

- **Duplicate titles** get a `-2`, `-3` suffix on the slug and filename; nothing
  is overwritten. Renaming a note in the preview rewrites the image links to
  match the new slug before saving.
- **Long notes** have no hard page limit, but more than 15 images at once draws a
  warning, since that usually means two notes got mixed together. Every page is
  an image Claude reads into context — roughly 2,700–2,900 visual tokens for a
  letter or A4 page at the default DPI — so a long note is a genuinely large
  request against your usage allowance. Two page-count thresholds on Claude's
  side matter if you raise the defaults: past 20 images per request, every image
  must be under 2000 px on both edges (which is why `GNMD_MAX_PAGE_EDGE`
  defaults to 2000), and a request tops out at 100 images on 200k-context
  models. Converting each lecture separately gives better summaries and
  exercises anyway.
- **A conversion that stalls** for `GNMD_TIMEOUT` seconds is killed; the timeout
  measures silence from the CLI, not total runtime, so a genuinely long note is
  not cut off mid-answer.
- **Unsaved uploads** are purged after `GNMD_UPLOAD_TTL_HOURS`. Saving a note
  clears its session immediately.
