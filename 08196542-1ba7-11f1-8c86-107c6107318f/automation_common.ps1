Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AutomationConfig {
    param([string]$Root)
    $path = Join-Path $Root 'automation_config.json'
    return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Get-AutomationRoot { return Split-Path -Parent $PSScriptRoot }

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

function Test-TradingDay {
    param([string]$Root, [datetime]$Date = (Get-Date))
    $calendar = Join-Path $Root 'trading_calendar.json'
    $config = Get-AutomationConfig $Root
    if ($config.auto_refresh_calendar) {
        try {
            $refresh = Join-Path $Root 'refresh_trading_calendar.py'
            $job = Start-Job -ScriptBlock { param($python, $script, $path) & $python $script --calendar $path } -ArgumentList 'python', $refresh, $calendar
            Wait-Job $job -Timeout 20 | Out-Null
            if ($job.State -eq 'Completed') { Receive-Job $job | Out-Null }
            Remove-Job $job -Force -ErrorAction SilentlyContinue
        } catch { }
    }
    $result = & python (Join-Path $Root 'trading_calendar.py') --date $Date.ToString('yyyy-MM-dd') --calendar $calendar
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

function Switch-ToVirtualDesktop {
    param([int]$TargetDesktop)
    if ($TargetDesktop -lt 1) { throw 'desktop number must be >= 1' }
    $keyType = 'VirtualDesktopKeys'
    if (-not ('VirtualDesktopKeys' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'VirtualDesktopKeys.cs')
    }
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
