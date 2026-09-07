import importlib.util
import pathlib
import sys
import time
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "src" / "quotaforge.py"
SPEC = importlib.util.spec_from_file_location("quotaforge", MODULE_PATH)
quotaforge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = quotaforge
SPEC.loader.exec_module(quotaforge)


class UrlTests(unittest.TestCase):
    def test_normalizes_exact_github_url(self):
        value = quotaforge.normalize_github_url("https://github.com/Owner/repo")
        self.assertEqual(value.url, "https://github.com/Owner/repo.git")
        self.assertEqual(value.slug, "Owner/repo")

    def test_rejects_credentials_and_non_github_hosts(self):
        bad = [
            "https://example.com/owner/repo",
            "https://user:secret@github.com/owner/repo",
            "git@github.com:owner/repo.git",
            "https://github.com/owner/repo/issues",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(quotaforge.QuotaForgeError):
                quotaforge.normalize_github_url(value)


class WindowTests(unittest.TestCase):
    def test_selects_nearest_eligible_reset(self):
        now = time.time()
        windows = [
            quotaforge.Window("weekly", 40, int(now + 20 * 60), 10080),
            quotaforge.Window("short", 50, int(now + 10 * 60), 300),
        ]
        selected = quotaforge.select_expiring_window(windows, 30, 99, 1)
        self.assertEqual(selected.name, "short")

    def test_ignores_target_and_distant_windows(self):
        now = time.time()
        windows = [
            quotaforge.Window("done", 99, int(now + 10 * 60), 300),
            quotaforge.Window("later", 2, int(now + 60 * 60), 300),
        ]
        self.assertIsNone(quotaforge.select_expiring_window(windows, 30, 99, 1))


class SensitivePathTests(unittest.TestCase):
    def test_blocks_sensitive_paths(self):
        for value in [".env", ".env.local", "keys/private.pem", "auth.json"]:
            with self.subTest(value=value):
                self.assertIsNotNone(quotaforge.SENSITIVE_NAMES.search(value))

    def test_allows_normal_source_paths(self):
        self.assertIsNone(quotaforge.SENSITIVE_NAMES.search("src/config.py"))


if __name__ == "__main__":
    unittest.main()
