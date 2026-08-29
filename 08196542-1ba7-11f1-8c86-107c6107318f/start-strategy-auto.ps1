param([switch]$DryRun)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')
$Config = Get-AutomationConfig $Root
$Log = Get-LogPath $Config $Root 'start'
$statePath = Join-Path $Root ([string]$Config.state_file)
$stateDir = Split-Path -Parent $statePath
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$children = @()
$terminal = $null
$terminalOwned = $false

try {
    if (-not (Test-TradingDay $Root)) { Write-Log $Log 'Not an A-share trading day; startup skipped.'; exit 0 }
    if (-not $env:GM_TOKEN) { throw 'Missing user environment variable GM_TOKEN' }
    if (-not $env:GOLDMINER_SIM_ACCOUNT) { throw 'Missing user environment variable GOLDMINER_SIM_ACCOUNT' }
    if (-not $env:GOLDMINER_LIVE_ACCOUNT) { throw 'Missing user environment variable GOLDMINER_LIVE_ACCOUNT' }

    if ($DryRun) {
        Write-Log $Log 'DryRun passed: no terminal, strategy, or desktop changes were made.'
        exit 0
    }
    Switch-ToVirtualDesktop ([int]$Config.desktop_number)
    $terminal = Get-Process -Name 'minmetals' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if (-not $terminal) {
        Write-Log $Log "Starting GoldMiner terminal: $($Config.terminal_exe)"
        $terminal = Start-Process -FilePath ([string]$Config.terminal_exe) -WorkingDirectory (Split-Path ([string]$Config.terminal_exe)) -PassThru
        $terminalOwned = $true
    } else { Write-Log $Log "GoldMiner terminal already running, PID=$($terminal.Id)" }
    if (-not (Wait-TcpReady ([string]$Config.terminal_ready_host) ([int]$Config.terminal_ready_port) ([int]$Config.terminal_ready_timeout_seconds))) {
        throw "GoldMiner service did not become ready within $($Config.terminal_ready_timeout_seconds) seconds"
    }

    foreach ($item in @(@{Name='simulation'; Entry=$Config.sim_entry}, @{Name='live'; Entry=$Config.live_entry})) {
        $workDir = if ($item.Name -eq 'simulation') { [string]$Config.sim_working_directory } else { [string]$Config.live_working_directory }
        if (-not (Test-Path -LiteralPath (Join-Path $workDir $item.Entry))) { throw "Strategy entry not found: $(Join-Path $workDir $item.Entry)" }
        $logFile = Join-Path (Split-Path $Log) ("strategy_{0}_{1}.log" -f $item.Name, (Get-Date -Format 'yyyyMMdd'))
        $errFile = "$logFile.err"
        $proc = Start-Process -FilePath ([string]$Config.python_exe) -ArgumentList @($item.Entry) -WorkingDirectory $workDir -RedirectStandardOutput $logFile -RedirectStandardError $errFile -PassThru
        $children += [pscustomobject]@{name=$item.Name; pid=$proc.Id; entry=$item.Entry; working_directory=$workDir; log=$logFile}
        Write-Log $Log "Started $($item.Name) strategy, PID=$($proc.Id), working_directory=$workDir"
    }
    $payload = [pscustomobject]@{started_at=(Get-Date).ToString('o'); terminal_pid=$terminal.Id; terminal_owned=$terminalOwned; strategies=$children}
    $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Log $Log "Automatic startup complete, state file=$statePath"
    exit 0
} catch {
    Write-Log $Log "Startup failed: $($_.Exception.Message)"
    foreach ($item in @($children)) {
        if ($item -and (Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue)) {
            Stop-Process -Id ([int]$item.pid) -Force -ErrorAction SilentlyContinue
        }
    }
    if ($terminalOwned -and $terminal -and (Get-Process -Id ([int]$terminal.Id) -ErrorAction SilentlyContinue)) {
        Stop-Process -Id ([int]$terminal.Id) -Force -ErrorAction SilentlyContinue
    }
    exit 1
}
