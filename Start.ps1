$ErrorActionPreference = 'Stop'
$taskName = 'TaskmgrFlatlineWatch'
$mutexName = 'Local\TaskmgrFlatlineWatch-ABSCOND'
$statusPath = 'D:\TaskmgrFreezeCaptures\status.json'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'

# Check if an instance is already running via mutex
$alreadyRunning = $false
try {
    $existingMutex = [System.Threading.Mutex]::OpenExisting($mutexName)
    $existingMutex.Dispose()
    $alreadyRunning = $true
} catch [System.Threading.WaitHandleCannotBeOpenedException] {
    $alreadyRunning = $false
} catch {
    $alreadyRunning = $true
}

if ($alreadyRunning) {
    $pidInfo = ""
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $curStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($curStatus.watcher_pid) {
                $pidInfo = " (PID: $($curStatus.watcher_pid))"
            }
        } catch {}
    }
    Write-Warning "Task Manager Flatline Watch is already running$pidInfo. Stop it first using Stop.ps1 if you want to restart."
    exit 3
}

$startTime = [DateTime]::UtcNow

# Launch the watcher:
# Prefer Start-ScheduledTask if no custom args and task is registered (starts with highest privileges)
$startedViaTask = $false
if ($args.Count -eq 0) {
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        try {
            Start-ScheduledTask -TaskName $taskName
            $startedViaTask = $true
        } catch {}
    }
}

if (-not $startedViaTask) {
    $procArgs = @('"' + $script + '"') + $args
    try {
        Start-Process -FilePath $python -ArgumentList $procArgs -Verb RunAs -WindowStyle Hidden
    } catch {
        # Fall back to starting directly if RunAs is not supported in non-interactive environment
        Start-Process -FilePath $python -ArgumentList $procArgs -WindowStyle Hidden
    }
}

# Confirm heartbeat in status.json
$timeoutSeconds = 15
$started = $false

while (([DateTime]::UtcNow - $startTime).TotalSeconds -lt $timeoutSeconds) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $statusPath) {
        try {
            $raw = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($raw.watcher_pid -and (Get-Process -Id $raw.watcher_pid -ErrorAction SilentlyContinue)) {
                $statusTime = [DateTime]::Parse($raw.timestamp).ToUniversalTime()
                if ($statusTime -ge $startTime.AddSeconds(-2)) {
                    Write-Host "Task Manager Flatline Watch started successfully."
                    Write-Host "  PID:        $($raw.watcher_pid)"
                    Write-Host "  State:      $($raw.state)"
                    Write-Host "  Mode:       $($raw.mode)"
                    if ($raw.light_etw) {
                        Write-Host "  Light ETW:  $($raw.light_etw.state)"
                    }
                    if ($raw.target_dll) {
                        $hashShort = if ($raw.target_dll.sha256) { $raw.target_dll.sha256.Substring(0,8) + "..." } else { "none" }
                        Write-Host "  Target DLL: SHA256=$hashShort changed=$($raw.target_dll.dll_changed)"
                    }
                    $started = $true
                    break
                }
            }
        } catch {}
    }
}

if (-not $started) {
    $errFile = Join-Path $PSScriptRoot '.local\startup-error.txt'
    $extra = ""
    if (Test-Path -LiteralPath $errFile) {
        $extra = Get-Content -LiteralPath $errFile -Raw -Encoding UTF8
    }
    Write-Error "Watcher startup heartbeat could not be confirmed within $timeoutSeconds seconds. $extra"
    exit 1
}
