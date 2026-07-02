"""Google Workspace queries via the gws-as.sh per-account wrapper.

Wrapper enforces account selection + token-store integrity (see CLAUDE.md).
We shell out with `asyncio.create_subprocess_exec`, parse stdout JSON,
return small structured dicts.

`when` parsing for calendar accepts: "today", "tomorrow", "YYYY-MM-DD",
or an ISO datetime range "<start>/<end>". Anything ambiguous falls back
to today.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ttyc.backends.gws")

GWS_WRAPPER = Path(os.path.expanduser("~/scripts/gws-as.sh"))
# Both empty by default — a public clone with no GWS config registers no
# Google Workspace tools and silently falls through if anything calls them.
# Personal deployments set both in .env. ALLOWED_ACCOUNTS is comma-separated.
DEFAULT_ACCOUNT = os.getenv("GWS_DEFAULT_ACCOUNT", "").strip()
ALLOWED_ACCOUNTS = {
    a.strip() for a in os.getenv("GWS_ALLOWED_ACCOUNTS", "").split(",") if a.strip()
}


def _local_tz() -> timezone:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.getenv("DOSSIER_TZ", "America/New_York"))  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return timezone.utc


def _check_account(acct: str) -> dict | None:
    """Return a structured error if `acct` isn't a configured account, else None.

    Surfacing the valid roster (instead of a silent failure) lets the voice
    model self-correct when it guessed/invented an address — the prior behavior
    was an empty result it would misread as "no mail from that person."
    """
    if ALLOWED_ACCOUNTS and acct not in ALLOWED_ACCOUNTS:
        return {"error": "unknown_account", "requested": acct, "valid": sorted(ALLOWED_ACCOUNTS)}
    return None


async def _run_gws(account: str, args: list[str], *, timeout_s: float = 8.0) -> dict | list | None:
    """Spawn the wrapper, parse JSON stdout. Returns None on failure (caller decides)."""
    if not GWS_WRAPPER.exists():
        log.warning("gws wrapper not at %s", GWS_WRAPPER)
        return None
    if account not in ALLOWED_ACCOUNTS:
        log.warning("gws account not allowed: %s", account)
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(GWS_WRAPPER), account, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("gws timeout (account=%s args=%s)", account, args[:3])
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("gws spawn failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("gws returncode=%s stderr=%s", proc.returncode, stderr[:300].decode(errors="replace"))
        return None
    out = stdout.decode(errors="replace").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        log.warning("gws non-JSON stdout: %s", out[:200])
        return None


# ---- calendar ----

_WHEN_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?\s*/\s*(\d{4}-\d{2}-\d{2})(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?\s*$")


def _resolve_when(when: str | None) -> tuple[datetime, datetime]:
    """Map a `when` arg to (timeMin, timeMax) in UTC. Falls back to today."""
    tz = _local_tz()
    today_local = datetime.now(tz).date()
    w = (when or "").strip().lower()
    if not w or w == "today":
        start = datetime.combine(today_local, time.min, tz)
        end = start + timedelta(days=1)
    elif w == "tomorrow":
        start = datetime.combine(today_local + timedelta(days=1), time.min, tz)
        end = start + timedelta(days=1)
    elif w == "this week":
        start = datetime.combine(today_local, time.min, tz)
        end = start + timedelta(days=7)
    elif (m := _WHEN_RE.match(when or "")):
        try:
            d1 = datetime.fromisoformat(m.group(1)).replace(tzinfo=tz)
            d2 = datetime.fromisoformat(m.group(2)).replace(tzinfo=tz) + timedelta(days=1)
            start, end = d1, d2
        except ValueError:
            start = datetime.combine(today_local, time.min, tz)
            end = start + timedelta(days=1)
    else:
        try:
            d = datetime.fromisoformat(w).date() if "-" in w else today_local
            start = datetime.combine(d, time.min, tz)
            end = start + timedelta(days=1)
        except ValueError:
            start = datetime.combine(today_local, time.min, tz)
            end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _fmt_hhmm(iso_or_date: str, tz: timezone) -> str:
    """Render an event start/end into HH:MM local. All-day events become 'all-day'."""
    if not iso_or_date:
        return "?"
    if "T" not in iso_or_date:
        return "all-day"
    try:
        dt = datetime.fromisoformat(iso_or_date.replace("Z", "+00:00")).astimezone(tz)
        return dt.strftime("%H:%M")
    except ValueError:
        return iso_or_date[:16]


async def calendar(when: str | None = None, account: str | None = None) -> list[dict] | dict:
    """Return events for `when` on the given account's primary calendar.

    Shape: [{start, end, title, attendees}] — local-time HH:MM, attendees as
    bare email list (organizer included if not the account holder).
    """
    acct = account or DEFAULT_ACCOUNT
    if (err := _check_account(acct)) is not None:
        return err
    time_min, time_max = _resolve_when(when)
    params = {
        "calendarId": "primary",
        "timeMin": time_min.isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.isoformat().replace("+00:00", "Z"),
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 25,
    }
    raw = await _run_gws(acct, ["calendar", "events", "list", "--params", json.dumps(params)])
    if raw is None:
        return {"error": "gws_unavailable"}
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return {"error": "unexpected_response"}
    tz = _local_tz()
    out: list[dict] = []
    for ev in items:
        if ev.get("status") == "cancelled" or ev.get("eventType") == "birthday":
            continue
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        attendees: list[str] = []
        for a in (ev.get("attendees") or []):
            e = a.get("email")
            if e and not a.get("self"):
                attendees.append(e)
        out.append({
            "start": _fmt_hhmm(start.get("dateTime") or start.get("date") or "", tz),
            "end": _fmt_hhmm(end.get("dateTime") or end.get("date") or "", tz),
            "title": ev.get("summary") or "(no title)",
            "location": ev.get("location") or "",
            "description": (ev.get("description") or "")[:700],
            "link": ev.get("htmlLink") or "",
            "attendees": attendees[:10],
        })
    return out


# ---- gmail ----

async def gmail_search(query: str, account: str | None = None,
                       limit: int = 10) -> list[dict] | dict:
    """Search Gmail.

    When `account` is omitted, fan out across all configured accounts and return
    the newest matches globally. Voice users often say "check my inbox" while
    meaning "personal or Crunchy"; searching only DEFAULT_ACCOUNT caused false
    negative answers even though the message existed in another configured inbox.

    Shape: [{account, from, subject, snippet, ts}].
    """
    if not query or not query.strip():
        return {"error": "empty_query"}
    max_results = int(max(1, min(limit, 25)))
    q = query.strip()

    if account:
        accounts = [account]
    else:
        # Stable order: default first for backwards-compatible latency/logs, then
        # the rest alphabetically. The final result is sorted by internalDate.
        accounts = []
        if DEFAULT_ACCOUNT:
            accounts.append(DEFAULT_ACCOUNT)
        accounts.extend(a for a in sorted(ALLOWED_ACCOUNTS) if a not in accounts)
    if not accounts:
        return {"error": "no_configured_accounts"}
    for acct in accounts:
        if (err := _check_account(acct)) is not None:
            return err

    async def _search_account(acct: str) -> list[dict] | dict:
        list_params = {"userId": "me", "q": q, "maxResults": max_results}
        listed = await _run_gws(acct, ["gmail", "users", "messages", "list",
                                       "--params", json.dumps(list_params)])
        if listed is None:
            return {"error": "gws_unavailable", "account": acct}
        messages = listed.get("messages") if isinstance(listed, dict) else None
        if not isinstance(messages, list) or not messages:
            return []

        # Hydrate each message's headers in parallel via separate wrapper calls.
        async def _hydrate(mid: str) -> dict | None:
            gparams = {"userId": "me", "id": mid, "format": "metadata",
                       "metadataHeaders": ["From", "Subject", "Date"]}
            raw = await _run_gws(acct, ["gmail", "users", "messages", "get",
                                        "--params", json.dumps(gparams)],
                                 timeout_s=6.0)
            if not isinstance(raw, dict):
                return None
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in (raw.get("payload", {}).get("headers") or [])}
            internal_ms = 0
            try:
                internal_ms = int(raw.get("internalDate") or 0)
            except (TypeError, ValueError):
                internal_ms = 0
            return {
                "account": acct,
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "snippet": (raw.get("snippet") or "")[:300],
                "ts": headers.get("date") or raw.get("internalDate"),
                "_internal_ms": internal_ms,
            }
        hydrated = await asyncio.gather(*[_hydrate(m["id"]) for m in messages if m.get("id")])
        return [m for m in hydrated if m]

    searched = await asyncio.gather(*[_search_account(acct) for acct in accounts])
    errors = [r for r in searched if isinstance(r, dict) and r.get("error")]
    rows: list[dict] = []
    for result in searched:
        if isinstance(result, list):
            rows.extend(result)

    if not rows and errors:
        return {"error": "gws_unavailable", "accounts": [e.get("account") for e in errors]}

    rows.sort(key=lambda m: int(m.get("_internal_ms") or 0), reverse=True)
    out = rows[:max_results]
    for row in out:
        row.pop("_internal_ms", None)
    return out


# ---- drive ----

_DRIVE_TYPE = {
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.google-apps.folder": "folder",
    "application/pdf": "pdf",
}


async def drive_search(query: str, account: str | None = None,
                       limit: int = 10) -> list[dict] | dict:
    """Find Drive files (Docs, Sheets, Slides, folders) by name or full-text.

    Locates a document/spreadsheet the user references but whose ID we don't
    have ("the sheet we were working in yesterday"). When `account` is omitted,
    fans out across all configured accounts and merges newest-first — Gary's
    files live across four accounts and he rarely remembers which. Returns
    [{id, name, type, modified, link, account}].
    """
    if not query or not query.strip():
        return {"error": "empty_query"}
    max_results = int(max(1, min(limit, 25)))
    # Drop single quotes so they can't break the Drive query string.
    q = query.strip().replace("'", " ")
    drive_q = f"(name contains '{q}' or fullText contains '{q}') and trashed = false"

    if account:
        accounts = [account]
    else:
        accounts = []
        if DEFAULT_ACCOUNT:
            accounts.append(DEFAULT_ACCOUNT)
        accounts.extend(a for a in sorted(ALLOWED_ACCOUNTS) if a not in accounts)
    if not accounts:
        return {"error": "no_configured_accounts"}
    for acct in accounts:
        if (err := _check_account(acct)) is not None:
            return err

    async def _search_account(acct: str) -> list[dict] | dict:
        params = {
            "q": drive_q,
            "pageSize": max_results,
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
            "orderBy": "modifiedTime desc",
        }
        raw = await _run_gws(acct, ["drive", "files", "list",
                                    "--params", json.dumps(params)])
        if raw is None:
            return {"error": "gws_unavailable", "account": acct}
        files = raw.get("files") if isinstance(raw, dict) else None
        if not isinstance(files, list):
            return []
        rows: list[dict] = []
        for f in files:
            mime = f.get("mimeType", "")
            rows.append({
                "id": f.get("id"),
                "name": f.get("name"),
                "type": _DRIVE_TYPE.get(mime, mime.rsplit(".", 1)[-1] if "." in mime else mime),
                "modified": f.get("modifiedTime"),
                "link": f.get("webViewLink"),
                "account": acct,
                "_mt": f.get("modifiedTime") or "",
            })
        return rows

    searched = await asyncio.gather(*[_search_account(a) for a in accounts])
    errors = [r for r in searched if isinstance(r, dict) and r.get("error")]
    rows: list[dict] = []
    for result in searched:
        if isinstance(result, list):
            rows.extend(result)
    if not rows and errors:
        return {"error": "gws_unavailable", "accounts": [e.get("account") for e in errors]}
    rows.sort(key=lambda x: x.get("_mt") or "", reverse=True)
    out = rows[:max_results]
    for row in out:
        row.pop("_mt", None)
    return out
