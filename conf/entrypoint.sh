#!/bin/sh

# This works because we use libpq compatible environment variables
# https://www.postgresql.org/docs/current/libpq-envars.html
until psql -c '\q'; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

python3 manage.py migrate
python3 manage.py setup
python3 manage.py entry_defaults

supervisord -c /etc/supervisord.conf
