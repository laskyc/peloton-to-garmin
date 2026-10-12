#!/usr/bin/env bash
# Cron / launchd wrapper. Loads ./.env (git-ignored) and runs the booker.
# Example crontab (machine in any timezone; the script computes Eastern time itself):
#   CRON_TZ=America/New_York
#   35 11 * * 4  /path/to/peloton-to-garmin/studio-booking/run.sh >> /path/to/peloton-to-garmin/studio-booking/logs/cron.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi
exec python3 book_studio_class.py "$@"
