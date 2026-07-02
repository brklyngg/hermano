"""Obsidian vault search via ripgrep.

Direct fs grep — no LLM, no indexing service, ~100–500ms typical. The query
is tokenized into content terms (stopwords dropped) and OR-matched, then
files are ranked by how many DISTINCT query terms they contain. Returns
top-k as `[{path, snippet, score, terms_matched}]` where `score` is the
fraction of query terms present in the file (best coverage first).

Prior behavior matched the whole multi-word query with `--fixed-strings`,
which required the entire phrase verbatim on one line — so natural-language
asks almost never matched (~81% returned []). Term-OR + coverage ranking
fixes that.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger("ttyc.backends.notes")

OBSIDIAN_VAULT = Path(os.path.expanduser(os.getenv(
    "OBSIDIAN_VAULT",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Obsidian Vault",
)))
RG = shutil.which("rg") or "/opt/homebrew/bin/rg"

# Dropped from note-search queries so multi-word natural-language asks match
# on their content terms (OR) instead of requiring the whole phrase verbatim.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with", "at",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "have", "has",
    "had", "that", "this", "it", "its", "we", "you", "i", "my", "me", "our",
    "your", "their", "his", "her", "they", "them", "he", "she", "us", "but",
    "not", "no", "do", "did", "does", "can", "could", "would", "should",
    "about", "into", "over", "up", "down", "out", "so", "if", "then", "than",
    "what", "which", "who", "when", "where", "how", "all", "any", "some", "one",
    "get", "got", "find", "look", "need", "want", "please", "hey", "yeah",
    "okay", "just", "like", "was", "were",
})


def _tokenize(query: str) -> list[str]:
    """Content tokens from a natural-language query: lowercased alphanumerics,
    stopwords and ≤2-char tokens dropped, de-duped in order. Falls back to all
    alnum tokens if filtering leaves nothing (query of only short/stop words)."""
    raw = re.findall(r"[a-z0-9]+", query.lower())
    toks = [t for t in raw if len(t) > 2 and t not in _STOPWORDS]
    if not toks:
        toks = [t for t in raw if t]
    seen: set[str] = set()
    return [t for t in toks if not (t in seen or seen.add(t))]


async def search_notes(query: str, k: int = 5) -> list[dict] | dict:
    if not query or not query.strip():
        return {"error": "empty_query"}
    if not OBSIDIAN_VAULT.exists():
        return {"error": "vault_not_found"}
    if not Path(RG).exists():
        return {"error": "ripgrep_missing"}
    k = max(1, min(int(k), 20))
    tokens = _tokenize(query)
    if not tokens:
        return []
    # OR the content tokens (rg -e per term). rg can't report which pattern
    # matched, so we score each file by how many DISTINCT query terms it
    # contains and rank by that coverage. --max-count bounds noisy files.
    args = [
        RG, "--no-heading", "--with-filename", "--line-number",
        "--smart-case", "--max-count", "50", "--max-columns", "200",
        "--type-add", "md:*.md", "-tmd",
    ]
    for t in tokens:
        args += ["-e", t]
    args.append(str(OBSIDIAN_VAULT))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=6.0)
        except asyncio.TimeoutError:
            proc.kill()
            return {"error": "ripgrep_timeout"}
    except Exception as e:  # noqa: BLE001
        log.warning("ripgrep failed: %s", e)
        return {"error": "ripgrep_failed"}
    # rg exits 1 with empty output when nothing matched — that's an empty
    # result, not an error. Group matching lines per file and score by the
    # count of distinct query terms the file covers.
    n_tokens = len(tokens)
    line_re = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?P<text>.*)$")
    per_file: dict[str, dict] = {}
    for raw in stdout.decode(errors="replace").splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        path = m.group("path")
        text = m.group("text").strip()
        low = text.lower()
        matched = {t for t in tokens if t in low}
        if not matched:
            continue
        entry = per_file.setdefault(path, {"tokens": set(), "best_n": 0, "best": text})
        entry["tokens"] |= matched
        if len(matched) > entry["best_n"]:
            entry["best_n"] = len(matched)
            entry["best"] = text
    ranked = sorted(
        per_file.items(),
        key=lambda kv: (len(kv[1]["tokens"]), kv[1]["best_n"]),
        reverse=True,
    )[:k]
    out: list[dict] = []
    for path, entry in ranked:
        rel = path
        try:
            rel = str(Path(path).relative_to(OBSIDIAN_VAULT))
        except ValueError:
            pass
        out.append({
            "path": rel,
            "snippet": entry["best"][:280],
            "score": round(len(entry["tokens"]) / n_tokens, 3),
            "terms_matched": f"{len(entry['tokens'])}/{n_tokens}",
        })
    return out
