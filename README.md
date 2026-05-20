# Talk to Your Context

A live, interruptible voice line to your own agent. Browser ↔ OpenAI Realtime (`gpt-realtime-2`) ↔ a tiny local sidecar ↔ direct backends for the fast stuff, plus a slow `deep_research` path for the rare hard questions.

The point: instead of starting every voice turn from zero ("hi, can you help me with…"), the model lands in your session already grounded in *today's* standing context — open loops, calendar, recent decisions, the people you've been talking to — and reaches for narrow, low-latency tools (calendar, email, notes, journal) for the rest. Voice that feels like a colleague you've been working with all week, not a stranger you're briefing every time.

> Status: early but runnable. Local-first. Bring your own agent backend, or use the bundled stub to kick the tires in 60 seconds.

## What this is

- A reference pattern for connecting OpenAI Realtime to your own context — your notes, your calendar, your task tracker, your decisions journal — without funneling every turn through a single agent endpoint.
- An **env-driven memory registry** that lets you wire the voice chat into an existing personal-assistant memory bank (user profile, shared facts, voice-specific learnings) without touching code. Empty by default.
- A **narrow toolkit** (`/api/tool/<name>`) of direct backends — Supabase-shaped cards/journal, Google Workspace calendar+gmail, ripgrep-over-notes, transcript recall — that bypasses LLM-in-the-middle for the fast stuff.
- A **single slow path** (`deep_research`) reserved for genuinely novel reasoning, drafting, or side-effecting work, with streamed milestone narration so the user knows it's working.
- A **post-call learning loop**: the same agent call that updates the next session's dossier also extracts enduring voice-specific learnings (corrections, preferences, style) and a one-liner for a recent-calls index, so the voice chat never starts from scratch.

## What this is not

- **Not a universal personal-data scraper.** Bring your own backends. The reference adapters point at services you have to own and configure.
- **Not a production security/compliance template.** The default is loopback-only and there's no encryption-at-rest, no audit logging, no DLP — make those decisions for your own deployment.
- **Not a promise that your private context is safe** just because the sidecar runs locally. Voice transcripts hit OpenAI's Realtime API; agent calls hit whatever backend you wire in. Review the trust boundary before pointing this at sensitive data.
- **Not a replacement for per-app permissions.** If your calendar adapter has write access, this voice line has write access to your calendar.

## Quickstart (60 seconds, no backend required)

```bash
git clone https://github.com/brklyngg/talk-to-your-context
cd talk-to-your-context
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: set OPENAI_API_KEY. Default AGENT_API_BASE=stub:// uses the
# bundled in-process stub so you can smoke-test without a real backend.
python server.py
# Sidecar listens on http://127.0.0.1:8090
```

Open <http://127.0.0.1:8090> in Chrome (Realtime needs WebRTC). Click **Start call**.

**Verification step:** Once connected, ask: *"What tools do you have?"*

The model should list at least:

- `lookup_open_loop` · `recent_decisions` · `mission_control_card` (Supabase-backed cards/journal)
- `search_notes` (ripgrep over a notes directory)
- `calendar` · `gmail_search` (Google Workspace, via the gws adapter)
- `recall_recent_call` (excerpt of a prior transcript)
- `deep_research` (slow streaming path)

With the stub backend, calls like `deep_research` will return canned content that exercises the milestone-narration UX. Swap to a real backend when you want real answers.

## Wiring a real backend

Set `AGENT_API_BASE` to a URL exposing an OpenAI-compatible `/v1/chat/completions` SSE endpoint. The sidecar adds `X-Session-Id: voice-<conv_id>` so your backend can thread per-conversation memory.

Per-tool backends live in [`backends/`](backends/) as pure async functions and are registered in `server.py:_TOOL_DISPATCH`. Replace any of them — or add your own — without touching the rest of the system.

## Memory: optional, opt-in

Three optional file paths in `.env` wire the voice chat into an existing personal-memory bank. All default empty (the voice chat works without them):

```bash
# Read-only — prepended to Realtime session instructions on every mint.
VOICE_MEMORY_USER_PROFILE_PATH=...   # who you are (markdown, free-form)
VOICE_MEMORY_SHARED_PATH=...         # facts/context the model should always carry

# Voice-owned — the post-call extractor appends learnings here.
VOICE_MEMORY_VOICE_PATH=...          # enduring corrections, style, preferences
VOICE_MEMORY_TRANSCRIPT_INDEX_PATH=... # map of recent calls, expand via recall_recent_call
```

See `.env.example` for a worked example. The architecture doc covers the full read/write contract.

## Architecture in one diagram

```
   Browser (web/app.js, vanilla JS)
   │
   │  WebRTC peer  ────────────►  OpenAI Realtime (gpt-realtime-2)
   │      │
   │      └── function_call ──►  Local sidecar (server.py, aiohttp)
   │                                   │
   │                ┌──────────────────┼───────────────────┐
   │                ▼                  ▼                   ▼
   │         /api/tool/<name>   /api/deep-research   /api/session etc.
   │                │                  │
   │                ▼                  ▼
   │         backends/* (direct)  agent backend (SSE)
   │         ~50ms–1.5s            ~30–240s, streamed
   │                                   │
   │                                   ▼  milestones narrated mid-call
   │
   └── Instructions baked at mint time:
       Dossier (today's standing context)
       + Memory sources (user profile, shared, voice-learnings)
       + Recent-calls map (expand via recall_recent_call)
```

Depth: see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Security

This sidecar can mint OpenAI Realtime sessions and proxy turns to your agent. Treat it like any service that holds an API key:

- Keep `.env` and `.agent-api-key` out of git (already in `.gitignore`).
- Default `VOICE_ALLOWED_CIDR` is loopback only. Don't widen it without a network you trust (Tailscale, WireGuard, etc.).
- The browser never sees `OPENAI_API_KEY` or `AGENT_API_KEY`; both stay server-side.
- Transcripts are written to disk in plain JSON under `TRANSCRIPT_DIR`. Set accordingly.
- The voice chat **reads** from memory-source files you point it at; it **writes** only to the explicit `VOICE_MEMORY_VOICE_PATH` and `VOICE_MEMORY_TRANSCRIPT_INDEX_PATH`. Use that distinction to control blast radius.

## Verification & debugging

- **Per-call NDJSON** at `logs/calls/<conv_id>.ndjson` — append-only event trace. First place to look for WebRTC / Realtime / tool interactions. `cat logs/calls/<id>.ndjson | jq` is your friend.
- **Voice transcripts** at `$TRANSCRIPT_DIR/<conv_id>.json` — for UX-quality audits and the `recall_recent_call` tool.
- **`in_context_followup_rate`** in `events.py:compute_routing_metrics` — the headline metric. Fraction of user turns immediately after a tool answer that were served WITHOUT another tool call. Targets: ≥0.6 dossier-only, ≥0.8 full toolkit.

Syntax sanity-check before committing:

```bash
python3 -m py_compile server.py dossier.py voice_memory.py events.py transcripts.py auth.py backends/*.py
node --check web/app.js
```

## Layout

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — how the pieces talk, full lifecycle, tradeoffs.
- [`docs/ADAPTERS.md`](docs/ADAPTERS.md) — plugging in messaging backends (Slack today; pattern generalizes).
- [`docs/PRODUCT_ANALYSIS.md`](docs/PRODUCT_ANALYSIS.md) — requirements, compromises, decision criteria.
- [`backends/`](backends/) — direct-backend adapters + the stub agent.
- [`server.py`](server.py) · [`dossier.py`](dossier.py) · [`voice_memory.py`](voice_memory.py) · [`web/app.js`](web/app.js) — the four files that matter.

## License

TBD. Treat as "look, don't redistribute" until that lands.
