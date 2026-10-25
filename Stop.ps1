$ErrorActionPreference = 'Stop'
$statusPath = 'D:\TaskmgrFreezeCaptures\status.json'
$requestPath = Join-Path $PSScriptRoot '.local\stop.request'

# 1. Check dump_running in status.json
$watcherPid = $null
if (Test-Path -LiteralPath $statusPath) {
    try {
        $curStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($curStatus.dump_running -eq $true) {
            Write-Error "Cannot stop watcher: dump_running is true in $statusPath. Wait for the dump to finish."
            exit 2
        }
        $watcherPid = $curStatus.watcher_pid
    } catch {
        Write-Warning "Failed to read status: $_"
    }
}

# 2. Check if watcher process is actually running
$targetProcess = $null
if ($watcherPid) {
    $targetProcess = Get-Process -Id $watcherPid -ErrorAction SilentlyContinue
}
if (-not $targetProcess) {
    $matches = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*FlatlineWatch.py*" }
    if ($matches) {
        $watcherPid = $matches[0].ProcessId
        $targetProcess = Get-Process -Id $watcherPid -ErrorAction SilentlyContinue
    }
}

if (-not $targetProcess) {
    Write-Host "Task Manager Flatline Watch is not running."
    exit 0
}

# 3. Request graceful stop
Set-Content -LiteralPath $requestPath -Value 'stop' -Encoding ASCII

# 4. Wait for process to exit with a timeout; never kill forcibly
$timeoutSeconds = 25
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$exited = $false

while ($stopwatch.Elapsed.TotalSeconds -lt $timeoutSeconds) {
    if ($targetProcess.HasExited) {
        $exited = $true
        break
    }
    Start-Sleep -Milliseconds 250
    $targetProcess = Get-Process -Id $watcherPid -ErrorAction SilentlyContinue
    if (-not $targetProcess) {
        $exited = $true
        break
    }
}

if ($exited) {
    Write-Host ("Task Manager Flatline Watch (PID {0}) stopped cleanly in {1:N1}s." -f $watcherPid, $stopwatch.Elapsed.TotalSeconds)
    exit 0
} else {
    Write-Warning ("Task Manager Flatline Watch (PID {0}) did not exit within {1} seconds. Stop request retained. The process was NOT terminated forcibly." -f $watcherPid, $timeoutSeconds)
    exit 1
}
