"""Voice-chat memory: env-driven memory-source registry + voice-owned learnings.

Three concerns:

1. **Memory-source registry** — env-named (path, header) pairs the mint pathway
   prepends to Realtime instructions. Empty/unset paths skip silently, so the
   public default is "no extra context"; deployments that already have a
   personal-assistant memory bank can wire it in via .env without code changes.

2. **Voice-owned learnings file** — one writable file (path from env), `§`-
   separated paragraphs matching the convention shared with peer memory files.
   Atomic writes via tempfile+rename. Never invent paragraphs from thin air;
   every entry carries timestamp + conv_id inline.

3. **Recent-calls transcript index** — small JSON the mint pathway renders as a
   compact "map" so the model knows which prior calls exist. Expansion happens
   on demand via the `recall_recent_call` tool, not by inlining transcripts.

All readers gracefully tolerate unset env vars and missing files. Mint must
never raise on memory-loading failure.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ttyc.voice_memory")

# --- Memory-source registry ------------------------------------------------
# Each source: (path env var, header env var, default header).
# Order = order in the rendered instruction suffix.
_MEMORY_SOURCES: list[tuple[str, str, str]] = [
    ("VOICE_MEMORY_USER_PROFILE_PATH", "VOICE_MEMORY_USER_PROFILE_HEADER", "User profile"),
    ("VOICE_MEMORY_SHARED_PATH",       "VOICE_MEMORY_SHARED_HEADER",       "Shared assistant memory"),
    ("VOICE_MEMORY_VOICE_PATH",        "VOICE_MEMORY_VOICE_HEADER",        "Voice chat learnings"),
]

# Per-source cap to bound prompt size. VOICE.md grows over time; older entries
# stay on disk but only the recency window lands in the prompt.
_MEMORY_BLOCK_CHAR_CAP = 5000

_TRANSCRIPT_INDEX_HEADER = "Recent voice calls"
_TRANSCRIPT_INDEX_MAX_ENTRIES = 30
_TRANSCRIPT_INDEX_RENDER_LIMIT = 5

# Cache to avoid re-reading files on every session mint (mint is hot path).
_CACHE_TTL_SEC = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, loader):
    """Tiny TTL cache for file reads. Keyed by a path-or-tag string."""
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def _resolve(path_str: str | None) -> Path | None:
    if not path_str:
        return None
    p = Path(os.path.expanduser(path_str.strip()))
    return p if str(p) else None


def _read_text_safely(path: Path, cap_chars: int | None = None) -> str | None:
    try:
        if not path.exists():
            return None
        body = path.read_text()
        if cap_chars and len(body) > cap_chars:
            # Keep the *head* — VOICE.md and peer files put most-recent at top
            # by convention.
            body = body[:cap_chars].rstrip() + "\n…"
        return body.strip() or None
    except Exception:  # noqa: BLE001
        log.exception("voice_memory: read failed for %s", path)
        return None


def load_memory_sources() -> list[dict]:
    """Return ordered list of `{header, body}` blocks for configured memory sources.

    Skips silently when:
      - the path env var is unset/empty,
      - the file doesn't exist,
      - the file is empty after strip,
      - read raises.

    No exception leaves this function.
    """
    def _load() -> list[dict]:
        out: list[dict] = []
        for path_env, header_env, default_header in _MEMORY_SOURCES:
            path = _resolve(os.getenv(path_env))
            if not path:
                continue
            body = _read_text_safely(path, cap_chars=_MEMORY_BLOCK_CHAR_CAP)
            if not body:
                continue
            header = (os.getenv(header_env) or default_header).strip()
            out.append({"header": header, "body": body, "source": path_env})
        return out
    return _cached("memory_sources", _load)


def render_memory_sources_markdown() -> list[str]:
    """Return one markdown string per loaded source, headered for prompt injection.

    Returns [] when no sources are configured/loaded — caller can splice into
    `instructions_suffixes` directly.
    """
    return [f"## {b['header']}\n\n{b['body']}" for b in load_memory_sources()]


# --- Voice-owned learnings file -------------------------------------------

def _voice_path() -> Path | None:
    return _resolve(os.getenv("VOICE_MEMORY_VOICE_PATH"))


def _write_text_atomic(path: Path, body: str) -> None:
    """Atomic-replace pattern matching dossier._write_atomic. Creates parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, path)


def append_voice_learning(paragraphs: list[str], *, conv_id: str | None = None) -> int:
    """Prepend `§`-separated paragraphs to the voice-learnings file.

    Recency-at-top mirrors the convention used by peer memory files. Each
    paragraph is dated inline (caller may further tag with conv_id; we add a
    timestamp prefix if the paragraph doesn't already begin with `[`).

    Returns the number of paragraphs written. Zero when:
      - no path configured,
      - empty input,
      - all paragraphs blank.

    Never raises.
    """
    if not paragraphs:
        return 0
    path = _voice_path()
    if not path:
        return 0
    cleaned: list[str] = []
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tag_prefix = f"[{stamp}"
    if conv_id:
        tag_prefix += f" conv={conv_id}"
    tag_prefix += "]"
    for p in paragraphs:
        if not isinstance(p, str):
            continue
        text = p.strip()
        if not text:
            continue
        if not text.startswith("["):
            text = f"{tag_prefix} {text}"
        cleaned.append(text)
    if not cleaned:
        return 0
    try:
        existing = path.read_text() if path.exists() else ""
    except Exception:  # noqa: BLE001
        log.exception("voice_memory: read-before-append failed; recreating %s", path)
        existing = ""
    new_block = "\n§\n".join(cleaned)
    if existing.strip():
        # Insert new entries at the top so recency-first holds.
        merged = new_block + "\n§\n" + existing.lstrip()
    else:
        merged = new_block + "\n"
    try:
        _write_text_atomic(path, merged)
    except Exception:  # noqa: BLE001
        log.exception("voice_memory: write failed for %s", path)
        return 0
    # Invalidate cache so the next mint sees the update.
    _cache.pop("memory_sources", None)
    log.info("voice_memory: appended %d learning(s) to %s", len(cleaned), path)
    return len(cleaned)


# --- Transcript index ------------------------------------------------------

def _index_path() -> Path | None:
    return _resolve(os.getenv("VOICE_MEMORY_TRANSCRIPT_INDEX_PATH"))


def load_transcript_index(limit: int = _TRANSCRIPT_INDEX_RENDER_LIMIT) -> list[dict]:
    """Return last N transcript-index entries (most recent first). Empty list
    on missing file/path."""
    def _load() -> list[dict]:
        path = _index_path()
        if not path or not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            log.exception("voice_memory: index read failed for %s", path)
            return []
        if not isinstance(data, list):
            return []
        # Stored newest-first; defensive sort.
        try:
            data = sorted(data, key=lambda e: e.get("started_at", 0), reverse=True)
        except Exception:  # noqa: BLE001
            pass
        return data
    full = _cached("transcript_index", _load)
    return full[:limit] if limit else full


def update_transcript_index(*, conv_id: str, started_at: float,
                            duration_s: int, one_liner: str,
                            transcript_path: str | None = None) -> bool:
    """Upsert one entry; cap file at MAX entries; atomic-write JSON. Returns True
    on success, False when no path is configured or on error.
    """
    if not conv_id:
        return False
    path = _index_path()
    if not path:
        return False
    try:
        existing = json.loads(path.read_text()) if path.exists() else []
        if not isinstance(existing, list):
            existing = []
    except Exception:  # noqa: BLE001
        log.exception("voice_memory: index pre-read failed; replacing %s", path)
        existing = []
    # Drop any prior entry with the same conv_id.
    existing = [e for e in existing if e.get("conv_id") != conv_id]
    entry = {
        "conv_id": conv_id,
        "started_at": float(started_at),
        "duration_s": int(duration_s),
        "one_liner": (one_liner or "").strip()[:240],
    }
    if transcript_path:
        entry["transcript_path"] = transcript_path
    existing.insert(0, entry)
    # Newest-first cap.
    existing = sorted(existing, key=lambda e: e.get("started_at", 0), reverse=True)[
        :_TRANSCRIPT_INDEX_MAX_ENTRIES
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("voice_memory: index write failed for %s", path)
        return False
    _cache.pop("transcript_index", None)
    log.info("voice_memory: index updated for conv=%s (%d total)", conv_id, len(existing))
    return True


def render_transcript_index_markdown() -> str | None:
    """Compact map of recent calls for prompt injection. None when empty/unset.

    Format keeps it terse so token cost stays low — model can expand any
    entry via `recall_recent_call(conv_id)`.
    """
    rows = load_transcript_index()
    if not rows:
        return None
    lines = [f"## {_TRANSCRIPT_INDEX_HEADER}", ""]
    lines.append(
        "(Use `recall_recent_call(conv_id)` to pull excerpts. "
        "Don't restate these unprompted — they're a map, not a script.)"
    )
    lines.append("")
    for r in rows:
        try:
            started = datetime.fromtimestamp(r.get("started_at", 0)).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            started = "?"
        dur = r.get("duration_s") or 0
        mm = dur // 60
        ss = dur % 60
        dur_str = f"{mm}m{ss:02d}s" if mm else f"{ss}s"
        one_liner = (r.get("one_liner") or "").strip() or "(no summary)"
        lines.append(f"- `{r.get('conv_id','?')}` ({started}, {dur_str}): {one_liner}")
    return "\n".join(lines)


# --- Cache invalidation helper for tests -----------------------------------

def _reset_cache() -> None:
    _cache.clear()
