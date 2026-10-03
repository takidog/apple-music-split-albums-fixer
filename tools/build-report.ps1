param([string]$Label)
$ErrorActionPreference = 'Stop'
$researchRoot = Split-Path -Parent $PSScriptRoot
if (-not $Label) {
    $latest = Get-ChildItem -LiteralPath (Join-Path $researchRoot 'observations') -Filter '*.jsonl' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { throw 'No observation JSONL file found.' }
    $Label = $latest.BaseName
}
$source = Join-Path $researchRoot "observations\$Label.jsonl"
$target = Join-Path $researchRoot "reports\$Label-summary.md"
$rows = @()
if (Test-Path -LiteralPath $source) {
    $rows = @(Get-Content -LiteralPath $source | ForEach-Object { $_ | ConvertFrom-Json })
}
$requests = @($rows | Where-Object event -eq 'request')
$markers = @($rows | Where-Object event -eq 'manual_marker')
$groups = $requests | Group-Object host,method,path | Sort-Object Count -Descending
$writes = @($requests | Where-Object write_candidate)
$targets = @($rows | Where-Object { $_.body.target_hits.Count -gt 0 -or $_.request_body.target_hits.Count -gt 0 })

$lines = [System.Collections.Generic.List[string]]::new()
$lines.Add("# Capture summary: $Label")
$lines.Add('')
$lines.Add("- Requests: $($requests.Count)")
$lines.Add("- Write candidates: $($writes.Count)")
$lines.Add("- Target-title hits: $($targets.Count)")
$lines.Add('')
$lines.Add('## Manual markers')
$lines.Add('')
$lines.Add('| Time | Marker | Note |')
$lines.Add('|---|---|---|')
foreach ($item in $markers) { $lines.Add("| $($item.time) | $($item.marker) | $($item.note -replace '\|','\\|') |") }
$lines.Add('')
$lines.Add('## Endpoints')
$lines.Add('')
$lines.Add('| Count | Host | Method | Path |')
$lines.Add('|---:|---|---|---|')
foreach ($group in $groups) {
    $first = $group.Group[0]
    $lines.Add("| $($group.Count) | $($first.host) | $($first.method) | $($first.path -replace '\|','\\|') |")
}
$lines.Add('')
$lines.Add('## Potential writes')
$lines.Add('')
$lines.Add('| Time | Host | Method | Path | Reasons | Body SHA-256 |')
$lines.Add('|---|---|---|---|---|---|')
foreach ($item in $writes) {
    $lines.Add("| $($item.time) | $($item.host) | $($item.method) | $($item.path -replace '\|','\\|') | $($item.write_reasons -join ', ') | $($item.body.sha256) |")
}
$lines | Set-Content -LiteralPath $target -Encoding utf8
Write-Output $target
