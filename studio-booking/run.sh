#!/usr/bin/env bash
# Cron / launchd wrapper. Loads ./.env (not committed) and runs the booker,
# appending output to logs/. Example crontab (local machine, Eastern time):
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
