$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$admin = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Requesting Administrator privileges for the address-chain capture..."
    $proc = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru -ArgumentList (
        '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    if ($null -eq $proc.ExitCode) { Write-Host 'ELEVATED_EXIT cancelled'; exit 1 }
    Write-Host "ELEVATED_EXIT $($proc.ExitCode)"
    exit $proc.ExitCode
}

$statusPath = 'D:\TaskmgrFreezeCaptures\status.json'
$status = Get-Content -Raw -Encoding UTF8 $statusPath | ConvertFrom-Json
if ($status.dump_running) {
    throw 'watcher is capturing; refusing to stop it'
}
$taskmgr = Get-Process -Id ([int]$status.taskmgr_pid) -ErrorAction SilentlyContinue
if (-not $taskmgr -or $taskmgr.ProcessName -ne 'Taskmgr') {
    throw "status.json taskmgr_pid $($status.taskmgr_pid) is not a running Taskmgr"
}

$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
$outDir = "D:\TaskmgrFreezeCaptures\reproduce-$stamp"
$log = "D:\TaskmgrFreezeCaptures\reproduce-$stamp.host.log"
Write-Host "Taskmgr PID: $([int]$taskmgr.Id)"
Write-Host "Output:      $outDir"
$code = 1
try {
    & '.\.venv\Scripts\python.exe' -u 'tools\capture-address-chain.py' --pid $taskmgr.Id --minutes 8 --induce 4 --output $outDir *>&1 |
        Tee-Object -FilePath $log
    $code = $LASTEXITCODE
}
finally {
    $pointer = "D:\TaskmgrFreezeCaptures\reproduce-live-latest.txt"
    [System.IO.File]::WriteAllText($pointer, "$code`r`n$outDir`r`n", [System.Text.UTF8Encoding]::new($false))
}
Write-Host "Capture exit code: $code"
exit $code
