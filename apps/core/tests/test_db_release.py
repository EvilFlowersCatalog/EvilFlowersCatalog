"""#133: a streaming response gives its database connection back before the body is sent."""

from django.db import connection
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase

from apps.api.middleware.db_release import ReleaseConnectionBeforeStreamingMiddleware


def view(streaming: bool):
    def respond(request):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return StreamingHttpResponse(iter([b"x"])) if streaming else HttpResponse(b"x")

    return respond


class ReleaseBeforeStreamingTests(TransactionTestCase):
    def test_streaming_response_releases_the_connection(self):
        ReleaseConnectionBeforeStreamingMiddleware(view(streaming=True))(RequestFactory().get("/"))
        self.assertIsNone(connection.connection)

    def test_plain_response_keeps_it_until_the_request_ends(self):
        ReleaseConnectionBeforeStreamingMiddleware(view(streaming=False))(RequestFactory().get("/"))
        self.assertIsNotNone(connection.connection)


class ReleaseInsideTransactionTests(TestCase):
    def test_an_open_transaction_is_left_alone(self):
        ReleaseConnectionBeforeStreamingMiddleware(view(streaming=True))(RequestFactory().get("/"))
        self.assertTrue(connection.in_atomic_block)
        self.assertIsNotNone(connection.connection)
