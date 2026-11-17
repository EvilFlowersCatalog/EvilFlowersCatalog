from .base import *
from .base import _bool_env

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# IP-035: hashed, pre-compressed static files from the `collectstatic` run in the image build
# (Dockerfile). A template that names a static file missing from the manifest fails loudly, so the
# build's `collectstatic` and the console tests catch it before a deploy does.
STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# The admin's session and CSRF cookies only over HTTPS (the edge terminates TLS). `0` only for a local
# run of these settings over plain http://localhost.
SESSION_COOKIE_SECURE = _bool_env("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = _bool_env("CSRF_COOKIE_SECURE", default=True)

LOGGING["root"]["level"] = "INFO"
