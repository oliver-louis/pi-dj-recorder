#!/usr/bin/env bash
set -euo pipefail

echo "==> Restarting Pi recorder services"
sudo systemctl restart pi-recorder.service
sudo systemctl restart pi-prolink-onair.service

echo "==> Service status"
systemctl --no-pager --full status pi-recorder.service pi-prolink-onair.service
