# Windows set-up for the PC server: virtual environment, packages, config with
# fresh tokens, a test run, and (optionally) a scheduled task that starts the
# server when Windows starts.
#
#   powershell -ExecutionPolicy Bypass -File deploy\install_server.ps1 -Dev        # fake models, no GPU
#   powershell -ExecutionPolicy Bypass -File deploy\install_server.ps1             # full install
#   powershell -ExecutionPolicy Bypass -File deploy\install_server.ps1 -Service    # also start at boot (run as Administrator)
param(
    [switch]$Dev,
    [switch]$Service
)
# Not "Stop": Windows PowerShell 5.1 turns any text a program writes to stderr (pip
# warnings, a failed import check) into a fatal error. Exit codes are checked instead.
$ErrorActionPreference = "Continue"

function Check($what) {
    if ($LASTEXITCODE -ne 0) { Write-Host "FAILED: $what (exit code $LASTEXITCODE)" -ForegroundColor Red; exit 1 }
}
$Root = Split-Path -Parent $PSScriptRoot
$ServerDir = Join-Path $Root "server"

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

Say "Checking Python"
$py = $null
foreach ($cand in @("py -3.12", "py -3.11", "python")) {
    $exe, $arg = $cand.Split(" ", 2)
    try {
        $ok = & $exe $arg -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($ok -eq "True") { $py = $cand; break }
    } catch { }
}
if (-not $py) {
    Write-Host "Python 3.11 or 3.12 is needed: https://www.python.org/downloads/ (tick 'Add python.exe to PATH')." -ForegroundColor Red
    exit 1
}
Write-Host "Using $py"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: ffmpeg not found (winget install Gyan.FFmpeg); recordings stay as WAV." -ForegroundColor Yellow
}

Say "Creating server\.venv"
Set-Location $ServerDir
if (-not (Test-Path ".venv")) {
    $exe, $arg = $py.Split(" ", 2)
    if ($arg) { & $exe $arg -m venv .venv } else { & $exe -m venv .venv }
    Check "creating the virtual environment"
}
$venvPy = Join-Path $ServerDir ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip wheel | Out-Null

if ($Dev) {
    & $venvPy -m pip install -r requirements-dev.txt
    Check "installing requirements-dev.txt"
} else {
    $hasTorch = & $venvPy -c "import importlib.util; print(importlib.util.find_spec('torch') is not None)"
    if ($hasTorch -ne "True") {
        Write-Host "Installing PyTorch with CUDA. If this picks the wrong build, see https://pytorch.org/get-started/locally/"
        & $venvPy -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
        Check "installing PyTorch"
    }
    & $venvPy -m pip install -r requirements.txt
    Check "installing requirements.txt"
    & $venvPy -m pip install sounddevice pyserial websockets
    Check "installing the tool packages"
    $cuda = & $venvPy -c "import torch; print(torch.cuda.is_available())"
    Write-Host "PyTorch sees the GPU: $cuda"
}
& $venvPy -m pip install -q pytest

Say "Config"
if (-not (Test-Path "config.yaml")) {
    $esp = & $venvPy -c "import secrets; print(secrets.token_urlsafe(18))"
    $app = & $venvPy -c "import secrets; print(secrets.token_urlsafe(18))"
    $text = Get-Content "config.example.yaml" -Raw
    $text = $text.Replace('esp_token: "change-me"', "esp_token: `"$esp`"").Replace('app_token: "change-me-too"', "app_token: `"$app`"")
    if ($Dev) { $text = $text.Replace("backend: local ", "backend: fake  ") }
    Set-Content -Path "config.yaml" -Value $text -Encoding UTF8
    Write-Host "Created server\config.yaml with new tokens:"
    Write-Host "  esp_token: $esp   (firmware\main\config.h ESP_TOKEN)" -ForegroundColor Green
    Write-Host "  app_token: $app   (Android app Settings)" -ForegroundColor Green
    Write-Host "Edit owner name and number and the model names before starting."
} else {
    Write-Host "server\config.yaml exists; left as it is."
}

Say "Running the test suite"
& $venvPy -m pytest -q -p no:warnings
if ($LASTEXITCODE -ne 0) { Write-Host "Tests failed; fix before continuing." -ForegroundColor Red; exit 1 }

if ($Service) {
    Say "Scheduling the server to start with Windows (needs Administrator)"
    $action = New-ScheduledTaskAction -Execute $venvPy -Argument "-m app.main --config `"$ServerDir\config.yaml`"" -WorkingDirectory $ServerDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName "CallAssistant" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
    Write-Host "Registered task 'CallAssistant'. Start now with:  Start-ScheduledTask CallAssistant"
    Write-Host "Allow the port through the firewall (private network only):"
    Write-Host "  New-NetFirewallRule -DisplayName 'Call assistant' -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow"
}

Say "Next:  .venv\Scripts\python ..\tools\doctor.py --config config.yaml"
