$ErrorActionPreference = 'Stop'
$researchRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $researchRoot 'observations\active-capture.json'
$writeHoldFile = Join-Path $researchRoot 'observations\library-write-hold.enabled'
if (-not (Test-Path -LiteralPath $pidFile)) { throw 'No active capture record was found.' }
if (Test-Path -LiteralPath $writeHoldFile) {
    throw 'Library writes are still held. Run .\tools\release-library-writes.ps1 and allow the pending sync before stopping capture.'
}
$active = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$process = Get-Process -Id $active.pid -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $active.pid
    Wait-Process -Id $active.pid -ErrorAction SilentlyContinue
}
$active | Add-Member -NotePropertyName stopped -NotePropertyValue (Get-Date).ToString('o') -Force
$archive = Join-Path $researchRoot "observations\$($active.label).session.json"
$active | ConvertTo-Json | Set-Content -LiteralPath $archive -Encoding utf8
Remove-Item -LiteralPath $pidFile
Write-Output "Stopped capture '$($active.label)'."
