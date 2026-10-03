param(
    [string]$Label = (Get-Date -Format 'yyyyMMdd-HHmmss-baseline'),
    [string]$TargetText = 'Album title',
    [switch]$HoldLibraryWrites
)

$ErrorActionPreference = 'Stop'
$researchRoot = Split-Path -Parent $PSScriptRoot
$mitmwebCommand = Get-Command mitmweb.exe -ErrorAction SilentlyContinue
if (-not $mitmwebCommand) { $mitmwebCommand = Get-Command mitmweb -ErrorAction SilentlyContinue }
$mitmweb = if ($mitmwebCommand) { $mitmwebCommand.Source } else { $null }
$addon = Join-Path $PSScriptRoot 'apple_music_observer.py'
$captureFile = Join-Path $researchRoot "captures\$Label.mitm"
$stdoutFile = Join-Path $researchRoot "observations\$Label.mitmweb.stdout.log"
$stderrFile = Join-Path $researchRoot "observations\$Label.mitmweb.stderr.log"
$pidFile = Join-Path $researchRoot 'observations\active-capture.json'
$writeHoldFile = Join-Path $researchRoot 'observations\library-write-hold.enabled'

if (-not $mitmweb -or -not (Test-Path -LiteralPath $mitmweb)) {
    throw 'mitmweb was not found on PATH. Install mitmproxy and reopen PowerShell.'
}
if (Test-Path -LiteralPath $pidFile) {
    $active = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    if (Get-Process -Id $active.pid -ErrorAction SilentlyContinue) {
        throw "Capture is already active (PID $($active.pid), label $($active.label))."
    }
}

if ($HoldLibraryWrites) {
    [ordered]@{
        enabled = (Get-Date).ToString('o')
        label = $Label
        scope = 'librarydaap /edit only'
    } | ConvertTo-Json | Set-Content -LiteralPath $writeHoldFile -Encoding utf8
} elseif (Test-Path -LiteralPath $writeHoldFile) {
    Remove-Item -LiteralPath $writeHoldFile
}

$oldRoot = $env:APPLE_MUSIC_RESEARCH_DIR
$oldLabel = $env:APPLE_MUSIC_SESSION_LABEL
$oldTargets = $env:APPLE_MUSIC_TARGETS
$env:APPLE_MUSIC_RESEARCH_DIR = $researchRoot
$env:APPLE_MUSIC_SESSION_LABEL = $Label
$env:APPLE_MUSIC_TARGETS = $TargetText

try {
    $arguments = @(
        '--mode', 'local:AppleMusic,AMPLibraryAgent',
        '--set', "save_stream_file=$captureFile",
        '--set', 'web_open_browser=false',
        '--set', 'web_port=8081',
        '--ignore-hosts', '^init\.push\.apple\.com$',
        '-s', $addon
    )
    if ($env:MITMWEB_PASSWORD) {
        $arguments = @('--set', "web_password=$($env:MITMWEB_PASSWORD)") + $arguments
    }
    $process = Start-Process -FilePath $mitmweb -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    Start-Sleep -Milliseconds 1200
    if ($process.HasExited) { throw "mitmweb exited with code $($process.ExitCode). See $stderrFile" }

    $active = [ordered]@{
        pid = $process.Id
        label = $Label
        started = (Get-Date).ToString('o')
        capture_file = $captureFile
        event_file = (Join-Path $researchRoot "observations\$Label.jsonl")
        web_ui = 'http://127.0.0.1:8081/'
        targets = $TargetText
        processes = @('AppleMusic', 'AMPLibraryAgent')
        hold_library_writes = [bool]$HoldLibraryWrites
    }
    $active | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    $active | ConvertTo-Json
}
finally {
    $env:APPLE_MUSIC_RESEARCH_DIR = $oldRoot
    $env:APPLE_MUSIC_SESSION_LABEL = $oldLabel
    $env:APPLE_MUSIC_TARGETS = $oldTargets
}
