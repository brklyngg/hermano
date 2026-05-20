"""In-process agent stub for quickstart-without-backend.

Activated when `AGENT_API_BASE` starts with `stub://`. The sidecar's two
external entry points to the agent backend are:

  - `dossier._agent_call_json(prompt, ...)` — JSON-only single-shot call
  - `server._agent_chat_stream(conv_id, user_text)` — streaming SSE generator

Both detect the stub scheme and call into this module instead of making an
HTTP request. Stub responses are heuristic: we sniff the prompt for keywords
and return canned content shaped to match what the live agent would produce,
just enough to verify the full plumbing — narrow tool dispatch, dossier
refresh, deep-research milestone narration, post-call extraction.

This is NOT a production backend. It's a smoke-test fixture.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import AsyncIterator

log = logging.getLogger("ttyc.backends.stub_agent")


def is_stub() -> bool:
    base = (os.getenv("AGENT_API_BASE") or "").strip().lower()
    return base.startswith("stub://")


# --- Heuristic prompt classification ---------------------------------------

_DOSSIER_HINTS = ("open_loops", "calendar_today", "hot_people", "last_handoff_summary")
_EXTRACT_HINTS = ("decisions", "commitments", "voice_learnings", "call_one_liner")


def _classify(prompt: str) -> str:
    p = prompt.lower()
    if any(h in p for h in _EXTRACT_HINTS) and "transcript" in p:
        return "extract_working_state"
    if any(h in p for h in _DOSSIER_HINTS):
        return "dossier_refresh"
    return "deep_research"


# --- Canned payloads --------------------------------------------------------

_DOSSIER_JSON = {
    "open_loops": [
        {"id": "stub-loop-1", "column": "todo",       "priority": "normal", "title": "Smoke-test the voice stack end-to-end"},
        {"id": "stub-loop-2", "column": "in_review",  "priority": "high",   "title": "Decide whether to wire a real agent backend"},
    ],
    "calendar_today": [
        {"start": "10:00", "end": "10:30", "title": "Quickstart walkthrough"},
        {"start": "14:00", "end": "15:00", "title": "Demo to a friend"},
    ],
    "recent_decisions": [
        {"date": "2026-05-18", "decision": "Use the stub backend for first-run quickstart",
         "context": "Avoids needing a personal agent on day one"},
    ],
    "hot_people": [
        {"name": "First-time reader", "role": "you, probably",
         "recent_context": "Trying to figure out whether this thing is worth running"},
    ],
    "last_handoff_summary": (
        "This is a stub-backed dossier. The voice agent has enough scaffolding "
        "to demonstrate the dossier-grounded flow; swap AGENT_API_BASE to your "
        "real backend when you're ready."
    ),
}

_EXTRACT_JSON = {
    "decisions": [],
    "commitments": [],
    "open_questions": [],
    "deltas": [],
    "voice_learnings": [],
    "call_one_liner": "Stub backend run — no real learnings extracted.",
}

_DEEP_RESEARCH_SECTIONS = [
    "## Quick orientation",
    "",
    "You're talking to the bundled stub backend. The voice model just called "
    "`deep_research`, and this response is being streamed back section by section "
    "to exercise the milestone-narration path. None of this is real research — "
    "swap `AGENT_API_BASE` to a real chat-completions SSE endpoint to get actual "
    "answers.",
    "",
    "## What works end-to-end with the stub",
    "",
    "The dossier load, narrow-tool dispatch, deep-research streaming, milestone "
    "narration, post-call working-state extraction, and the new voice_memory "
    "registry are all exercised. You can run a full call, ask the model to "
    "research something, and watch the consulting chip and OOB narration fire.",
    "",
    "## What's stubbed out",
    "",
    "All substantive cognition. The stub returns deterministic canned content "
    "regardless of the prompt — it's a fixture, not a model. Replace it the "
    "moment you want real answers.",
]


# --- Public entry points ----------------------------------------------------

async def call_json(prompt: str) -> dict:
    """Single-shot JSON return for `dossier._agent_call_json` consumers.

    Classifies the prompt and returns the matching canned dict. Sleeps a tiny
    bit so callers see a non-zero latency in NDJSON.
    """
    kind = _classify(prompt)
    log.info("stub_agent.call_json: classified as %s", kind)
    await asyncio.sleep(0.05)
    if kind == "dossier_refresh":
        return dict(_DOSSIER_JSON)
    if kind == "extract_working_state":
        return dict(_EXTRACT_JSON)
    # deep_research-shaped prompt asked via the JSON entry point — unusual but
    # safe fallback.
    return {"answer": "\n".join(_DEEP_RESEARCH_SECTIONS)}


async def stream(prompt: str) -> AsyncIterator[str]:
    """Streaming chunks for `server._agent_chat_stream` consumers.

    For deep_research-shaped prompts we yield section-by-section so the
    milestone-narration UX fires. For JSON-shaped prompts we emit the JSON
    body in one chunk (caller parses it whole).
    """
    kind = _classify(prompt)
    log.info("stub_agent.stream: classified as %s", kind)
    if kind == "dossier_refresh":
        yield json.dumps(_DOSSIER_JSON)
        return
    if kind == "extract_working_state":
        yield json.dumps(_EXTRACT_JSON)
        return
    # deep_research path — emit section by section with a short delay so the
    # browser-side milestone detector trips on each `\n## ` boundary.
    for chunk in _DEEP_RESEARCH_SECTIONS:
        yield chunk + "\n"
        await asyncio.sleep(0.4)


async def stream_lines(prompt: str) -> AsyncIterator[str]:
    """Convenience for callers that prefer one-line-per-yield framing."""
    async for chunk in stream(prompt):
        for line in chunk.splitlines() or [""]:
            yield line
