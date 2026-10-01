param([Parameter(Mandatory=$true)][int]$TargetPid)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$clock=[System.Diagnostics.Stopwatch]::StartNew()
function Step($label) { [Console]::Out.WriteLine(('{0} {1}ms' -f $label,$clock.ElapsedMilliseconds)) }
$process=Get-Process -Id $TargetPid
if ($process.ProcessName -ne 'Taskmgr') { throw 'Target is not Task Manager' }
# A freshly launched Task Manager may not have published its main window yet.
while (-not $process.MainWindowHandle -and $clock.ElapsedMilliseconds -lt 10000) {
    Start-Sleep -Milliseconds 250
    $process.Refresh()
}
if (-not $process.MainWindowHandle) { throw 'Task Manager window is unavailable' }
$root=[System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
Step 'window'
function Activate-Element($element) {
    $pattern=$null
    if ($element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) { $pattern.Invoke(); return $true }
    if ($element.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern,[ref]$pattern)) { $pattern.Select(); return $true }
    return $false
}
function Test-Stale($record) {
    return $record.Exception.GetBaseException() -is [System.Windows.Automation.ElementNotAvailableException]
}
# Fetch all names in one cached request; reading Current.Name per element is a
# cross-process call each and was slow on large pages such as Processes.
function Find-Named($pattern) {
    $cache=New-Object System.Windows.Automation.CacheRequest
    $cache.Add([System.Windows.Automation.AutomationElement]::NameProperty)
    $cache.TreeScope=[System.Windows.Automation.TreeScope]::Element
    $cache.Push()
    try {
        $elements=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
    } catch {
        if (-not (Test-Stale $_)) { throw }
        return @()  # The tree changes while a page switches; poll again.
    } finally { $cache.Pop() }
    return @($elements | Where-Object { $_.Cached.Name -match $pattern })
}
function Activate-First($pattern, $label) {
    $deadline=$clock.ElapsedMilliseconds+8000
    do {
        foreach ($element in (Find-Named $pattern)) {
            try {
                if (Activate-Element $element) { Step $label; return $true }
            } catch { if (-not (Test-Stale $_)) { throw } }
        }
        Start-Sleep -Milliseconds 250
    } while ($clock.ElapsedMilliseconds -lt $deadline)
    Step "$label-not-found"
    return $false
}
$performance=('^(Performance|{0})$' -f ([string][char]0x6027+[char]0x80fd))
if (-not (Activate-First $performance 'performance')) { exit 2 }
if (-not (Activate-First '^CPU(\s|$)' 'cpu')) { exit 3 }
exit 0
