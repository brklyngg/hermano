"""Honcho context block for voice session mint.

Injected into the Realtime session instructions alongside the dossier and
voice_memory blocks. Provides cross-session Gary peer context (operating
model + evolving user representation + per-session summary) that hermes-mini
otherwise lacks — the dossier handles today-hot facts, this handles who
Gary is and what he's been working on across surfaces.

Reads the same ~/.honcho/config.json that the main Hermes Honcho plugin
uses, so the peer model compounds across CLI and voice. No coupling to
Hermes Python code — shares config, not implementation.

Two entry points used by server.py / dossier.py:
- render_mint_context_block(conv_id) -> markdown block (inference-free,
  fails silently to empty string, hard-capped to 1500 chars).
- record_call_facts(conv_id, working_state) -> push post-call decisions
  / commitments / open_questions / deltas into the voice session so the
  peer model learns from voice turns too.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ttyc.honcho_voice")

CONFIG_PATH = Path(os.getenv("HONCHO_CONFIG_PATH", "~/.honcho/config.json")).expanduser()
PEER_ID = os.getenv("HONCHO_PEER_ID", "gary")
ASSISTANT_PEER_ID = os.getenv("HONCHO_ASSISTANT_PEER_ID", "hermes")
BLOCK_CHAR_CAP = 1500
CONTEXT_TOKEN_BUDGET = 1500  # what we ask Honcho to give us; truncated again locally

_client: Any = None
_client_lock = threading.Lock()
_client_init_failed = False


def _load_config() -> dict | None:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        log.exception("failed to read honcho config at %s", CONFIG_PATH)
        return None


def _get_client():
    """Lazy singleton. Returns None on any failure so callers stay silent."""
    global _client, _client_init_failed
    if _client is not None or _client_init_failed:
        return _client
    with _client_lock:
        if _client is not None or _client_init_failed:
            return _client
        cfg = _load_config()
        if not cfg:
            _client_init_failed = True
            return None
        api_key = cfg.get("apiKey") or os.environ.get("HONCHO_API_KEY")
        workspace = cfg.get("workspace") or "hermes"
        if not api_key:
            log.warning("no Honcho api key in config or env — honcho_voice disabled")
            _client_init_failed = True
            return None
        try:
            from honcho import Honcho
            _client = Honcho(workspace_id=workspace, api_key=api_key)
        except Exception:
            log.exception("honcho client init failed — honcho_voice disabled")
            _client_init_failed = True
            return None
    return _client


def _truncate_word_boundary(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    cut = text[:cap]
    last_space = cut.rfind(" ")
    if last_space > cap - 200:  # don't drop too much
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def render_mint_context_block(conv_id: str) -> str:
    """Build the Honcho markdown block for a fresh voice session mint.

    Inference-free: uses session.context() + peer card reads only.
    Fails silently to empty string on any error.
    """
    h = _get_client()
    if h is None:
        return ""
    try:
        t0 = time.monotonic()
        peer = h.peer(PEER_ID)
        session = h.session(f"voice-{conv_id}")
        # Idempotent: ensures the session tracks this peer so session-level
        # context queries return their card/representation in subsequent calls.
        try:
            session.add_peers([peer])
        except Exception:
            pass

        parts: list[str] = ["## Honcho Context"]

        # Try session-level context first (carries session summary + cross-session
        # representation built from prior voice + CLI turns).
        try:
            ctx = session.context(summary=True, tokens=CONTEXT_TOKEN_BUDGET)
            summary_obj = getattr(ctx, "summary", None)
            summary_content = (getattr(summary_obj, "content", None) or "").strip() if summary_obj else ""
            if summary_content:
                parts.append(f"### Session summary so far\n{summary_content}")
            session_peer_rep = (getattr(ctx, "peer_representation", None) or "").strip()
        except Exception:
            log.exception("session.context failed; falling back to peer-level reads")
            session_peer_rep = ""

        # Peer card: source of truth is the peer's own card (seeded via set_card).
        # Defensive: try get_card then legacy card attribute.
        peer_card = None
        for attr in ("get_card", "card"):
            method = getattr(peer, attr, None)
            if callable(method):
                try:
                    peer_card = method()
                    break
                except Exception:
                    continue
        if peer_card:
            card_str = "\n".join(f"- {fact}" for fact in peer_card)
            parts.append(f"### Gary peer card\n{card_str}")

        # Peer representation: prefer session-level (has session-relevant focus);
        # fall back to peer-level for cold starts.
        peer_rep = session_peer_rep
        if not peer_rep:
            try:
                pctx = peer.context()
                peer_rep = (getattr(pctx, "representation", None)
                            or getattr(pctx, "peer_representation", None) or "").strip()
            except Exception:
                pass
        if peer_rep:
            parts.append(f"### Gary, as Honcho models him\n{peer_rep}")

        if len(parts) == 1:  # only header, nothing useful
            return ""

        block = "\n\n".join(parts)
        block = _truncate_word_boundary(block, BLOCK_CHAR_CAP)
        log.info(
            "honcho_voice mint block: %d chars in %.0fms",
            len(block), (time.monotonic() - t0) * 1000,
        )
        return block
    except Exception:
        log.exception("honcho_voice render failed — returning empty block")
        return ""


def record_call_facts(conv_id: str, working_state: dict) -> None:
    """Push post-call working state into the Honcho voice session.

    working_state shape from dossier.extract_working_state:
        {decisions: [{"text": str}], commitments: [...],
         open_questions: [...], deltas: [...]}
    """
    h = _get_client()
    if h is None:
        return
    if not working_state or not any(working_state.get(k) for k in ("decisions", "commitments", "open_questions", "deltas")):
        return
    try:
        session = h.session(f"voice-{conv_id}")
        peer = h.peer(PEER_ID)
        payload = json.dumps(working_state, ensure_ascii=False)
        session.add_messages([peer.message(payload)])
        log.info("honcho_voice recorded call facts for conv=%s (%d chars)", conv_id, len(payload))
    except Exception:
        log.exception("honcho_voice record_call_facts failed for conv=%s", conv_id)
