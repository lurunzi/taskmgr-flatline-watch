param(
    [Parameter(Mandatory=$true)][int]$TargetPid,
    [string]$DiagnoseFolder
)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WindowFinderUtil {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern int GetClassName(IntPtr hWnd, StringBuilder lpClassName, int nMaxCount);

    public static IntPtr FindTaskmgrWindow(int targetPid) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((hWnd, lParam) => {
            uint pid;
            GetWindowThreadProcessId(hWnd, out pid);
            if (pid == (uint)targetPid) {
                StringBuilder sb = new StringBuilder(256);
                GetClassName(hWnd, sb, 256);
                if (sb.ToString() == "TaskManagerWindow") {
                    found = hWnd;
                    return false;
                }
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }
}
'@ -ErrorAction SilentlyContinue

$clock=[System.Diagnostics.Stopwatch]::StartNew()
function Step($label) { [Console]::Out.WriteLine(('{0} {1}ms' -f $label,$clock.ElapsedMilliseconds)) }
$process=Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
if (-not $process -or $process.ProcessName -ne 'Taskmgr') { throw 'Target is not Task Manager' }

# A freshly launched Task Manager may not have published its main window yet.
$targetHwnd = [IntPtr]::Zero
while ($clock.ElapsedMilliseconds -lt 10000) {
    if ($process.MainWindowHandle -and $process.MainWindowHandle -ne [IntPtr]::Zero) {
        $targetHwnd = $process.MainWindowHandle
        break
    }
    try {
        $candidate = [WindowFinderUtil]::FindTaskmgrWindow($TargetPid)
        if ($candidate -and $candidate -ne [IntPtr]::Zero) {
            $targetHwnd = $candidate
            break
        }
    } catch {}
    Start-Sleep -Milliseconds 250
    $process.Refresh()
}
if ($targetHwnd -eq [IntPtr]::Zero) { throw 'Task Manager window is unavailable' }
$root=[System.Windows.Automation.AutomationElement]::FromHandle($targetHwnd)
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
function Save-Diagnostics($exitCode) {
    if (-not $DiagnoseFolder) { return }
    try {
        if (-not (Test-Path -LiteralPath $DiagnoseFolder)) { return }
        $diagPath = Join-Path $DiagnoseFolder 'navigation-uia-tree.json'
        $cache = New-Object System.Windows.Automation.CacheRequest
        $cache.Add([System.Windows.Automation.AutomationElement]::NameProperty)
        $cache.Add([System.Windows.Automation.AutomationElement]::AutomationIdProperty)
        $cache.Add([System.Windows.Automation.AutomationElement]::ClassNameProperty)
        $cache.Add([System.Windows.Automation.AutomationElement]::ControlTypeProperty)
        $cache.TreeScope = [System.Windows.Automation.TreeScope]::Element
        $cache.Push()
        try {
            $allElements = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        } catch {
            $allElements = @()
        } finally {
            $cache.Pop()
        }
        $sample = @()
        $idx = 0
        foreach ($el in $allElements) {
            $idx++
            if ($idx -gt 150) { break }
            $sample += @{
                Name = $el.Cached.Name
                AutomationId = $el.Cached.AutomationId
                ClassName = $el.Cached.ClassName
                ControlType = if ($el.Cached.ControlType) { $el.Cached.ControlType.ProgrammaticName } else { $null }
            }
        }
        $summary = @{
            exit_code = $exitCode
            target_pid = $TargetPid
            timestamp = (Get-Date).ToUniversalTime().ToString('o')
            elements_count = $allElements.Count
            elements_sample = $sample
        }
        $json = $summary | ConvertTo-Json -Depth 4
        [System.IO.File]::WriteAllText($diagPath, $json, [System.Text.Encoding]::UTF8)
    } catch {}
}

$performance=('^(Performance|{0})$' -f ([string][char]0x6027+[char]0x80fd))
if (-not (Activate-First $performance 'performance')) {
    Save-Diagnostics 2
    exit 2
}

$cpuPattern = '^(CPU(\s|$)|中央处理器|Cpu)'
$foundCpu = $false
$cpuDeadline = $clock.ElapsedMilliseconds + 8000
while ($clock.ElapsedMilliseconds -lt $cpuDeadline) {
    foreach ($element in (Find-Named $cpuPattern)) {
        try {
            if (Activate-Element $element) {
                Step 'cpu'
                $foundCpu = $true
                break
            }
        } catch { if (-not (Test-Stale $_)) { throw } }
    }
    if ($foundCpu) { break }
    if ($clock.ElapsedMilliseconds -gt ($cpuDeadline - 5500)) {
        foreach ($pEl in (Find-Named $performance)) {
            try { Activate-Element $pEl | Out-Null } catch {}
        }
    }
    Start-Sleep -Milliseconds 250
}
if (-not $foundCpu) {
    Step 'cpu-not-found'
    Save-Diagnostics 3
    exit 3
}
exit 0
