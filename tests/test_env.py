"""Tests for the zero-dependency .env loader."""

import os
import tempfile
import unittest
from pathlib import Path

from moodful_responder.env import load_dotenv


class DotEnvLoader(unittest.TestCase):
    def _write(self, body: str) -> str:
        d = tempfile.mkdtemp()
        p = Path(d) / ".env"
        p.write_text(body, encoding="utf-8")
        return str(p)

    def test_parses_keys_quotes_and_export_prefix(self):
        path = self._write(
            '# a comment\n'
            'BSKY_IDENTIFIER=alice.bsky.social\n'
            'export BSKY_APP_PASSWORD="aaaa-bbbb-cccc-dddd"\n'
            "BSKY_PDS='https://bsky.social'\n"
            "\n"
        )
        for k in ("BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "BSKY_PDS"):
            os.environ.pop(k, None)
        try:
            load_dotenv(path)
            self.assertEqual(os.environ["BSKY_IDENTIFIER"], "alice.bsky.social")
            self.assertEqual(os.environ["BSKY_APP_PASSWORD"], "aaaa-bbbb-cccc-dddd")
            self.assertEqual(os.environ["BSKY_PDS"], "https://bsky.social")
        finally:
            for k in ("BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "BSKY_PDS"):
                os.environ.pop(k, None)

    def test_existing_env_is_not_overridden(self):
        path = self._write("BSKY_IDENTIFIER=from-file\n")
        os.environ["BSKY_IDENTIFIER"] = "from-real-env"
        try:
            load_dotenv(path)
            self.assertEqual(os.environ["BSKY_IDENTIFIER"], "from-real-env")
        finally:
            os.environ.pop("BSKY_IDENTIFIER", None)


if __name__ == "__main__":
    unittest.main()
