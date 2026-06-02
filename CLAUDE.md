# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local voice sidecar that connects a browser to OpenAI Realtime (voice/ears) and to a structured **dossier of standing context** plus a granular **direct-backend toolkit** (Supabase, Google Workspace, Obsidian) so the voice model is deeply contextual from second 1, not "generic until deep dive." A single slow path (`deep_research`) reaches the agent backend for novel reasoning. See `docs/ARCHITECTURE.md` for the rationale.

## Run / dev commands

```bash
# First-time setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit OPENAI_API_KEY + AGENT_API_BASE + DOSSIER_PATH

# Run
python server.py       # serves http://127.0.0.1:8090 by default

# Live deploy (personal Mac Mini — symlinks source into ~/.hermes-custom/hermes-mini/ and kicks launchd)
./sync-to-deploy.sh
```

No test suite or linter. Verification:
- **Per-call NDJSON** at `logs/calls/<conv_id>.ndjson` — append-only event trace. First place to look for WebRTC / Realtime / tool interactions.
- **Voice transcripts** at `$TRANSCRIPT_DIR/*.json` — for UX-quality audits. Each transcript carries `packet_ref` → `$TRANSCRIPT_DIR/packets/<conv_id>.instructions.md`, the exact instructions payload the Realtime session was minted with (labeled "secondary handoff artifact", not source-of-truth — revalidate before reuse).
- **`in_context_followup_rate`** in `events.py:compute_routing_metrics` — the headline metric. Fraction of user turns immediately after a tool answer that were served WITHOUT another tool call. Targets: ≥0.6 dossier-only, ≥0.8 full toolkit.

Syntax sanity-check:

```bash
python3 -m py_compile server.py dossier.py events.py transcripts.py auth.py backends/*.py
node --check web/app.js
```

## Architecture (the parts that span multiple files)

**Four layers:**

1. **Browser** (`web/app.js`, vanilla JS, no build step). WebRTC peer to OpenAI Realtime, mic stream, soft synth-pad icebreaker (cap-swap / forced-reconnect only — not in-call tool waits), per-tool consulting chips, SSE reader for `deep_research`, forced-reconnect continuity state, and a first-turn hint-echo guard (drops a transcript that's just the `OPENAI_REALTIME_TRANSCRIPTION_HINTS` prompt echoed back on opening silence). Loaded via `index.html`.
2. **Sidecar** (`server.py`, aiohttp). Mints Realtime ephemerals with the dossier baked into `instructions`. Dispatches `/api/tool/<name>` to `backends/*` (no LLM in path) and `/api/deep-research` to the streaming agent path. Owns `CONVERSATIONS` in-memory dict, persists transcripts.
3. **Dossier** (`dossier.py` + `~/.hermes/dossier/today.json`). Structured snapshot of today's standing context — open loops, calendar, recent decisions, hot people, last call's working state. Rendered as markdown and prepended to every session's instructions via `_mint_realtime_session(instructions_suffixes=[…])`, alongside `_accounts_brief()` — the real GWS account roster (with labels) so the model never invents an address and knows to fan out across inboxes. Regenerated on server boot and after every call ends (post-call extractor → refresh chain).
3b. **Honcho peer memory** (`honcho_voice.py`). Cross-session user model shared with the main Hermes agent via `~/.honcho/config.json` (workspace `hermes`, peer `gary`). At mint, injects a capped (1500 char) markdown block with the seeded peer card + any session summary/representation Honcho has accumulated — inference-free reads (`session.context()`, `peer.get_card()`). Fails silently to empty string. Post-call, `dossier.extract_working_state` routes `{decisions, commitments, open_questions, deltas}` to the Honcho `voice-{conv_id}` session so voice turns feed the same model CLI turns do. Guard at mint drops the Honcho block if total instructions would exceed ~14k tokens (Realtime cap is 16,384).
4. **Agent backend** (external). Reached via `AGENT_API_BASE` (default `http://127.0.0.1:8642`). Speaks OpenAI-style chat-completions SSE. Now reached only by: `deep_research`, dossier refresh, post-call working-state extraction, `/api/text-turn`. `X-Session-Id: voice-{conv_id}` header threads continuity (concurrent `deep_research` runs get a `#{call_id}` suffix to isolate per-session state).

**Tools registered per session (in `TOOLKIT_SCHEMAS`):**

| Tool | Latency | Transport | Backend |
|---|---|---|---|
| `lookup_open_loop(id)` | ~150ms | function | `backends.supabase` |
| `recent_decisions(days)` | ~100ms | function | `backends.supabase` |
| `mission_control_card(id)` | ~150ms | function | `backends.supabase` |
| `search_notes(query, k)` | 100–3000ms | function | `backends.notes` (ripgrep) |
| `calendar(when, account)` | ~500ms | function | `backends.gws` (gws-as.sh) |
| `gmail_search(query, account, limit)` | 500–1500ms | function | `backends.gws` |
| `recall_recent_call(conv_id, query?)` | ~10ms | function | `backends.transcripts_recall` (local files) |
| `deep_research(prompt, scope, expected_seconds)` | 30–240s | SSE | agent backend |
| `cancel_research(reason?)` | <50ms | client-only | — (aborts in-flight `deep_research` fetches) |

`deep_research` covers both *thinking* (reasoning/drafting/synthesis) and *doing* (actions with side effects: filesystem writes, Gmail drafts, Calendar writes, scripts) — the agent backend has the tools. Scope arg is `action|drafting|reasoning|synthesis`. For "save this to ~/Desktop/foo.md" or similar, the model calls `deep_research` with `scope=action` and the agent executes. Multiple **independent** `deep_research` calls may run in parallel (prompt rule 5b permits it); `cancel_research` stops all in-flight ones so the model can redirect. `cancel_research` is handled entirely client-side (no `_TOOL_DISPATCH` backend) — it aborts the fetch(es) and returns cancelled outputs.
| `triage_verdict(loop_id, verdict, …)` | <50ms | function | `/api/triage-verdict` |

Per-tool TTL cache in `server.py:_TOOL_TTL` keyed on `(tool, args_hash)` via `backends/cache.py`.

**Narrow-tool flow:**
- Realtime emits `function_call` over the data channel. Browser POSTs `/api/tool/<name>` with `{conv_id, args}`. Sidecar dispatches through `_TOOL_DISPATCH` table → memoized `backends/*` call → JSON return. Sidecar persists a structured tool turn to the transcript; browser feeds JSON verbatim to Realtime as `function_call_output` so the model addresses fields directly instead of paraphrasing prose. Per-tool consulting chips fade in/out per `call_id`, supporting native parallel function calls on gpt-realtime-2.

**`deep_research` flow:**
- Browser POSTs `/api/deep-research` (with the Realtime `call_id`) and reads the SSE stream. Sidecar (`_agent_chat_stream`) streams agent chunks, detects markdown section boundaries (`\n## ` headers), and emits `{type:"milestone", section, text}` events on a 3-second server-side throttle. Each milestone in the browser does two things: (1) appends `[research-finding] section=…: …` to conversation history via `conversation.item.create` (silent), and (2) fires an out-of-band narration (`response.conversation: "none"`, `output_modalities: ["audio"]`) for that one finding. Narration is throttled separately from milestone ingest (≥10s between spoken updates). The consulting chip shows `Researching… (Ns)` elapsed so a long run never reads as a hang. On `{type:"done"}` browser sends the assembled answer as `function_call_output`. Liveness: `ASK_AGENT_IDLE_TIMEOUT_SEC` (45s) is the primary read-watchdog via httpx; `ASK_AGENT_TIMEOUT_SEC` (600s) is the runaway guard. Errors surface via `_AGENT_UNREACHABLE_SENTINEL` so prompt rule fires.
- **Parallel + cancel:** the client tracks in-flight runs in `activeResearch` (call_id → AbortController). Concurrent runs are isolated server-side by an `X-Session-Id: voice-{conv_id}#{call_id}` suffix so the agent's per-session working state can't interleave (the first/sole run keeps the base session for cross-turn continuity). `cancel_research` aborts the fetch(es); an atomic `activeResearch.delete()` is the single arbiter that prevents a double `function_call_output` (whoever wins the delete emits).
- **Gated response dispatcher (`requestResponse()`):** the Realtime API allows ONE active response at a time (`conversation_already_has_active_response`). Every `response.create` in `web/app.js` — opener, handoff note, OOB narration, function-call returns — routes through `requestResponse()`, which fires when idle or queues until `response.done` drains it. This is what makes parallel returns + narration safe to overlap. Do NOT send raw `response.create` outside this helper.

**Dossier refresh + post-call working-state extraction:**
- `dossier.refresh_dossier()`: one agent call with a JSON-only directive → atomic write to `DOSSIER_PATH`. 30-min debounce; `force=True` bypasses. Schema: `{open_loops, calendar_today, recent_decisions, hot_people, last_handoff_summary, working_state_from_last_call}`.
- `dossier.extract_working_state(conv_id, entries, started_at)`: one agent call after a call ends produces *three* outputs from one transcript pass: `{decisions, commitments, open_questions, deltas}` merged into the dossier (cross-call continuity); `voice_learnings[]` appended to `VOICE_MEMORY_VOICE_PATH` as `§`-separated paragraphs (enduring lessons across all future calls — corrections, preferences, style); and `call_one_liner` upserted into `VOICE_MEMORY_TRANSCRIPT_INDEX_PATH` so the next session's instructions carry a compact map of prior calls (model expands any entry on demand via `recall_recent_call`).
- Local-date gating (`DOSSIER_TZ`, default America/New_York): when the on-disk dossier was generated on a prior local date (first call after midnight, etc.) it's *still* served to the in-flight session — `load_dossier()` prepends a `> Standing context — snapshot from <date>; today's regen in progress.` header rather than returning `None`. Mint never blocks on dossier and additionally kicks a background `refresh_dossier(force=True)` so the next session lands fresh (self-healing on day rollover). `session_minted` carries `dossier_stale`, `dossier_chars`, `instructions_chars`, `instructions_hash` (sha1[:12]), a per-source `sources[]` breakdown, `dedupe_dropped_lines`, and `packet_ref` so prompt drift across calls is auditable.

**`triage_verdict` (opt-in via `?mode=triage`):**
- `_load_open_loops_brief()` lazy-imports `~/.hermes-custom/open-loops/brief_format.py`, calls `render()`, then runs the result through `_triage_guardrails()` which prepends two override blocks: (a) tool-use rules — only `triage_verdict` is allowed in triage; `lookup_open_loop` will 400 because the brief's `ol_*` IDs are a separate ID space from Mission Control UUIDs; (b) a presentation rule that overrides the brief's "BLUF in ≤8 words" with the universal CONTEXT-SUFFICIENCY rule (1–3 sentences of grounding before each verdict ask, phrased naturally per-loop). If `render()` returns `None` because the brief is from yesterday, the wrapper passes the on-disk JSON back through with `generated_at` spoofed to bypass the date gate, prepended with a staleness header — same pattern as the dossier's stale-load fallback. Client routes `triage_verdict` function_calls to `POST /api/triage-verdict`. Reconcile cron (open-loops repo) creates `[Hermes Draft]`-prefixed calendar events for `act` verdicts.

**Forced-reconnect continuity (load-bearing, non-obvious):**
Realtime sessions hard-die at the 60-min cap; mobile resumes (visibility, network blip, silent freeze, `session_expired`) all funnel through `triggerResume` → `/api/resume`. Two mechanisms preserve continuity:

1. **Handoff note + recent-conversation recap** — two complementary signals baked into the resumed session's `instructions` alongside the dossier. The handoff note is the dying Realtime model's ≤80-word continuation cue (text-only, 3.5s deadline, partial-buffer fallback, persisted via `POST /api/handoff-note`). The recap is a bounded (~12 turns / ~2k chars) factual tail built by `server.py:_build_resume_recap` from `conv["entries"]` (tool turns — files saved, research results) merged with the client's recent `clientEntries` (audio turns) sent in the `/api/resume` body. The recap is the fallback that keeps continuity intact when the handoff note fails — a `silent_freeze` yields a 0-char note, and without the recap the resumed session forgets actions like "saved file X". Client `onDcOpen` echoes assembled `instructions` + `tools` via `session.update` as defense against the legacy `gpt-realtime` tools-drop quirk.
2. **60-min cap pre-mint + comfort-bed bridge** — at 58:30 client POSTs `/api/premint-session`; cap-swap fires at `min(59:00, expires_at − 10s)` and reuses `/api/resume`. Icebreaker (a quiet procedural sine pad via WebAudio — `createBrownNoiseIcebreaker`, kept that name; swaps to `/static/ambient.mp3` if served) fades in *before* peer teardown and out only after `pc.ontrack` first audio frame on the new peer. Client helpers: `schedulePremint`, `doPremint`, `gracefulCapSwap`, `endIcebreaker`.

**Cost controls (load-bearing finding):**
This app mints with `semantic_vad`, so per OpenAI's cost guide **idle/wait silence is not billed** and an idle connection has ~no token cost. The real spend driver is per-turn token accumulation (the whole conversation is re-billed as input each turn) + audio output, mitigated ~80× by within-session prompt caching. Levers, all env-tunable: native `session.truncation = {type: retention_ratio, retention_ratio: REALTIME_TRUNCATION_RETENTION_RATIO, token_limits: {post_instructions: REALTIME_POST_INSTRUCTIONS_TOKENS}}` in the mint body (default cap 48000 — favors in-call continuity over marginal cost; set `REALTIME_POST_INSTRUCTIONS_TOKENS=0` to omit the cap and use the model's full window); `OPENAI_REALTIME_MODEL` toggle (try `gpt-realtime-mini` for cheaper rates); `VOICE_MEMORY_BLOCK_CHAR_CAP`; `REAPER_IDLE_TIMEOUT_SEC`. **Do not hand-roll `conversation.item.delete` pruning** — it busts the cached prefix and is net-negative. Per-call `cache_hit_ratio` + `cost_estimate_usd` from `response.done.usage` are emitted in `events.py:compute_routing_metrics`; cache-hit ratio is the headline efficiency metric.

**Hard cutover on legacy `/api/ask-agent`:** returns 410 `{error:"client_outdated", reload_required:true}`. The `no_cache_static_middleware` already forces shell revalidation; one reload restores normal operation. Stale-cache fallthrough into the slow path is exactly the regression the toolkit refactor exists to kill.

**Shared memory contract (`voice_memory.py`):**
The voice chat reads up to three external memory files via the `VOICE_MEMORY_*` env registry (`USER_PROFILE`, `SHARED`, `VOICE`) plus a transcript index. Defaults are all empty so the public quickstart needs no external memory. Personal deployments wire these to an existing agent's memory bank (e.g. `~/.hermes/memories/{USER,MEMORY,VOICE}.md`).

- **Read-only from this codebase**: `USER_PROFILE` and `SHARED`. The voice chat ingests them as opaque markdown and prepends them to instructions under their configured `## <header>`. Format drift in the source files (e.g. Hermes upstream renaming or restructuring) is graceful — we don't parse, we splice.
- **Voice-owned (writable)**: `VOICE_MEMORY_VOICE_PATH` and `VOICE_MEMORY_TRANSCRIPT_INDEX_PATH`. Atomic writes via tempfile+rename. Never edit these manually during an in-flight call (last-writer-wins race on close).
- **`ingest_into_agent` continues to fire** in parallel with the new VOICE.md loop. The two are complementary: the agent backend gets the raw transcript via the existing ingest call; the voice-owned VOICE.md is the structured, prompt-injected, voice-side learning loop. Do not delete `ingest_into_agent` on the assumption they're redundant.
- **Post-call extraction covers abrupt endings**: both `/api/end` and the `_reap_stale_conversations` reaper call `dossier.schedule_extract`, which now produces all three outputs (dossier working state, voice learnings, transcript-index entry). Calls that end without `/api/end` (driving, signal loss) still feed the learning loop via the reaper path within `REAPER_IDLE_TIMEOUT_SEC`.

**Concurrency hazards handled:**
- `responseInFlight` tracking gates handoff-note requests (Realtime allows only one response at a time).
- Mute state preserved across resume by re-applying `track.enabled = false` after `attachPeer`.
- Parallel function calls each get their own `call_id`-keyed chip in `consultingChips`; independent fade-out.

## Key files

- `server.py` — sidecar; routes (`/api/session`, `/api/tool/{name}`, `/api/deep-research`, `/api/text-turn`, `/api/resume`, `/api/premint-session`, `/api/handoff-note`, `/api/end`, `/api/client-event`, `/api/triage-verdict`, `/api/health`). `/api/ask-agent` returns 410.
- `dossier.py` — structured dossier schema, `render_markdown`, `load_dossier`, `refresh_dossier`, `extract_working_state` (also produces voice_learnings + call_one_liner), fire-and-forget schedulers.
- `voice_memory.py` — env-driven memory-source registry, voice-owned learnings file, transcript-index helpers. All-optional; public default is empty.
- `honcho_voice.py` — Honcho cross-session peer-memory injector + post-call recorder. Reads `~/.honcho/config.json`; no-op when missing. See section 3b above.
- `backends/` — `supabase` (Mission Control), `gws` (Calendar/Gmail), `notes` (Obsidian/ripgrep), `transcripts_recall` (local-file recall_recent_call backend), `cache` (TTL memo), `stub_agent` (in-process canned-response stub for `AGENT_API_BASE=stub://`). Pure async functions; designed so Phase D can lift them into an MCP server unchanged.
- `web/app.js` — WebRTC peer + DC switch (`onDcMessage`), per-tool consulting chips, `dispatchToolCall` table, `handleNarrowTool`, `handleDeepResearch` (SSE reader + system-message milestone injection), `handleTriageVerdict`, forced-reconnect machinery.
- `events.py` — NDJSON logger; `compute_routing_metrics` derives per-tool counters/latencies, `deep_research_ratio`, `in_context_followup_rate`, plus cost telemetry (`cache_hit_ratio`, `cost_estimate_usd`, `usage_tokens`) from `response.done.usage` against a public `REALTIME_RATES_USD_PER_MTOK` table.
- `transcripts.py` — end-of-call persistence + optional Slack archive.
- `auth.py` — request auth + CIDR allowlist.

## Style

- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
- `cleanupCall()` is for **true end** only. Forced reconnects must NOT call it — they preserve `convId`, `clientEntries`, the icebreaker, and the AudioContext across the gap. `endIcebreaker()` exists so the resume path doesn't accidentally close the AudioContext.
- `voice` is **locked** after the first audio response. Never re-send it on `session.update` — doing so tears the session down.
- **UX language: describe the action, not the architecture.** Tool-call chips describe what's happening for the user ("Searching email…", "Researching: …") — never how the system is doing it. The dossier/toolkit/agent split is an implementation detail; users see a single colleague.
- **CONTEXT-SUFFICIENCY (`_BASE_PROMPT` rule 9b, load-bearing):** Gary is managing many parallel threads. Whenever the model introduces *any* item — open loop, calendar event, email, card, research finding, draft for review — it must give 2–4 sentences of disambiguating grounding (what it is, why it surfaced, what's open) before asking for a decision. Never lead with "Item N: drop, park, or act?" — that puts the cognitive load on the user. Any new tool/feature that surfaces items inherits this contract; the prompt rule already covers it but UI affordances should not undercut it (e.g., don't add a chip that says only "Card 7?" with no context).
- New backends go in `backends/` as pure async functions; register in `server.py:_TOOL_DISPATCH` with a TTL in `_TOOL_TTL`. The tool schema lives next to its peers in `server.py` and goes into `TOOLKIT_SCHEMAS`. Browser dispatch picks it up via the `NARROW_TOOLS` set and `TOOL_CHIPS` phrasing table.

## Security boundaries

The browser never sees `OPENAI_API_KEY`, `AGENT_API_KEY`, or `SUPABASE_SERVICE_KEY` — all server-side. `VOICE_ALLOWED_CIDR` defaults to loopback; widen only on a trusted network (Tailscale, WireGuard). Transcripts and dossier are plain JSON on disk (`TRANSCRIPT_DIR`, `DOSSIER_PATH`).
