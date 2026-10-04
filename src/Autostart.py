"""Enable or disable this installation's logon scheduled task without unregistering it."""
import re
import subprocess

from AutomaticWatch import ROOT

TASK = 'TaskmgrFlatlineWatch'
SCRIPT = str(ROOT / 'src' / 'FlatlineWatch.py')

def _schtasks(*args):
    return subprocess.run(['schtasks', *args], capture_output=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW, check=False)

def state():
    """Return True/False for this installation's task, or None when absent or owned elsewhere."""
    try:
        result = _schtasks('/Query', '/TN', TASK, '/XML')
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    xml = result.stdout.decode('oem', errors='replace')
    arguments = re.search(r'<Arguments>(.*?)</Arguments>', xml, re.S)
    if not arguments or arguments.group(1).strip().strip('"').lower() != SCRIPT.lower():
        return None
    settings = re.search(r'<Settings>(.*?)</Settings>', xml, re.S)
    return not (settings and re.search(r'<Enabled>\s*false\s*</Enabled>', settings.group(1)))

def set_enabled(enabled):
    """Switch the task; the running watcher is not stopped. Returns the resulting state."""
    if state() is None:
        return None
    try:
        _schtasks('/Change', '/TN', TASK, '/ENABLE' if enabled else '/DISABLE')
    except (OSError, subprocess.TimeoutExpired):
        pass
    return state()
