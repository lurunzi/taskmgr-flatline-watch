$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'
# Forward options such as --query-trace to the watcher.
Start-Process -FilePath $python -ArgumentList (@('"' + $script + '"') + $args) -Verb RunAs -WindowStyle Hidden
