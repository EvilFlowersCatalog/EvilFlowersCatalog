from django.db import connections


class ReleaseConnectionBeforeStreamingMiddleware:
    """Give the database connection back before a streaming body is sent (#133).

    With the connection pool (`DATABASES["default"]["OPTIONS"]["pool"]`) a request holds its pooled
    connection until `request_finished`, which for a streaming response fires only after the last
    byte. A file download or an assistant answer would keep one of the worker's `PGPOOL_MAX` slots for
    the whole transfer. Closing here returns it to the pool; a body that queries while it streams
    takes a connection again for that.

    Outermost in `MIDDLEWARE`, so it runs after every other middleware has finished with the response.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.streaming:
            for connection in connections.all(initialized_only=True):
                # An open transaction (a test case, an `atomic` around the view) is not ours to end.
                if not connection.in_atomic_block:
                    connection.close()
        return response
