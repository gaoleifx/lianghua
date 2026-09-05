param([switch]$DryRun, [switch]$SinglePass)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')
$Config = Get-AutomationConfig $Root
$Log = Get-LogPath $Config $Root 'watchdog'
$stopSignal = Join-Path $Root ([string]$Config.stop_signal_file)
$startScript = Join-Path $Root 'start-strategy-auto.ps1'
$failureCount = 0
$lastAlert = [datetime]::MinValue

try {
    if (-not (Test-TradingDay $Root)) { Write-Log $Log 'Not an A-share trading day; watchdog exiting.'; exit 0 }
    if ($DryRun) {
        & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript -DryRun | ForEach-Object { Write-Log $Log $_ }
        if ($LASTEXITCODE -ne 0) { throw "Startup DryRun failed with exit code $LASTEXITCODE" }
        Write-Log $Log 'Watchdog DryRun passed.'
        exit 0
    }

    Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
    Write-Log $Log 'Watchdog started.'
    Publish-TradingStatus $Config 'watching' 'info' 'Trading watchdog is starting health checks.' $false 0 0 $true 0
    while ($true) {
        $now = Get-Date
        $shutdown = [datetime]::ParseExact($now.ToString('yyyy-MM-dd') + ' ' + [string]$Config.shutdown_time, 'yyyy-MM-dd HH:mm', $null)
        if ($now -ge $shutdown -or (Test-Path -LiteralPath $stopSignal)) {
            Write-Log $Log 'Shutdown time or stop signal reached; watchdog exiting.'
            Publish-TradingStatus $Config 'stopped' 'neutral' 'Trading watchdog stopped after market close.' $false 0 0 $false 0
            exit 0
        }

        $terminalReady = Wait-TcpReady ([string]$Config.terminal_ready_host) ([int]$Config.terminal_ready_port) 2
        $sim = Get-ManagedStrategyProcess $Config $Root 'simulation'
        $live = Get-ManagedStrategyProcess $Config $Root 'live'
        if ($terminalReady -and $sim -and $live) {
            if ($failureCount -gt 0) { Write-Log $Log "Automation recovered: simulation PID=$($sim.Id), live PID=$($live.Id)" }
            $failureCount = 0
            Publish-TradingStatus $Config 'healthy' 'success' 'Simulation and live strategies are healthy; automatic live orders are enabled.' $true $sim.Id $live.Id $true 0
        } else {
            Write-Log $Log "Health check failed: terminal=$terminalReady simulation=$([bool]$sim) live=$([bool]$live); invoking idempotent startup"
            Publish-TradingStatus $Config 'recovering' 'warning' 'Trading automation is recovering a missing service or strategy process.' $terminalReady $(if($sim){$sim.Id}else{0}) $(if($live){$live.Id}else{0}) $true $failureCount
            & PowerShell.exe -NoProfile -ExecutionPolicy Bypass -File $startScript | ForEach-Object { Write-Log $Log $_ }
            if ($LASTEXITCODE -eq 0) {
                $failureCount = 0
                Write-Log $Log 'Recovery startup succeeded.'
            } else {
                $failureCount++
                Write-Log $Log "Recovery startup failed, consecutive failures=$failureCount"
                Publish-TradingStatus $Config 'degraded' 'error' "Trading recovery failed $failureCount consecutive times." $terminalReady 0 0 $true $failureCount
                $cooldown = [int]$Config.watchdog_alert_cooldown_seconds
                if ($failureCount -ge [int]$Config.watchdog_alert_after_failures -and ((Get-Date) - $lastAlert).TotalSeconds -ge $cooldown) {
                    Send-AutomationAlert $Config $Root "Startup or recovery failed $failureCount consecutive times; inspect GoldMiner and automation logs."
                    $lastAlert = Get-Date
                }
            }
        }
        if ($SinglePass) { Write-Log $Log 'SinglePass completed.'; exit $(if($failureCount -eq 0){0}else{1}) }
        Start-Sleep -Seconds ([int]$Config.watchdog_poll_seconds)
    }
} catch {
    $message = "Watchdog failed: $($_.Exception.Message)"
    Write-Log $Log $message
    Publish-TradingStatus $Config 'failed' 'error' $message $false 0 0 $false 1
    Send-AutomationAlert $Config $Root $message
    exit 1
}
