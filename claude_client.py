"""Talks to Claude through the Claude Code CLI in headless mode.

No Anthropic SDK and no API key: the CLI is shelled out to as a subprocess so
usage draws from the machine's `claude login` subscription. `ANTHROPIC_API_KEY`
(and friends) are deliberately stripped from the child environment, because if
the CLI sees one it authenticates with it instead and starts metered billing.

The prompt hands Claude absolute paths to the uploaded page images and asks it
to use its own Read tool to look at them, which is what makes vision work here.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import config

# Variables that would silently move billing off the logged-in subscription:
# a credential the CLI would prefer over the login, or a switch onto a cloud
# provider's metered endpoint. These are removed from the child environment.
STRIPPED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

# Worth mentioning but *not* stripped: a base URL only redirects the endpoint,
# and removing it would break a legitimate gateway or proxy setup.
NOTED_ENV_VARS = ("ANTHROPIC_BASE_URL",)

LOGIN_HINT = (
    "Check that this machine is logged in to Claude Code (run `claude login` "
    "in a terminal, then `claude -p \"hello\"` to confirm it answers)."
)


class ConversionError(RuntimeError):
    """Raised with a message that is safe and useful to show in the UI."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


def subprocess_env() -> dict[str, str]:
    """A copy of the environment with the metered-billing variables removed."""
    env = os.environ.copy()
    for var in STRIPPED_ENV_VARS:
        env.pop(var, None)
    return env


def env_notices() -> list[dict[str, str]]:
    """Billing-relevant variables present in the server's own environment."""
    notices = [
        {"var": var, "action": "stripped"}
        for var in STRIPPED_ENV_VARS
        if os.environ.get(var)
    ]
    notices += [
        {"var": var, "action": "kept"}
        for var in NOTED_ENV_VARS
        if os.environ.get(var)
    ]
    return notices


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

PROMPT_TEMPLATE = """You are transcribing handwritten study notes that were \
exported as screenshots from the GoodNotes iPad app.

Read each of these page images, in this order. They are consecutive pages of a \
single note, so interpret them together as one document:

{page_list}

Use your Read tool on each absolute path above before answering. Look at every \
page; do not skip any.

Then respond with a single JSON object and nothing else. No markdown fences, no \
commentary before or after, no explanation. The object must have exactly these \
keys:

- "title": a short, specific, human-readable title for this note (no date in it \
unless the note is essentially a dated log entry).
- "date_guess": a date written somewhere in the notes, as YYYY-MM-DD if you can \
resolve it, otherwise null. Do not invent one.
- "subject": a best-guess subject or topic tag, lowercase, one to three words \
(for example "organic chemistry", "us history", "linear algebra").
- "summary": 2-4 sentences of plain language describing what this note covers, \
written for someone who has not seen it.
- "markdown_body": the transcribed content as Markdown. See the rules below.
- "diagrams": a list of objects, one per page that contains a diagram, sketch, \
chart, graph, or drawing (as opposed to purely text handwriting). Each object is \
{{"page": <1-indexed page number>, "alt": "<short alt text>", "description": \
"<2-4 sentences of prose describing what the diagram depicts and what it is \
showing the reader>"}}. Use an empty list if no page has a diagram.

Rules for "markdown_body":
- Reproduce the structure of the handwritten note using `##` and `###` headings. \
Do not include a top-level `#` heading and do not include a "Summary" or \
"Practice Exercises" heading; those are added separately.
- Transcribe the content faithfully. Keep the note's own lists, arrows, \
equations, and emphasis.
- Mathematical notation is rendered with KaTeX, so write it as LaTeX: `$...$` \
for maths inside a sentence, and `$$...$$` on its own lines for anything \
displayed, which includes every matrix, determinant, integral, summation, \
system of equations, and multi-line derivation. Use real LaTeX environments \
(`\\begin{{bmatrix}}`, `\\begin{{cases}}`, `\\frac`, `\\int`, `\\sum`) rather \
than approximating them with text. Prose stays plain: do not wrap ordinary \
words, units, or bare numbers in `$`.
- Where you are not confident of a transcription, write the span as \
`[unclear: best guess text]` instead of silently guessing. Do this for blurry, \
cut-off, faded, or illegible handwriting.
- For every page you listed in "diagrams", insert the literal placeholder \
`{{page_N}}` on its own line at the point in the content where that page's \
drawing belongs (N is that page's 1-indexed number, e.g. `{{page_2}}`). The app \
substitutes the real image embed and your description there. Insert the \
placeholder for diagram pages only.
- Never redraw, recreate, or approximate a diagram as ASCII art, SVG, Mermaid, \
or a table. Always rely on the embedded original image plus your prose \
description.

If the pages are too unreadable to transcribe at all, still return the JSON \
object, with your best attempt and `[unclear: ...]` markers throughout.
"""


EXERCISES_TEMPLATE = """Below is a set of study notes, transcribed from a \
student's handwritten pages.

Write 3 to 6 original practice exercises that test recall and application of \
what these notes actually cover.

Respond with a single JSON object and nothing else - no markdown fences, no \
commentary. It has exactly one key, "exercises", whose value is a list of \
strings.

Rules:
- Base every exercise on the actual content below, not on general knowledge of \
the subject. A question that could be answered without having read these notes \
is a bad question.
- Match the question type to the material: worked problems for maths and \
science, short-answer recall for conceptual and humanities material, \
fill-in-the-blank or term-to-definition for vocabulary. Mix the types where the \
note mixes material. Decide the mix yourself.
- Where a span is marked `[unclear: ...]`, the transcription was uncertain. Do \
not build an exercise that depends on it.
- Write mathematics as LaTeX, `$...$` inline and `$$...$$` displayed, matching \
the notes.
- Do not include answers.

The notes:

{note}
"""


def build_prompt(image_paths: Sequence[Path]) -> str:
    page_list = "\n".join(
        f"- Page {i}: {path}" for i, path in enumerate(image_paths, start=1)
    )
    return PROMPT_TEMPLATE.format(page_list=page_list)


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text.strip())


def _first_json_object(text: str) -> str | None:
    """Return the first balanced {...} span, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_model_json(result_text: str) -> dict[str, Any]:
    """Pull the model's own JSON object out of the CLI's `result` text.

    The CLI envelope has already been unwrapped by the caller; what arrives here
    is the model's final message, which should be bare JSON but in practice may
    arrive fenced or with a stray sentence around it.
    """
    candidates = [_strip_fences(result_text)]
    inner = _first_json_object(result_text)
    if inner:
        candidates.append(inner)

    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate.strip():
            continue
        # `strict=False` is the second attempt on purpose: it tolerates literal
        # newlines and tabs inside strings, which models do sometimes emit
        # instead of escaping them, and which strict JSON rejects outright.
        for strict in (True, False):
            try:
                parsed = json.loads(candidate, strict=strict)
            except json.JSONDecodeError as exc:  # noqa: PERF203 - few candidates
                last_error = exc
                continue
            if isinstance(parsed, dict):
                return parsed
            last_error = ValueError("model returned JSON that was not an object")
            break

    snippet = result_text.strip()
    if len(snippet) > 800:
        snippet = snippet[:800] + " ... [truncated]"
    raise ConversionError(
        "Claude's reply was not valid JSON, so the note could not be built. "
        "Try Regenerate - this usually works on a second pass. "
        f"({last_error})",
        detail=snippet or "(empty reply)",
    )


def normalize_payload(raw: dict[str, Any], page_count: int) -> dict[str, Any]:
    """Coerce the model's object into the shape the rest of the app expects."""

    def as_text(value: Any) -> str:
        return value.strip() if isinstance(value, str) else ""

    diagrams: list[dict[str, Any]] = []
    seen_pages: set[int] = set()
    for entry in raw.get("diagrams") or []:
        if isinstance(entry, dict):
            page_raw, alt, description = (
                entry.get("page"),
                as_text(entry.get("alt")),
                as_text(entry.get("description")),
            )
        else:  # tolerate a bare list of page numbers
            page_raw, alt, description = entry, "", ""
        try:
            page = int(page_raw)
        except (TypeError, ValueError):
            continue
        if not 1 <= page <= page_count or page in seen_pages:
            continue
        seen_pages.add(page)
        diagrams.append(
            {
                "page": page,
                "alt": alt or "diagram",
                "description": description,
            }
        )
    diagrams.sort(key=lambda d: d["page"])

    # Some replies use the older `diagram_pages` key instead.
    for page_raw in raw.get("diagram_pages") or []:
        try:
            page = int(page_raw)
        except (TypeError, ValueError):
            continue
        if 1 <= page <= page_count and page not in seen_pages:
            seen_pages.add(page)
            diagrams.append({"page": page, "alt": "diagram", "description": ""})
    diagrams.sort(key=lambda d: d["page"])

    date_guess = as_text(raw.get("date_guess"))

    return {
        "title": as_text(raw.get("title")) or "Untitled note",
        "date_guess": date_guess or None,
        "subject": as_text(raw.get("subject")),
        "summary": as_text(raw.get("summary")),
        "markdown_body": as_text(raw.get("markdown_body")),
        "diagrams": diagrams,
    }


# --------------------------------------------------------------------------- #
# Running the CLI
# --------------------------------------------------------------------------- #


@dataclass
class ProgressEvent:
    """One line of human-readable progress for the UI."""

    kind: str  # "status" | "reading" | "writing" | "warning"
    message: str
    data: dict[str, Any] = field(default_factory=dict)


# A single stream-json line can be a base64-inlined image worth several MB.
# Lines above this size are dropped unparsed rather than blowing up json.loads.
MAX_LINE_BYTES = 2 * 1024 * 1024


async def _iter_json_lines(
    stream: asyncio.StreamReader, proc: asyncio.subprocess.Process
) -> AsyncIterator[str]:
    """Yield non-empty stdout lines, chunk-buffered so long lines are safe.

    `StreamReader.readline` raises once a line exceeds its internal limit, which
    the base64 image lines in the stream reliably do, so the buffering is done
    here instead. The timeout is an idle timeout: it fires only when the CLI has
    produced nothing at all for CONVERT_TIMEOUT seconds.
    """
    buffer = bytearray()
    oversized = False
    while True:
        try:
            chunk = await asyncio.wait_for(
                stream.read(65536), timeout=config.CONVERT_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            raise ConversionError(
                f"Claude went quiet for {config.CONVERT_TIMEOUT} seconds and was "
                "stopped. Try fewer pages at once, or raise GNMD_TIMEOUT.",
            ) from exc
        if not chunk:
            break
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline == -1:
                if len(buffer) > MAX_LINE_BYTES:
                    # Keep discarding until this line ends.
                    oversized = True
                    buffer.clear()
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if oversized:
                oversized = False
                continue
            text = line.decode("utf-8", "replace").strip()
            if text:
                yield text
    tail = bytes(buffer).decode("utf-8", "replace").strip()
    if tail and not oversized:
        yield tail


def _describe_tool_use(block: dict[str, Any], page_names: dict[str, int]) -> str:
    name = block.get("name") or "a tool"
    tool_input = block.get("input") or {}
    target = tool_input.get("file_path") or tool_input.get("path") or ""
    if name == "Read" and target:
        base = os.path.basename(str(target))
        page = page_names.get(str(target)) or page_names.get(base)
        if page:
            return f"Looking at page {page} ({base})"
        return f"Reading {base}"
    return f"Using {name}"


async def run_cli(
    prompt: str,
    *,
    workdir: Path,
    model: str | None = None,
    read_dir: Path | None = None,
    page_names: dict[str, int] | None = None,
) -> AsyncIterator[ProgressEvent | str]:
    """Run one `claude -p` call, yielding ProgressEvents then the result text.

    `read_dir` grants the Read tool access to a directory, which is what lets
    Claude look at page images. A prompt that carries its own content - the
    exercise generator, for instance - passes no directory and so runs with no
    tools at all, which is both cheaper and tighter.

    Errors are raised as ConversionError with a message meant for the UI.
    """
    model = model or config.DEFAULT_MODEL
    page_names = page_names or {}

    cmd = [
        config.CLAUDE_BIN,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
    ]
    if read_dir is not None:
        cmd += ["--allowedTools", "Read", "--add-dir", str(read_dir)]

    yield ProgressEvent("status", f"Starting Claude Code ({model})...")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise ConversionError(
            f"Could not run the Claude Code CLI ({config.CLAUDE_BIN!r}). "
            "Install it with `npm install -g @anthropic-ai/claude-code`, or set "
            "GNMD_CLAUDE_BIN to its full path.",
            detail=str(exc),
        ) from exc

    result_text: str | None = None
    envelope_error: str | None = None
    stderr_chunks: list[bytes] = []
    writing_chars = 0
    last_writing_report = 0

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            stderr_chunks.append(chunk)

    stderr_task = asyncio.create_task(drain_stderr())

    try:
        assert proc.stdout is not None
        async for text in _iter_json_lines(proc.stdout, proc):
            try:
                event = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            etype = event.get("type")

            if etype == "system" and event.get("subtype") == "init":
                yield ProgressEvent("status", "Claude session ready.")

            elif etype == "assistant":
                for block in event.get("message", {}).get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        yield ProgressEvent(
                            "reading", _describe_tool_use(block, page_names)
                        )

            elif etype == "stream_event":
                delta = (event.get("event") or {}).get("delta") or {}
                piece = delta.get("text")
                if isinstance(piece, str) and piece:
                    writing_chars += len(piece)
                    if writing_chars - last_writing_report >= 400:
                        last_writing_report = writing_chars
                        yield ProgressEvent(
                            "writing",
                            f"Writing the note... {writing_chars:,} characters",
                            {"chars": writing_chars},
                        )

            elif etype == "rate_limit_event":
                note = event.get("message") or event.get("subtype")
                if note:
                    yield ProgressEvent("warning", f"Usage limit notice: {note}")

            elif etype == "result":
                if event.get("is_error") or event.get("subtype") != "success":
                    envelope_error = (
                        str(event.get("result") or "")
                        or str(event.get("subtype") or "")
                        or "the CLI reported an error"
                    )
                else:
                    result_text = event.get("result")
                    if not isinstance(result_text, str):
                        # Older/newer envelopes may nest the text differently.
                        result_text = json.dumps(event.get("result"))

        await proc.wait()
    finally:
        await stderr_task
        if proc.returncode is None:
            proc.kill()

    stderr_text = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
    stderr_tail = stderr_text[-1500:]

    if envelope_error:
        raise ConversionError(
            f"Claude Code reported an error: {envelope_error} {LOGIN_HINT}",
            detail=stderr_tail,
        )

    if proc.returncode != 0:
        raise ConversionError(
            f"The Claude Code CLI exited with status {proc.returncode}. "
            f"{LOGIN_HINT}",
            detail=stderr_tail or "(no stderr output)",
        )

    if not result_text:
        raise ConversionError(
            "The Claude Code CLI finished without returning a result. "
            f"{LOGIN_HINT}",
            detail=stderr_tail or "(no stderr output)",
        )

    yield result_text


async def convert_pages(
    image_paths: Sequence[Path],
    *,
    workdir: Path,
    model: str | None = None,
) -> AsyncIterator[ProgressEvent | dict[str, Any]]:
    """Convert page images to a note, yielding progress then the payload dict."""
    if not image_paths:
        raise ConversionError("No page images were provided.")

    page_names: dict[str, int] = {}
    for index, path in enumerate(image_paths, start=1):
        page_names[str(path)] = index
        page_names[path.name] = index

    result_text = ""
    async for item in run_cli(
        build_prompt(image_paths),
        workdir=workdir,
        model=model,
        read_dir=workdir,
        page_names=page_names,
    ):
        if isinstance(item, ProgressEvent):
            yield item
        else:
            result_text = item

    yield ProgressEvent("status", "Parsing Claude's reply...")
    yield normalize_payload(
        parse_model_json(result_text), page_count=len(image_paths)
    )


async def generate_exercises(
    markdown: str, *, workdir: Path, model: str | None = None
) -> list[str]:
    """Write practice exercises for a note that has already been transcribed.

    The note's own text is the only input needed, so this never re-reads the
    page images - which is what makes it far cheaper than a conversion.
    """
    body = (markdown or "").strip()
    if not body:
        raise ConversionError("There is no note content to build exercises from.")

    result_text = ""
    async for item in run_cli(
        EXERCISES_TEMPLATE.format(note=body), workdir=workdir, model=model
    ):
        if not isinstance(item, ProgressEvent):
            result_text = item

    parsed = parse_model_json(result_text)
    exercises = [
        item.strip()
        for item in (parsed.get("exercises") or [])
        if isinstance(item, str) and item.strip()
    ]
    if not exercises:
        raise ConversionError(
            "Claude did not return any exercises for this note. Try again, or "
            "check that the note has enough content to build questions from.",
            detail=result_text[:800],
        )
    return exercises
