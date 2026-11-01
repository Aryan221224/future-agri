<#
.SYNOPSIS
  Start the Future Agri Mission Console on this Windows computer.

.DESCRIPTION
  The first run creates a Python environment in .venv and installs the packages (a few
  minutes, needs internet). Later runs start in seconds. The console opens in your browser
  at http://127.0.0.1:8099 and is reachable only from this computer. Close this window, or
  press Ctrl+C, to stop it. Your farms and plans are kept in %USERPROFILE%\.futureagri.

.EXAMPLE
  .\Start-FutureAgri.ps1
.EXAMPLE
  .\Start-FutureAgri.ps1 -Port 8100
#>
param([int]$Port = 8099)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$Req = Join-Path $Root 'services\console\requirements.txt'
$ReqWorker = Join-Path $Root 'scripts\console\requirements-worker.txt'
$Url = "http://127.0.0.1:$Port/"

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

# Already running? Just open it.
try {
  $wc = New-Object System.Net.WebClient
  $wc.Proxy = $null
  $null = $wc.DownloadString("${Url}api/health")
  Say "The console is already running: opening $Url" Cyan
  Start-Process $Url
  exit 0
} catch { }

# A Python 3.10+ to build the environment with (the py launcher first, then python on PATH).
if (-not (Test-Path $VenvPy)) {
  $base = $null
  foreach ($spec in 'py -3.12', 'py -3.11', 'py -3.10', 'py -3', 'python') {
    $parts = @($spec -split ' ')
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $pyArgs = @($parts | Select-Object -Skip 1)
    & $exe @pyArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>$null
    if ($LASTEXITCODE -eq 0) { $base = $parts; break }
  }
  if (-not $base) {
    Say 'Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/' Red
    Say '(tick "Add python.exe to PATH"), then run this again.' Red
    exit 1
  }
  Say 'First start: creating the Python environment (a few minutes)...' Cyan
  $exe = $base[0]; $pyArgs = @($base | Select-Object -Skip 1) + @('-m', 'venv', $Venv)
  & $exe @pyArgs
  if ($LASTEXITCODE -ne 0) { Say 'Could not create the Python environment.' Red; exit 1 }
}

# Install or update packages when the requirement lists change.
$stamp = Join-Path $Venv '.fa-requirements'
$want = (Get-FileHash $Req).Hash + (Get-FileHash $ReqWorker).Hash
$have = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }
if ($have -ne $want) {
  Say 'Installing packages...' Cyan
  # --no-cache-dir: a locked or stale entry in pip's own cache otherwise fails the install
  & $VenvPy -m pip install -q --disable-pip-version-check --no-cache-dir -r $Req
  if ($LASTEXITCODE -ne 0) { Say 'Package installation failed (the reason is above). Fix that, then run this again.' Red; exit 1 }
  Set-Content -Path $stamp -Value $want -Encoding ascii
}

$cache = Join-Path $Root 'data_cache'
if ((Test-Path $cache) -and -not $env:FA_DATA_CACHE) { $env:FA_DATA_CACHE = $cache }
$env:PYTHONPATH = Join-Path $Root 'services\console'
$env:FA_PORT = "$Port"
Say "Future Agri Mission Console: $Url  (close this window to stop)" Green
& $VenvPy -m fa_console --open --port $Port
