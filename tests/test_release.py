from __future__ import annotations

import unittest

from tools.release import findings, unsafe_path


class ReleaseSafetyTests(unittest.TestCase):
    def test_private_and_generated_paths_are_excluded(self):
        for name in ("config.json", "config.local.json", "data/news-agent.json", ".venv/lib/a.py", "a.log", "a.db-wal", ".env.local", "remote-access.json", "../secret", "dist/bundle.zip", "key.pem"):
            self.assertTrue(unsafe_path(name), name)

    def test_source_examples_and_tests_are_allowed(self):
        for name in ("config.example.json", "stock_alert/news_agent.py", "tests/test_remote.py", "docs/USER_GUIDE.md", ".env.example"):
            self.assertFalse(unsafe_path(name), name)

    def test_secret_report_does_not_print_the_secret(self):
        token = b"ghp" + b"_" + b"x" * 32
        result = findings("sample.py", b"first\nkey=" + token)
        self.assertEqual(result, ["sample.py:2: github-token"])
        self.assertNotIn(token.decode(), "\n".join(result))

    def test_real_remote_domain_is_blocked_but_test_domain_is_allowed(self):
        private = b"https://desktop." + b"tail123456.ts.net/"
        self.assertTrue(findings("README.md", private))
        self.assertEqual(findings("tests/test_remote.py", b"https://stock.example.ts.net"), [])


if __name__ == "__main__":
    unittest.main()
