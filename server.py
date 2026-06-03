"""Hermano — local voice sidecar.

Browser <-> OpenAI Realtime (WebRTC, direct) <-> your agent (via this proxy).

Endpoints:
  GET  /                   - UI
  GET  /static/*           - assets
  GET  /api/health         - open (for launchd / monitoring)
  POST /api/session        - mints OpenAI Realtime ephemeral key
  POST /api/ask-agent      - proxies a single user turn to your agent
  POST /api/text-turn      - same as ask-agent but framed as the dual-mode
                             text channel (UI shows in the agent-text bubble)
  POST /api/end            - persists transcript + ingests into agent memory
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict

import httpx
from aiohttp import web
from dotenv import load_dotenv

# Load env from sibling .env then process env (process env wins for OPENAI_API_KEY)
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from auth import tailnet_middleware  # noqa: E402
import dossier  # noqa: E402
import voice_memory  # noqa: E402
import honcho_voice  # noqa: E402
from events import log_call_event, compute_routing_metrics  # noqa: E402
from transcripts import write_transcript, ingest_into_agent, post_to_slack  # noqa: E402
# Imported at module load (after load_dotenv) so the account roster is available
# when the tool schemas below are built. backends.gws is stdlib-only — no cycle.
from backends.gws import (  # noqa: E402
    ALLOWED_ACCOUNTS as GWS_ALLOWED_ACCOUNTS,
    DEFAULT_ACCOUNT as GWS_DEFAULT_ACCOUNT,
)

LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "server.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("ttyc.server")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "alloy")
# Realtime context-truncation cost lever. With semantic_vad, idle/wait silence is
# free; the real spend driver is per-turn context re-billing (the whole
# conversation is re-sent each turn). `post_instructions` caps conversation tokens
# *after* the instruction block, bounding per-turn input regardless of call length;
# `retention_ratio` < 1.0 drops extra on truncation so the next (cache-busting)
# truncation lands later. Docs: https://developers.openai.com/api/docs/guides/realtime-costs
REALTIME_TRUNCATION_RETENTION_RATIO = float(
    os.getenv("REALTIME_TRUNCATION_RETENTION_RATIO", "0.8")
)
# Generous default. History: 10000 proved too low (model lost recent turns);
# 24000 still truncated saved working state on long, context-heavy calls
# (Gary flagged the continuity loss explicitly). Bumped to 48000 — continuity
# wins over marginal cost here, since semantic_vad means idle silence isn't
# billed and prompt caching absorbs most per-turn input cost. This caps only
# pathological runaway; set to 0 to omit the cap and use the full window.
# DO NOT hand-roll `conversation.item.delete` pruning as a cheaper alternative —
# it busts the cached prefix and is net-negative (see CLAUDE.md cost-controls).
REALTIME_POST_INSTRUCTIONS_TOKENS = int(
    os.getenv("REALTIME_POST_INSTRUCTIONS_TOKENS", "48000")
)
# Operator's first name — used in prompts and tool-schema descriptions so the
# model addresses them naturally instead of saying "the user". Default keeps
# the public scaffold clean; personal deployments set this in .env.
OPERATOR_NAME = os.getenv("OPERATOR_NAME", "the user").strip() or "the user"
# Comma- or space-separated proper nouns the audio transcriber should bias
# toward. Improves recognition of names/jargon you say often (people, products,
# acronyms). Empty default; personal deployments populate via .env.
OPENAI_REALTIME_TRANSCRIPTION_HINTS = os.getenv("OPENAI_REALTIME_TRANSCRIPTION_HINTS", "").strip()


def _account_label(addr: str) -> str:
    """Human label for a Google Workspace address so the model can map
    'my personal email' / 'crunchy' / 'flowocity' to a real account. Custom
    domains use the domain stem; gmail uses 'personal' for the default account
    and the local handle for any others (avoids two indistinct 'personal's)."""
    local, _, domain = addr.partition("@")
    if domain and domain != "gmail.com":
        return domain.split(".")[0]  # crunchy.tools -> crunchy, flowocity.ai -> flowocity
    if addr == GWS_DEFAULT_ACCOUNT:
        return "personal"
    return (local.split(".")[0] or "gmail")  # jerome.cbmb -> jerome


def _gws_account_desc() -> str:
    """Tool-schema description for the `account` arg, enumerating the REAL
    configured accounts (not the env-var name) so the model stops inventing
    addresses. Falls back to a generic line when none are configured."""
    if not GWS_ALLOWED_ACCOUNTS:
        return "The Google Workspace account to query. Omit to use the default."
    listed = ", ".join(f"{a} ({_account_label(a)})" for a in sorted(GWS_ALLOWED_ACCOUNTS))
    default = GWS_DEFAULT_ACCOUNT or "the first configured"
    return (
        f"Which Google Workspace account to query. Configured accounts: {listed}. "
        f"Use these EXACT addresses — never guess or invent one. Omit to use the "
        f"default ({default})."
    )


def _accounts_brief() -> str | None:
    """Markdown block listing the real account roster, injected into session
    instructions so the model always knows which inboxes/calendars exist."""
    if not GWS_ALLOWED_ACCOUNTS:
        return None
    lines = [f"- {a} — {_account_label(a)}" for a in sorted(GWS_ALLOWED_ACCOUNTS)]
    default = GWS_DEFAULT_ACCOUNT or sorted(GWS_ALLOWED_ACCOUNTS)[0]
    return (
        "## Google Workspace accounts\n"
        "These are the ONLY email/calendar accounts that exist. Use the exact "
        "address; never invent one. When searching for a person's mail and the "
        f"default account ({default}) returns nothing, fan out across the others "
        "before concluding it isn't there.\n" + "\n".join(lines)
    )
AGENT_API_BASE = os.getenv("AGENT_API_BASE", "http://127.0.0.1:8642").rstrip("/")
# Liveness model for ask_agent forwards:
#   - ASK_AGENT_IDLE_TIMEOUT_SEC: primary watchdog, surfaced via httpx's `read`
#     timeout. Raises httpx.ReadTimeout if no SSE bytes arrive within the
#     window mid-stream — catches a hung backend without killing legitimately
#     slow streaming work.
#   - ASK_AGENT_TIMEOUT_SEC: runaway guard via asyncio.wait_for. Catches the
#     pathological "backend dribbles forever" case.
ASK_AGENT_TIMEOUT_SEC = float(os.getenv("ASK_AGENT_TIMEOUT_SEC", "600"))
ASK_AGENT_IDLE_TIMEOUT_SEC = float(os.getenv("ASK_AGENT_IDLE_TIMEOUT_SEC", "45"))
HOST = os.getenv("VOICE_HOST", "127.0.0.1")
PORT = int(os.getenv("VOICE_PORT", "8090"))
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CALL_CHANNEL_ID = os.environ.get("SLACK_CALL_CHANNEL_ID", "").strip()

# Agent API key. Prefer env var; fall back to a sibling file (chmod 600) for
# operators who'd rather not put a long-lived key in their shell rc.
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", "").strip()
if not AGENT_API_KEY:
    _key_file = HERE / ".agent-api-key"
    if _key_file.exists():
        AGENT_API_KEY = _key_file.read_text().strip()

if not AGENT_API_KEY and not (os.getenv("AGENT_API_BASE", "").startswith("stub://")):
    log.warning(
        "AGENT_API_KEY not set - dossier refresh, deep_research, and post-call "
        "extraction will be skipped. Set AGENT_API_BASE=stub:// for quickstart."
    )
if not OPENAI_API_KEY:
    log.warning("OPENAI_API_KEY not in env - /api/session will 500")

# Active conversations: conv_id -> {
#   started_at: float, last_activity_ts: float,
#   entries: [...],            # transcript items (user/assistant/tool_question/tool_answer)
#   handoff_note: str?,        # ≤80-word continuation note from a dying session
# }
CONVERSATIONS: Dict[str, Dict[str, Any]] = {}

# Idle-reap window. Backgrounding no longer ends a call, so we use this as
# the cost guard instead of the prior 65-min started_at ceiling.
REAPER_INTERVAL_SEC = 60
# Lowered default (was 20min): with semantic_vad an idle open session bills ~0 for
# silence, but ending sooner caps spurious-VAD re-bills if the user walks away
# mid-conversation. Env-configurable for tuning against cost telemetry.
REAPER_IDLE_TIMEOUT_SEC = int(os.getenv("REAPER_IDLE_TIMEOUT_SEC", str(10 * 60)))


def _touch(conv: Dict[str, Any]) -> None:
    conv["last_activity_ts"] = time.time()


def _ensure_conv_shape(conv: Dict[str, Any]) -> None:
    """Backfill optional keys so existing in-memory conversations from older
    server versions don't KeyError after a code update."""
    conv.setdefault("entries", [])
    conv.setdefault("last_activity_ts", conv.get("started_at", time.time()))
    conv.setdefault("research_inflight", set())  # in-flight deep_research call_ids


# Granular tool schemas. Each fans out to a direct backend (Supabase / gws /
# Obsidian fs) and returns small structured JSON — no LLM in the dispatch
# path. The model prefers these over `deep_research` for any factual lookup;
# `deep_research` is reserved for novel reasoning, drafting, or synthesis no
# narrow tool covers.
LOOKUP_OPEN_LOOP_SCHEMA = {
    "type": "function",
    "name": "lookup_open_loop",
    "description": (
        "Look up a single open loop / Mission Control card by its UUID. Prefer "
        "this over `deep_research` whenever the user references a specific loop. "
        "The dossier in your instructions lists today's open loops with IDs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Card UUID (e.g. from §open_loops in the dossier)"},
        },
        "required": ["id"],
    },
}

RECENT_DECISIONS_SCHEMA = {
    "type": "function",
    "name": "recent_decisions",
    "description": (
        "List recent decisions / committed actions from the journal. Use for "
        "'what did we decide about X' or 'what's been going on this week.' "
        "Returns structured rows you can quote directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "Look-back window (1–30)", "default": 7},
        },
        "required": [],
    },
}

SEARCH_NOTES_SCHEMA = {
    "type": "function",
    "name": "search_notes",
    "description": (
        "Full-text search the user's Obsidian vault. Use for 'what did I write "
        "about X', 'find the note on Y', or to surface adjacent context. "
        "Returns top-k matches with snippets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "description": "Max results (1–20)", "default": 5},
        },
        "required": ["query"],
    },
}

CALENDAR_SCHEMA = {
    "type": "function",
    "name": "calendar",
    "description": (
        "Read calendar events for a window. Use for 'what's on for today', "
        "'tomorrow's meetings', or specific dates. Always prefer this over "
        "`deep_research` for scheduling questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "Accepted: 'today', 'tomorrow', 'this week', YYYY-MM-DD, "
                    "or 'YYYY-MM-DD/YYYY-MM-DD' for a range. Defaults to today."
                ),
            },
            "account": {
                "type": "string",
                "description": _gws_account_desc(),
            },
        },
        "required": [],
    },
}

GMAIL_SEARCH_SCHEMA = {
    "type": "function",
    "name": "gmail_search",
    "description": (
        "Search Gmail with a query. Use Gmail's native search syntax "
        "(`from:foo`, `subject:bar`, `newer_than:3d`). Returns top messages "
        "with from/subject/snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search syntax"},
            "account": {
                "type": "string",
                "description": _gws_account_desc(),
            },
            "limit": {"type": "integer", "description": "Max results (1–25)", "default": 10},
        },
        "required": ["query"],
    },
}

MISSION_CONTROL_CARD_SCHEMA = {
    "type": "function",
    "name": "mission_control_card",
    "description": (
        "Fuller view of a Mission Control card: title, status, description, "
        "extended context, position. Use when you need more detail than "
        "`lookup_open_loop` returns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Card UUID"},
        },
        "required": ["id"],
    },
}

DEEP_RESEARCH_TOOL_SCHEMA = {
    "type": "function",
    "name": "deep_research",
    "description": (
        "Slow agent path for anything no narrow tool covers — including ACTIONS "
        "with side effects (write/save files, draft and save emails, create "
        "calendar events, edit notes, run scripts) AND novel reasoning, "
        "drafting, or synthesis. The agent backend has full filesystem access, "
        "Gmail draft / Calendar write, and shell tools — use this tool for any "
        f"user request that requires *doing* something on {OPERATOR_NAME}'s machine, not just "
        "looking something up. Typically 30–240 seconds, but some tasks can run "
        "longer. Give an honest rough estimate only when you have one; otherwise "
        "say you'll work on it and report back. Don't default to 'about a minute'. Partial "
        "findings stream in via `[research-finding]` system messages — narrate "
        "them; don't claim completion until you receive the function_call_output. "
        "When the work includes an action, the function_call_output will include "
        "a concrete handle (file path, message id, event link) — only then say 'done'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Full natural-language request, with all relevant context. "
                    "For actions, state the goal AND the success criteria — e.g. "
                    "'Save this draft to ~/Desktop/foo.md and tell me the final path.' "
                    "The agent will interpret and execute; you don't need to "
                    "describe how."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["action", "drafting", "reasoning", "synthesis"],
                "description": (
                    "action: side-effecting work (filesystem, email draft, "
                    "calendar). drafting: write a document/message. reasoning: "
                    "novel multi-step thinking. synthesis: combine sources."
                ),
            },
            "expected_seconds": {
                "type": "integer",
                "description": "Your honest estimate; used for user expectations",
            },
        },
        "required": ["prompt", "scope", "expected_seconds"],
    },
}

CANCEL_RESEARCH_TOOL_SCHEMA = {
    "type": "function",
    "name": "cancel_research",
    "description": (
        "Stop deep_research that's currently running. Call this when the user "
        "redirects mid-research ('stop that', 'actually, look at X instead') or "
        "no longer wants the result. Frees you to start the new task immediately. "
        "By default cancels ALL in-flight research. Note: this stops you from "
        "waiting/narrating; a side-effecting action the agent already started may "
        "still finish on its own — don't claim it was undone."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief why, for the log (e.g. 'user redirected to pricing').",
            },
        },
        "required": [],
    },
}

RECALL_RECENT_CALL_SCHEMA = {
    "type": "function",
    "name": "recall_recent_call",
    "description": (
        "Pull an excerpt from a prior voice call's transcript. Use ONLY when the "
        "user references a specific recent conversation and the recent-calls map "
        "in your instructions has a matching conv_id. Don't fish; if the relevant "
        "conv_id isn't in the map, ask the user to remind you what call they mean."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conv_id": {
                "type": "string",
                "description": "The conv_id from the §Recent voice calls list in your instructions.",
            },
            "query": {
                "type": "string",
                "description": "Optional substring to filter the transcript by (case-insensitive).",
            },
        },
        "required": ["conv_id"],
    },
}

_ALL_TOOLKIT_SCHEMAS = [
    LOOKUP_OPEN_LOOP_SCHEMA,
    RECENT_DECISIONS_SCHEMA,
    SEARCH_NOTES_SCHEMA,
    CALENDAR_SCHEMA,
    GMAIL_SEARCH_SCHEMA,
    MISSION_CONTROL_CARD_SCHEMA,
    RECALL_RECENT_CALL_SCHEMA,
    DEEP_RESEARCH_TOOL_SCHEMA,
    CANCEL_RESEARCH_TOOL_SCHEMA,
]


TRIAGE_VERDICT_TOOL_SCHEMA = {
    "type": "function",
    "name": "triage_verdict",
    "description": (
        f"Record {OPERATOR_NAME}'s verdict on an open loop the moment a decision is reached. "
        f"DEFAULT TO 'drop' if {OPERATOR_NAME} signals indifference, fatigue, or vague intent. "
        "Only use 'park' for items they explicitly defer with a reason. "
        "Only use 'act' when they commit to a concrete next step with a date/time. "
        "Strategic discipline: maintenance/curiosity loops should drop unless they "
        "concretely unlock revenue, distribution, authority, or compounding capability."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "loop_id": {"type": "string", "description": "The ol_<hash> ID from the briefing"},
            "verdict": {"type": "string", "enum": ["drop", "park", "act"]},
            "next_action": {"type": "string", "description": f"For 'act' only: the concrete next step {OPERATOR_NAME} stated"},
            "calendar_when": {"type": "string", "description": "For 'act' only: ISO datetime or natural language"},
            "note": {"type": "string", "description": "Optional brief context"},
        },
        "required": ["loop_id", "verdict"],
    },
}


def _load_open_loops_brief() -> str | None:
    """Load the open-loops triage brief.

    When the on-disk brief is from a prior local date (e.g. first triage call
    after midnight), serve it anyway with a staleness header — the same
    pattern dossier.load_dossier() uses. Empty triage is worse than a
    yesterday's-snapshot triage.

    Imports lazily so the open-loops repo is only required when actually used.
    """
    try:
        ol_path = os.path.expanduser("~/.hermes-custom/open-loops")
        if ol_path not in sys.path:
            sys.path.insert(0, ol_path)
        from brief_format import render  # type: ignore
        import json
        from datetime import datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo
        out = render()
        if out:
            return _triage_guardrails(out)
        # render() returned None — could be missing file, empty bins, or
        # (most likely on first call after midnight) a stale date gate. Read
        # the file directly and try to recover with a staleness header.
        path = Path.home() / ".hermes" / "open-loops" / "today.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        gen_raw = data.get("generated_at")
        if not gen_raw:
            return None
        ny = ZoneInfo("America/New_York")
        try:
            gen_date = datetime.fromisoformat(gen_raw).astimezone(ny).date()
        except ValueError:
            return None
        # Spoof generated_at to now so render() bypasses its date gate, then
        # prepend a staleness header so the model knows.
        data["generated_at"] = datetime.now(ny).isoformat()
        body = render(today=data)
        if not body:
            return None
        return _triage_guardrails(body, stale_from=gen_date.isoformat())
    except Exception:
        log.exception("open-loops brief load failed")
        return None


_TRIAGE_GUARDRAILS = f"""\
TRIAGE TOOL RULES — overrides toolkit defaults:
- The context for every loop is RIGHT HERE in the brief (BLUF + source blob).
  Read both fields. Don't fetch.
- DO NOT call `lookup_open_loop` in triage mode. Those `ol_*` triage IDs are
  NOT the Mission Control UUIDs `lookup_open_loop` expects; the call will fail.
- DO NOT call `deep_research` for "let me get context on this loop" — the
  triage call is a fast pass, not a research session.
- DO NOT call `calendar`, `gmail_search`, `search_notes`, `recent_decisions`,
  or `mission_control_card` for routine triage. Use only `triage_verdict`.
- The only tool you should call in triage is `triage_verdict`, once per loop,
  with the `ol_*` ID directly from the brief.
- If a loop genuinely needs lookup before a verdict is possible, default-DROP
  it and move on — that's already the doctrine. Don't research your way out.

TRIAGE PRESENTATION (overrides the brief's "BLUF in <=8 words"):
- Rule 9b (CONTEXT-SUFFICIENCY) wins. {OPERATOR_NAME} has many parallel threads — a
  fragment title is not enough for them to recall what a loop is about.
- For each loop, before asking for a verdict, give 1–3 sentences of grounding
  pulled from the loop's `BLUF`, `source_blob`, scoring metadata (age, score,
  surfaced count), and surrounding signal: what it's about, why it surfaced,
  what's open. Be concrete; quote distinctive phrasing from the source blob
  if it helps disambiguate.
- THEN ask for the verdict — phrased naturally for that specific loop, not
  "drop, park, or act?" robotically. Example: "Want to kill this one, park it
  for later, or do something with it now?"
- If the BLUF is genuinely a thin fragment with no useful source_blob, SAY
  THAT plainly ("the entry for this is just a fragment — want me to skip it
  or pull it up?") rather than pretending it has meaning.
"""


def _triage_guardrails(body: str, *, stale_from: str | None = None) -> str:
    """Prepend the triage tool-use guardrails (and a staleness header if the
    brief was a yesterday's-snapshot fallback) to the rendered brief."""
    pre = []
    if stale_from:
        pre.append(
            f"> Triage brief — snapshot from {stale_from}; refresh in progress. "
            f"Loop states may have shifted; verify before acting."
        )
    pre.append(_TRIAGE_GUARDRAILS.rstrip())
    return "\n\n".join(pre) + "\n\n" + body


# Optional deployment-specific facts the realtime model can rely on without
# guessing. Inject things like "your filesystem-write tools are real" or
# "your logs live at <path>" so the model stops hallucinating refusals about
# its own capabilities. Leave empty for the generic public scaffold.
AGENT_DEPLOYMENT_NOTE = os.getenv("AGENT_DEPLOYMENT_NOTE", "").strip()

# Tool-call routing prompt. Rewritten May 2026 for the granular toolkit cutover:
# narrow tools hit direct backends (sub-second), `deep_research` is the only
# slow path. gpt-realtime-2's native parallel function calling means small
# lookups should fan out, not serialize. The dossier in your instructions
# carries today's standing context — address it directly.
_BASE_PROMPT = """\
You are a live voice assistant with deep context about the user. Your
DOSSIER (appended to these instructions) carries today's open loops,
calendar, recent decisions, hot people, and the working state from your
previous call. ADDRESS IT DIRECTLY — don't re-fetch what's already there.

TOOLKIT:
- Narrow read tools (sub-second): `lookup_open_loop`, `recent_decisions`,
  `search_notes`, `calendar`, `gmail_search`, `mission_control_card`.
- One slow tool: `deep_research` — covers BOTH (a) novel reasoning,
  drafting, synthesis, AND (b) any ACTION with side effects: write/save
  files, draft and save emails, create calendar events, edit notes, run
  scripts. The agent backend has full filesystem access, Gmail draft /
  Calendar write, and shell tools. Use this tool for any "do X for me"
  request, not just "think about X."

TOOL-CALL DOCTRINE:
1. Prefer the narrowest applicable read tool for factual lookups (calendar,
   email, notes, cards, decisions). Don't escalate read-only questions to
   `deep_research`.
2. Any user request that requires *doing* something (saving a file,
   drafting an email and saving it, creating a calendar event, editing
   a note, etc.) goes through `deep_research` with scope="action". The
   agent will perform the action and return a concrete handle.
3. Fan out small tools in parallel when a single turn needs multiple
   lookups. The API supports it — don't serialize "let me check your
   calendar… ok now let me check your email."
4. Prefer in-session reasoning when the answer is already in the dossier
   or a prior tool result this call. Don't re-fetch what's in your context.
5. `deep_research` is the slow path. When you call it: set expectations
   honestly. Give a rough duration only when you have a decent estimate;
   otherwise say you'll work on it and report back. Do NOT default to
   "about a minute" as filler.
   Partial findings stream in as `[research-finding] section=…` system
   messages — narrate them as they arrive; don't claim completion until
   you receive the function_call_output.
5b. You CAN run multiple `deep_research` tasks at once. If the user raises a
   second, independent task while one is already running, launch it in
   parallel — do NOT refuse with "one at a time" or make them wait. The only
   thing to avoid is firing the SAME request twice concurrently. When the user
   redirects mid-research ("stop that, do X instead") or no longer wants a
   running result, call `cancel_research` to stop it, then start the new task.
6. ANTI-FABRICATION: never invent user-specific facts. If you're unsure
   whether the dossier or a prior tool result covers a name, date, file,
   commitment, or decision, escalate to a tool rather than guess. "I'd
   need to check" beats a confident wrong answer; a tool call is better.

ANSWER-QUALITY RULES:
7. If a tool returns `{"error": …}`, say so plainly ("I couldn't reach
   your calendar — try again in a sec"). Never invent a fallback.
8. If a tool returns a list, enumerate briefly first ("you have three:
   A, B, C"), then synthesize. Don't blend distinct items into one
   fuzzy summary.
9. Don't synthesize beyond what the tool returned. If the data isn't
   there, say it isn't there.
9b. CONTEXT-SUFFICIENCY (load-bearing): the user is managing many parallel
    threads simultaneously. When you introduce ANY item — an open loop, a
    calendar event, an email, a card, a research finding, a draft for
    review — assume he does NOT remember it cold from a one-line title or
    summary. Before asking for a decision or proposing a next step, give
    enough disambiguating context that he can place the item in his head:
    what it's about, why it exists / how it surfaced, where it stands now,
    and what's open about it. Then offer the decision options or next
    step. Two to four sentences of grounding is usually right — not a
    headline, not an essay. If the dossier/brief summary is a thin
    fragment, expand it with what you can infer from surrounding context
    or say plainly "the summary is thin; want me to look it up?" Never
    lead with "Item N: drop, park, or act?" — that puts the cognitive
    load on him instead of you.

VERIFIED COMPLETION RULES:
10. Don't say "done", "sent", "created", "updated", or "saved" unless a
    tool response included a concrete handle — a file path, ID, link, or
    timestamp confirming the action. Drafted ≠ done. If you asked for an
    action via `deep_research` and the response didn't include a handle,
    say "I asked for it" rather than "done."

STYLE RULES:
11. Lead with the answer — the number, the time, the yes/no, the decision.
    Reasons after, only if asked or load-bearing.
12. Phone-call tempo: ≤2 sentences per turn unless asked for more. Numbers
    spoken naturally ("ten thirty", not "10:30 colon zero zero").

CAPABILITIES (TRUTH — DO NOT CONTRADICT):
- Questions about your model, voice, logs, or where data lives can be
  answered via `deep_research` if the dossier doesn't cover them. Don't
  improvise refusals like "I don't have access to that".

LANGUAGE:
- Always speak English unless the user explicitly switches mid-conversation.
"""

VOICE_SYSTEM_PROMPT = (
    _BASE_PROMPT
    + (("\nDEPLOYMENT NOTE:\n" + AGENT_DEPLOYMENT_NOTE + "\n") if AGENT_DEPLOYMENT_NOTE else "")
    + """
PERSONA:
Warm, dry-witted, and concise. Sound like a sharp colleague who doesn't waste
time. Avoid corporate hedging. Don't say "I can help you with that" - just help.
"""
)


# ---- helpers ---------------------------------------------------------------

def _new_conv_id() -> str:
    return secrets.token_urlsafe(12)


async def _agent_chat_stream(conv_id: str, user_text: str, *, session_suffix: str = ""):
    """Async generator yielding agent SSE chunks as they arrive.

    Used by `/api/deep-research` to emit section milestones mid-stream and
    by `_agent_chat_collect` to assemble a final string (dossier refresh,
    post-call extraction, /api/text-turn).

    `session_suffix` is appended to the `X-Session-Id` (`voice-{conv_id}{suffix}`)
    so a SECOND, concurrent deep_research on the same call lands in a distinct
    backend session — the agent stores history server-side keyed by this header,
    and two concurrent turns on one session id would interleave its working
    state. Independent parallel tasks don't need shared context, so isolating
    them is the safe default. Empty suffix (the sole/first call) keeps the base
    session for continuity across sequential turns.

    Liveness model:
      - httpx `read` timeout = ASK_AGENT_IDLE_TIMEOUT_SEC (raises ReadTimeout
        if the backend goes silent mid-stream).
      - Caller wraps in asyncio.wait_for(ASK_AGENT_TIMEOUT_SEC) for the outer
        runaway guard.
    """
    from backends import stub_agent
    if stub_agent.is_stub():
        async for chunk in stub_agent.stream(user_text):
            yield chunk
        return
    if not AGENT_API_KEY:
        raise RuntimeError("Agent API key not configured")
    timeout = httpx.Timeout(
        connect=10.0, read=ASK_AGENT_IDLE_TIMEOUT_SEC, write=10.0, pool=10.0,
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{AGENT_API_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {AGENT_API_KEY}",
                "X-Session-Id": f"voice-{conv_id}{session_suffix}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={
                "model": "agent",
                "stream": True,
                "messages": [{"role": "user", "content": user_text}],
            },
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                payload = line[len("data: "):]
                if payload == "[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = (evt.get("choices") or [{}])[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    yield piece


async def _agent_chat_collect(conv_id: str, user_text: str) -> str:
    """Drain the stream into a single string. Idle/timeout exceptions bubble."""
    chunks: list[str] = []
    async for piece in _agent_chat_stream(conv_id, user_text):
        chunks.append(piece)
    return "".join(chunks)


async def _agent_chat(conv_id: str, user_text: str) -> str:
    """Cap at ASK_AGENT_TIMEOUT_SEC. Raises asyncio.TimeoutError on overrun."""
    return await asyncio.wait_for(
        _agent_chat_collect(conv_id, user_text),
        timeout=ASK_AGENT_TIMEOUT_SEC,
    )


# ---- routes ----------------------------------------------------------------

async def health(request: web.Request) -> web.Response:
    agent_ok = False
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{AGENT_API_BASE}/health")
            agent_ok = r.status_code == 200
    except Exception:
        pass
    return web.json_response({
        "ok": True,
        "agent": agent_ok,
        # Back-compat for older Hermes-named PWA clients still cached on
        # devices. The pre-rename app.js looks for `hermes` here.
        "hermes": agent_ok,
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "openai_key": bool(OPENAI_API_KEY),
    })


def _instructions_audit(suffixes: list[str | None] | None) -> tuple[int, str]:
    """Compute (chars, sha1[:12]) of the exact instructions string the mint
    would send. Mirrors the assembly in `_mint_realtime_session` so logged
    hashes are directly comparable across calls."""
    parts = [VOICE_SYSTEM_PROMPT]
    for s in (suffixes or []):
        if s and s.strip():
            parts.append(s.strip())
    s = "\n\n".join(parts)
    return len(s), hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _build_instructions(suffixes: list[str | None] | None) -> str:
    """Mirror of the assembly in `_mint_realtime_session`. Kept in one place so
    the persisted packet matches the mint payload exactly."""
    parts = [VOICE_SYSTEM_PROMPT]
    for s in (suffixes or []):
        if s and s.strip():
            parts.append(s.strip())
    return "\n\n".join(parts)


_DEDUPE_RE = __import__("re").compile(r"^[\s>\-\*\+\d\.\)]+")


def _dedupe_context_layers(
    suffixes: list[str | None],
) -> tuple[list[str | None], int]:
    """Drop normalized-duplicate lines that appear in later suffixes when an
    earlier suffix already contains them. Conservative: line-level match after
    lowercasing, collapsing whitespace, and stripping leading bullet/markdown
    markers. Order encodes precedence — earlier suffixes win.

    Expected v1 impact is near-zero (different layers have different markdown
    shapes). The dropped-count metric is the signal for whether to escalate to
    semantic dedupe later.
    """
    def norm(line: str) -> str:
        s = _DEDUPE_RE.sub("", line).strip().lower()
        return " ".join(s.split())

    seen: set[str] = set()
    out: list[str | None] = []
    dropped = 0
    for s in suffixes:
        if not s:
            out.append(s)
            continue
        kept_lines: list[str] = []
        for raw in s.split("\n"):
            n = norm(raw)
            if not n or len(n) < 8:  # don't dedupe headers, short markers
                kept_lines.append(raw)
                continue
            if n in seen:
                dropped += 1
                continue
            seen.add(n)
            kept_lines.append(raw)
        out.append("\n".join(kept_lines))
    return out, dropped


def _persist_mint_packet(
    conv_id: str,
    instructions: str,
    meta: Dict[str, Any],
) -> str | None:
    """Persist the exact mint-instructions payload as a secondary artifact next
    to the transcript. Returns the relative path of the .md file (for the
    `packet_ref` field), or None on failure. Never raises — packet writes are
    observability, not load-bearing for the mint response.
    """
    try:
        from transcripts import TRANSCRIPT_DIR
        pkt_dir = TRANSCRIPT_DIR / "packets"
        pkt_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        iso_ts = datetime.fromtimestamp(meta.get("minted_at", time.time())).astimezone().isoformat(timespec="seconds")
        header = (
            f"> Secondary handoff artifact — snapshot of context supplied to "
            f"session {conv_id} at {iso_ts}. Not source-of-truth; revalidate "
            f"before reuse.\n\n"
        )
        md_path = pkt_dir / f"{conv_id}.instructions.md"
        json_path = pkt_dir / f"{conv_id}.packet.json"
        full_meta = {**meta, "conv_id": conv_id, "secondary_artifact": True}
        # tempfile + os.replace for atomic writes (matches voice_memory.py pattern)
        for path, payload in ((md_path, header + instructions),
                              (json_path, json.dumps(full_meta, indent=2))):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload)
            os.replace(tmp, path)
        return f"packets/{conv_id}.instructions.md"
    except Exception as e:  # noqa: BLE001
        log.warning("mint packet persist failed for %s: %s", conv_id, e)
        return None


def _truncation_cfg() -> dict:
    """Realtime context-truncation config for the mint body. Omits the
    post_instructions cap when set to 0 so the model uses its full window."""
    cfg: dict = {
        "type": "retention_ratio",
        "retention_ratio": REALTIME_TRUNCATION_RETENTION_RATIO,
    }
    if REALTIME_POST_INSTRUCTIONS_TOKENS > 0:
        cfg["token_limits"] = {"post_instructions": REALTIME_POST_INSTRUCTIONS_TOKENS}
    return cfg


async def _mint_realtime_session(
    instructions_suffixes: list[str | None] | None = None,
) -> dict:
    """Mint an OpenAI Realtime ephemeral session. Raises on HTTP/network error.

    Uses the May 2026 GA endpoint (/v1/realtime/client_secrets). The session
    config is nested under ``session`` and audio under ``audio.input`` /
    ``audio.output``. ``instructions_suffixes`` is a list of optional suffixes
    appended in order to ``VOICE_SYSTEM_PROMPT`` (``None``/empty entries are
    skipped). Typical compose: ``[dossier_md, triage_brief?, handoff_note?]``.

    Returns a legacy-compatible shape so the existing client code keeps reading
    ``session.client_secret.value`` and ``session.client_secret.expires_at``.
    """
    parts = [VOICE_SYSTEM_PROMPT]
    for s in (instructions_suffixes or []):
        if s and s.strip():
            parts.append(s.strip())
    instructions = "\n\n".join(parts)
    transcription_cfg: Dict[str, Any] = {
        "model": "gpt-4o-mini-transcribe",
        "language": "en",
    }
    if OPENAI_REALTIME_TRANSCRIPTION_HINTS:
        # Phrase the bias hint as a natural sentence rather than a bare
        # comma-list. A raw keyword dump gets echoed back verbatim by
        # gpt-4o-mini-transcribe on the opening silence/breath, which the
        # model then treats as a real user turn (the "I didn't say that"
        # bug). A sentence is far less prone to verbatim echo. The client
        # also drops a first-turn transcript that matches the hint string
        # (belt-and-suspenders) — see app.js hint-echo guard.
        transcription_cfg["prompt"] = (
            f"The speaker often mentions these names and terms: "
            f"{OPENAI_REALTIME_TRANSCRIPTION_HINTS}."
        )
    body = {
        "session": {
            "type": "realtime",
            "model": OPENAI_REALTIME_MODEL,
            "instructions": instructions,
            "tools": TOOLKIT_SCHEMAS + [TRIAGE_VERDICT_TOOL_SCHEMA],
            "tool_choice": "auto",
            "output_modalities": ["audio"],
            "max_output_tokens": 1500,
            # Bound per-turn context growth (cost lever — see constants above).
            "truncation": _truncation_cfg(),
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "noise_reduction": {"type": "near_field"},
                    "transcription": transcription_cfg,
                    "turn_detection": {
                        "type": "semantic_vad",
                        "eagerness": "low",
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": OPENAI_REALTIME_VOICE,
                },
            },
        }
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.openai.com/v1/realtime/client_secrets",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        r.raise_for_status()
        ga = r.json()
    # Reshape GA response to the legacy contract the client expects.
    inner = ga.get("session") or {}
    return {
        "client_secret": {
            "value": ga.get("value"),
            "expires_at": ga.get("expires_at"),
        },
        "model": inner.get("model") or OPENAI_REALTIME_MODEL,
    }


async def session_mint(request: web.Request) -> web.Response:
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    conv_id = _new_conv_id()
    now = time.time()
    CONVERSATIONS[conv_id] = {
        "started_at": now,
        "last_activity_ts": now,
        "entries": [],
    }
    # Triage mode is opt-in via ?mode=triage. Default sessions stay user-driven.
    mode = (request.query.get("mode") or "").strip().lower()
    # Dossier first (today's standing context); triage brief second when
    # opted in (mode-specific doctrine layered over the dossier).
    dossier_md, dossier_meta = dossier.load_dossier()
    # Self-healing: kick a background refresh when the dossier is missing or
    # stale (generated on a prior local date). The in-flight session still
    # gets yesterday's snapshot with a staleness header so it has *some*
    # standing context to orient on; the refresh updates disk for the next
    # session.
    if AGENT_API_KEY and (not dossier_meta["loaded"] or dossier_meta.get("stale")):
        log.info("dossier stale/missing at mint — kicking background refresh")
        asyncio.create_task(dossier.refresh_dossier(
            agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY, force=True,
        ))
    triage_suffix = _load_open_loops_brief() if mode == "triage" else None
    # Memory sources (env-driven registry): user profile, shared assistant
    # memory, voice-learnings file — each optional, skipped silently when
    # unconfigured. Public default loads nothing; personal deployments wire
    # these via .env to local memory files. Transcript-index map appended last
    # so the model has a compact list of prior calls reachable via
    # recall_recent_call.
    memory_blocks = voice_memory.render_memory_sources_markdown()
    transcript_index_block = voice_memory.render_transcript_index_markdown()
    honcho_block = honcho_voice.render_mint_context_block(conv_id)
    # Real GWS account roster so the model never invents addresses (e.g.
    # "ggurevich.gary@…") and knows to fan out across inboxes. None when no
    # GWS accounts are configured.
    accounts_block = _accounts_brief()
    suffixes = [dossier_md, triage_suffix, accounts_block, honcho_block, *memory_blocks, transcript_index_block]
    # Conservative cross-layer dedupe (line-level, normalized). Earlier suffixes
    # win; later layers drop verbatim restatements. Expected v1 impact is
    # near-zero — dropped_count is the measurement signal.
    suffixes, dedupe_dropped_lines = _dedupe_context_layers(suffixes)
    # Guard the Realtime 16,384-token instructions cap. If the Honcho block
    # would push us over ~14k tokens, drop it rather than fail the mint.
    if sum(len(s or "") for s in suffixes) // 4 > 14000 and honcho_block:
        log.warning("dropping honcho_block to stay under instructions cap (conv=%s)", conv_id)
        suffixes = [dossier_md, triage_suffix, accounts_block, *memory_blocks, transcript_index_block]
        suffixes, _ = _dedupe_context_layers(suffixes)
        honcho_block = ""
    try:
        data = await _mint_realtime_session(instructions_suffixes=suffixes)
    except httpx.HTTPStatusError as e:
        log.error("ephemeral mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("ephemeral mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    instructions_chars, instructions_hash = _instructions_audit(suffixes)
    instructions_tokens_est = instructions_chars // 4
    memory_sources = voice_memory.load_memory_sources()
    memory_sources_loaded = [b["header"] for b in memory_sources]
    memory_total_chars = sum(len(b) for b in memory_blocks)
    if instructions_tokens_est > 12000:
        log.warning(
            "session_minted: instructions ~%d tokens — approaching 16,384 cap (conv=%s)",
            instructions_tokens_est, conv_id,
        )
    # Per-source breakdown for telemetry + packet metadata. Only sources that
    # actually contributed chars get an entry. `stale`/`generated_at` are
    # populated where the source exposes them (today: dossier only).
    sources_meta: list[Dict[str, Any]] = []
    if dossier_md:
        sources_meta.append({
            "name": "dossier",
            "chars": dossier_meta["chars"],
            "stale": bool(dossier_meta.get("stale", False)),
            "age_h": dossier_meta.get("age_h"),
        })
    if triage_suffix:
        sources_meta.append({"name": "triage_brief", "chars": len(triage_suffix)})
    if accounts_block:
        sources_meta.append({"name": "accounts", "chars": len(accounts_block)})
    if honcho_block:
        sources_meta.append({"name": "honcho", "chars": len(honcho_block)})
    for header, block in zip(memory_sources_loaded, memory_blocks):
        if block:
            sources_meta.append({"name": f"memory:{header}", "chars": len(block)})
    if transcript_index_block:
        sources_meta.append({"name": "transcript_index", "chars": len(transcript_index_block)})
    # Persist exact mint-instructions packet as a secondary artifact next to
    # the transcript. Observability only — failures do not block the response.
    minted_at = time.time()
    instructions_full = _build_instructions(suffixes)
    packet_ref = _persist_mint_packet(conv_id, instructions_full, {
        "minted_at": minted_at,
        "model": OPENAI_REALTIME_MODEL,
        "voice": OPENAI_REALTIME_VOICE,
        "mode": mode or "default",
        "sources": sources_meta,
        "dedupe_dropped_lines": dedupe_dropped_lines,
        "instructions_chars": instructions_chars,
        "instructions_hash": instructions_hash,
    })
    CONVERSATIONS[conv_id]["packet_ref"] = packet_ref
    log_call_event(
        LOG_DIR, conv_id, "session_minted",
        model=OPENAI_REALTIME_MODEL, voice=OPENAI_REALTIME_VOICE,
        mode=mode or "default", brief_injected=bool(triage_suffix),
        dossier_loaded=dossier_meta["loaded"],
        dossier_chars=dossier_meta["chars"],
        dossier_age_h=dossier_meta["age_h"],
        dossier_stale=dossier_meta.get("stale", False),
        working_state_present=dossier_meta["working_state_present"],
        memory_sources_loaded=memory_sources_loaded,
        memory_total_chars=memory_total_chars,
        honcho_chars=len(honcho_block or ""),
        transcript_index_present=bool(transcript_index_block),
        instructions_chars=instructions_chars,
        instructions_tokens_est=instructions_tokens_est,
        instructions_hash=instructions_hash,
        sources=sources_meta,
        dedupe_dropped_lines=dedupe_dropped_lines,
        packet_ref=packet_ref,
    )
    return web.json_response({
        "conv_id": conv_id, "session": data, "mode": mode or "default",
        "operator_name": OPERATOR_NAME,
        # Raw hint string so the client can drop a first-turn transcript that
        # is just the transcriber echoing it back (see app.js hint-echo guard).
        "transcription_hint": OPENAI_REALTIME_TRANSCRIPTION_HINTS,
    })


_AGENT_UNREACHABLE_SENTINEL = "Agent is temporarily unreachable"


def _structured_unreachable(error_type: str) -> dict:
    """Tool-result envelope the realtime model recognizes as a clean failure.

    System prompt rule #10 keys off the leading sentinel, so the realtime
    model says "the agent didn't answer, please repeat" instead of inventing
    a refusal.
    """
    return {
        "answer": f"{_AGENT_UNREACHABLE_SENTINEL} - please ask the user to repeat that.",
        "_error": error_type,
    }


async def ask_agent_gone(request: web.Request) -> web.Response:
    """Hard cutover marker for stale PWA caches.

    The pre-cutover client called this route with `{conv_id, question, ...}`
    as the single fat tool. Stale-cache fallthrough into the slow path is
    exactly the regression this refactor exists to kill, so we 410 and ask
    the client to reload. The no-cache middleware (`no_cache_static_middleware`)
    forces shell revalidation on the next visit; one reload restores normal
    operation.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    conv_id = body.get("conv_id") or "unknown"
    log.warning("legacy /api/ask-agent hit — client outdated (conv=%s)", conv_id)
    log_call_event(LOG_DIR, conv_id, "legacy_ask_agent_blocked")
    return web.json_response(
        {
            "error": "client_outdated",
            "reload_required": True,
            "answer": (
                "Your voice client is out of date — please reload the page. "
                "The toolkit was upgraded."
            ),
        },
        status=410,
    )


# Dispatch table: tool name → (backend module, function name). The handler
# below imports lazily so missing backend deps (e.g. ripgrep absent) don't
# crash mint — they only surface when the tool fires.
_ALL_TOOL_DISPATCH = {
    "lookup_open_loop":     ("backends.supabase",           "lookup_open_loop"),
    "recent_decisions":     ("backends.supabase",           "recent_decisions"),
    "mission_control_card": ("backends.supabase",           "mission_control_card"),
    "search_notes":         ("backends.notes",              "search_notes"),
    "calendar":             ("backends.gws",                "calendar"),
    "gmail_search":         ("backends.gws",                "gmail_search"),
    "recall_recent_call":   ("backends.transcripts_recall", "recall_recent_call"),
}

# Per-tool cache TTLs (seconds). Calendar and Gmail need shorter windows so
# "what's on for the next hour" reflects late additions; static-ish queries
# (notes, recent_decisions) can ride a longer tail.
_ALL_TOOL_TTL = {
    "calendar": 120.0,
    "gmail_search": 90.0,
    "lookup_open_loop": 180.0,
    "mission_control_card": 180.0,
    "recent_decisions": 300.0,
    "search_notes": 300.0,
    "recall_recent_call": 300.0,
}


def _build_toolkit() -> tuple[list, dict, dict]:
    """Filter the full toolkit down to tools whose backends are actually
    configured for this process. Called once at module load.

    A clean public clone with only `OPENAI_API_KEY` ends up with three tools
    registered: `search_notes`, `recall_recent_call`, `deep_research`. The
    Realtime model never advertises a `calendar` or `gmail_search` it can't
    actually call — avoiding the "tool returns null, model narrates
    confusion" failure mode.

    Reuses each backend's own availability check (no parallel env logic that
    could drift):
      - Supabase tools gate on `backends.supabase._creds()` returning both
        a URL and a key (env or SECRETS_DIR fallback — single source of
        truth).
      - GWS tools gate on `backends.gws.ALLOWED_ACCOUNTS` being non-empty
        AND the `gws-as.sh` wrapper existing on disk.
      - `search_notes`, `recall_recent_call`, `deep_research` always
        register (filesystem ripgrep, local-file recall, agent backend or
        stub) — these have no external dependency to gate on.
    """
    # cancel_research is handled entirely client-side (aborts the in-flight
    # /api/deep-research fetch); it has no _TOOL_DISPATCH backend, so it rides
    # along in the schema set but never appears in `dispatch`.
    available = {"search_notes", "recall_recent_call", "deep_research", "cancel_research"}
    try:
        from backends.supabase import _creds as _supabase_creds
        url, key = _supabase_creds()
        if url and key:
            available.update({"lookup_open_loop", "recent_decisions", "mission_control_card"})
    except Exception:  # noqa: BLE001
        pass
    try:
        from backends.gws import ALLOWED_ACCOUNTS as _gws_accounts, GWS_WRAPPER as _gws_wrapper
        if _gws_accounts and _gws_wrapper.exists():
            available.update({"calendar", "gmail_search"})
    except Exception:  # noqa: BLE001
        pass
    schemas = [s for s in _ALL_TOOLKIT_SCHEMAS if s["name"] in available]
    dispatch = {k: v for k, v in _ALL_TOOL_DISPATCH.items() if k in available}
    ttl = {k: v for k, v in _ALL_TOOL_TTL.items() if k in available}
    return schemas, dispatch, ttl


TOOLKIT_SCHEMAS, _TOOL_DISPATCH, _TOOL_TTL = _build_toolkit()
log.info("toolkit registered: %s", sorted(s["name"] for s in TOOLKIT_SCHEMAS))


async def tool_dispatch(request: web.Request) -> web.Response:
    """Generic dispatch for the granular toolkit.

    Browser POSTs `{conv_id, name, args}` (or path param `/api/tool/<name>`).
    Handler looks up the backend, runs it with a TTL cache, returns JSON,
    and appends a tool turn to the conversation transcript.
    """
    name = request.match_info.get("name", "")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    conv_id = body.get("conv_id")
    args = body.get("args") if isinstance(body.get("args"), dict) else {}
    if not name:
        return web.json_response({"error": "missing_tool"}, status=400)
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    entry = _TOOL_DISPATCH.get(name)
    if entry is None:
        return web.json_response({"error": "unknown_tool", "name": name}, status=404)
    module_name, fn_name = entry
    _touch(conv)
    started_at = time.time()
    log_call_event(
        LOG_DIR, conv_id, "tool_call_spawned",
        tool=name, args_keys=list(args.keys()),
    )
    error_type: str | None = None
    cache_hit = False
    try:
        from importlib import import_module
        mod = import_module(module_name)
        fn = getattr(mod, fn_name)
        from backends import cache
        result, cache_hit = await cache.memoize(
            name, args, lambda: fn(**args), ttl_sec=_TOOL_TTL.get(name),
        )
    except TypeError as e:
        # Bad args (missing required, wrong types). Surface to the model as a
        # structured error so it can re-call with the right shape.
        result = {"error": "bad_args", "detail": str(e)[:200]}
        error_type = "bad_args"
    except Exception as e:  # noqa: BLE001
        log.exception("tool dispatch failed (%s)", name)
        result = {"error": "tool_failed", "detail": type(e).__name__}
        error_type = type(e).__name__
    finished_at = time.time()
    latency_ms = int((finished_at - started_at) * 1000)
    chars = len(json.dumps(result, default=str)) if result is not None else 0
    log_call_event(
        LOG_DIR, conv_id, "tool_call_done",
        tool=name, latency_ms=latency_ms, chars=chars,
        cache_hit=cache_hit, error_type=error_type,
    )
    # Persist a tool turn so the transcript has the structured I/O even
    # though the model only ever sees its own paraphrase in audio.
    conv["entries"].append({
        "role": "tool_question",
        "text": f"{name}({json.dumps(args, default=str)})",
        "ts": started_at,
    })
    conv["entries"].append({
        "role": "tool_answer",
        "text": json.dumps(result, default=str)[:6000],
        "ts": finished_at,
    })
    conv["last_activity_ts"] = finished_at
    return web.json_response({"ok": True, "result": result})


async def deep_research(request: web.Request) -> web.Response:
    """The one slow path. Streams from the agent backend, emits milestone
    events on markdown section boundaries, and assembles the final answer.

    Response is SSE: each line `data: <json>\\n\\n`. Browser injects each
    milestone into the Realtime conversation as a system message so the
    voice model can narrate progress between utterances. On `done`, the
    browser feeds the assembled answer back as `function_call_output`.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    prompt = (body.get("prompt") or "").strip()
    scope = (body.get("scope") or "reasoning").strip()
    expected_seconds = body.get("expected_seconds")
    call_id = (body.get("call_id") or secrets.token_hex(4)).strip()
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not prompt:
        return web.json_response({"error": "empty_prompt"}, status=400)
    _touch(conv)
    started_at = time.time()
    # Concurrency: if another deep_research is already running on this call,
    # isolate this one in its own backend session so the agent's per-session
    # working state can't interleave (see _agent_chat_stream docstring). The
    # first/sole task keeps the base session for cross-turn continuity.
    inflight: set = conv.setdefault("research_inflight", set())
    session_suffix = f"#{call_id}" if inflight else ""
    inflight.add(call_id)
    log_call_event(
        LOG_DIR, conv_id, "deep_research_spawned",
        chars=len(prompt), scope=scope, expected_seconds=expected_seconds,
        call_id=call_id, concurrent=bool(session_suffix), inflight_n=len(inflight),
    )

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await resp.prepare(request)

    async def _send(payload: dict) -> None:
        line = "data: " + json.dumps(payload, default=str) + "\n\n"
        await resp.write(line.encode("utf-8"))

    chunks: list[str] = []
    last_milestone_ts = 0.0
    pending_section = ""
    pending_buffer = ""

    async def _emit_milestone(section: str, text: str) -> None:
        nonlocal last_milestone_ts
        now = time.time()
        if now - last_milestone_ts < 3.0:
            return
        last_milestone_ts = now
        snippet = text.strip().replace("\n", " ")[:120]
        if not snippet:
            return
        await _send({"type": "milestone", "section": section or "progress", "text": snippet})

    status = "done"
    error_type: str | None = None
    try:
        async for piece in _agent_chat_stream(conv_id, prompt, session_suffix=session_suffix):
            chunks.append(piece)
            pending_buffer += piece
            # Section boundary: line starting with `## ` (Markdown H2).
            while "\n## " in pending_buffer or pending_buffer.startswith("## "):
                if pending_buffer.startswith("## "):
                    idx = 0
                else:
                    idx = pending_buffer.find("\n## ") + 1
                head = pending_buffer[:idx].strip()
                if head and pending_section:
                    await _emit_milestone(pending_section, head)
                rest = pending_buffer[idx:]
                # Extract the new section heading text up to the next newline.
                newline = rest.find("\n")
                if newline < 0:
                    pending_section = rest[3:].strip()[:60]
                    pending_buffer = ""
                    break
                pending_section = rest[3:newline].strip()[:60]
                pending_buffer = rest[newline + 1:]
    except asyncio.TimeoutError:
        status = "error"; error_type = "runaway"
    except httpx.ReadTimeout:
        status = "error"; error_type = "idle"
    except httpx.TimeoutException:
        status = "error"; error_type = "timeout"
    except Exception as e:  # noqa: BLE001
        log.exception("deep_research stream failed")
        status = "error"; error_type = type(e).__name__
    finally:
        # Free the slot whether we finished, errored, or were cancelled by a
        # client abort (cancel_research / disconnect). CancelledError is a
        # BaseException, so it isn't swallowed by `except Exception` above — it
        # propagates out after this runs, closing the httpx stream to Hermes.
        inflight.discard(call_id)
    # Flush any trailing buffered section text as a final milestone before done.
    if pending_buffer.strip() and pending_section:
        await _emit_milestone(pending_section, pending_buffer.strip())

    answer = "".join(chunks).strip()
    if status == "error" and not answer:
        answer = _AGENT_UNREACHABLE_SENTINEL + " - please ask the user to repeat that."
    finished_at = time.time()
    latency_ms = int((finished_at - started_at) * 1000)
    log_call_event(
        LOG_DIR, conv_id, "deep_research_done",
        latency_ms=latency_ms, chars=len(answer),
        status=status, error_type=error_type,
        milestones_emitted=int(last_milestone_ts > 0),
    )
    conv["entries"].append({"role": "tool_question", "text": prompt, "ts": started_at})
    conv["entries"].append({"role": "tool_answer", "text": answer, "ts": finished_at})
    conv["last_activity_ts"] = finished_at
    try:
        await _send({"type": "done", "answer": answer, "status": status, "error_type": error_type})
        await resp.write_eof()
    except (ConnectionResetError, ConnectionError):
        # Client aborted (e.g. cancel_research) before we flushed the final
        # event. Nothing to do — the transcript turn above is already recorded.
        pass
    return resp


async def text_turn(request: web.Request) -> web.Response:
    """Dual-mode: pure agent turn (no realtime model). Slower but full agent voice."""
    body = await request.json()
    conv_id = body.get("conv_id")
    user_text = (body.get("text") or "").strip()
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    if not user_text:
        return web.json_response({"answer": ""}, status=200)
    _touch(conv)
    started_at = time.time()
    try:
        answer = await _agent_chat(conv_id, user_text)
    except Exception as e:  # noqa: BLE001
        log.exception("text_turn failed")
        log_call_event(LOG_DIR, conv_id, "tool_error", channel="text_turn", error_type=type(e).__name__)
        return web.json_response(_structured_unreachable(type(e).__name__), status=200)
    finished_at = time.time()
    conv["entries"].append({"role": "user_text", "text": user_text, "ts": started_at})
    conv["entries"].append({"role": "assistant_text", "text": answer, "ts": finished_at})
    conv["last_activity_ts"] = finished_at
    log_call_event(
        LOG_DIR, conv_id, "text_turn",
        latency_ms=int((finished_at - started_at) * 1000), chars=len(answer),
    )
    return web.json_response({"answer": answer})


def _build_resume_recap(conv: dict, client_recent: list) -> str | None:
    """Compact recent-conversation recap for resume continuity.

    Merges the server's tool turns (``conv['entries']`` — e.g. files saved,
    research results) with the client's audio turns (sent in the resume body),
    sorts by ts, and renders a bounded tail. This is the continuity FALLBACK for
    when the dying session's handoff note fails (a silent freeze yields an empty
    note): the recap is reconstructed from preserved state and does not depend on
    the frozen session responding, so the resumed model still knows what was said
    and done — including actions like "saved file X" that a blank note would lose.
    """
    label = {
        "user": "You", "user_text": "You",
        "assistant": "Assistant", "assistant_text": "Assistant",
        "tool_answer": "Result",
    }
    merged = []
    for e in list(conv.get("entries") or []) + list(client_recent or []):
        who = label.get(e.get("role"))
        text = (e.get("text") or "").strip()
        if who and text:
            merged.append((e.get("ts") or 0, who, text))
    if not merged:
        return None
    merged.sort(key=lambda x: x[0])
    lines = [f"- {who}: {text[:300]}" for _, who, text in merged[-12:]]
    while sum(len(l) for l in lines) > 2000 and len(lines) > 3:
        lines.pop(0)
    return (
        "Recent conversation so far in THIS SAME call (your own memory — you are "
        "resuming after a brief connection drop; do not greet, re-introduce "
        "yourself, or repeat any action already completed below):\n"
        + "\n".join(lines)
    )


async def resume_call(request: web.Request) -> web.Response:
    """Mint a fresh Realtime ephemeral session bound to the same conv.

    Continuity is delivered via the freshly-minted session's ``instructions``.
    Two complementary signals are baked in: the dying session's handoff note
    (best-effort "where we left off mid-thought") AND a recent-conversation
    recap reconstructed from preserved server + client state. The recap is the
    fallback that keeps continuity intact when the handoff note fails — a silent
    freeze yields a 0-char note, and without the recap the resumed session would
    start blank and forget everything said/done earlier in the call.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"expired": True})
    _ensure_conv_shape(conv)
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    note = (conv.get("handoff_note") or "").strip()
    handoff_suffix = None
    if note:
        handoff_suffix = (
            "Continuation context from earlier in this same call: "
            + note
            + "\n\nCritical: do not greet, do not acknowledge any pause, do "
            + "not say 'as I was saying' or similar filler. Continue exactly "
            + "where you left off in topic and tone. Wait for the user's next "
            + "utterance before responding."
        )
    recap_suffix = _build_resume_recap(conv, body.get("recent_entries") or [])
    # On resume we still want the dossier present — same standing context as the
    # original mint; recap (factual record) + handoff note (continuation cue)
    # layer on top for in-call continuity.
    dossier_md, _ = dossier.load_dossier()
    try:
        data = await _mint_realtime_session(
            instructions_suffixes=[dossier_md, recap_suffix, handoff_suffix],
        )
    except httpx.HTTPStatusError as e:
        log.error("resume mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("resume mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    _touch(conv)
    log_call_event(
        LOG_DIR, conv_id, "resumed",
        handoff_note_chars=len(note),
        recap_chars=len(recap_suffix or ""),
    )
    return web.json_response({
        "conv_id": conv_id,
        "session": data,
        "resumed": True,
        "handoff_note": note or None,
    })


async def premint_session(request: web.Request) -> web.Response:
    """Pre-mint a fresh ephemeral for an existing conv ahead of the 60-min cap.

    Does NOT touch conv state, does NOT mark any agent task delivered. The
    actual swap-in fires a regular /api/resume call to claim missed work.
    """
    body = await request.json()
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"expired": True})
    if not OPENAI_API_KEY:
        return web.json_response({"error": "no_openai_key"}, status=500)
    # Pre-mint inherits the dossier (handoff note layered server-side during
    # the actual swap-in via /api/resume).
    dossier_md, _ = dossier.load_dossier()
    try:
        data = await _mint_realtime_session(instructions_suffixes=[dossier_md])
    except httpx.HTTPStatusError as e:
        log.error("premint mint failed: %s %s", e.response.status_code, e.response.text[:300])
        return web.json_response({"error": "ephemeral_mint_failed", "detail": e.response.text[:300]}, status=502)
    except Exception as e:  # noqa: BLE001
        log.exception("premint mint exception")
        return web.json_response({"error": "ephemeral_mint_exception", "detail": str(e)}, status=502)
    log_call_event(LOG_DIR, conv_id, "session_preminted")
    return web.json_response({"conv_id": conv_id, "session": data})


async def handoff_note(request: web.Request) -> web.Response:
    """Persist the dying Realtime session's continuation note onto the conv,
    so the next resume can splice it into the new session's instructions."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    conv_id = body.get("conv_id")
    note = (body.get("note") or "").strip()
    generated_in_ms = body.get("generated_in_ms")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "no_conv"}, status=404)
    if note:
        conv["handoff_note"] = note
        conv["handoff_note_ts"] = time.time()
        log_call_event(
            LOG_DIR, conv_id, "handoff_note_saved",
            chars=len(note), generated_in_ms=generated_in_ms,
        )
    return web.json_response({"ok": True})


async def end_call(request: web.Request) -> web.Response:
    """Explicit End. Backgrounding NO LONGER triggers this -- only the End
    button or pagehide. Idempotent for duplicate fires."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    conv_id = body.get("conv_id")
    client_entries = body.get("entries") or []
    reason = body.get("reason") or "user_click"
    if not conv_id:
        return web.json_response({"ok": True, "noop": "no_conv_id"})
    # Legacy clients may still POST with reason="visibilitychange" -- treat as a no-op
    # so old PWA tabs don't accidentally end calls during background.
    if reason == "visibilitychange":
        log_call_event(LOG_DIR, conv_id, "legacy_background_end_ignored")
        return web.json_response({"ok": True, "noop": "background_is_pause"})
    conv = CONVERSATIONS.pop(conv_id, None)
    if conv is None:
        return web.json_response({"ok": True, "noop": "already_ended"})
    all_entries = sorted(conv["entries"] + client_entries, key=lambda e: e.get("ts", 0))
    metrics = compute_routing_metrics(LOG_DIR, conv_id)
    path = write_transcript(conv_id, conv["started_at"], all_entries, metrics=metrics, packet_ref=conv.get("packet_ref"))
    ended_at = time.time()
    asyncio.create_task(ingest_into_agent(
        conv_id, all_entries,
        agent_base=AGENT_API_BASE,
        agent_key=AGENT_API_KEY,
    ))
    # Extract working state + voice learnings + one-liner from this call, then
    # refresh the dossier so the next session sees both. Ordering matters:
    # extract → refresh; never blocks the live response.
    dossier.schedule_extract(
        conv_id, all_entries,
        agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY,
        started_at=conv["started_at"],
    )
    slack_posted = False
    if SLACK_BOT_TOKEN and SLACK_CALL_CHANNEL_ID:
        asyncio.create_task(post_to_slack(
            conv_id=conv_id,
            started_at=conv["started_at"],
            ended_at=ended_at,
            entries=all_entries,
            slack_token=SLACK_BOT_TOKEN,
            channel_id=SLACK_CALL_CHANNEL_ID,
        ))
        slack_posted = True
    log_call_event(
        LOG_DIR, conv_id, "ended",
        reason=reason, duration_s=int(ended_at - conv["started_at"]),
        slack_posted=slack_posted,
        ask_agent_count=metrics["ask_agent_count"],
        local_answer_turns=metrics["local_answer_turns"],
        ask_ratio=metrics["ask_ratio"],
    )
    return web.json_response({"ok": True, "transcript": str(path)})


async def client_event(request: web.Request) -> web.Response:
    """Sink for browser-side telemetry. Writes to the per-call NDJSON via the
    same log_call_event used by server-side events, so one timeline per call.
    Fire-and-forget from the client; never blocks audio/tool flow.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.Response(status=204)
    conv_id = (body.get("conv_id") or "unknown").strip() or "unknown"
    event = (body.get("event") or "client_unknown").strip() or "client_unknown"
    payload = {k: v for k, v in body.items() if k not in ("conv_id", "event")}
    if conv_id != "unknown" and conv_id not in CONVERSATIONS:
        payload["conv_status"] = "unknown_conv"
    log_call_event(LOG_DIR, conv_id, event, **payload)
    return web.Response(status=204)


# ---- session reaper --------------------------------------------------------
# Backgrounding is now a pause, not an end. The reaper guards against the
# truly-forgotten case: 20 min with zero activity (no /api/session,
# /api/ask-agent, /api/text-turn, /api/resume, or completed agent task).


async def _reap_stale_conversations() -> None:
    while True:
        await asyncio.sleep(REAPER_INTERVAL_SEC)
        cutoff = time.time() - REAPER_IDLE_TIMEOUT_SEC
        stale = [cid for cid, c in CONVERSATIONS.items() if c.get("last_activity_ts", c.get("started_at", 0)) < cutoff]
        for cid in stale:
            conv = CONVERSATIONS.pop(cid, None)
            if conv is None:
                continue
            ended_at = time.time()
            all_entries = sorted(conv.get("entries", []), key=lambda e: e.get("ts", 0))
            metrics = compute_routing_metrics(LOG_DIR, cid)
            try:
                write_transcript(cid, conv["started_at"], all_entries, metrics=metrics, packet_ref=conv.get("packet_ref"))
            except Exception:  # noqa: BLE001
                log.exception("reap: write_transcript failed for %s", cid)
            slack_posted = False
            if SLACK_BOT_TOKEN and SLACK_CALL_CHANNEL_ID:
                asyncio.create_task(post_to_slack(
                    conv_id=cid,
                    started_at=conv["started_at"],
                    ended_at=ended_at,
                    entries=all_entries,
                    slack_token=SLACK_BOT_TOKEN,
                    channel_id=SLACK_CALL_CHANNEL_ID,
                ))
                slack_posted = True
            # Same extract → refresh chain as end_call. Idle-reaped calls still
            # produce signal worth carrying forward to the next session — this
            # is the path that covers abrupt endings (driving, signal loss).
            dossier.schedule_extract(
                cid, all_entries,
                agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY,
                started_at=conv["started_at"],
            )
            log.info("reaped idle conversation %s (>%dmin no activity)", cid, REAPER_IDLE_TIMEOUT_SEC // 60)
            log_call_event(
                LOG_DIR, cid, "reaped",
                reason="idle_timeout", duration_s=int(ended_at - conv["started_at"]),
                slack_posted=slack_posted,
                ask_agent_count=metrics["ask_agent_count"],
                local_answer_turns=metrics["local_answer_turns"],
                ask_ratio=metrics["ask_ratio"],
            )


async def _start_reaper(app: web.Application) -> None:
    app["reaper_task"] = asyncio.create_task(_reap_stale_conversations())


async def _start_dossier_refresh(app: web.Application) -> None:
    """Fire-and-forget dossier refresh at server boot.

    Never blocks startup — mint reads whatever's on disk and gracefully
    skips when stale, so a slow agent backend at boot doesn't keep the
    server from accepting calls.
    """
    if not AGENT_API_KEY:
        return
    asyncio.create_task(
        dossier.refresh_dossier(agent_base=AGENT_API_BASE, agent_key=AGENT_API_KEY)
    )


async def _stop_reaper(app: web.Application) -> None:
    task = app.get("reaper_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---- app -------------------------------------------------------------------

async def triage_verdict(request: web.Request) -> web.Response:
    """Record a structured triage_verdict tool call into the conv transcript.

    Returns immediately. Reconcile cron reads these entries from the
    persisted voice transcript and creates calendar events for `act` verdicts.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"error": "bad_json"}, status=400)
    conv_id = body.get("conv_id")
    conv = CONVERSATIONS.get(conv_id) if conv_id else None
    if conv is None:
        return web.json_response({"error": "unknown_conv_id"}, status=400)
    _ensure_conv_shape(conv)
    verdict = (body.get("verdict") or "").lower()
    if verdict not in ("drop", "park", "act"):
        return web.json_response({"error": "bad_verdict"}, status=400)
    loop_id = (body.get("loop_id") or "").strip()
    if not loop_id:
        return web.json_response({"error": "missing_loop_id"}, status=400)
    entry = {
        "role": "triage_verdict",
        "ts": time.time(),
        "loop_id": loop_id,
        "verdict": verdict,
        "next_action": body.get("next_action") or "",
        "calendar_when": body.get("calendar_when") or "",
        "note": body.get("note") or "",
    }
    conv["entries"].append(entry)
    conv["last_activity_ts"] = entry["ts"]
    log_call_event(LOG_DIR, conv_id, "triage_verdict", loop_id=loop_id, verdict=verdict)
    return web.json_response({"recorded": True})


@web.middleware
async def no_cache_static_middleware(request: web.Request, handler):
    """Force iOS PWA / Safari to revalidate the SPA shell on every load.

    Without this, the realtime tool name was cached as `ask_hermes` after the
    repo rename, breaking every voice session until the user wiped the PWA.
    """
    resp = await handler(request)
    path = request.path
    if path == "/" or path.startswith("/static/") or path.endswith(".webmanifest"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def make_app() -> web.Application:
    app = web.Application(middlewares=[tailnet_middleware, no_cache_static_middleware], client_max_size=8 * 1024 * 1024)
    app.on_startup.append(_start_reaper)
    app.on_startup.append(_start_dossier_refresh)
    app.on_cleanup.append(_stop_reaper)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/session", session_mint)
    # Legacy `/api/ask-agent` is hard-cut to 410 so stale PWA caches can't
    # silently regress into the slow path — see `ask_agent_gone`.
    app.router.add_post("/api/ask-agent", ask_agent_gone)
    app.router.add_post("/api/tool/{name}", tool_dispatch)
    app.router.add_post("/api/deep-research", deep_research)
    app.router.add_post("/api/client-event", client_event)
    app.router.add_post("/api/text-turn", text_turn)
    app.router.add_post("/api/resume", resume_call)
    app.router.add_post("/api/premint-session", premint_session)
    app.router.add_post("/api/handoff-note", handoff_note)
    app.router.add_post("/api/end", end_call)
    app.router.add_post("/api/triage-verdict", triage_verdict)
    # Static SPA - index.html and /static/*
    app.router.add_get("/", lambda r: web.FileResponse(HERE / "web" / "index.html"))
    app.router.add_get("/manifest.webmanifest", lambda r: web.FileResponse(HERE / "web" / "manifest.webmanifest"))
    app.router.add_static("/static", path=str(HERE / "web"), show_index=False)
    return app


def main() -> None:
    log.info("hermano starting on %s:%s - model=%s voice=%s", HOST, PORT, OPENAI_REALTIME_MODEL, OPENAI_REALTIME_VOICE)
    web.run_app(make_app(), host=HOST, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
