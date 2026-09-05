Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
. (Join-Path $Root 'automation_common.ps1')

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "SELFTEST FAILED: $Message" }
}

$temp = Join-Path ([IO.Path]::GetTempPath()) ("goldminer-automation-selftest-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
$proc = $null
try {
    $config = [pscustomobject]@{
        pid_directory = $temp
        strategy_ready_timeout_seconds = 3
    }
    $proc = Start-Process -FilePath 'C:\Users\Gaole\AppData\Local\Programs\Python\Python310\python.exe' -ArgumentList '-c "import time; time.sleep(30)"' -WindowStyle Hidden -PassThru
    $pidPath = Join-Path $temp 'live.pid.json'
    [pscustomobject]@{role='live'; pid=$proc.Id; started_at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8
    $managed = Get-ManagedStrategyProcess $config $Root 'live'
    Assert-True ($null -ne $managed -and $managed.Id -eq $proc.Id) 'role PID registry did not adopt a live process without runtime_state.json'

    $stdout = Join-Path $temp 'ready.out.log'
    $stderr = Join-Path $temp 'ready.err.log'
    Set-Content -LiteralPath $stdout -Value '{"role":"live","event":"initialized"}' -Encoding UTF8
    Set-Content -LiteralPath $stderr -Value '' -Encoding UTF8
    Assert-True (Wait-StrategyReady $config $Root 'live' $proc.Id $stdout $stderr) 'initialized event and PID registry were not accepted as healthy'

    Stop-Process -Id $proc.Id -Force
    $proc.WaitForExit(5000) | Out-Null
    Assert-True ($null -eq (Get-ManagedStrategyProcess $config $Root 'live')) 'stale PID registry was not rejected'
    Assert-True (-not (Test-Path -LiteralPath $pidPath)) 'stale PID file was not removed'
    Write-Output 'AUTOMATION_SELFTEST_PASS: adoption, readiness, and stale-PID cleanup'
    exit 0
} finally {
    if ($proc -and (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Get-ChildItem -LiteralPath $temp -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
}
