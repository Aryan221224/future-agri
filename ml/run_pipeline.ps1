# Future Agri perception: data -> train -> evaluate -> export, on the lab PC.
#
#   cd C:\Users\aryan\Documents\FutureAgri\futureagri
#   powershell -ExecutionPolicy Bypass -File ml\run_pipeline.ps1                 # RF-DETR-Medium
#   powershell -ExecutionPolicy Bypass -File ml\run_pipeline.ps1 -Size small -Trt
#
# Everything is logged to ml\runs\<name>\pipeline_log.txt -- send that file back.
param(
    [ValidateSet("nano", "small", "medium", "large")] [string]$Size = "medium",
    [string]$Name = "",
    [int]$Epochs = 150,
    [string]$BatchSize = "auto",
    [switch]$Trt,
    [switch]$SkipData,
    [switch]$SkipTrain
)
$ErrorActionPreference = "Stop"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repo
$py = Join-Path (Split-Path -Parent $repo) ".venv-fa\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Python venv not found at $py -- run setup_pc.ps1 first" }
if ($Name -eq "") { $Name = "wg-$Size" }
$data = Join-Path (Split-Path -Parent $repo) "data\weedsgalore_coco"
$run = Join-Path $repo "ml\runs\$Name"
New-Item -ItemType Directory -Force -Path $run | Out-Null
$log = Join-Path $run "pipeline_log.txt"
"`n##### $(Get-Date -Format s)  size=$Size name=$Name epochs=$Epochs trt=$Trt" | Out-File -FilePath $log -Append -Encoding utf8

function Invoke-Step([string]$title, [string[]]$pyArgs) {
    Write-Host "`n=== $title ===" -ForegroundColor Cyan
    "`n=== $title ===" | Out-File -FilePath $log -Append -Encoding utf8
    $t0 = Get-Date
    # Python writes warnings to stderr; keep going on those and judge by the exit code.
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $py @pyArgs 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        $line | Out-File -FilePath $log -Append -Encoding utf8
    }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $old
    if ($code -ne 0) { throw "$title failed (exit $code). Send this file: $log" }
    $msg = "    {0} done in {1:N1} min" -f $title, ((Get-Date) - $t0).TotalMinutes
    Write-Host $msg
    $msg | Out-File -FilePath $log -Append -Encoding utf8
}

Invoke-Step "GPU check" @("-c", "import torch,sys; ok=torch.cuda.is_available(); print('CUDA', ok, torch.version.cuda, torch.cuda.get_device_name(0) if ok else ''); sys.exit(0 if ok else 3)")
if (-not $SkipData) {
    Invoke-Step "1/4 data (download 321 MB + convert)" @("ml\prepare_data.py")
}
if (-not $SkipTrain) {
    Invoke-Step "2/4 train RF-DETR-$Size" @("ml\train.py", "--data", $data, "--size", $Size, "--name", $Name,
                                           "--epochs", "$Epochs", "--batch-size", $BatchSize)
}
Invoke-Step "3/4 evaluate + calibrate" @("ml\evaluate.py", "--run", $run)
$exportArgs = @("ml\export.py", "--run", $run)
if ($Trt) { $exportArgs += "--trt" }
Invoke-Step "4/4 export + latency" $exportArgs
Write-Host "`nResults: ml\results\$Name  (report.md, MODEL_CARD.md, coverage.png, shift.png, latency.json)" -ForegroundColor Green
Write-Host "Log: $log"
