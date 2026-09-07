[CmdletBinding()]
param(
    [switch]$RemoveData
)

$task = Get-ScheduledTask -TaskName 'QuotaForge' -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName 'QuotaForge' -Confirm:$false
    Write-Host "Removed scheduled task 'QuotaForge'."
}

if ($RemoveData) {
    $dataPath = Join-Path $env:LOCALAPPDATA 'QuotaForge'
    if (Test-Path -LiteralPath $dataPath) {
        $resolved = (Resolve-Path -LiteralPath $dataPath).Path
        $expected = [System.IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'QuotaForge'))
        if ($resolved -ne $expected) {
            throw "Refusing to remove unexpected path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
        Write-Host "Removed QuotaForge data at $resolved. This is not recoverable from the Recycle Bin."
    }
}
