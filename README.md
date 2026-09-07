# QuotaForge

QuotaForge turns Codex capacity that is about to reset into useful, reviewable
improvements for GitHub repositories you explicitly allow.

It is deliberately conservative. A cycle starts only when:

- a Codex quota window resets within the configured threshold (30 minutes by
  default);
- Windows has been idle for the configured period (20 minutes by default);
- Codex is authenticated with ChatGPT, not an API key;
- the repository is on the exact GitHub allowlist; and
- the managed checkout is clean and can fast-forward.

QuotaForge checks idle time throughout a run and stops the active Codex process
when you return. It never redeems reset credits or spends API credits. Every
successful change is surrounded by an empty preflight commit and a result
commit, then pushed with ordinary Git credentials. Windows notifications and an
optional webhook explain what happened.

## Install on Windows

Requirements: Git, Python 3.11+, Codex CLI, and a ChatGPT login in Codex.

```powershell
git clone https://github.com/YOUR_ACCOUNT/quotaforge.git
cd quotaforge
.\install.ps1
```

The installer creates `%LOCALAPPDATA%\QuotaForge\config.json` and a hidden
per-user Scheduled Task that checks every five minutes. When the current clone
has a GitHub `origin`, it is added as the first disabled allowlist example; edit
the config and set `enabled` to `true` after reviewing it.

Useful commands:

```powershell
python .\src\quotaforge.py --status
python .\src\quotaforge.py --once --dry-run
python .\src\quotaforge.py --once --force --dry-run
.\uninstall.ps1
```

`--force` bypasses only time/idle gates. It never bypasses authentication,
allowlist, clean-tree, or remote-safety checks. Use `--dry-run` to verify the
selection without invoking Codex, committing, or pushing.

## Configuration

The generated configuration lives outside the repository so it cannot be
committed accidentally. See [config.example.json](config.example.json).

- `whitelist`: exact `https://github.com/OWNER/REPO[.git]` URLs only.
- `minutesBeforeReset`: start only shortly before a window resets.
- `minimumIdleMinutes`: protect interactive use.
- `targetUsedPercent`: stop once the expiring bucket reaches this value.
- `maxCycleMinutes` and `maxTurnsPerCycle`: hard runaway limits.
- `push`: set false to keep commits local while evaluating the service.
- `webhookUrl`: optional generic JSON webhook. Prefer the
  `QUOTAFORGE_WEBHOOK_URL` environment variable for a secret URL.

## Commit model

For each attempted improvement QuotaForge:

1. fetches and fast-forwards a clean managed clone;
2. creates and pushes an empty `preflight` commit;
3. asks a non-interactive, workspace-sandboxed Codex run to implement one small
   improvement and run relevant checks;
4. blocks suspicious secret/key file paths;
5. commits and pushes the result; and
6. sends a notification with the repository, summary, and commit ID.

Codex is instructed not to commit or push. QuotaForge owns those boundaries.

## Important limits

Usage is reported as a percentage of a quota window, not as an exact count of
remaining tokens. QuotaForge therefore works toward `targetUsedPercent` and
stops at the reset boundary or hard time limit; it cannot guarantee an exact
zero remainder. Work complexity also changes how quickly usage is consumed.

The Codex app-server surface used for local quota reads is documented, while
the command itself is still marked experimental. QuotaForge fails closed if it
cannot read usage.

## License

MIT
