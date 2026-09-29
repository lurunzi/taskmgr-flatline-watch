$ErrorActionPreference = 'Stop'
$taskName = 'TaskmgrFlatlineWatch'
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    exit
}
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
    $script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'
    $dump = Join-Path $PSScriptRoot '.local\procdump\procdump64.exe'
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $dump)) { throw 'Run Setup.cmd first.' }
    $signature = Get-AuthenticodeSignature -LiteralPath $dump
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*O=Microsoft Corporation*') { throw 'ProcDump signature verification failed.' }
    if (-not (Test-Path -LiteralPath 'D:\')) { throw 'D: drive is unavailable.' }
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Actions.Arguments -notcontains ('"' + $script + '"')) { throw 'Task name is owned by another installation.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $python -Argument ('"' + $script + '"') -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $trigger.Delay = 'PT15S'
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Automatically capture Task Manager CPU aggregate freeze evidence to D:\TaskmgrFreezeCaptures.' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    @{ status='registered_and_start_requested'; task=$taskName; timestamp=(Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot '.local\installation.json') -Encoding UTF8
} catch {
    @{ status='failed'; error=$_.Exception.Message; timestamp=(Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot '.local\installation.json') -Encoding UTF8
    throw
}
