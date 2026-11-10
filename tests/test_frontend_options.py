"""--ui option handling: one-line hints for missing extras, chat-only options, Web address/token checks.

These need no optional extra: the checks run before any frontend is imported.
"""

import contextlib
import io
import unittest
from unittest.mock import patch

from baseagent.main import main
from baseagent.web.config import is_loopback, web_settings
from chat_helpers import ChatTestCase
from test_main_agent import Duplex


class FrontendOptionTests(ChatTestCase):
    def run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(["--root", str(self.root), "--db", str(self.db), *argv],
                            model_factory=lambda args: Duplex())
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_extra_prints_one_line_and_exits_nonzero(self):
        real = __import__("importlib.util").util.find_spec
        for ui, module, extra in (("tui", "textual", "tui"), ("web", "fastapi", "web")):
            def absent(name, *args, missing=module):
                return None if name == missing else real(name, *args)

            with self.subTest(ui=ui), patch("importlib.util.find_spec", side_effect=absent):
                code, stdout, stderr = self.run_main(["--ui", ui])
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertEqual(len(stderr.strip().splitlines()), 1, stderr)
            self.assertIn(f"uv sync --extra {extra}", stderr)
            self.assertIn(f"baseagent[{extra}]", stderr)
            self.assertNotIn("Traceback", stderr)
        self.assertIsNone(self.store().load("demo"))

    def test_frontend_options_apply_to_interactive_chat_only(self):
        for argv in (["task", "--ui", "tui"], ["--ui", "web", "--list-sessions"], ["--host", "127.0.0.1"],
                     ["--ui", "tui", "--port", "9000"], ["--ui", "cli", "--token", "abcdefgh"]):
            with self.subTest(argv=argv):
                code, _, stderr = self.run_main(argv)
                self.assertEqual(code, 2)
                self.assertIn("error:", stderr)

    def test_web_refuses_a_public_address_without_a_token(self):
        code, _, stderr = self.run_main(["--ui", "web", "--host", "0.0.0.0"])
        self.assertEqual(code, 2)
        self.assertIn("refusing to bind 0.0.0.0 without --token", stderr)
        self.assertEqual(len(stderr.strip().splitlines()), 1)
        with self.assertRaises(ValueError):
            web_settings("192.168.1.5", 8765, None)
        with self.assertRaises(ValueError):
            web_settings("127.0.0.1", 70000, None)
        self.assertEqual(web_settings(None, None, None), {"host": "127.0.0.1", "port": 8765, "token": None})
        self.assertEqual(web_settings("0.0.0.0", 9000, "long-enough-token")["token"], "long-enough-token")
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"):
            self.assertTrue(is_loopback(host), host)
        for host in ("0.0.0.0", "example.com", "10.0.0.1", ""):
            self.assertFalse(is_loopback(host), host)


if __name__ == "__main__":
    unittest.main()
