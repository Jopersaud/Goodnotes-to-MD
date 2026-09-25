"""An append-only record of what each Claude call cost.

Every conversion and exercise run draws on the same weekly and session
allowance as interactive Claude Code and claude.ai. The CLI reports exactly
what a call used, and without somewhere to put it that number is gone the
moment the request finishes. This keeps it, so the app can answer "what have I
spent on this today" without guessing.

One JSONL line per call. No database, matching the rest of the app.
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import config

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)

# Stop the ledger growing without bound on a machine used for years.
MAX_ENTRIES = int(os.environ.get("GNMD_USAGE_MAX_ENTRIES", "5000"))


def _path() -> Path:
    return config.USAGE_LOG


def record(
    kind: str,
    usage: dict[str, Any],
    *,
    slug: str | None = None,
    title: str | None = None,
    pages: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Append one call to the ledger. Never raises into the request path."""
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "slug": slug,
        "title": title,
        "pages": pages,
        "model": model or (usage.get("models") or [None])[0],
        "cost_usd": usage.get("cost_usd"),
        "duration_ms": usage.get("duration_ms"),
    }
    for field in TOKEN_FIELDS:
        entry[field] = usage.get(field) or 0

    try:
        with _path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        # Losing a usage line must never fail the conversion that produced it.
        pass
    return entry


def _read() -> list[dict[str, Any]]:
    path = _path()
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except OSError:
        return []
    return entries[-MAX_ENTRIES:]


def _totals(entries: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {field: 0 for field in TOKEN_FIELDS}
    totals["calls"] = len(entries)
    totals["cost_usd"] = 0.0
    for entry in entries:
        for field in TOKEN_FIELDS:
            value = entry.get(field)
            if isinstance(value, int):
                totals[field] += value
        cost = entry.get("cost_usd")
        if isinstance(cost, (int, float)):
            totals["cost_usd"] += cost
    return totals


def summary(recent: int = 10) -> dict[str, Any]:
    """Totals for today, the last seven days, and all time, plus recent calls."""
    entries = _read()
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=6)).isoformat()

    def on_or_after(cutoff: str) -> list[dict[str, Any]]:
        return [e for e in entries if str(e.get("at", ""))[:10] >= cutoff]

    return {
        "today": _totals([e for e in entries if str(e.get("at", "")).startswith(today)]),
        "week": _totals(on_or_after(week_ago)),
        "all": _totals(entries),
        "recent": list(reversed(entries[-recent:])),
        "path": str(_path()),
    }


def prune() -> None:
    """Rewrite the ledger keeping only the newest MAX_ENTRIES lines."""
    entries = _read()
    try:
        with _path().open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def touch() -> None:
    """Create the ledger file so the first read does not have to special-case it."""
    try:
        _path().touch(exist_ok=True)
    except OSError:
        pass


_LAST_PRUNE = 0.0


def maybe_prune(interval_seconds: int = 3600) -> None:
    global _LAST_PRUNE
    now = time.time()
    if now - _LAST_PRUNE > interval_seconds:
        _LAST_PRUNE = now
        prune()
