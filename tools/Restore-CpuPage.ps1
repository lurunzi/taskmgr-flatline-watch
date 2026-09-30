param([Parameter(Mandatory=$true)][int]$TargetPid)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$process=Get-Process -Id $TargetPid
if ($process.ProcessName -ne 'Taskmgr') { throw 'Target is not Task Manager' }
$root=[System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
if (-not $root) { throw 'Task Manager window is unavailable' }
function Activate-Element($element) {
    $pattern=$null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) { $pattern.Invoke(); return $true }
    if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) { $pattern.Select(); return $true }
    return $false
}
$elements=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
foreach ($element in $elements) {
    if ($element.Current.Name -in @('Performance',([string][char]0x6027+[char]0x80fd))) {
        if (Activate-Element $element) { break }
    }
}
Start-Sleep -Milliseconds 500
$elements=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
foreach ($element in $elements) {
    if ($element.Current.Name -match '^CPU(\s|$)') {
        if (Activate-Element $element) { break }
    }
}
