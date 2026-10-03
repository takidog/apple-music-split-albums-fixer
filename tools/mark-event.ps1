param(
    [Parameter(Mandatory = $true)][string]$Event,
    [string]$Note = ''
)
$researchRoot = Split-Path -Parent $PSScriptRoot
$activeFile = Join-Path $researchRoot 'observations\active-capture.json'
if (-not (Test-Path -LiteralPath $activeFile)) { throw 'No active capture session.' }
$active = Get-Content -LiteralPath $activeFile -Raw | ConvertFrom-Json
$record = [ordered]@{
    time = (Get-Date).ToString('o')
    session = $active.label
    event = 'manual_marker'
    marker = $Event
    note = $Note
}
($record | ConvertTo-Json -Compress) | Add-Content -LiteralPath $active.event_file -Encoding utf8
$record | ConvertTo-Json
