"""Google Sheets read/write via the same gws-as.sh wrapper as gws.py.

Direct narrow tools so the voice model can read and edit a spreadsheet in
sub-second calls instead of routing every edit through the slow deep_research
agent path (observed 30–120s per op). Pure async, JSON in → JSON out; reuses
`gws._run_gws` so there's one wrapper-invocation + account-gating path.

Auth: the granted `auth/drive` scope authorizes the Sheets API v4 (values
get/batchUpdate, spreadsheets.get) — no separate `spreadsheets` scope needed.

`write_cells` always goes through values.batchUpdate so one call applies a
single range or many — the model batches multiple cell changes into one write,
avoiding per-edit round-trips and gws-as.sh per-uid lock contention.
"""
from __future__ import annotations

import json
import logging

from backends.gws import (
    ALLOWED_ACCOUNTS,  # noqa: F401  (kept for parity / availability checks)
    DEFAULT_ACCOUNT,
    _check_account,
    _run_gws,
)

log = logging.getLogger("ttyc.backends.sheets")


async def read_cells(*, spreadsheet_id: str, range: str,
                     account: str | None = None) -> dict:
    """Read one A1 range. Returns {range, values:[[...]]} or {error}."""
    if not spreadsheet_id or not spreadsheet_id.strip():
        return {"error": "missing_spreadsheet_id"}
    if not range or not range.strip():
        return {"error": "missing_range"}
    acct = account or DEFAULT_ACCOUNT
    if (err := _check_account(acct)) is not None:
        return err
    params = {"spreadsheetId": spreadsheet_id.strip(), "range": range.strip()}
    raw = await _run_gws(acct, ["sheets", "spreadsheets", "values", "get",
                                "--params", json.dumps(params)])
    if raw is None:
        return {"error": "gws_unavailable"}
    if not isinstance(raw, dict):
        return {"error": "unexpected_response"}
    return {"range": raw.get("range", range), "values": raw.get("values", [])}


async def describe_sheet(*, spreadsheet_id: str,
                         account: str | None = None) -> dict:
    """List the spreadsheet title + tabs (name + dimensions) so the model can
    align before reading/writing. Returns {title, tabs:[{title, rows, cols}]}
    or {error}."""
    if not spreadsheet_id or not spreadsheet_id.strip():
        return {"error": "missing_spreadsheet_id"}
    acct = account or DEFAULT_ACCOUNT
    if (err := _check_account(acct)) is not None:
        return err
    params = {"spreadsheetId": spreadsheet_id.strip(), "includeGridData": False}
    raw = await _run_gws(acct, ["sheets", "spreadsheets", "get",
                                "--params", json.dumps(params)])
    if raw is None:
        return {"error": "gws_unavailable"}
    if not isinstance(raw, dict):
        return {"error": "unexpected_response"}
    tabs = []
    for sh in (raw.get("sheets") or []):
        props = sh.get("properties") or {}
        grid = props.get("gridProperties") or {}
        tabs.append({
            "title": props.get("title", ""),
            "rows": grid.get("rowCount"),
            "cols": grid.get("columnCount"),
        })
    return {"title": (raw.get("properties") or {}).get("title", ""), "tabs": tabs}


async def write_cells(*, spreadsheet_id: str, edits: list,
                      account: str | None = None) -> dict:
    """Write one or more A1 ranges in a single values.batchUpdate.

    `edits` is a list of {range, values:[[...]]}. Values parse with
    USER_ENTERED (formulas/numbers/dates behave like UI entry). A flat
    `values` list is coerced to a single row. Returns
    {ok, updated_cells, updated_ranges} or {error}.
    """
    if not spreadsheet_id or not spreadsheet_id.strip():
        return {"error": "missing_spreadsheet_id"}
    if not isinstance(edits, list) or not edits:
        return {"error": "no_edits"}
    data = []
    for e in edits:
        if not isinstance(e, dict):
            return {"error": "bad_edit", "detail": "each edit must be an object"}
        rng = (e.get("range") or "").strip()
        vals = e.get("values")
        if not rng:
            return {"error": "bad_edit", "detail": "edit missing range"}
        if not isinstance(vals, list):
            return {"error": "bad_edit", "detail": f"values for {rng} must be an array of rows"}
        # Coerce a flat list of cells into a single row for convenience.
        if vals and not isinstance(vals[0], list):
            vals = [vals]
        data.append({"range": rng, "values": vals})
    acct = account or DEFAULT_ACCOUNT
    if (err := _check_account(acct)) is not None:
        return err
    params = {"spreadsheetId": spreadsheet_id.strip()}
    body = {"valueInputOption": "USER_ENTERED", "data": data}
    raw = await _run_gws(acct, ["sheets", "spreadsheets", "values", "batchUpdate",
                                "--params", json.dumps(params),
                                "--json", json.dumps(body)])
    if raw is None:
        return {"error": "gws_unavailable"}
    if not isinstance(raw, dict):
        return {"error": "unexpected_response"}
    return {
        "ok": True,
        "updated_cells": raw.get("totalUpdatedCells"),
        "updated_ranges": [
            r.get("updatedRange") for r in (raw.get("responses") or [])
            if isinstance(r, dict) and r.get("updatedRange")
        ],
    }
