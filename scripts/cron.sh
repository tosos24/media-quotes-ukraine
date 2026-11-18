#!/usr/bin/env sh
# Linux/macOS cron entry point. Example crontab line (06:00 UTC daily):
#   0 6 * * * /path/to/repo/scripts/cron.sh >> /path/to/repo/logs/cron.out 2>&1
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python -m mqcs daily
