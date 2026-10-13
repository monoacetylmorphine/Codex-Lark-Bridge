#!/bin/sh
set -eu

data_dir="${HOME}/.codex_bridge"
mkdir -p "$data_dir" "${CODEX_HOME:-${HOME}/.codex}/workspace"

# Both services use the same defaults and never overwrite mounted configuration, even on concurrent first startup.
python /opt/codex_bridge/configs/initialization.py

exec "$@"
