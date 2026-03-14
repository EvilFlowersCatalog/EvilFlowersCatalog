"""Gunicorn configuration for gevent workers."""

# Worker
worker_class = "gevent"
workers = 4
timeout = 240

# Bind
bind = "0.0.0.0:8000"