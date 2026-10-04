param([ValidateSet('On','Off','Status')][string]$State = 'Status')
$ErrorActionPreference = 'Stop'
$taskName = 'TaskmgrFlatlineWatch'
$script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) { throw 'Autostart is not installed. Run Install.cmd first.' }
if ($task.Actions.Arguments -notcontains ('"' + $script + '"')) { throw 'Task belongs to another installation.' }
if ($State -eq 'Status') { if ($task.Settings.Enabled) { 'On' } else { 'Off' }; exit }
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -State ' + $State)
    $task = Get-ScheduledTask -TaskName $taskName
} elseif ($State -eq 'On') {
    $task = Enable-ScheduledTask -TaskName $taskName
} else {
    $task = Disable-ScheduledTask -TaskName $taskName
}
if ($task.Settings.Enabled) { 'On' } else { 'Off' }
