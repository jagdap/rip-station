#!/bin/sh
set -e
# First run: drop in a default config the owner can edit at <config volume>/config.toml.
[ -f "$RIPSTATION_CONFIG" ] || cp /app/config.default.toml "$RIPSTATION_CONFIG"

echo "rip-station: optical drives visible to the container:"
ls -l /dev/sr* /dev/sg* 2>/dev/null || echo "  none (plug a USB DVD drive into the NAS; check /dev is mapped)"
exec "$@"
