Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AutomationConfig {
    param([string]$Root)
    $path = Join-Path $Root 'automation_config.json'
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-LogPath {
    param($Config, [string]$Root, [string]$Prefix)
    $dir = Join-Path $Root ([string]$Config.log_directory)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    return Join-Path $dir ("{0}_{1}.log" -f $Prefix, (Get-Date -Format 'yyyyMMdd_HHmmss'))
}

function Write-Log {
    param([string]$Path, [string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    Write-Output $line
}

function Send-AutomationAlert {
    param($Config, [string]$Root, [string]$Message)
    $alertLog = Join-Path (Join-Path $Root ([string]$Config.log_directory)) 'alerts.log'
    Write-Log $alertLog $Message | Out-Null
    if ($Config.desktop_alerts_enabled -eq $true) {
        try { & msg.exe $env:USERNAME "GoldMiner automation: $Message" 2>$null | Out-Null } catch { }
    }
}

function Publish-TradingStatus {
    param(
        $Config,
        [string]$Phase,
        [string]$Severity,
        [string]$Message,
        [bool]$TerminalReady = $false,
        [int]$SimulationPid = 0,
        [int]$LivePid = 0,
        [bool]$WatchdogRunning = $false,
        [int]$FailureCount = 0
    )
    try {
        $path = [string]$Config.pet_status_file
        if (-not $path) { return }
        $autoOrdersEnabled = $false
        if ($Config.strategy_config_path -and (Test-Path -LiteralPath ([string]$Config.strategy_config_path))) {
            $strategyConfig = Get-Content -LiteralPath ([string]$Config.strategy_config_path) -Raw -Encoding UTF8 | ConvertFrom-Json
            $autoOrdersEnabled = [bool]$strategyConfig.deployment.live_new_entries_enabled
        }
        $payload = [ordered]@{
            schemaVersion = 1
            updatedAt = (Get-Date).ToString('o')
            phase = $Phase
            severity = $Severity
            message = $Message
            terminal = [ordered]@{ ready = $TerminalReady }
            simulation = [ordered]@{ running = ($SimulationPid -gt 0); pid = $(if ($SimulationPid -gt 0) { $SimulationPid } else { $null }) }
            live = [ordered]@{ running = ($LivePid -gt 0); pid = $(if ($LivePid -gt 0) { $LivePid } else { $null }); autoOrdersEnabled = $autoOrdersEnabled }
            watchdog = [ordered]@{ running = $WatchdogRunning; consecutiveFailures = $FailureCount }
        }
        $dir = Split-Path -Parent $path
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $temp = "$path.tmp"
        $json = $payload | ConvertTo-Json -Depth 5
        [IO.File]::WriteAllText($temp, $json, (New-Object Text.UTF8Encoding($false)))
        Move-Item -LiteralPath $temp -Destination $path -Force
    } catch {
        # Pet status is observational and must never block trading automation.
    }
}

function Resolve-AutomationStateDirectory {
    param($Config, [string]$Root)
    $configured = [string]$Config.pid_directory
    if ([IO.Path]::IsPathRooted($configured)) { return $configured }
    return Join-Path $Root $configured
}

function Get-RolePidPath {
    param($Config, [string]$Root, [string]$Role)
    return Join-Path (Resolve-AutomationStateDirectory $Config $Root) ("{0}.pid.json" -f $Role)
}

function Get-ManagedStrategyProcess {
    param($Config, [string]$Root, [string]$Role)
    $path = Get-RolePidPath $Config $Root $Role
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $payload = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ([string]$payload.role -ne $Role) { throw 'PID role mismatch' }
        $proc = Get-Process -Id ([int]$payload.pid) -ErrorAction SilentlyContinue
        if (-not $proc -or $proc.ProcessName -notmatch '^python') {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            return $null
        }
        $registeredAt = [datetimeoffset]::Parse([string]$payload.started_at).LocalDateTime
        if ([math]::Abs(($proc.StartTime - $registeredAt).TotalSeconds) -gt 120) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            return $null
        }
        return $proc
    } catch {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Wait-StrategyReady {
    param($Config, [string]$Root, [string]$Role, [int]$ProcessId, [string]$Stdout, [string]$Stderr)
    $deadline = (Get-Date).AddSeconds([int]$Config.strategy_ready_timeout_seconds)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return $false }
        $registered = Get-ManagedStrategyProcess $Config $Root $Role
        $text = ''
        if (Test-Path -LiteralPath $Stdout) { $text += Get-Content -LiteralPath $Stdout -Raw -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $Stderr) { $text += Get-Content -LiteralPath $Stderr -Raw -ErrorAction SilentlyContinue }
        if ($registered -and $registered.Id -eq $ProcessId -and $text -match '"event"\s*:\s*"initialized"') { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Test-TradingDay {
    param([string]$Root, [datetime]$Date = (Get-Date))
    $calendar = Join-Path $Root 'trading_calendar.json'
    $config = Get-AutomationConfig $Root
    if ($config.auto_refresh_calendar) {
        try {
            $refresh = Join-Path $Root 'refresh_trading_calendar.py'
            $job = Start-Job -ScriptBlock { param($python, $script, $path) & $python $script --calendar $path } -ArgumentList ([string]$config.python_exe), $refresh, $calendar
            Wait-Job $job -Timeout 20 | Out-Null
            if ($job.State -eq 'Completed') { Receive-Job $job | Out-Null }
            Remove-Job $job -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    $result = & ([string]$config.python_exe) (Join-Path $Root 'trading_calendar.py') --date $Date.ToString('yyyy-MM-dd') --calendar $calendar
    if ($LASTEXITCODE -ne 0) { throw 'Trading calendar check failed' }
    return ($result -contains 'trading')
}

function Wait-TcpReady {
    param([string]$HostName, [int]$Port, [int]$TimeoutSeconds)
    $end = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $end) {
        try {
            $client = New-Object Net.Sockets.TcpClient
            $async = $client.BeginConnect($HostName, $Port, $null, $null)
            if ($async.AsyncWaitHandle.WaitOne(1000) -and $client.Connected) {
                $client.Close(); return $true
            }
            $client.Close()
        } catch { }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Get-TradingWatchdogProcess {
    param([string]$Root)
    $watchdogScript = Join-Path $Root 'watchdog-strategy-auto.ps1'
    return Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -match '^powershell(\.exe)?$' -and
            $_.CommandLine -like "*$watchdogScript*" -and
            $_.CommandLine -notmatch '(?i)-DryRun'
        } |
        Select-Object -First 1
}

function Start-TradingWatchdogIfNeeded {
    param($Config, [string]$Root, [string]$Log)
    $existing = Get-TradingWatchdogProcess $Root
    if ($existing) {
        Write-Log $Log "Trading watchdog already running, PID=$($existing.ProcessId)" | Out-Null
        return $existing
    }

    $now = Get-Date
    $shutdown = [datetime]::ParseExact($now.ToString('yyyy-MM-dd') + ' ' + [string]$Config.shutdown_time, 'yyyy-MM-dd HH:mm', $null)
    if ($now -ge $shutdown) {
        Write-Log $Log "Trading watchdog not launched after shutdown time $($Config.shutdown_time)." | Out-Null
        return $null
    }

    $watchdogScript = Join-Path $Root 'watchdog-strategy-auto.ps1'
    $process = Start-Process -FilePath 'PowerShell.exe' -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$watchdogScript`"") -WindowStyle Hidden -PassThru
    Write-Log $Log "Started trading watchdog, PID=$($process.Id)" | Out-Null
    return $process
}

function Switch-ToVirtualDesktop {
    param([int]$TargetDesktop)
    if ($TargetDesktop -lt 1) { throw 'desktop number must be >= 1' }
    if (-not ('VirtualDesktopKeys' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'VirtualDesktopKeys.cs') }
    $vdKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VirtualDesktops'
    $props = Get-ItemProperty -Path $vdKey -ErrorAction Stop
    $ids = [byte[]]$props.VirtualDesktopIDs
    $current = [byte[]]$props.CurrentVirtualDesktop
    $count = [int]($ids.Length / 16)
    $currentIndex = -1
    for ($i = 0; $i -lt $count; $i++) {
        $same = $true
        for ($j = 0; $j -lt 16; $j++) { if ($ids[$i * 16 + $j] -ne $current[$j]) { $same = $false; break } }
        if ($same) { $currentIndex = $i; break }
    }
    if ($currentIndex -lt 0 -or $TargetDesktop -gt $count) { throw "Target virtual desktop $TargetDesktop is unavailable (count=$count)" }
    $targetIndex = $TargetDesktop - 1
    if ($targetIndex -gt $currentIndex) {
        for ($i = $currentIndex; $i -lt $targetIndex; $i++) { [VirtualDesktopKeys]::Press([VirtualDesktopKeys]::RIGHT); Start-Sleep -Milliseconds 250 }
    } elseif ($targetIndex -lt $currentIndex) {
        for ($i = $currentIndex; $i -gt $targetIndex; $i--) { [VirtualDesktopKeys]::Press([VirtualDesktopKeys]::LEFT); Start-Sleep -Milliseconds 250 }
    }
}
