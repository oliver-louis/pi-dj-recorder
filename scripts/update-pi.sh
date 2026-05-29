#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Updating repository"
git pull --ff-only

echo "==> Installing Python dependencies"
if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
".venv/bin/pip" install --upgrade pip
".venv/bin/pip" install -r requirements.txt

if [[ -f "prolink-onair/pom.xml" ]]; then
  echo "==> Building Pro DJ Link sidecar"
  if ! command -v mvn >/dev/null 2>&1; then
    echo "mvn was not found. Install it with: sudo apt install -y maven openjdk-21-jdk-headless" >&2
    exit 1
  fi
  (cd prolink-onair && mvn -DskipTests package)
fi

echo "==> Installing systemd units"
sudo cp "$ROOT_DIR/systemd/pi-recorder.service" /etc/systemd/system/pi-recorder.service
sudo cp "$ROOT_DIR/systemd/pi-prolink-onair.service" /etc/systemd/system/pi-prolink-onair.service
sudo systemctl daemon-reload

echo "==> Restarting services"
sudo systemctl restart pi-recorder.service
sudo systemctl restart pi-prolink-onair.service

echo "==> Service status"
systemctl --no-pager --full status pi-recorder.service pi-prolink-onair.service
