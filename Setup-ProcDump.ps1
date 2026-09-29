$ErrorActionPreference = 'Stop'
$local = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $local -Force | Out-Null
$zip = Join-Path $local 'Procdump.zip'
Invoke-WebRequest -Uri 'https://download.sysinternals.com/files/Procdump.zip' -OutFile $zip
Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $local 'procdump') -Force
$signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $local 'procdump\procdump64.exe')
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*O=Microsoft Corporation*') {
    throw 'ProcDump signature verification failed.'
}
