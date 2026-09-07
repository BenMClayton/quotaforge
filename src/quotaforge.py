#!/usr/bin/env python3
"""QuotaForge: spend expiring Codex capacity on allowlisted GitHub repositories."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


APP_NAME = "QuotaForge"
VERSION = "0.1.0"
DEFAULT_DATA = pathlib.Path.home() / ".quotaforge"
DEFAULT_CONFIG = DEFAULT_DATA / "config.json"
GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SENSITIVE_NAMES = re.compile(
    r"(^|/)(\.env($|\.)|credentials?($|\.)|secrets?($|\.)|auth\.json$|id_rsa$|id_ed25519$)|\.(pem|pfx|p12|key)$",
    re.IGNORECASE,
)


class QuotaForgeError(RuntimeError):
    pass


class CycleAlreadyRunning(QuotaForgeError):
    pass


@dataclass(frozen=True)
class RepoSpec:
    url: str
    owner: str
    repo: str
    branch: str | None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class Window:
    name: str
    used_percent: float
    resets_at: int
    duration_minutes: int

    def minutes_remaining(self, now: float | None = None) -> float:
        return (self.resets_at - (time.time() if now is None else now)) / 60


@dataclass(frozen=True)
class PacingPlan:
    weekly: Window
    short: Window | None
    weekly_progress_percent: float
    weekly_cap_percent: float
    final_drain: bool
    budget_available: bool
    eligible: bool
    reason: str


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def load_config(path: pathlib.Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise QuotaForgeError(f"Config not found: {path}. Run install.ps1 first.") from exc
    except json.JSONDecodeError as exc:
        raise QuotaForgeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise QuotaForgeError("Config root must be an object.")
    return config


def normalize_github_url(raw: str) -> RepoSpec:
    parsed = urllib.parse.urlsplit(raw.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise QuotaForgeError(f"Only exact https://github.com URLs are allowed: {raw}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port:
        raise QuotaForgeError(f"Credentials, ports, queries, and fragments are forbidden: {raw}")
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not GITHUB_RE.fullmatch(path):
        raise QuotaForgeError(f"Expected https://github.com/OWNER/REPO[.git]: {raw}")
    owner, repo = path.split("/", 1)
    canonical = f"https://github.com/{owner}/{repo}.git"
    return RepoSpec(canonical, owner, repo, None)


def enabled_repos(config: dict[str, Any]) -> list[RepoSpec]:
    results: list[RepoSpec] = []
    for entry in config.get("whitelist", []):
        if not isinstance(entry, dict) or not entry.get("enabled", False):
            continue
        spec = normalize_github_url(str(entry.get("url", "")))
        branch = entry.get("branch")
        if branch is not None and not re.fullmatch(r"[A-Za-z0-9._/-]+", str(branch)):
            raise QuotaForgeError(f"Unsafe branch name for {spec.slug}: {branch}")
        results.append(RepoSpec(spec.url, spec.owner, spec.repo, str(branch) if branch else None))
    return results


def command_path(name: str) -> str:
    candidates = [f"{name}.exe", f"{name}.cmd", name] if os.name == "nt" else [name]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    if os.name == "nt" and name == "codex":
        local_app_data = pathlib.Path(os.environ.get("LOCALAPPDATA", ""))
        native_root = local_app_data / "OpenAI" / "Codex" / "bin"
        native_candidates = sorted(
            native_root.glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if native_candidates:
            return str(native_candidates[0])
        app_data = pathlib.Path(os.environ.get("APPDATA", ""))
        npm_launcher = app_data / "npm" / "codex.cmd"
        if npm_launcher.is_file():
            return str(npm_launcher)
    raise QuotaForgeError(f"Required command not found: {name}")


def write_fatal_log(config_path: pathlib.Path, exc: BaseException) -> None:
    """Best-effort diagnostics for background launches that have no console."""
    try:
        log_path = config_path.parent / "logs" / "fatal.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time": utc_now(),
            "event": "fatal",
            "error": str(exc),
            "type": type(exc).__name__,
            "python": sys.executable,
            "traceback": traceback.format_exc(limit=8),
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def hidden_process_flags() -> int:
    """Prevent every child process from allocating a visible Windows console."""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def run(
    args: list[str], cwd: pathlib.Path | None = None, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        shell=False,
        creationflags=hidden_process_flags(),
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise QuotaForgeError(f"Command failed ({result.returncode}): {args[0]}\n{detail}")
    return result


def codex_auth_mode() -> str:
    result = run([command_path("codex"), "login", "status"], check=False, timeout=30)
    combined = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode != 0:
        raise QuotaForgeError("Codex is not authenticated. Run 'codex login'.")
    if "ChatGPT" not in combined:
        raise QuotaForgeError(
            "QuotaForge requires a ChatGPT Codex login and refuses API-key billing. "
            f"Current status: {combined}"
        )
    return combined


def app_server_request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    codex = command_path("codex")
    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        creationflags=hidden_process_flags(),
    )
    assert proc.stdin and proc.stdout
    messages = [
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "quotaforge", "title": APP_NAME, "version": VERSION}
            },
        },
        {"method": "initialized", "params": {}},
        {"method": method, "id": 2, "params": params or {}},
    ]
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == 2:
                if "error" in payload:
                    raise QuotaForgeError(f"Codex app-server error: {payload['error']}")
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise QuotaForgeError("Codex app-server returned an invalid result.")
                return result
        stderr = proc.stderr.read()[-2000:] if proc.stderr else ""
        raise QuotaForgeError(f"Timed out reading Codex app-server. {stderr}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def read_windows() -> tuple[dict[str, Any], list[Window]]:
    payload = app_server_request("account/rateLimits/read")
    raw_limits = payload.get("rateLimitsByLimitId") or {}
    if not raw_limits and payload.get("rateLimits"):
        raw_limits = {"codex": payload["rateLimits"]}
    windows: list[Window] = []
    for limit_id, limit in raw_limits.items():
        if not isinstance(limit, dict):
            continue
        for bucket in ("primary", "secondary"):
            value = limit.get(bucket)
            if not isinstance(value, dict):
                continue
            if value.get("usedPercent") is None or value.get("resetsAt") is None:
                continue
            windows.append(
                Window(
                    f"{limit_id}:{bucket}",
                    float(value["usedPercent"]),
                    int(value["resetsAt"]),
                    int(value.get("windowDurationMins") or 0),
                )
            )
    if not windows:
        raise QuotaForgeError("Codex returned no readable ChatGPT quota windows; failing closed.")
    return payload, windows


def idle_minutes() -> float:
    if os.name != "nt":
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    elapsed_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 60000


def build_pacing_plan(
    windows: list[Window],
    *,
    minutes_before_short_reset: float,
    target_used: float,
    minimum_remaining: float,
    pacing_headroom: float,
    final_drain_minutes: float,
    minimum_weekly_duration: int,
    now: float | None = None,
) -> PacingPlan:
    """Limit autonomous use to a gradually increasing weekly budget."""
    current_time = time.time() if now is None else now
    weekly_candidates = [
        window for window in windows if window.duration_minutes >= minimum_weekly_duration
    ]
    if not weekly_candidates:
        raise QuotaForgeError("No weekly Codex quota window was found; failing closed.")
    weekly = max(weekly_candidates, key=lambda item: item.duration_minutes)
    weekly_remaining = weekly.minutes_remaining(current_time)
    if weekly_remaining <= 0:
        raise QuotaForgeError("The weekly quota window is stale; waiting for refreshed usage data.")

    elapsed_minutes = max(0.0, weekly.duration_minutes - weekly_remaining)
    progress = min(1.0, elapsed_minutes / weekly.duration_minutes)
    progress_percent = progress * 100
    final_drain = weekly_remaining <= final_drain_minutes
    if final_drain:
        cap = target_used
    else:
        cap = max(0.0, min(target_used, target_used * progress - pacing_headroom))

    limit_id = weekly.name.split(":", 1)[0]
    short_candidates = [
        window
        for window in windows
        if window.name.split(":", 1)[0] == limit_id
        and window.duration_minutes < weekly.duration_minutes
        and 0 < window.minutes_remaining(current_time) <= minutes_before_short_reset
    ]
    short = min(short_candidates, key=lambda item: item.resets_at) if short_candidates else None
    budget_available = (
        weekly.used_percent < cap and (100 - weekly.used_percent) >= minimum_remaining
    )
    if not budget_available:
        reason = (
            f"Weekly usage {weekly.used_percent:.1f}% is at or ahead of the "
            f"current {cap:.1f}% pacing cap."
        )
    elif not final_drain and short is None:
        reason = "Weekly budget is available, but no short window is close to reset."
    elif final_drain:
        reason = "Final weekly drain window is active and paced budget remains."
    else:
        reason = "Short window is close to reset and paced weekly budget remains."
    return PacingPlan(
        weekly=weekly,
        short=short,
        weekly_progress_percent=progress_percent,
        weekly_cap_percent=cap,
        final_drain=final_drain,
        budget_available=budget_available,
        eligible=budget_available and (final_drain or short is not None),
        reason=reason,
    )


class JsonLogger:
    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        record = {"time": utc_now(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(record, ensure_ascii=False))


def notify(config: dict[str, Any], title: str, message: str, logger: JsonLogger) -> None:
    notifications = config.get("notifications", {})
    if notifications.get("windowsBalloon", True) and os.name == "nt":
        safe_title = title.replace("'", "''")[:63]
        safe_message = message.replace("'", "''")[:255]
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            f"$n.BalloonTipTitle='{safe_title}';$n.BalloonTipText='{safe_message}';"
            "$n.Visible=$true;$n.ShowBalloonTip(8000);Start-Sleep -Seconds 9;$n.Dispose()"
        )
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=hidden_process_flags(),
        )
    webhook = os.environ.get("QUOTAFORGE_WEBHOOK_URL") or notifications.get("webhookUrl")
    if webhook:
        try:
            data = json.dumps({"title": title, "message": message, "source": APP_NAME}).encode()
            request = urllib.request.Request(
                webhook, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=10):
                pass
        except Exception as exc:  # Notification failure must not corrupt a completed run.
            logger.write("notification_failed", error=str(exc))


def git(repo: pathlib.Path, *args: str, check: bool = True, timeout: int = 120) -> str:
    return run([command_path("git"), *args], cwd=repo, check=check, timeout=timeout).stdout.strip()


def managed_checkout(data_dir: pathlib.Path, spec: RepoSpec, logger: JsonLogger) -> pathlib.Path:
    root = (data_dir / "repos").resolve()
    repo = (root / spec.owner / spec.repo).resolve()
    if root not in repo.parents:
        raise QuotaForgeError(f"Unsafe checkout path: {repo}")
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        run([command_path("git"), "clone", "--", spec.url, str(repo)], timeout=300)
        logger.write("cloned", repo=spec.slug, path=str(repo))
    if not (repo / ".git").exists():
        raise QuotaForgeError(f"Managed path is not a Git checkout: {repo}")

    remote = normalize_github_url(git(repo, "remote", "get-url", "origin"))
    if remote.url.lower() != spec.url.lower():
        raise QuotaForgeError(f"Remote changed for {spec.slug}; expected {spec.url}, found {remote.url}")
    if git(repo, "status", "--porcelain"):
        raise QuotaForgeError(f"Managed checkout is dirty; refusing to proceed: {repo}")
    git(repo, "fetch", "--prune", "origin", timeout=300)
    if spec.branch:
        git(repo, "switch", spec.branch)
    git(repo, "pull", "--ff-only", timeout=300)
    return repo


def write_schema(path: pathlib.Path) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "tests": {"type": "string"},
            "noChange": {"type": "boolean"},
        },
        "required": ["title", "summary", "tests", "noChange"],
        "additionalProperties": False,
    }
    path.write_text(json.dumps(schema), encoding="utf-8")


def improvement_prompt(run_id: str) -> str:
    return f"""You are running as QuotaForge autonomous maintenance cycle {run_id}.
Inspect this repository and implement exactly one small, high-value improvement that is safe to
ship without product-owner input. Prefer a concrete bug fix, missing regression test, reliability
improvement, developer-experience improvement, or precise documentation correction supported by
the repository. Read and obey repository instructions. Keep the change tightly scoped. Run the
most relevant available checks. Do not commit, push, create branches, modify Git configuration,
read files outside this repository, add secrets, or change credential/key files. Do not make a
change merely to consume capacity; if no justified safe improvement exists, leave the tree clean.
Return the required JSON with a short title, useful summary, tests run/results, and noChange."""


def terminate_process(proc: subprocess.Popen[str]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_codex_improvement(
    repo: pathlib.Path,
    config: dict[str, Any],
    run_id: str,
    minimum_idle: float,
    hard_deadline: float,
) -> dict[str, Any]:
    behavior = config.get("behavior", {})
    with tempfile.TemporaryDirectory(prefix="quotaforge-") as temp:
        temp_path = pathlib.Path(temp)
        schema = temp_path / "result.schema.json"
        output = temp_path / "result.json"
        write_schema(schema)
        args = [
            command_path("codex"),
            "exec",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "--color",
            "never",
            "-C",
            str(repo),
            "-c",
            'approval_policy="never"',
            "-c",
            f'model_reasoning_effort="{behavior.get("reasoningEffort", "medium")}"',
        ]
        if behavior.get("model"):
            args += ["--model", str(behavior["model"])]
        args.append(improvement_prompt(run_id))
        proc = subprocess.Popen(
            args,
            cwd=str(repo),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=hidden_process_flags(),
        )
        while proc.poll() is None:
            if time.monotonic() >= hard_deadline:
                terminate_process(proc)
                raise QuotaForgeError("Cycle deadline reached; stopped Codex.")
            if idle_minutes() < minimum_idle:
                terminate_process(proc)
                raise QuotaForgeError("User activity detected; stopped Codex to protect interactivity.")
            time.sleep(2)
        stdout, stderr = proc.communicate()
        if proc.returncode:
            raise QuotaForgeError(f"Codex run failed ({proc.returncode}): {(stderr or stdout)[-2000:]}")
        try:
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaForgeError(f"Codex returned no valid structured result: {exc}") from exc
        return result


def changed_paths(repo: pathlib.Path) -> list[str]:
    output = git(repo, "status", "--porcelain=v1", "-z")
    if not output:
        return []
    paths: list[str] = []
    for record in output.split("\0"):
        if len(record) >= 4:
            paths.append(record[3:].replace("\\", "/"))
    return paths


def rollback_managed_changes(repo: pathlib.Path) -> None:
    git(repo, "reset", "--hard", "HEAD")
    git(repo, "clean", "-fd")


def improve_once(
    data_dir: pathlib.Path,
    spec: RepoSpec,
    config: dict[str, Any],
    logger: JsonLogger,
    minimum_idle: float,
    hard_deadline: float,
    dry_run: bool,
) -> dict[str, Any]:
    repo = managed_checkout(data_dir, spec, logger)
    branch = git(repo, "branch", "--show-current")
    if not branch:
        raise QuotaForgeError(f"Detached HEAD is not supported for {spec.slug}.")
    if dry_run:
        return {"repo": spec.slug, "dryRun": True, "branch": branch}

    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    preflight_message = f"chore(quotaforge): preflight {run_id}"
    git(repo, "commit", "--allow-empty", "-m", preflight_message)
    if config.get("behavior", {}).get("push", True):
        git(repo, "push", "origin", f"HEAD:{branch}", timeout=300)
    logger.write("preflight_committed", repo=spec.slug, runId=run_id)

    try:
        result = run_codex_improvement(repo, config, run_id, minimum_idle, hard_deadline)
    except Exception:
        if changed_paths(repo):
            rollback_managed_changes(repo)
        raise

    paths = changed_paths(repo)
    if result.get("noChange") or not paths:
        if paths:
            rollback_managed_changes(repo)
        logger.write("no_change", repo=spec.slug, runId=run_id, summary=result.get("summary", ""))
        return {"repo": spec.slug, "runId": run_id, "noChange": True, **result}
    unsafe = [path for path in paths if SENSITIVE_NAMES.search(path)]
    if unsafe:
        rollback_managed_changes(repo)
        raise QuotaForgeError(f"Blocked suspicious sensitive paths: {', '.join(unsafe)}")

    git(repo, "add", "--all")
    title = str(result.get("title") or "autonomous improvement").strip().splitlines()[0][:72]
    git(repo, "commit", "-m", f"fix(quotaforge): {title}")
    commit = git(repo, "rev-parse", "--short", "HEAD")
    if config.get("behavior", {}).get("push", True):
        git(repo, "push", "origin", f"HEAD:{branch}", timeout=300)
    logger.write(
        "improvement_committed",
        repo=spec.slug,
        runId=run_id,
        commit=commit,
        title=title,
        paths=paths,
        tests=result.get("tests", ""),
    )
    return {"repo": spec.slug, "runId": run_id, "commit": commit, "paths": paths, **result}


def load_state(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: pathlib.Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def acquire_lock(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise CycleAlreadyRunning("Another QuotaForge cycle is already running.") from exc
    return handle


def status(config_path: pathlib.Path, config: dict[str, Any]) -> int:
    auth = codex_auth_mode()
    _, windows = read_windows()
    print(f"Config: {config_path}")
    print(f"Auth: {auth}")
    print(f"Idle: {idle_minutes():.1f} minutes")
    print(f"Enabled repositories: {len(enabled_repos(config))}")
    for window in sorted(windows, key=lambda item: item.resets_at):
        reset = dt.datetime.fromtimestamp(window.resets_at, dt.timezone.utc).astimezone()
        print(
            f"{window.name}: {window.used_percent:.0f}% used, "
            f"resets {reset.isoformat(timespec='seconds')} ({window.minutes_remaining():.1f} min)"
        )
    trigger = config.get("trigger", {})
    plan = build_pacing_plan(
        windows,
        minutes_before_short_reset=float(trigger.get("minutesBeforeReset", 30)),
        target_used=float(trigger.get("targetUsedPercent", 99)),
        minimum_remaining=float(trigger.get("minimumRemainingPercent", 1)),
        pacing_headroom=float(trigger.get("pacingHeadroomPercent", 5)),
        final_drain_minutes=float(trigger.get("finalWeeklyDrainMinutes", 180)),
        minimum_weekly_duration=int(trigger.get("weeklyWindowMinimumMinutes", 8640)),
    )
    print(
        f"Weekly pacing: {plan.weekly_progress_percent:.1f}% through week, "
        f"autonomous cap {plan.weekly_cap_percent:.1f}%, eligible={plan.eligible}"
    )
    print(f"Pacing decision: {plan.reason}")
    return 0


def cycle(config_path: pathlib.Path, force: bool, dry_run: bool) -> int:
    config = load_config(config_path)
    data_dir = config_path.parent
    logger = JsonLogger(data_dir / "logs" / "quotaforge.jsonl")
    try:
        lock = acquire_lock(data_dir / "quotaforge.lock")
    except CycleAlreadyRunning as exc:
        logger.write("skipped", reason=str(exc))
        return 0
    try:
        repos = enabled_repos(config)
        if not repos:
            logger.write("skipped", reason="No enabled repositories in the allowlist.")
            return 0
        auth = codex_auth_mode()
        logger.write("auth_checked", status=auth)

        trigger = config.get("trigger", {})
        minutes_before = float(trigger.get("minutesBeforeReset", 30))
        minimum_idle = float(trigger.get("minimumIdleMinutes", 20))
        target_used = float(trigger.get("targetUsedPercent", 99))
        minimum_remaining = float(trigger.get("minimumRemainingPercent", 1))
        pacing_headroom = float(trigger.get("pacingHeadroomPercent", 5))
        final_drain_minutes = float(trigger.get("finalWeeklyDrainMinutes", 180))
        minimum_weekly_duration = int(trigger.get("weeklyWindowMinimumMinutes", 8640))
        max_minutes = float(trigger.get("maxCycleMinutes", 25))
        max_turns = int(trigger.get("maxTurnsPerCycle", 1))
        safety_buffer = float(trigger.get("safetyBufferMinutes", 3))

        _, windows = read_windows()
        plan = build_pacing_plan(
            windows,
            minutes_before_short_reset=minutes_before,
            target_used=target_used,
            minimum_remaining=minimum_remaining,
            pacing_headroom=pacing_headroom,
            final_drain_minutes=final_drain_minutes,
            minimum_weekly_duration=minimum_weekly_duration,
        )
        if not plan.budget_available and not (force and dry_run):
            logger.write(
                "skipped",
                reason=plan.reason,
                weeklyUsedPercent=plan.weekly.used_percent,
                weeklyCapPercent=plan.weekly_cap_percent,
            )
            return 0
        if not force and not plan.eligible:
            logger.write("skipped", reason=plan.reason)
            return 0
        if not force and idle_minutes() < minimum_idle:
            logger.write("skipped", reason="Computer is not idle enough.", idleMinutes=idle_minutes())
            return 0

        anchor_reset = (
            plan.weekly.resets_at
            if plan.final_drain
            else plan.short.resets_at
            if plan.short
            else int(time.time() + max_minutes * 60)
        )
        available_seconds = max(0, anchor_reset - time.time() - safety_buffer * 60)
        if available_seconds <= 0 and not dry_run:
            logger.write("skipped", reason="Too close to reset to finish safely.")
            return 0
        hard_deadline = time.monotonic() + min(
            max_minutes * 60, available_seconds if available_seconds > 0 else max_minutes * 60
        )
        state_path = data_dir / "state.json"
        state = load_state(state_path)
        start_index = int(state.get("nextRepoIndex", 0)) % len(repos)
        completed: list[dict[str, Any]] = []

        for turn in range(max_turns):
            if time.monotonic() >= hard_deadline:
                break
            if not force and idle_minutes() < minimum_idle:
                logger.write("stopped", reason="User activity detected between turns.")
                break
            _, current_windows = read_windows()
            current_plan = build_pacing_plan(
                current_windows,
                minutes_before_short_reset=minutes_before,
                target_used=target_used,
                minimum_remaining=minimum_remaining,
                pacing_headroom=pacing_headroom,
                final_drain_minutes=final_drain_minutes,
                minimum_weekly_duration=minimum_weekly_duration,
            )
            if (
                current_plan.weekly.resets_at != plan.weekly.resets_at
                or not current_plan.budget_available
            ):
                logger.write(
                    "pacing_cap_reached",
                    weeklyUsedPercent=current_plan.weekly.used_percent,
                    weeklyCapPercent=current_plan.weekly_cap_percent,
                )
                break
            if not force and not current_plan.eligible:
                logger.write("stopped", reason=current_plan.reason)
                break
            spec = repos[(start_index + turn) % len(repos)]
            try:
                effective_minimum_idle = 0 if force else minimum_idle
                result = improve_once(
                    data_dir,
                    spec,
                    config,
                    logger,
                    effective_minimum_idle,
                    hard_deadline,
                    dry_run,
                )
                completed.append(result)
            except QuotaForgeError as exc:
                logger.write("repo_failed", repo=spec.slug, error=str(exc))
                notify(config, "QuotaForge needs attention", f"{spec.slug}: {exc}", logger)
            state["nextRepoIndex"] = (start_index + turn + 1) % len(repos)
            state["lastRunAt"] = utc_now()
            save_state(state_path, state)
            if dry_run:
                break

        if completed:
            changed = [item for item in completed if item.get("commit")]
            if changed:
                latest = changed[-1]
                notify(
                    config,
                    f"QuotaForge improved {latest['repo']}",
                    f"{latest.get('summary', latest.get('title', 'Improvement complete'))} "
                    f"({latest['commit']})",
                    logger,
                )
            elif not dry_run:
                notify(config, "QuotaForge checked repositories", "No safe change was needed.", logger)
        logger.write("cycle_completed", attempts=len(completed), dryRun=dry_run)
        return 0
    finally:
        lock.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=pathlib.Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Run one scheduler cycle.")
    parser.add_argument("--status", action="store_true", help="Show current usage and safety state.")
    parser.add_argument("--force", action="store_true", help="Bypass only reset-time and idle gates.")
    parser.add_argument("--dry-run", action="store_true", help="Do not invoke Codex, commit, or push.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        config = load_config(args.config)
        if args.status:
            return status(args.config, config)
        if args.once:
            return cycle(args.config, args.force, args.dry_run)
        raise QuotaForgeError("Choose --status or --once.")
    except (QuotaForgeError, subprocess.TimeoutExpired) as exc:
        write_fatal_log(args.config, exc)
        print(f"QuotaForge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
