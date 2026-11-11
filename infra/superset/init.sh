#!/bin/bash
# First start of the real Superset container: migrate metadata DB, create admin, serve.
set -euo pipefail
superset db upgrade
superset fab create-admin \
  --username "${SUPERSET_ADMIN_USERNAME}" --password "${SUPERSET_ADMIN_PASSWORD}" \
  --firstname Demo --lastname Admin --email admin@example.com || true
superset init
exec gunicorn --bind 0.0.0.0:8088 --workers 2 --timeout 120 "superset.app:create_app()"
