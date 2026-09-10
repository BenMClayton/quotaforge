import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


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
            bootstrap_allowance=3,
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

    def test_bootstrap_allows_first_expiring_window_to_anchor_week(self):
        now = 2_000_000_000
        windows = [
            quotaforge.Window("codex:secondary", 0, int(now + 10080 * 60), 10080),
            quotaforge.Window("codex:primary", 0, int(now + 10 * 60), 300),
        ]
        plan = self.plan(windows, now)
        self.assertEqual(plan.weekly_progress_percent, 0)
        self.assertEqual(plan.weekly_cap_percent, 3)
        self.assertTrue(plan.budget_available)
        self.assertTrue(plan.eligible)

    def test_bootstrap_still_waits_until_short_window_is_expiring(self):
        now = 2_000_000_000
        windows = [
            quotaforge.Window("codex:secondary", 0, int(now + 10080 * 60), 10080),
            quotaforge.Window("codex:primary", 0, int(now + 60 * 60), 300),
        ]
        plan = self.plan(windows, now)
        self.assertTrue(plan.budget_available)
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


class BackgroundProcessTests(unittest.TestCase):
    def test_child_processes_use_no_console_flag_on_windows(self):
        expected = (
            getattr(quotaforge.subprocess, "CREATE_NO_WINDOW", 0)
            if quotaforge.os.name == "nt"
            else 0
        )
        self.assertEqual(quotaforge.hidden_process_flags(), expected)

    def test_bundled_codex_selects_newest_desktop_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime_root = pathlib.Path(temporary) / "OpenAI" / "Codex" / "bin"
            older = runtime_root / "old" / "codex.exe"
            newer = runtime_root / "new" / "codex.exe"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            time.sleep(0.01)
            newer.touch()
            self.assertEqual(
                quotaforge.bundled_codex_path(pathlib.Path(temporary)), str(newer)
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows Codex resolver test")
    def test_command_path_prefers_bundled_codex_over_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundled = (
                pathlib.Path(temporary)
                / "OpenAI"
                / "Codex"
                / "bin"
                / "current"
                / "codex.exe"
            )
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with mock.patch.dict(quotaforge.os.environ, {"LOCALAPPDATA": temporary}):
                with mock.patch.object(quotaforge.shutil, "which", return_value="old-codex.cmd"):
                    self.assertEqual(quotaforge.command_path("codex"), str(bundled))


class ContinuationTests(unittest.TestCase):
    @staticmethod
    def git(repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_extracts_session_id_from_json_events(self):
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        with tempfile.TemporaryDirectory() as temporary:
            events = pathlib.Path(temporary) / "events.jsonl"
            events.write_text(
                '{"type":"turn.started"}\n'
                f'{{"type":"thread.started","thread_id":"{session_id}"}}\n',
                encoding="utf-8",
            )
            self.assertEqual(quotaforge.session_id_from_events(events), session_id)

    def test_initial_exec_is_persistent_and_resume_targets_saved_session(self):
        repo = pathlib.Path("repo")
        schema = pathlib.Path("schema.json")
        output = pathlib.Path("output.json")
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        with mock.patch.object(quotaforge, "command_path", return_value="codex.exe"):
            initial = quotaforge.codex_exec_args(repo, {}, "run-1", schema, output, None)
            resumed = quotaforge.codex_exec_args(
                repo, {}, "run-1", schema, output, session_id
            )
        self.assertNotIn("--ephemeral", initial)
        self.assertEqual(initial[:2], ["codex.exe", "exec"])
        self.assertEqual(resumed[:3], ["codex.exe", "exec", "resume"])
        self.assertIn(session_id, resumed)

    def test_paused_run_preserves_work_branch_and_session(self):
        repo = pathlib.Path("managed-repo")
        spec = quotaforge.RepoSpec(
            "https://github.com/Owner/repo.git", "Owner", "repo", None
        )
        logger = mock.Mock()
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        revisions = iter(["base-sha", "preflight-sha"])

        def fake_git(_repo, *args, **_kwargs):
            if args == ("branch", "--show-current"):
                return "master"
            if args == ("rev-parse", "HEAD"):
                return next(revisions)
            return ""

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            with mock.patch.object(quotaforge, "managed_checkout", return_value=repo):
                with mock.patch.object(quotaforge, "git", side_effect=fake_git) as git_mock:
                    with mock.patch.object(
                        quotaforge,
                        "run_codex_improvement",
                        side_effect=quotaforge.CyclePaused("Cycle deadline reached.", session_id),
                    ):
                        with self.assertRaises(quotaforge.CyclePaused):
                            quotaforge.improve_once(
                                data_dir,
                                spec,
                                {},
                                logger,
                                0,
                                time.monotonic() + 10,
                                False,
                            )
            continuation = quotaforge.load_continuation(data_dir, spec)

        self.assertIsNotNone(continuation)
        self.assertEqual(continuation["sessionId"], session_id)
        self.assertTrue(continuation["workBranch"].startswith("quotaforge/wip/"))
        self.assertNotIn(mock.call(repo, "reset", "--hard", "base-sha"), git_mock.mock_calls)

    def test_next_attempt_resumes_saved_run_and_session(self):
        repo = pathlib.Path("managed-repo")
        spec = quotaforge.RepoSpec(
            "https://github.com/Owner/repo.git", "Owner", "repo", None
        )
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        continuation = {
            "version": 1,
            "phase": "working",
            "repo": spec.slug,
            "runId": "run-1",
            "targetBranch": "master",
            "workBranch": "quotaforge/wip/Owner-repo-run-1",
            "baseCommit": "base-sha",
            "preflightCommit": "preflight-sha",
            "sessionId": session_id,
            "startedAt": "2026-09-10T07:00:00+00:00",
        }
        logger = mock.Mock()
        deadline = time.monotonic() + 10

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            quotaforge.save_continuation(data_dir, spec, continuation)
            with mock.patch.object(
                quotaforge, "managed_checkout", return_value=repo
            ) as checkout_mock:
                with mock.patch.object(quotaforge, "git") as git_mock:
                    with mock.patch.object(
                        quotaforge,
                        "run_codex_improvement",
                        side_effect=quotaforge.CyclePaused(
                            "Cycle deadline reached.", session_id
                        ),
                    ) as run_mock:
                        with self.assertRaises(quotaforge.CyclePaused):
                            quotaforge.improve_once(
                                data_dir, spec, {}, logger, 20, deadline, False
                            )

            saved = quotaforge.load_continuation(data_dir, spec)

        checkout_mock.assert_called_once()
        self.assertEqual(checkout_mock.call_args.args[:3], (data_dir, spec, logger))
        self.assertEqual(checkout_mock.call_args.args[3]["runId"], "run-1")
        self.assertEqual(checkout_mock.call_args.args[3]["sessionId"], session_id)
        run_mock.assert_called_once()
        self.assertEqual(
            run_mock.call_args.args[:6],
            (repo, {}, "run-1", 20, deadline, session_id),
        )
        self.assertTrue(callable(run_mock.call_args.args[6]))
        git_mock.assert_not_called()
        self.assertEqual(saved["runId"], "run-1")
        self.assertEqual(saved["sessionId"], session_id)

    def test_real_git_work_survives_pause_then_publishes_on_resume(self):
        spec = quotaforge.RepoSpec(
            "https://github.com/Owner/repo.git", "Owner", "repo", None
        )
        session_id = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        logger = mock.Mock()

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            data_dir = root / "data"
            repo = data_dir / "repos" / "Owner" / "repo"
            remote = root / "remote.git"
            repo.mkdir(parents=True)
            self.git(root, "init", "--bare", "--initial-branch=master", str(remote))
            self.git(repo, "init", "--initial-branch=master")
            self.git(repo, "config", "user.name", "QuotaForge Tests")
            self.git(repo, "config", "user.email", "quotaforge-tests@example.invalid")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "README.md")
            self.git(repo, "commit", "-m", "base")
            self.git(repo, "remote", "add", "origin", str(remote))
            self.git(repo, "push", "-u", "origin", "master")

            def pause_with_work(*args):
                (repo / "improvement.txt").write_text("preserved\n", encoding="utf-8")
                args[6](session_id)
                raise quotaforge.CyclePaused("Cycle deadline reached.", session_id)

            with mock.patch.object(quotaforge, "normalize_github_url", return_value=spec):
                with mock.patch.object(
                    quotaforge, "run_codex_improvement", side_effect=pause_with_work
                ):
                    with self.assertRaises(quotaforge.CyclePaused):
                        quotaforge.improve_once(
                            data_dir,
                            spec,
                            {"behavior": {"push": True}},
                            logger,
                            0,
                            time.monotonic() + 10,
                            False,
                        )

            continuation = quotaforge.load_continuation(data_dir, spec)
            self.assertIsNotNone(continuation)
            self.assertEqual(self.git(repo, "branch", "--show-current"), continuation["workBranch"])
            self.assertEqual((repo / "improvement.txt").read_text(encoding="utf-8"), "preserved\n")

            result = {
                "title": "preserve paused work",
                "summary": "Verified resumable work.",
                "tests": "temporary Git lifecycle",
                "noChange": False,
            }
            with mock.patch.object(quotaforge, "normalize_github_url", return_value=spec):
                with mock.patch.object(
                    quotaforge,
                    "run_codex_improvement",
                    return_value=(result, session_id),
                ):
                    published = quotaforge.improve_once(
                        data_dir,
                        spec,
                        {"behavior": {"push": True}},
                        logger,
                        0,
                        time.monotonic() + 10,
                        False,
                    )

            self.assertEqual(self.git(repo, "branch", "--show-current"), "master")
            self.assertEqual(self.git(repo, "rev-list", "--count", "HEAD"), "3")
            self.assertEqual(
                self.git(repo, "rev-parse", "HEAD"),
                self.git(remote, "rev-parse", "refs/heads/master"),
            )
            self.assertEqual((repo / "improvement.txt").read_text(encoding="utf-8"), "preserved\n")
            self.assertIsNone(quotaforge.load_continuation(data_dir, spec))
            self.assertEqual(published["title"], "preserve paused work")


class FailedAttemptTests(unittest.TestCase):
    def test_failed_codex_run_removes_local_preflight_and_changes(self):
        repo = pathlib.Path("managed-repo")
        spec = quotaforge.RepoSpec(
            "https://github.com/Owner/repo.git", "Owner", "repo", None
        )
        logger = mock.Mock()

        revisions = iter(["base-sha", "preflight-sha"])

        def fake_git(_repo, *args, **_kwargs):
            if args == ("branch", "--show-current"):
                return "master"
            if args == ("rev-parse", "HEAD"):
                return next(revisions)
            return ""

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = pathlib.Path(temporary)
            with mock.patch.object(quotaforge, "managed_checkout", return_value=repo):
                with mock.patch.object(quotaforge, "git", side_effect=fake_git) as git_mock:
                    with mock.patch.object(
                        quotaforge,
                        "run_codex_improvement",
                        side_effect=quotaforge.QuotaForgeError("incompatible runtime"),
                    ):
                        with self.assertRaises(quotaforge.QuotaForgeError):
                            quotaforge.improve_once(
                                data_dir,
                                spec,
                                {},
                                logger,
                                0,
                                time.monotonic() + 10,
                                False,
                            )
            self.assertIsNone(quotaforge.load_continuation(data_dir, spec))

        git_mock.assert_any_call(repo, "reset", "--hard", "base-sha")
        git_mock.assert_any_call(repo, "clean", "-fd")


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
