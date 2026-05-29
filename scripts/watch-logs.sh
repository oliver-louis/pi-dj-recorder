#!/usr/bin/env bash
set -euo pipefail

journalctl -u pi-recorder.service -u pi-prolink-onair.service -f
