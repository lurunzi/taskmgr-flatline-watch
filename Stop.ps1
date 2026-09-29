$ErrorActionPreference = 'Stop'
Set-Content -LiteralPath (Join-Path $PSScriptRoot '.local\stop.request') -Value 'stop' -Encoding ASCII
