param([switch]$Unregister)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$Config = Get-Content -LiteralPath (Join-Path $Root 'automation_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$StartScript = Join-Path $Root 'watchdog-strategy-auto.ps1'
$StopScript = Join-Path $Root 'stop-strategy-auto.ps1'
$startName = [string]$Config.start_task_name
$stopName = [string]$Config.stop_task_name
$actionStart = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
$actionStop = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StopScript`""
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 8)

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $startName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $stopName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Removed automation tasks.'
    exit 0
}

$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At ([string]$Config.startup_time)
$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At ([string]$Config.shutdown_time)
Register-ScheduledTask -TaskName $startName -Action $actionStart -Trigger $startTrigger -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName $stopName -Action $actionStop -Trigger $stopTrigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "Registered $startName watchdog at $($Config.startup_time) and $stopName at $($Config.shutdown_time) (weekdays; scripts apply A-share calendar filter)."
