param([switch]$Unregister)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = $PSScriptRoot
$Config = Get-Content -LiteralPath (Join-Path $Root 'automation_config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$StartScript = Join-Path $Root 'start-strategy-auto.ps1'
$StopScript = Join-Path $Root 'stop-strategy-auto.ps1'
$startName = [string]$Config.start_task_name
$stopName = [string]$Config.stop_task_name
$actionStart = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
$actionStop = New-ScheduledTaskAction -Execute 'PowerShell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StopScript`""
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $startName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $stopName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Removed automation tasks.'
    exit 0
}

$startTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:45'
$stopTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '15:10'
Register-ScheduledTask -TaskName $startName -Action $actionStart -Trigger $startTrigger -Principal $principal -Settings $settings -Force | Out-Null
Register-ScheduledTask -TaskName $stopName -Action $actionStop -Trigger $stopTrigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Output "Registered $startName at 08:45 and $stopName at 15:10 (weekdays; scripts apply A-share calendar filter)."
