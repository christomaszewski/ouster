#!/usr/bin/env bash
# The supervisor owns transitions; this client never configures the sensor.
set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SCRIPT_DIR/operational_state.py" health
