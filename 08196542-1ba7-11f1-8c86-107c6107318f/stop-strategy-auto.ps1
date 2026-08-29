Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')
$Config = Get-AutomationConfig $Root
$Log = Get-LogPath $Config $Root 'stop'
$statePath = Join-Path $Root ([string]$Config.state_file)

try {
    if (-not (Test-Path -LiteralPath $statePath)) { Write-Log $Log 'No runtime state file; strategy cleanup skipped.'; exit 0 }
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($item in @($state.strategies)) {
        $proc = Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Log $Log "Stopping $($item.name) strategy, PID=$($item.pid)"
            Stop-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
        }
    }
    $deadline=(Get-Date).AddSeconds([int]$Config.process_stop_timeout_seconds)
    while ((Get-Date) -lt $deadline) {
        $alive=@($state.strategies | Where-Object { Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    foreach ($item in @($state.strategies)) {
        if (Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue) {
            Write-Log $Log "Strategy did not exit normally; force stopping PID=$($item.pid)"
            Stop-Process -Id ([int]$item.pid) -Force -ErrorAction SilentlyContinue
        }
    }
    if ($state.terminal_owned -and $state.terminal_pid -and (Get-Process -Id ([int]$state.terminal_pid) -ErrorAction SilentlyContinue)) {
        Write-Log $Log "Stopping GoldMiner terminal, PID=$($state.terminal_pid)"
        Stop-Process -Id ([int]$state.terminal_pid) -Force -ErrorAction SilentlyContinue
    }
    if ($state.terminal_owned) {
        $installRoot = Split-Path (Split-Path ([string]$Config.terminal_exe))
        Get-Process -Name 'gmterm-serv' -ErrorAction SilentlyContinue | Where-Object {
            $_.Path -and $_.Path.StartsWith($installRoot, [StringComparison]::OrdinalIgnoreCase)
        } | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Write-Log $Log 'Automatic shutdown complete.'
    exit 0
} catch {
    Write-Log $Log "Shutdown failed: $($_.Exception.Message)"
    exit 1
}
