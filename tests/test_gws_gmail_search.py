import asyncio
import json
import unittest
from unittest.mock import patch

from backends import gws


class GmailSearchTests(unittest.TestCase):
    def test_without_account_fans_out_and_sorts(self):
        list_results = {
            "personal@example.com": {"messages": [{"id": "p1"}]},
            "crunchy@example.com": {"messages": [{"id": "c1"}, {"id": "c2"}]},
            "flowocity@example.com": {"messages": []},
        }
        get_results = {
            ("personal@example.com", "p1"): {
                "internalDate": "1000",
                "snippet": "personal older",
                "payload": {"headers": [
                    {"name": "From", "value": "Pat <pat@example.com>"},
                    {"name": "Subject", "value": "Older note"},
                    {"name": "Date", "value": "Wed, 3 Jun 2026 08:00:00 -0400"},
                ]},
            },
            ("crunchy@example.com", "c1"): {
                "internalDate": "3000",
                "snippet": "approved registration",
                "payload": {"headers": [
                    {"name": "From", "value": "Filament <filament@calendar.luma-mail.com>"},
                    {"name": "Subject", "value": "Registration approved for CoClaude & CoWork NYC"},
                    {"name": "Date", "value": "Wed, 3 Jun 2026 09:20:00 -0400"},
                ]},
            },
            ("crunchy@example.com", "c2"): {
                "internalDate": "2000",
                "snippet": "pending registration",
                "payload": {"headers": [
                    {"name": "From", "value": "Filament <filament@calendar.luma-mail.com>"},
                    {"name": "Subject", "value": "Registration pending approval for CoClaude & CoWork NYC"},
                    {"name": "Date", "value": "Wed, 3 Jun 2026 08:20:00 -0400"},
                ]},
            },
        }

        async def fake_run(account, args, *, timeout_s=8.0):
            if args[:4] == ["gmail", "users", "messages", "list"]:
                return list_results[account]
            if args[:4] == ["gmail", "users", "messages", "get"]:
                mid = json.loads(args[5])["id"]
                return get_results[(account, mid)]
            raise AssertionError(args)

        with patch.object(gws, "DEFAULT_ACCOUNT", "personal@example.com"), \
             patch.object(gws, "ALLOWED_ACCOUNTS", {"personal@example.com", "crunchy@example.com", "flowocity@example.com"}), \
             patch.object(gws, "_run_gws", fake_run):
            out = asyncio.run(gws.gmail_search("CoClaude", limit=2))

        self.assertEqual([m["account"] for m in out], ["crunchy@example.com", "crunchy@example.com"])
        self.assertEqual(
            [m["subject"] for m in out],
            [
                "Registration approved for CoClaude & CoWork NYC",
                "Registration pending approval for CoClaude & CoWork NYC",
            ],
        )

    def test_with_account_stays_scoped(self):
        seen_accounts = []

        async def fake_run(account, args, *, timeout_s=8.0):
            seen_accounts.append(account)
            return {"messages": []}

        with patch.object(gws, "DEFAULT_ACCOUNT", "personal@example.com"), \
             patch.object(gws, "ALLOWED_ACCOUNTS", {"personal@example.com", "crunchy@example.com"}), \
             patch.object(gws, "_run_gws", fake_run):
            out = asyncio.run(gws.gmail_search("CoClaude", account="crunchy@example.com"))

        self.assertEqual(out, [])
        self.assertEqual(seen_accounts, ["crunchy@example.com"])


if __name__ == "__main__":
    unittest.main()
