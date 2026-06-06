import asyncio
import unittest
from unittest.mock import patch

from backends import gws


class CalendarTests(unittest.TestCase):
    def test_includes_location_description_and_link(self):
        async def fake_run(account, args, *, timeout_s=8.0):
            return {"items": [{
                "summary": "CoClaude & CoWork NYC",
                "location": "Betaworks, 29 Little W 12th St, New York, NY 10014, USA",
                "description": "Address:\nBetaworks\nNew York, NY\n\nBuild with Claude.",
                "htmlLink": "https://calendar.google.com/event?eid=abc",
                "start": {"dateTime": "2026-06-03T09:30:00-04:00"},
                "end": {"dateTime": "2026-06-03T11:30:00-04:00"},
            }]}

        with patch.object(gws, "DEFAULT_ACCOUNT", "personal@example.com"), \
             patch.object(gws, "ALLOWED_ACCOUNTS", {"personal@example.com"}), \
             patch.object(gws, "_run_gws", fake_run):
            out = asyncio.run(gws.calendar("today"))

        self.assertEqual(out[0]["location"], "Betaworks, 29 Little W 12th St, New York, NY 10014, USA")
        self.assertIn("Build with Claude", out[0]["description"])
        self.assertEqual(out[0]["link"], "https://calendar.google.com/event?eid=abc")


if __name__ == "__main__":
    unittest.main()
