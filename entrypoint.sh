#!/bin/sh
set -e

# If the Google service account JSON is supplied as base64 (from a secret),
# write it to a file and point the config var at it.
if [ -n "${GOOGLE_SA_JSON_B64:-}" ]; then
    if printf '%s' "$GOOGLE_SA_JSON_B64" | base64 -d > /app/service_account.json 2>/dev/null; then
        export GOOGLE_SERVICE_ACCOUNT_JSON=/app/service_account.json
    else
        echo "[entrypoint] WARNING: GOOGLE_SA_JSON_B64 is set but failed to decode — Google Calendar disabled"
        rm -f /app/service_account.json
    fi
fi

exec python main.py
