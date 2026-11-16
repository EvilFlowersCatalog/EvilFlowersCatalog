import logging
import sys
import socket
import xmlrpc.client
from http import HTTPStatus
from http.client import HTTPConnection
from typing import Callable

from django.conf import settings
from django.db import connection
from django.utils import timezone
from django.views import View

from apps import openapi
from apps.api.response import SingleResponse
from apps.api.serializers.status import (
    StatusClientIP,
    StatusStatistics,
    StatusSerializer,
)
from apps.files.storage import get_storage
from apps.files.views import _get_client_ip
from apps.core.errors import ProblemDetailException
from apps.core.models import Catalog, Entry, Acquisition, User

logger = logging.getLogger(__name__)


class UnixStreamHTTPConnection(xmlrpc.client.Transport):
    def __init__(self, socket_path, use_datetime=False):
        super().__init__(use_datetime=use_datetime)
        self.socket_path = socket_path

    def make_connection(self, host):
        # Custom HTTPConnection to use the Unix socket
        class UnixSocketHTTPConnection(HTTPConnection):
            def __init__(self, host, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
                super().__init__(host, timeout=timeout)
                self.socket_path = self.sock = None

            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(self.socket_path)

        connection = UnixSocketHTTPConnection("localhost")
        connection.socket_path = self.socket_path
        return connection


def check_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def check_broker() -> None:
    """One connection to the Celery broker, bounded by `EVILFLOWERS_STATUS_TIMEOUT` (no kombu retry loop).

    `ensure_connection(timeout=...)` alone does not bound the socket connect: an unreachable Redis hung the check.
    """
    from evil_flowers_catalog.celery import app

    timeout = settings.EVILFLOWERS_STATUS_TIMEOUT
    with app.connection_for_write(
        connect_timeout=timeout, transport_options={"socket_timeout": timeout, "socket_connect_timeout": timeout}
    ) as broker:
        broker.ensure_connection(max_retries=1, interval_start=0, interval_step=0, timeout=timeout)


def check_storage() -> None:
    """List the storage root: the data directory, or the bucket's top level on S3."""
    get_storage().listdir("")


#: Dependencies every runtime has (#195): the web pod on k8s and the Compose container alike.
CHECKS: dict[str, Callable[[], None]] = {
    "database": check_database,
    "broker": check_broker,
    "storage": check_storage,
}


def run_checks() -> dict[str, str]:
    """`{name: "ok" | "failed: <ExceptionClass>"}`.

    The endpoint is anonymous and a database or Redis error names hosts, ports and users: the message is logged,
    and shown in the answer only under DEBUG.
    """
    results = {}
    for name, check in CHECKS.items():
        try:
            check()
            results[name] = "ok"
        except Exception as error:  # noqa: BLE001 - any failure of a dependency is the answer, not a crash
            logger.warning("status.check_failed check=%s error=%s: %s", name, type(error).__name__, error)
            results[name] = f"failed: {type(error).__name__}"
            if settings.DEBUG:
                results[name] = f"{results[name]}: {error}"[:300]
    return results


def supervisord_processes() -> dict[str, str]:
    transport = UnixStreamHTTPConnection(settings.EVILFLOWERS_STATUS_SUPERVISOR_SOCKET)
    server = xmlrpc.client.ServerProxy("http://localhost", transport=transport)
    return {proc["name"]: proc["statename"] for proc in server.supervisor.getAllProcessInfo()}


class StatusManagement(View):
    @openapi.metadata(
        description=(
            "Health check. Checks the database, the Celery broker and storage, and answers 503 when one of them "
            "fails (`additional_data` names it). Where `EVILFLOWERS_STATUS_SUPERVISOR` is on (the Compose image), "
            "it also asks supervisord and answers 503 unless every process is RUNNING; on Kubernetes the "
            "processes run in their own pods and that check is off."
        ),
        tags=["Status"],
        summary="System status and health check",
    )
    def get(self, request):
        checks = run_checks()
        if any(result != "ok" for result in checks.values()):
            raise ProblemDetailException(
                "A dependency is not available",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                additional_data=checks,
            )

        response = StatusSerializer(
            timestamp=timezone.now(),
            instance=settings.INSTANCE_NAME,
            checks=checks,
            stats=StatusStatistics(
                catalogs=Catalog.objects.count(),
                entries=Entry.objects.count(),
                acquisitions=Acquisition.objects.count(),
                users=User.objects.count(),
            ),
        )

        processes = {}
        if settings.EVILFLOWERS_STATUS_SUPERVISOR:
            try:
                processes = supervisord_processes()
            except Exception as e:
                raise ProblemDetailException(
                    "Error connecting to Supervisord", status=HTTPStatus.SERVICE_UNAVAILABLE, previous=e
                )

        if settings.DEBUG:
            response.python = sys.version
            response.version = settings.VERSION
            response.build = settings.BUILD
            response.supervisord = processes if settings.EVILFLOWERS_STATUS_SUPERVISOR else None
            response.client_ip = StatusClientIP(
                remote_addr=request.META.get("REMOTE_ADDR"),
                x_real_ip=request.META.get("HTTP_X_REAL_IP"),
                x_forwarded_for=request.META.get("HTTP_X_FORWARDED_FOR"),
                resolved=_get_client_ip(request),
            )
            response.allowed_ip_ranges = settings.EVILFLOWERS_ALLOWED_IP_RANGES

        if not all(value == "RUNNING" for value in processes.values()):
            raise ProblemDetailException(
                "Some processes are not running properly",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
                additional_data=processes,
            )

        return SingleResponse(request, data=response, status=HTTPStatus.OK)
