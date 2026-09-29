$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $PSScriptRoot 'src\FlatlineWatch.py'
Start-Process -FilePath $python -ArgumentList ('"' + $script + '"') -Verb RunAs -WindowStyle Hidden
