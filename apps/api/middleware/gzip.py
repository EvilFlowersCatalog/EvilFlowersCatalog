from django.http import FileResponse
from django.middleware.gzip import GZipMiddleware as DjangoGZipMiddleware


class GZipMiddleware(DjangoGZipMiddleware):
    """Django's gzip, minus stored files (#70).

    Gzip on a streamed response drops `Content-Length`, and a compressed `206`
    would carry byte positions of the uncompressed file. Books and covers are
    already compressed (PDF streams, JPEG, EPUB zip), so there is nothing to gain.
    """

    def process_response(self, request, response):
        if isinstance(response, FileResponse) or response.has_header("Accept-Ranges"):
            return response
        return super().process_response(request, response)
