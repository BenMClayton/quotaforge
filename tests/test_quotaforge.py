import importlib.util
import pathlib
import sys
import tempfile
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
    def plan(self, windows, now):
        return quotaforge.build_pacing_plan(
            windows,
            minutes_before_short_reset=30,
            target_used=99,
            minimum_remaining=1,
            pacing_headroom=5,
            final_drain_minutes=180,
            minimum_weekly_duration=8640,
            now=now,
        )

    def test_allows_catchup_near_short_reset_below_weekly_pace(self):
        now = 2_000_000_000
        windows = [
            quotaforge.Window("codex:secondary", 5, int(now + 6 * 86400), 10080),
            quotaforge.Window("codex:primary", 50, int(now + 10 * 60), 300),
        ]
        plan = self.plan(windows, now)
        self.assertTrue(plan.eligible)
        self.assertAlmostEqual(plan.weekly_progress_percent, 100 / 7, places=1)
        self.assertAlmostEqual(plan.weekly_cap_percent, 99 / 7 - 5, places=1)

    def test_blocks_automation_when_weekly_usage_is_ahead_of_pace(self):
        now = 2_000_000_000
        windows = [
            quotaforge.Window("codex:secondary", 12, int(now + 6 * 86400), 10080),
            quotaforge.Window("codex:primary", 2, int(now + 10 * 60), 300),
        ]
        plan = self.plan(windows, now)
        self.assertFalse(plan.budget_available)
        self.assertFalse(plan.eligible)

    def test_final_weekly_drain_does_not_require_short_reset(self):
        now = 2_000_000_000
        windows = [
            quotaforge.Window("codex:secondary", 90, int(now + 60 * 60), 10080),
            quotaforge.Window("codex:primary", 2, int(now + 240 * 60), 300),
        ]
        plan = self.plan(windows, now)
        self.assertTrue(plan.final_drain)
        self.assertTrue(plan.eligible)
        self.assertEqual(plan.weekly_cap_percent, 99)

    def test_fails_closed_without_weekly_window(self):
        now = time.time()
        with self.assertRaises(quotaforge.QuotaForgeError):
            self.plan([quotaforge.Window("codex:primary", 2, int(now + 600), 300)], now)


class SensitivePathTests(unittest.TestCase):
    def test_blocks_sensitive_paths(self):
        for value in [".env", ".env.local", "keys/private.pem", "auth.json"]:
            with self.subTest(value=value):
                self.assertIsNotNone(quotaforge.SENSITIVE_NAMES.search(value))

    def test_allows_normal_source_paths(self):
        self.assertIsNone(quotaforge.SENSITIVE_NAMES.search("src/config.py"))


@unittest.skipUnless(sys.platform == "win32", "Windows file locking test")
class LockTests(unittest.TestCase):
    def test_second_cycle_lock_is_reported_as_expected_contention(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = pathlib.Path(temporary) / "quotaforge.lock"
            first = quotaforge.acquire_lock(lock_path)
            try:
                with self.assertRaises(quotaforge.CycleAlreadyRunning):
                    quotaforge.acquire_lock(lock_path)
            finally:
                first.close()


if __name__ == "__main__":
    unittest.main()
