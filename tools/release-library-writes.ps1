$ErrorActionPreference = 'Stop'
$researchRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $researchRoot 'observations\active-capture.json'
$writeHoldFile = Join-Path $researchRoot 'observations\library-write-hold.enabled'

if (-not (Test-Path -LiteralPath $pidFile)) {
    throw 'No active capture is running. Keep mitmweb active while releasing writes so the final sync is recorded.'
}
if (-not (Test-Path -LiteralPath $writeHoldFile)) {
    Write-Output 'Library writes are already released.'
    exit 0
}

$active = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $writeHoldFile
& (Join-Path $PSScriptRoot 'mark-event.ps1') -Event 'library_writes_released' -Note "Pending local changes may now sync in capture $($active.label)"
Write-Output "Released Cloud Library writes. Keep capture '$($active.label)' running until the revision delta completes."
