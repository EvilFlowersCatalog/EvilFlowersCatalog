#!/bin/sh

until PGPASSWORD=$DATABASE_PASSWORD psql -h "$DATABASE_HOST" -U "$DATABASE_USER" -c '\q'; do
  >&2 echo "Postgres is unavailable - sleeping"
  sleep 1
done

python3 manage.py migrate
python3 manage.py setup

supervisord -c /etc/supervisord.conf
