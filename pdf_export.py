"""Optional server-side PDF rendering.

The browser's own print dialog is the dependency-free path and always works.
This module adds one-click and batch export on top, by driving headless
Chromium over the same `/print` page the print dialog uses - so what downloads
is what the preview showed, math and diagrams included.

Playwright is optional. If it is not installed the app still runs; the PDF
endpoints report that clearly instead of failing obscurely.
"""

from __future__ import annotations

import asyncio
import importlib.util

import config

INSTALL_HINT = (
    "One-click PDF needs Playwright and a Chromium build. Run "
    "`pip install playwright` then `playwright install chromium`, and restart "
    "the server. Until then, use Print / Save as PDF, which needs nothing."
)


class PdfUnavailable(RuntimeError):
    """Raised when PDF rendering cannot run, with a message for the UI."""


def available() -> bool:
    """Whether the optional one-click path can be attempted.

    This only checks that Playwright is importable - whether a Chromium build
    is actually present shows up when a render is attempted, and is reported
    with the same install hint.

    `find_spec` on a dotted name imports the parent package first and raises
    if it is missing, so a plain `find_spec(...) is not None` would blow up on
    exactly the machines this is meant to detect. Everything is caught: this
    answers an optional-feature question and must never be the reason a
    request fails.
    """
    try:
        return importlib.util.find_spec("playwright.async_api") is not None
    except Exception:  # noqa: BLE001 - absence is the expected outcome here
        return False


async def render_note_pdf(base_url: str, slug: str) -> bytes:
    """Render one saved note to PDF bytes via headless Chromium."""
    return (await render_many(base_url, [slug]))[slug]


async def render_many(base_url: str, slugs: list[str]) -> dict[str, bytes]:
    """Render several notes in one browser, which is much faster than one each."""
    if not slugs:
        return {}
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise PdfUnavailable(INSTALL_HINT) from exc

    results: dict[str, bytes] = {}
    try:
        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(
                    executable_path=config.CHROMIUM_PATH or None
                )
            except Exception as exc:  # noqa: BLE001 - surface the install hint
                raise PdfUnavailable(
                    f"Chromium could not be started. {INSTALL_HINT} ({exc})"
                ) from exc
            try:
                page = await browser.new_page()
                for slug in slugs:
                    url = f"{base_url.rstrip('/')}/print?slug={slug}"
                    await page.goto(url, wait_until="load")
                    # print.js sets this only after math and images settle.
                    await page.wait_for_function(
                        "document.body.dataset.ready === 'true' || "
                        "document.body.dataset.ready === 'error'",
                        timeout=config.PDF_TIMEOUT_MS,
                    )
                    state = await page.evaluate("document.body.dataset.ready")
                    if state != "true":
                        message = await page.evaluate(
                            "(document.querySelector('.loading') || {}).textContent || ''"
                        )
                        raise PdfUnavailable(
                            f"The print page could not render {slug!r}. {message}".strip()
                        )
                    results[slug] = await page.pdf(
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                    )
            finally:
                await browser.close()
    except PdfUnavailable:
        raise
    except asyncio.TimeoutError as exc:
        raise PdfUnavailable(
            "Rendering the note timed out. A very long note may need a higher "
            "GNMD_PDF_TIMEOUT_MS."
        ) from exc
    return results
