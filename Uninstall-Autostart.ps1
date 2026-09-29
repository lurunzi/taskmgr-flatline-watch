$ErrorActionPreference = 'Stop'
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '"')
    exit
}
$task = Get-ScheduledTask -TaskName 'TaskmgrFlatlineWatch' -ErrorAction SilentlyContinue
$script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'
if ($task) {
    if ($task.Actions.Arguments -notcontains ('"' + $script + '"')) { throw 'Task belongs to another installation.' }
    Unregister-ScheduledTask -TaskName 'TaskmgrFlatlineWatch' -Confirm:$false
}
& (Join-Path $PSScriptRoot 'Stop.ps1')
