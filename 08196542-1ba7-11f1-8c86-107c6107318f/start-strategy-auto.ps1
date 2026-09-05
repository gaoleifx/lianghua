param([switch]$DryRun)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')
$Config = Get-AutomationConfig $Root
$Log = Get-LogPath $Config $Root 'start'
$statePath = Join-Path $Root ([string]$Config.state_file)
$stateDir = Split-Path -Parent $statePath
$pidDir = Resolve-AutomationStateDirectory $Config $Root
$stopSignal = Join-Path $Root ([string]$Config.stop_signal_file)
New-Item -ItemType Directory -Force -Path $stateDir, $pidDir | Out-Null
$startedHere = @()
$strategies = @()
$terminal = $null
$terminalOwned = $false

try {
    if (-not (Test-TradingDay $Root)) { Write-Log $Log 'Not an A-share trading day; startup skipped.'; exit 0 }
    foreach ($name in @('GM_TOKEN', 'GOLDMINER_SIM_ACCOUNT', 'GOLDMINER_LIVE_ACCOUNT')) {
        if (-not [Environment]::GetEnvironmentVariable($name, 'User') -and -not (Get-Item "Env:$name" -ErrorAction SilentlyContinue)) {
            throw "Missing user environment variable $name"
        }
    }
    if ($DryRun) {
        Write-Log $Log 'DryRun passed: calendar, credentials, paths, and configuration are valid.'
        exit 0
    }

    Publish-TradingStatus $Config 'starting' 'info' 'Trading automation is starting.' $false 0 0 $false 0

    Remove-Item -LiteralPath $stopSignal -Force -ErrorAction SilentlyContinue
    $env:GOLDMINER_AUTOMATION_STATE_DIR = $pidDir
    $terminal = Get-Process -Name 'minmetals' -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
    if (-not $terminal) {
        Switch-ToVirtualDesktop ([int]$Config.desktop_number)
        Write-Log $Log "Starting GoldMiner terminal: $($Config.terminal_exe)"
        $terminal = Start-Process -FilePath ([string]$Config.terminal_exe) -WorkingDirectory (Split-Path ([string]$Config.terminal_exe)) -PassThru
        $terminalOwned = $true
    } else {
        Write-Log $Log "GoldMiner terminal already running, PID=$($terminal.Id)"
        if (Test-Path -LiteralPath $statePath) {
            try {
                $prior = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($prior.terminal_owned -and [int]$prior.terminal_pid -eq $terminal.Id) { $terminalOwned = $true }
            } catch { }
        }
    }
    if (-not (Wait-TcpReady ([string]$Config.terminal_ready_host) ([int]$Config.terminal_ready_port) ([int]$Config.terminal_ready_timeout_seconds))) {
        throw "GoldMiner service did not become ready within $($Config.terminal_ready_timeout_seconds) seconds"
    }

    foreach ($item in @(@{Name='simulation'; Entry=$Config.sim_entry}, @{Name='live'; Entry=$Config.live_entry})) {
        $workDir = if ($item.Name -eq 'simulation') { [string]$Config.sim_working_directory } else { [string]$Config.live_working_directory }
        $entryPath = Join-Path $workDir ([string]$item.Entry)
        if (-not (Test-Path -LiteralPath $entryPath)) { throw "Strategy entry not found: $entryPath" }
        $existing = Get-ManagedStrategyProcess $Config $Root $item.Name
        if ($existing) {
            Write-Log $Log "Adopted existing $($item.Name) strategy, PID=$($existing.Id)"
            $strategies += [pscustomobject]@{name=$item.Name; pid=$existing.Id; entry=$item.Entry; working_directory=$workDir; owned=$false; healthy=$true}
            continue
        }

        $logFile = Join-Path (Split-Path $Log) ("strategy_{0}_{1}.log" -f $item.Name, (Get-Date -Format 'yyyyMMdd_HHmmss'))
        $errFile = "$logFile.err"
        $proc = Start-Process -FilePath ([string]$Config.python_exe) -ArgumentList @([string]$item.Entry) -WorkingDirectory $workDir -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError $errFile -PassThru
        $startedHere += $proc
        Write-Log $Log "Started $($item.Name) strategy, PID=$($proc.Id), waiting for initialized event"
        if (-not (Wait-StrategyReady $Config $Root $item.Name $proc.Id $logFile $errFile)) {
            throw "$($item.Name) strategy failed readiness check, PID=$($proc.Id)"
        }
        Write-Log $Log "$($item.Name) strategy healthy, PID=$($proc.Id)"
        $strategies += [pscustomobject]@{name=$item.Name; pid=$proc.Id; entry=$item.Entry; working_directory=$workDir; owned=$true; healthy=$true; log=$logFile; error_log=$errFile}
    }

    $payload = [pscustomobject]@{updated_at=(Get-Date).ToString('o'); terminal_pid=$terminal.Id; terminal_owned=$terminalOwned; strategies=$strategies}
    $payload | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
    $simPid = [int](($strategies | Where-Object { $_.name -eq 'simulation' } | Select-Object -First 1).pid)
    $livePid = [int](($strategies | Where-Object { $_.name -eq 'live' } | Select-Object -First 1).pid)
    Publish-TradingStatus $Config 'healthy' 'success' 'Simulation and live strategies are healthy; automatic live orders are enabled.' $true $simPid $livePid $false 0
    Start-TradingWatchdogIfNeeded $Config $Root $Log | Out-Null
    Write-Log $Log "Automatic startup healthy, state file=$statePath"
    exit 0
} catch {
    $message = "Startup failed: $($_.Exception.Message)"
    Write-Log $Log $message
    Publish-TradingStatus $Config 'failed' 'error' $message $false 0 0 $false 1
    foreach ($proc in @($startedHere)) {
        if ($proc -and (Get-Process -Id ([int]$proc.Id) -ErrorAction SilentlyContinue)) {
            Stop-Process -Id ([int]$proc.Id) -Force -ErrorAction SilentlyContinue
        }
    }
    if ($terminalOwned -and $terminal -and (Get-Process -Id ([int]$terminal.Id) -ErrorAction SilentlyContinue)) {
        Write-Log $Log 'Leaving GoldMiner terminal running for watchdog retry.'
    }
    exit 1
}
