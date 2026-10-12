#!/usr/bin/env bash
# End-to-end smoke test against the local mock page (no network, no real account).
set -euo pipefail
cd "$(dirname "$0")"
export P2G_PELOTON__EMAIL="${P2G_PELOTON__EMAIL:-smoke@example.com}"
export P2G_PELOTON__PASSWORD="${P2G_PELOTON__PASSWORD:-not-a-real-password}"
export STUDIO_STORAGE_STATE="$(mktemp -d)/auth.json"
export STUDIO_OUT_DIR="${STUDIO_OUT_DIR:-./out-smoke}"
rm -rf "$STUDIO_OUT_DIR"
MOCK="file://$PWD/mock/index.html"

echo "== dry run, Book button appears 4s after start =="
python3 book_studio_class.py --now --dry-run --discover --site-url "$MOCK?release_in=4" \
  --discipline Tread --class-time "7:00 AM" --poll-seconds 15 --poll-interval 0.5
echo "== waitlist fallback on a sold-out class =="
python3 book_studio_class.py --now --waitlist --site-url "$MOCK" --class-time "9:00 AM" --poll-seconds 10
echo "== sold out, waitlist disabled: expect exit 2 =="
if python3 book_studio_class.py --now --site-url "$MOCK" --class-time "9:00 AM" --poll-seconds 2; then
  echo "expected exit 2"; exit 1
fi
echo "smoke test OK (artifacts in $STUDIO_OUT_DIR)"
