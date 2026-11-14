"""Listen to Celery task events and write them to the job ledger (IP-031).

Every `task-*` event of every worker (the catalog's and the services') goes through
`apps.tasks.ledger.apply_event`, one transaction and one span per event.

Celery events are pub/sub: an event nobody stores is gone. So an event this process cannot store
because the database is unreachable is kept in memory (bounded) and replayed before the next one;
only an event that fails for its own data is logged and dropped (#108). An exception never escapes
`handle_event`: that would end `recv.capture()` and lose every event until supervisord restarts it.
"""

import logging
import time
from collections import deque

from django import db
from django.core.management.base import BaseCommand
from django.db import transaction
from celery.events import EventReceiver
from kombu import Connection

from apps.tasks import ledger
from evil_flowers_catalog import profiling, telemetry
from evil_flowers_catalog.celery import app

logger = logging.getLogger(__name__)
_tracer = telemetry.get_tracer("apps.tasks.event_handler")

#: Events held while the database is unreachable; the oldest go first when full.
BUFFER_LIMIT = 20_000
#: Seconds between replay attempts while the database is down.
RETRY_SECONDS = 5.0

_CONNECTION_ERRORS = (db.OperationalError, db.InterfaceError)


class Command(BaseCommand):
    help = "Listen to Celery task events and store them in the job ledger"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.buffer = deque(maxlen=BUFFER_LIMIT)
        self._next_retry = 0.0
        self._outage_logged = False

    def handle_event(self, event):
        if not event.get("type", "").startswith("task-") or "uuid" not in event:
            return
        if self.buffer and not self._replay():
            self._hold(event)
            return
        if not self._store(event):
            self._hold(event)

    def _store(self, event) -> bool:
        """True when the event is done with (stored or dropped), False when the database is down."""
        try:
            with _tracer.start_as_current_span(f"celery.event {event['type']}"), transaction.atomic():
                ledger.apply_event(event)
            return True
        except _CONNECTION_ERRORS:
            self._reset_connections()
            if not self._outage_logged:
                logger.warning(
                    "jobs.listener_db_unavailable type=%s task_id=%s; holding events", event.get("type"), event["uuid"]
                )
                self._outage_logged = True
            return False
        except Exception as error:
            logger.exception(
                "Skipping Celery event: type=%s task_id=%s parent_id=%s",
                event.get("type"),
                event.get("uuid"),
                event.get("parent_id"),
            )
            if isinstance(error, db.DatabaseError):
                # A failed query may leave the connection unusable; the next event reconnects.
                db.close_old_connections()
            return True

    @staticmethod
    def _reset_connections():
        """Drop broken connections; the next query reconnects."""
        db.close_old_connections()
        for connection in db.connections.all():
            connection.close_if_unusable_or_obsolete()

    def _hold(self, event):
        if len(self.buffer) == self.buffer.maxlen:
            dropped = self.buffer[0]
            logger.error(
                "jobs.listener_buffer_full dropped type=%s task_id=%s", dropped.get("type"), dropped.get("uuid")
            )
        self.buffer.append(event)
        self._next_retry = time.monotonic() + RETRY_SECONDS

    def _replay(self) -> bool:
        """Store held events in order; False when the database is still down."""
        if time.monotonic() < self._next_retry:
            return False
        while self.buffer:
            if not self._store(self.buffer[0]):
                self._next_retry = time.monotonic() + RETRY_SECONDS
                return False
            self.buffer.popleft()
        if self._outage_logged:
            logger.warning("jobs.listener_db_back replayed held events")
            self._outage_logged = False
        return True

    def start_event_listener(self):
        with Connection(app.conf.broker_url) as connection:
            recv = EventReceiver(connection, handlers={"*": self.handle_event})
            recv.capture(limit=None, timeout=None, wakeup=True)

    def handle(self, *args, **options):
        # IP-019: this process is started by supervisord and never forks.
        # SIGTERM becomes SystemExit so `atexit` flushes the exporters.
        profiling.setup("event-handler")
        telemetry.setup("event-handler")
        telemetry.exit_on_sigterm()

        logger.info("Starting Celery event listener")
        self.start_event_listener()
