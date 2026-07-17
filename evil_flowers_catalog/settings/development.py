from .base import *

DEBUG = True

TIME_ZONE = "Europe/Bratislava"

ALLOWED_HOSTS = ["*"]

# The dev instance sits behind the same TLS-terminating proxy as production.
# Without this, `request.build_absolute_uri()` mints `http://` URLs (e.g.
# `LicenseSerializer.download_url`), which the https portal cannot follow.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

EMAIL_BACKEND = "django_imap_backend.ImapBackend"
EMAIL_IMAP_SECRETS = [
    {
        "HOST": os.getenv("EMAIL_IMAP_HOST"),
        "USER": os.getenv("EMAIL_IMAP_USER"),
        "PASSWORD": os.getenv("EMAIL_IMAP_PASSWORD"),
        "MAILBOX": os.getenv("EMAIL_IMAP_MAILBOX"),
        "SSL": False,
    }
]
