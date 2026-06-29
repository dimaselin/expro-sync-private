#!/bin/bash
# ExPro sync — cron setup
# Runs every day at 6:00 AM

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CRON_LINE="0 6 * * * cd \"$SCRIPT_DIR\" && python3 run.py >> logs/cron.log 2>&1"

echo "Cron line to add:"
echo "$CRON_LINE"
echo ""
echo "To install: crontab -e and paste the line above"
echo "Or run: (crontab -l 2>/dev/null; echo \"$CRON_LINE\") | crontab -"
