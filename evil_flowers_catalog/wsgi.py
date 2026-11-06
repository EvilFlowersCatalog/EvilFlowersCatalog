"""
WSGI config for the catalog.

It exposes the WSGI callable as a module-level variable named ``application``.

OpenTelemetry (IP-019) is set up here because this module is the first project
code a gunicorn worker runs after it has forked and after gevent has patched it:
`GeventWorker.init_process()` calls `patch()` (`monkey.patch_all()`), then
`hub.reinit()`, then `Worker.init_process()`, which ends with `load_wsgi()` —
the import of this module (`preload_app` is off). The exporter threads and their
locks are therefore created green, in the process that uses them.

Order inside the module matters too:
1. `django.setup()` applies `LOGGING`, so the telemetry log lines are JSON.
2. `telemetry.setup()` installs the providers and the client instrumentations.
3. `instrument_django()` inserts the OpenTelemetry middleware into
   `settings.MIDDLEWARE`; the chain is built by `get_wsgi_application()`.
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evil_flowers_catalog.settings.development")

django.setup(set_prefix=False)

from evil_flowers_catalog import telemetry  # noqa: E402

if telemetry.setup("gunicorn"):
    telemetry.instrument_django()

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
