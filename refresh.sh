#!/bin/bash
# launchd wrapper for refresh.py.
#
# Python runs as a child of /bin/bash (no exec), so it inherits whatever folder access you have already granted
# to bash in System Settings, Privacy & Security. If refresh.log shows "Operation not permitted", grant access
# to the exports folder there or move the exports to a folder the job can read. See the README.
#
# Settings: the plist's EnvironmentVariables, or an optional env file of KEY=value lines
# (default ~/.config/linkedin-network-map/env, override with LINKEDIN_MAP_ENV_FILE).
HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${LINKEDIN_MAP_ENV_FILE:-$HOME/.config/linkedin-network-map/env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
cd "$HERE" || exit 1
"${LINKEDIN_MAP_PYTHON:-/usr/bin/python3}" -I "$HERE/refresh.py" "$@"
exit $?
