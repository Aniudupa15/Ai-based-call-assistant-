#!/usr/bin/env bash
# Sets up the PC server: virtual environment, packages, config, and a systemd
# service that starts at boot and restarts on failure.
#
#   ./deploy/install_server.sh            # full install with speech models
#   ./deploy/install_server.sh --dev      # fake models only (no GPU needed)
#   ./deploy/install_server.sh --no-service
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEV=0
SERVICE=1
for arg in "$@"; do
  case "$arg" in
    --dev) DEV=1 ;;
    --no-service) SERVICE=0 ;;
    *) echo "unknown option $arg"; exit 1 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }

say "Checking Python"
PY=python3
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ needed"' \
  || { echo "Install Python 3.11 or newer first."; exit 1; }

command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found (sudo apt install ffmpeg); recordings stay as WAV."

say "Creating server/.venv"
cd "$DIR/server"
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install --upgrade pip wheel >/dev/null

if [ "$DEV" = 1 ]; then
  pip install -r requirements-dev.txt
else
  if ! python -c 'import torch' 2>/dev/null; then
    echo "Installing PyTorch with CUDA. If this picks the wrong build, see https://pytorch.org/get-started/locally/"
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
  fi
  pip install -r requirements.txt
  pip install sounddevice pyserial websockets   # desk test, bench test, replay tool
fi

say "Config"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  # Fresh random tokens; copy them into firmware/main/config.h and the app.
  ESP_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(18))')
  APP_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(18))')
  sed -i "s/esp_token: \"change-me\"/esp_token: \"$ESP_TOKEN\"/; s/app_token: \"change-me-too\"/app_token: \"$APP_TOKEN\"/" config.yaml
  [ "$DEV" = 1 ] && sed -i 's/backend: local /backend: fake  /' config.yaml
  echo "Created server/config.yaml with new tokens:"
  echo "  esp_token: $ESP_TOKEN   (firmware/main/config.h ESP_TOKEN)"
  echo "  app_token: $APP_TOKEN   (Android app Settings)"
  echo "Edit owner name and number and the model names before starting."
else
  echo "server/config.yaml exists; left as it is."
fi

say "Running the test suite"
pip install -q pytest
python -m pytest -q -p no:warnings || { echo "Tests failed; fix before continuing."; exit 1; }

if [ "$SERVICE" = 1 ] && command -v systemctl >/dev/null; then
  say "Installing systemd service (needs sudo)"
  sed "s|@USER@|$USER|; s|@DIR@|$DIR|g" "$DIR/deploy/call-assistant.service" \
    | sudo tee /etc/systemd/system/call-assistant.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable call-assistant.service
  echo "Enabled. Start it with:  sudo systemctl start call-assistant"
  echo "Logs:                    journalctl -u call-assistant -f"
fi

say "Next: python ../tools/doctor.py --config config.yaml"
