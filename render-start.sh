#!/usr/bin/env bash
# Exit on error
set -o errexit

# Apply database migrations and ensure the cache table is present
python manage.py migrate --noinput
python manage.py createcachetable

# Start Gunicorn server
exec gunicorn taskpilot.wsgi:application --bind 0.0.0.0:$PORT
