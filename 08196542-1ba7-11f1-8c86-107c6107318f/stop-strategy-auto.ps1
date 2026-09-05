Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')
$Config = Get-AutomationConfig $Root
$Log = Get-LogPath $Config $Root 'stop'
$statePath = Join-Path $Root ([string]$Config.state_file)
$stopSignal = Join-Path $Root ([string]$Config.stop_signal_file)
$pidDir = Resolve-AutomationStateDirectory $Config $Root
New-Item -ItemType Directory -Force -Path (Split-Path $stopSignal), $pidDir | Out-Null
Set-Content -LiteralPath $stopSignal -Value (Get-Date).ToString('o') -Encoding UTF8

try {
    Publish-TradingStatus $Config 'stopping' 'info' 'Trading automation is stopping.' (Wait-TcpReady ([string]$Config.terminal_ready_host) ([int]$Config.terminal_ready_port) 1) 0 0 $false 0
    $targets = @()
    $terminalPid = $null
    $terminalOwned = $false
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $targets += @($state.strategies | ForEach-Object { [pscustomobject]@{name=$_.name; pid=[int]$_.pid} })
            $terminalPid = $state.terminal_pid
            $terminalOwned = [bool]$state.terminal_owned
        } catch { Write-Log $Log "Runtime state unreadable; falling back to role PID registry: $($_.Exception.Message)" }
    }
    foreach ($role in @('simulation', 'live')) {
        $proc = Get-ManagedStrategyProcess $Config $Root $role
        if ($proc) { $targets += [pscustomobject]@{name=$role; pid=$proc.Id} }
    }
    $targets = @($targets | Sort-Object pid -Unique)
    if ($targets.Count -eq 0) { Write-Log $Log 'No managed strategy process is running.' }
    foreach ($item in $targets) {
        $proc = Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Log $Log "Stopping $($item.name) strategy, PID=$($item.pid)"
            Stop-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
        }
    }
    $deadline = (Get-Date).AddSeconds([int]$Config.process_stop_timeout_seconds)
    while ((Get-Date) -lt $deadline) {
        $alive = @($targets | Where-Object { Get-Process -Id ([int]$_.pid) -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    foreach ($item in $targets) {
        if (Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue) {
            Write-Log $Log "Strategy did not exit normally; force stopping PID=$($item.pid)"
            Stop-Process -Id ([int]$item.pid) -Force -ErrorAction SilentlyContinue
        }
    }
    Get-ChildItem -LiteralPath $pidDir -Filter '*.pid.json' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    if ($terminalOwned -and $terminalPid -and (Get-Process -Id ([int]$terminalPid) -ErrorAction SilentlyContinue)) {
        Write-Log $Log "Stopping automation-owned GoldMiner terminal, PID=$terminalPid"
        Stop-Process -Id ([int]$terminalPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    Publish-TradingStatus $Config 'stopped' 'neutral' 'Trading automation is stopped after market close.' $false 0 0 $false 0
    Write-Log $Log 'Automatic shutdown complete.'
    exit 0
} catch {
    $message = "Shutdown failed: $($_.Exception.Message)"
    Write-Log $Log $message
    Publish-TradingStatus $Config 'failed' 'error' $message $false 0 0 $false 1
    Send-AutomationAlert $Config $Root $message
    exit 1
}
