$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& '.\.venv\Scripts\python.exe' -m compileall -q src
exit $LASTEXITCODE
