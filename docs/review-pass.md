# Spec: transcription review pass

Status: **planned, not built.**

## Why

The first conversion pass transcribes and never re-examines its own output. That
lets a certain class of error through silently, because the output is plausible
in isolation and only wrong against the page or against itself. A real example
from a set of optimisation notes:

```
$x_1^0, x_2^0, x^2, \dots, x_{n_0}^0$
```

The third term should be `x_3^0`. Nothing about it is malformed — it renders
fine, it is valid LaTeX — but the sequence it sits in makes it obviously wrong.
A reader spots it instantly. The first pass does not, because it is writing
left to right and never reads back.

`[unclear: ...]` markers cover the case where Claude *knows* it is unsure. This
covers the case where it is confidently wrong.

## Shape of the feature

A second, opt-in pass over a note that has already been converted. It re-reads
the original page images **and** the generated markdown, and reports findings.
It does not rewrite the note on its own.

Opt-in matters: a review costs roughly what the conversion cost, against the
same subscription allowance. It should be a button, never automatic.

## Two tiers, cheapest first

### Tier 1 — local checks, no Claude call, free

Run before spending anything. These are mechanical and deterministic:

- **LaTeX validity.** Render every math span through KaTeX with
  `throwOnError: true` and collect the failures. Catches unbalanced braces, a
  `\begin` with no matching `\end`, unknown macros, `\left` without `\right`.
  The renderer already isolates every span, so this is a loop over `mathSpans`.
- **Structural checks.** A `{{page_N}}` placeholder that survived substitution,
  an image link pointing at a file not in `assets/<slug>/`, front matter whose
  `source_images` disagrees with what is on disk, a diagram listed with no
  description.
- **Exercise sanity.** Fewer than 3 or more than 6 exercises; an exercise
  identical to another.

Tier 1 findings should surface in the preview immediately after conversion,
before the user decides whether to pay for Tier 2.

### Tier 2 — semantic review by Claude

A second `claude -p` call, same subscription route as the conversion, reading
the same absolute image paths plus the markdown produced from them.

What it is asked to look for:

- **Transcription errors against the page.** A symbol read wrongly, a digit
  dropped, a sign flipped, a subscript read as a superscript.
- **Broken patterns.** A sequence, series, or enumeration where one element does
  not follow the pattern the others establish — the `x^2` case.
- **Internal inconsistency.** A quantity defined one way and used another; a
  derivation step that does not follow from the one above it; a result that
  contradicts a statement earlier in the note.
- **Unsupported content.** An exercise that tests material not in the note; a
  summary claiming coverage the note does not contain; a diagram description
  that does not match what the image shows.
- **Missed content.** Something legible on the page that the transcription
  dropped entirely.

Explicitly *not* in scope: correcting the source material. If the handwritten
note itself contains a mistake, the transcription should keep it. The review
checks fidelity to the page and internal consistency, not whether the student
was right. A finding may say "the page says this, and it appears to be an error
in the original" — flagged, never silently fixed.

## Response shape

```json
{
  "findings": [
    {
      "severity": "high" | "medium" | "low",
      "kind": "transcription" | "pattern" | "consistency" | "unsupported" | "omission",
      "page": 2,
      "quote": "x_1^0, x_2^0, x^2, \\dots, x_{n_0}^0",
      "issue": "The third term breaks the sequence; the page shows a subscript 3.",
      "suggestion": "x_1^0, x_2^0, x_3^0, \\dots, x_{n_0}^0",
      "confidence": "high" | "medium" | "low"
    }
  ],
  "verdict": "clean" | "minor" | "needs-attention"
}
```

`quote` must be an exact substring of the markdown so the UI can locate and
highlight it. Reject a finding whose quote does not appear — that is the cheap
guard against a hallucinated location.

## UI

In the preview pane and in the library detail view:

- A **Check transcription** button. Tier 1 runs instantly and free; Tier 2 shows
  the usual streaming progress.
- Findings render as a list under the preview, each with severity, the quoted
  span, the issue, and the suggested replacement.
- Clicking a finding scrolls to and highlights that span in the rendered view.
- **Apply** swaps `quote` for `suggestion` in the markdown; **Dismiss** hides
  it. Applying is a plain string replace on an exact substring, so it is
  reversible by undo in the editor.
- Never auto-apply. A wrong "fix" to a correct transcription is worse than the
  original error, because the user has been told it was checked.

## Server

```
POST /api/notes/{slug}/review      -> SSE, same envelope as /api/convert
POST /api/review                   -> same, for an unsaved note (session_id + markdown)
```

Reuses `claude_client.convert_pages`' subprocess plumbing: the same
`stream-json` parsing, the same error paths, the same env stripping. The only
new parts are the prompt and the payload schema, so most of `claude_client.py`
should be generalised rather than copied — extract the "run a prompt over these
pages and parse JSON back" core, and have conversion and review both call it.

## Cost

A review re-reads every page image, so it costs on the order of a conversion:
roughly 2,700–2,900 visual tokens per page plus output. For a 4-page note that
is a second call of similar size. Worth it for a dense maths lecture, wasteful
for a page of prose — hence opt-in, and hence Tier 1 running first so an obvious
structural problem never costs a Claude call.

## Open questions

- Should a review run against the *edited* markdown or the original? Edited, so
  the user can fix something and re-check — but then the quotes must be located
  in the current text, which the exact-substring rule already handles.
- Is a cheaper model adequate for Tier 2? A review is arguably harder than the
  transcription, so probably not Haiku. Worth measuring on a real note before
  assuming Sonnet is enough.
- Should findings be stored alongside the note, so a note carries its review
  history? Probably yes for `verdict`, as front matter (`reviewed: 2026-09-24`),
  so the library can show which notes have been checked.
