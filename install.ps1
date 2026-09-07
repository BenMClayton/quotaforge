[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.quotaforge\config.json'),
    [switch]$EnableOrigin
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$runner = Join-Path $projectRoot 'src\quotaforge.py'
$example = Join-Path $projectRoot 'config.example.json'
$taskName = 'QuotaForge'

foreach ($command in @('git', 'python', 'codex')) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command '$command' was not found on PATH."
    }
}

& codex login status | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'Codex is not logged in. Run: codex login'
}

$configDirectory = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Force -Path $configDirectory | Out-Null

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    $config = Get-Content -LiteralPath $example -Raw | ConvertFrom-Json
    $origin = (& git -C $projectRoot remote get-url origin 2>$null)
    if ($LASTEXITCODE -eq 0 -and $origin -match '^https://github\.com/[^/]+/[^/]+(?:\.git)?$') {
        $config.whitelist[0].url = $origin.Trim()
        $config.whitelist[0].enabled = [bool]$EnableOrigin
    }
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ConfigPath -Encoding utf8
    Write-Host "Created config: $ConfigPath"
}

$pythonCommand = Get-Command 'pythonw.exe' -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw 'pythonw.exe is required so QuotaForge can run without opening a terminal window.'
}
$taskArguments = '"' + $runner + '" --config "' + $ConfigPath + '" --once'
$action = New-ScheduledTaskAction -Execute $pythonCommand.Source -Argument $taskArguments `
    -WorkingDirectory $projectRoot
$intervalTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($intervalTrigger, $logonTrigger) `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Installed scheduled task '$taskName'."
Write-Host "Review and enable repositories in: $ConfigPath"
Write-Host "Test safely with: python `"$runner`" --config `"$ConfigPath`" --once --force --dry-run"
