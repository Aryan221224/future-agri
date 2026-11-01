<#
.SYNOPSIS
  Connect a GPU server (the H200) to the Future Agri console running on this computer.

.DESCRIPTION
  Opens ONE SSH session to the server that
    1. copies the worker code (and a one-time copy of the worker token) over the session,
    2. tunnels the server's port 18099 back to this console (ssh -R), and
    3. starts the worker there (in the NVIDIA PyTorch container if Docker has it, else a venv).
  You type the server password when SSH asks: this script never sees or stores it.
  Keep this window open while the server works; close it (or press Ctrl+C) to disconnect.
  The worker on the server stops by itself two minutes after the session closes.

.EXAMPLE
  .\Connect-GPU-Server.ps1 -Server paawan_kumar@h200.example.org

.EXAMPLE
  .\Connect-GPU-Server.ps1 -Server me@gpu-box -Port 2222 -TunnelPort 18100 -Gpu 3
#>
param(
  [string]$Server,
  [int]$Port = 22,
  [int]$ConsolePort = 8099,
  [int]$TunnelPort = 18099,
  [string]$Kinds = 'perfect,mission,detect',
  [string]$Gpu = 'auto',
  [ValidateSet('auto', 'docker', 'venv')][string]$Mode = 'auto'
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$State = Join-Path $env:LOCALAPPDATA 'FutureAgri'
$Remember = Join-Path $State 'gpu-server.txt'

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }

# -- which server ----------------------------------------------------------------------------
if (-not $Server -and (Test-Path $Remember)) { $last = (Get-Content $Remember -Raw).Trim() } else { $last = '' }
if (-not $Server) {
  $prompt = 'GPU server to connect (user@host)'
  if ($last) { $prompt += " [$last]" }
  $Server = (Read-Host $prompt).Trim()
  if (-not $Server) { $Server = $last }
}
if ($Server -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$') {
  Say "'$Server' is not user@host (for example paawan_kumar@h200.example.org)." Red; exit 1
}
if ($Kinds -notmatch '^[a-z,]+$' -or $Gpu -notmatch '^(auto|none|\d+)$') { Say 'Bad -Kinds or -Gpu value.' Red; exit 1 }
New-Item -ItemType Directory -Force -Path $State | Out-Null
Set-Content -Path $Remember -Value $Server -Encoding ascii

# -- the console must be running here ----------------------------------------------------------
try {
  $wc = New-Object System.Net.WebClient
  $wc.Proxy = $null                                   # loopback: never through a proxy
  $info = $wc.DownloadString("http://127.0.0.1:$ConsolePort/api/workers/token") | ConvertFrom-Json
} catch {
  Say "The Future Agri console is not running on this computer (port $ConsolePort)." Red
  Say 'Start it first with scripts\console\Start-FutureAgri.cmd, then run this again.'
  exit 1
}

foreach ($tool in 'ssh.exe', 'tar.exe') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
    Say "$tool was not found. Turn on 'OpenSSH Client' in Windows Settings > Apps > Optional features." Red
    exit 1
  }
}

# -- the bundle: worker code + a one-time token file -----------------------------------------
# Keep this list in step with PYTHONPATH in worker_remote.sh.
$paths = @('packages/fa_core', 'services/twin', 'services/planner', 'services/gateway', 'services/loop',
           'services/farm', 'services/scan', 'services/drone', 'services/runner', 'services/console',
           'scripts/console/worker_remote.sh', 'scripts/console/requirements-worker.txt')
$stage = Join-Path $env:TEMP ('fa-worker-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
try {
  [System.IO.File]::WriteAllText((Join-Path $stage 'worker.token'), $info.token)
  $bundle = Join-Path $stage 'bundle.tgz'
  $tarArgs = @('-czf', $bundle, '--exclude', '__pycache__', '--exclude', '*.pyc', '--exclude', 'static',
               '--exclude', 'node_modules', '-C', $Root) + $paths + @('-C', $stage, 'worker.token')
  & tar.exe @tarArgs
  if ($LASTEXITCODE -ne 0) { Say 'Could not pack the worker code.' Red; exit 1 }
  $mb = [math]::Round((Get-Item $bundle).Length / 1MB, 1)

  # Remote command: no double quotes inside (Windows passes it as one argument).
  $remote = "umask 077; D=`$HOME/futureagri-worker; mkdir -p `$D && rm -rf `$D/services `$D/packages && " +
            "tar -xzf - -C `$D && sed -i 's/\r`$//' `$D/scripts/console/worker_remote.sh && " +
            "FA_TUNNEL_PORT=$TunnelPort FA_KINDS=$Kinds FA_GPU=$Gpu FA_WORKER_MODE=$Mode " +
            "exec bash `$D/scripts/console/worker_remote.sh"
  $sshArgs = "-p $Port -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=4 " +
             "-R ${TunnelPort}:127.0.0.1:${ConsolePort} $Server `"$remote`""

  Say ''
  Say "Connecting to $Server (worker code $mb MB)." Cyan
  Say 'Type the server password when SSH asks. Keep this window open while the GPU server works;'
  Say 'close it or press Ctrl+C to disconnect. The first start installs Python packages (a few minutes).'
  Say ''
  $p = Start-Process -FilePath 'ssh.exe' -ArgumentList $sshArgs -RedirectStandardInput $bundle -NoNewWindow -Wait -PassThru
  switch ($p.ExitCode) {
    0 { Say 'Disconnected.' }
    4 { Say 'The worker stopped: the session closed.' }
    255 {
      Say 'SSH could not connect: wrong password, the server is unreachable, or port' Red
      Say "$TunnelPort is already used on the server (try -TunnelPort 18100)." Red
    }
    default { Say "The worker ended with code $($p.ExitCode). The messages above say why." Yellow }
  }
  exit $p.ExitCode
} finally {
  Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
}
