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
$ErrorActionPreference = "Stop"
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
}
$venvPy = Join-Path $ServerDir ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip wheel | Out-Null

if ($Dev) {
    & $venvPy -m pip install -r requirements-dev.txt
} else {
    & $venvPy -c "import torch" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing PyTorch with CUDA. If this picks the wrong build, see https://pytorch.org/get-started/locally/"
        & $venvPy -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
    }
    & $venvPy -m pip install -r requirements.txt
    & $venvPy -m pip install sounddevice pyserial websockets
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
