"""recall_recent_call backend.

Reads a prior call's transcript from `$TRANSCRIPT_DIR/<conv_id>.json` and
returns a focused excerpt — either the whole thing trimmed to `max_chars`, or
a substring-filtered window when `query` is provided.

Sibling tools are HTTP-bound (Supabase, GWS); this one is local-file-only, so
~10ms p99. Cached upstream by the toolkit's per-tool TTL.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("ttyc.backends.transcripts_recall")

TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "./transcripts")).expanduser()


def _format_entries(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for e in entries:
        role = e.get("role", "?")
        text = (e.get("text") or "").strip()
        if not text:
            continue
        if role in ("tool_question", "tool_answer"):
            # Compact summary so a tool-heavy transcript doesn't drown the recall window.
            tool = e.get("tool") or "?"
            preview = text if len(text) <= 160 else text[:160] + "…"
            lines.append(f"[{role} {tool}] {preview}")
        else:
            lines.append(f"{role}: {text}")
    return lines


async def recall_recent_call(
    *, conv_id: str, query: str | None = None, max_chars: int = 3000,
) -> dict[str, Any]:
    """Return an excerpt from a prior call's transcript.

    Args:
      conv_id: the conv_id from the recent-calls map in instructions.
      query: optional substring to filter entries by (case-insensitive). When
        omitted, returns the tail of the transcript.
      max_chars: cap on the returned text body. Default 3000 (≈750 tokens).

    Returns:
      {"conv_id", "query", "excerpt", "match_count", "total_entries"} on success;
      {"error": "not_found", "conv_id"} when the transcript doesn't exist.
    """
    if not conv_id:
        return {"error": "missing_conv_id"}
    path = TRANSCRIPT_DIR / f"{conv_id}.json"
    if not path.exists():
        return {"error": "not_found", "conv_id": conv_id}
    try:
        payload = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        log.exception("recall: failed to parse %s", path)
        return {"error": "parse_failed", "conv_id": conv_id}
    entries = payload.get("entries") or []
    lines = _format_entries(entries)
    total = len(lines)
    match_count = total
    if query and query.strip():
        q = query.strip().lower()
        filtered = [ln for ln in lines if q in ln.lower()]
        match_count = len(filtered)
        if filtered:
            lines = filtered
    body = "\n".join(lines)
    if len(body) > max_chars:
        # Prefer the *tail* — most recent material is usually most useful.
        body = "…\n" + body[-max_chars:]
    return {
        "conv_id": conv_id,
        "query": query or "",
        "excerpt": body,
        "match_count": match_count,
        "total_entries": total,
    }
