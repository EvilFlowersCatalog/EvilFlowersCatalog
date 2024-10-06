import sys
import socket
import xmlrpc.client
from http import HTTPStatus
from http.client import HTTPConnection

from django.conf import settings
from django.utils import timezone
from django.views import View

from apps.api.response import SingleResponse
from apps.api.serializers.status import (
    StatusStatistics,
    StatusSerializer,
)
from apps.core.errors import ProblemDetailException
from apps.core.models import Catalog, Entry, Acquisition, User


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


class StatusManagement(View):
    def get(self, request):
        response = StatusSerializer(
            timestamp=timezone.now(),
            instance=settings.INSTANCE_NAME,
            stats=StatusStatistics(
                catalogs=Catalog.objects.count(),
                entries=Entry.objects.count(),
                acquisitions=Acquisition.objects.count(),
                users=User.objects.count(),
            ),
        )

        processes = {}

        try:
            socket_path = "/tmp/supervisor.sock"  # Update if your socket path is different
            transport = UnixStreamHTTPConnection(socket_path)
            server = xmlrpc.client.ServerProxy("http://localhost", transport=transport)
            processes_info = server.supervisor.getAllProcessInfo()

            for proc in processes_info:
                processes[proc["name"]] = proc["statename"]

        except Exception as e:
            raise ProblemDetailException(
                "Error connecting to Supervisord", status=HTTPStatus.SERVICE_UNAVAILABLE, previous=e
            )

        if settings.DEBUG:
            response.python = sys.version
            response.version = settings.VERSION
            response.build = settings.BUILD
            response.supervisord = processes

        return SingleResponse(
            request,
            data=response,
            status=(
                HTTPStatus.OK
                if all(value == "RUNNING" for value in processes.values())
                else HTTPStatus.SERVICE_UNAVAILABLE
            ),
        )
