#!/bin/sh

python manage.py migrate

supervisord -c /etc/supervisord.conf
