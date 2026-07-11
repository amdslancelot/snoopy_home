#!/bin/sh
set -e

# If the Google service account JSON is supplied as base64 (from a secret),
# write it to a file and point the config var at it.
if [ -n "${GOOGLE_SA_JSON_B64:-}" ]; then
    printf '%s' "$GOOGLE_SA_JSON_B64" | base64 -d > /app/service_account.json
    export GOOGLE_SERVICE_ACCOUNT_JSON=/app/service_account.json
fi

exec python main.py
